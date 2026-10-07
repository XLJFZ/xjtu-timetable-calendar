"""``ensure_login`` 会话落盘的回归测试（注入假 Playwright 模块）。

auth 此前完全没有测试：登录流程依赖真实浏览器，但**落盘环节**
（storage_state.json 的写入方式）是纯本地行为，可以也应当被测到——
该文件等价于登录凭据，必须以「私有 + 原子」的方式写入
（见审阅发现 P2 与 :mod:`xjtu_calendar.fileutil`）。
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from xjtu_calendar.auth import ensure_login
from xjtu_calendar.config import Settings
from xjtu_calendar.fileutil import atomic_write_text

_COOKIES = [{"name": "SESSIONID", "value": "v", "domain": ".xjtu.edu.cn", "path": "/"}]


class _FakePage:
    def goto(self, url: str, **kwargs: Any) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.pages: list[_FakePage] = []
        self.closed = False

    def new_page(self) -> _FakePage:
        return _FakePage()

    def cookies(self) -> list[dict[str, Any]]:
        return list(_COOKIES)

    def storage_state(self) -> dict[str, Any]:
        return {"cookies": list(_COOKIES), "origins": []}

    def close(self) -> None:
        self.closed = True


class _FakePlaywright:
    """用作 ``sync_playwright()`` 返回的上下文管理器。"""

    def __init__(self, context: _FakeContext) -> None:
        self._context = context

    class _Chromium:
        def __init__(self, context: _FakeContext) -> None:
            self._context = context

        def launch_persistent_context(self, **kwargs: Any) -> _FakeContext:
            return self._context

    def __enter__(self) -> _FakePlaywright:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def chromium(self) -> _FakePlaywright._Chromium:
        return self._Chromium(self._context)


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> _FakeContext:
    context = _FakeContext()
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: _FakePlaywright(context)  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.sync_api = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return context


def test_ensure_login_writes_session_privately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_playwright: _FakeContext,
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
