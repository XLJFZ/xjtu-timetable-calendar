"""``ensure_login`` 会话落盘的回归测试（注入假 Playwright 模块）。

auth 此前完全没有测试：登录流程依赖真实浏览器，但**落盘环节**
（storage_state.json 的写入方式）是纯本地行为，可以也应当被测到——
该文件等价于登录凭据，必须以「私有 + 原子」的方式写入
（见审阅发现 P2 与 :mod:`xjtu_calendar.fileutil`）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xjtu_calendar.auth import ensure_login, inspect_session
from xjtu_calendar.config import Settings
from xjtu_calendar.fileutil import atomic_write_text

# 假 Playwright（`fake_playwright` fixture）在 tests/conftest.py：多个文件都要测
# 登录交互与落盘，重复一份迟早漂移。


def test_load_cookies_scopes_to_target_host(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP 路径只发送与 eHall 主机匹配的 cookie。

    ``load_cookies`` 曾把 storage_state 里**所有域**的 cookie 压平外发：
    CAS 登录域的会话名可能与 eHall 域同名并互相覆盖，且非 eHall 域的
    cookie 会被无差别发给接口。凭据等价文件应按域过滤后再交给 httpx。
    """
    import json as jsonlib

    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.setenv("XJTU_EHALL_BASE", "https://ehall.xjtu.edu.cn")
    session = tmp_path / "session"
    session.mkdir(parents=True)
    (session / "storage_state.json").write_text(
        jsonlib.dumps(
            {
                "cookies": [
                    {
                        "name": "JSESSIONID",
                        "value": "ehall-side",
                        "domain": ".xjtu.edu.cn",
                        "path": "/",
                    },
                    {"name": "TGC", "value": "cas-side", "domain": "cas.xjtu.edu.cn", "path": "/"},
                    {"name": "SAP", "value": "elsewhere", "domain": ".example.com", "path": "/"},
                ]
            }
        ),
        encoding="utf-8",
    )

    from xjtu_calendar.auth import load_cookies

    assert load_cookies(Settings.load()) == {"JSESSIONID": "ehall-side"}


def test_load_cookies_keeps_legacy_entries_without_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缺 domain 字段的旧快照保持宽容（宁多不漏，避免升级把会话打断）。"""
    import json as jsonlib

    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.setenv("XJTU_EHALL_BASE", "https://ehall.xjtu.edu.cn")
    session = tmp_path / "session"
    session.mkdir(parents=True)
    (session / "storage_state.json").write_text(
        jsonlib.dumps({"cookies": [{"name": "SESSIONID", "value": "v"}]}), encoding="utf-8"
    )

    from xjtu_calendar.auth import load_cookies

    assert load_cookies(Settings.load()) == {"SESSIONID": "v"}


def test_ensure_login_writes_session_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_playwright: Any,
) -> None:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.setattr("builtins.input", lambda *args: "")

    recorded: dict[str, bool] = {}

    def spy(path: Path, text: str, *, private: bool = False) -> None:
        recorded["private"] = private
        atomic_write_text(path, text, private=private)

    import xjtu_calendar.auth as auth_module

    monkeypatch.setattr(auth_module, "atomic_write_text", spy)

    info = ensure_login(Settings.load())

    assert info.usable
    state = tmp_path / "session" / "storage_state.json"
    payload = json.loads(state.read_text(encoding="utf-8"))
    assert payload["cookies"][0]["name"] == "SESSIONID"
    # 凭据等价文件必须走私有原子写入
    assert recorded["private"] is True
    # 同目录不留 *.tmp 残片
    leftovers = [p.name for p in (tmp_path / "session").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_inspect_session_counts_the_jwapp_scoped_cookies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """会话里有没有教务侧（``/jwapp``）的 cookie，是"登录走没走完"的判据。

    2026-10-14 实测：真人登录若停在「请选择身份」页面就按 Enter，导出的会话只有
    门户侧 cookie，教务三个 ``.do`` 接口一律 403；在窗口里点一下身份之后，服务器
    给 ``/jwapp`` 另发 ``JSESSIONID`` / ``_WEU`` / ``THEME`` / ``EMAP_LANG``，接口立刻 200。
    消融实验进一步表明**缺哪个都不如缺 ``_WEU`` 致命**，但按路径计数最稳，
    所以这里数的是"有没有任何一条 ``/jwapp`` 作用域的 cookie"。
    """
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    session = tmp_path / "session"
    session.mkdir(parents=True)
    cookies: list[dict[str, object]] = [
        {"name": "JSESSIONID", "value": "portal", "domain": "ehall.xjtu.edu.cn", "path": "/"},
        {"name": "CASTGC", "value": "c", "domain": "ehall.xjtu.edu.cn", "path": "/"},
    ]
    state = session / "storage_state.json"

    def write(items: list[dict[str, object]]) -> None:
        state.write_text(json.dumps({"cookies": items}, ensure_ascii=False), encoding="utf-8")

    write(cookies)
    info = inspect_session(Settings.load())
    assert info.usable
    assert info.jwapp_cookie_count == 0
    assert "教务" not in info.describe()

    app_cookies = [
        {
            "name": "JSESSIONID",
            "value": "app",
            "domain": "ehall.xjtu.edu.cn",
            "path": "/jwapp",
        },
        {"name": "_WEU", "value": "w", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp/"},
    ]
    write([*cookies, *app_cookies])
    info = inspect_session(Settings.load())
    assert info.jwapp_cookie_count == 2
    assert "教务侧 cookie 2 个" in info.describe()


def test_inspect_session_names_the_load_bearing_weu_cookie(tmp_path: Path) -> None:
    """**`_WEU` 判据**：教务侧有 cookie ≠ 那条承重的在里面。

    2026-10-14 的消融实验（`_notes/probe-403/`）：从一份可用会话里**只去掉 `_WEU`**，
    三个 ``.do`` 接口立刻回金智的异常页（403）；而门户 ``/`` 与教务 ``/jwapp`` 那两份
    同名 ``JSESSIONID`` 少掉任意一份都仍然 200。所以"数一数 /jwapp 有几条"这条判据
    有一个已知的空档：``THEME``/``EMAP_LANG`` 之类到了、``_WEU`` 却没到。
    本用例钉的就是这个空档 —— **不**把结论重新挂回 JSESSIONID 的取舍上。
    """
    from xjtu_calendar.auth import inspect_session

    session = tmp_path / "session"
    session.mkdir(parents=True)
    state = session / "storage_state.json"

    def write(cookies: list[dict[str, str]]) -> None:
        state.write_text(json.dumps({"cookies": cookies}, ensure_ascii=False), encoding="utf-8")

    write(
        [
            {"name": "JSESSIONID", "value": "a", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp"},
            {"name": "THEME", "value": "t", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp/"},
        ]
    )
    info = inspect_session(Settings(home=tmp_path))
    assert info.has_app_session, "判据不许收紧成「只有 _WEU 才算登录走完」——那是另一种误拦"
    assert not info.weu_present, "教务侧有 cookie、却没有 _WEU，这一格今天看不见"
    assert "_WEU" in info.describe()

    write(
        [
            {"name": "JSESSIONID", "value": "a", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp"},
            {"name": "_WEU", "value": "w", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp/"},
        ]
    )
    info = inspect_session(Settings(home=tmp_path))
    assert info.weu_present
    assert "含 _WEU" in info.describe()
