"""`login` 的可操作性提示（跟进备忘 §6：三处"看起来成功、其实什么都没做"）。

背景是 2026-10-11 一次真实排障：会话早已过期，`login` 不带 `--force` 时打印
「已检测到本地会话」并退出，`fetch` 继续 401；而 401 的错误提示又叫人
「重新运行 login」——不带 `--force` 的那条路恰恰是死路。本文件钉住修好后的口径。

断言全部针对**文案里的关键语义**（未验证 / `--force` / 未保存 / 独立窗口），
不逐字锁死整句，免得正常润色把测试变成阻力。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from exam_support import captured_logs

from xjtu_calendar.auth import ensure_login
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import AuthenticationExpired, AuthenticationRequired

#: 一个明显过期的会话：有 cookie、有 `_saved_at`，但没有任何东西验证过它。
STALE_STATE = {
    "cookies": [{"name": "JSESSIONID", "value": "v", "domain": ".xjtu.edu.cn", "path": "/"}],
    "origins": [],
    "_saved_at": "2026-01-01T00:00:00+00:00",
}


@pytest.fixture
def stale_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 home，里面放一份**已过期**的会话文件。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    session = tmp_path / "session"
    session.mkdir(parents=True)
    (session / "storage_state.json").write_text(
        json.dumps(STALE_STATE, ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


def _texts(records: list[Any]) -> str:
    return "\n".join(record.getMessage() for record in records)


def test_stale_session_short_circuit_admits_it_is_unverified(stale_home: Path) -> None:
    """不带 `--force` 沿用旧会话时，必须**明说没验证过**，并给出 `--force` 出口。

    旧文案「已检测到本地会话」+「会话就绪」读起来像成功，是这次排障最大的迷惑源。
    """
    with captured_logs() as records:
        info = ensure_login(Settings.load())

    assert info.usable  # 确实沿用了旧会话，没有去开浏览器
    text = _texts(records)
    assert "未验证" in text
    assert "login --force" in text


def test_login_command_stops_claiming_readiness(
    stale_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`login` 收尾那句不许再说「会话就绪」——它什么都没验证。

    直接调 `cmd_login` 而不是 `main(["login"])`：`main()` 会跑 `setup_logging`
    重建 handler，事先挂上的收集器会被摘掉，这条测试就成了空过（实测踩过）。
    """
    import argparse

    from xjtu_calendar.cli import cmd_login

    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(stale_home))
    with captured_logs() as records:
        code = cmd_login(argparse.Namespace(force=False), Settings.load())

    assert code == 0
    assert "就绪" not in _texts(records)


def test_every_relogin_hint_names_the_force_flag(
    stale_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_playwright: Any,
) -> None:
    """**所有**"请重新登录"的出口都必须点名 `--force`。

    不带它时 `login` 看见本地会话就直接退出——所以"叫你重跑 login"而不说 `--force`
    的提示，等于把人往刚才那条死路上再推一次（401 提示当时就是这样）。
    抓取失败（一个 cookie 都没拿到）那条路同理。
    """
    import builtins

    monkeypatch.setattr(builtins, "input", lambda *args: "")
    fake_playwright.cookies = lambda: []  # type: ignore[method-assign]

    with pytest.raises(AuthenticationRequired) as caught:
        ensure_login(Settings.load(), force=True)

    hints = [caught.value.hint, AuthenticationExpired("接口返回 401：timetable").hint]
    assert hints[0], "抓取失败的提示不能是空的"
    for hint in hints:
        assert "login --force" in hint


def test_login_instructions_say_the_window_is_a_separate_browser() -> None:
    """提示里要讲清这是**独立浏览器实例**——用户平时浏览器里的登录态对它无效。"""
    from xjtu_calendar.auth import login_instructions

    lines = "\n".join(login_instructions(Settings.load()))
    assert "独立" in lines
    assert "本窗口" in lines


def test_aborted_login_states_the_session_was_not_saved(
    stale_home: Path, monkeypatch: pytest.MonkeyPatch, fake_playwright: Any
) -> None:
    """用户在终端按 Ctrl-C / EOF 中止时，必须说清**会话未保存**，免得留下半截状态。"""
    import builtins

    def _eof(prompt: object = "") -> str:
        raise EOFError

    monkeypatch.setattr(builtins, "input", _eof)

    with pytest.raises(AuthenticationRequired) as caught:
        ensure_login(Settings.load(), force=True)

    assert "未保存" in caught.value.hint
