"""pytest 全局配置。

保证 ``src/`` 位于导入路径上，使得不安装包时（例如直接 clone 后跑 pytest）
测试也能找到 ``xjtu_calendar``。

另外提供 ``fake_playwright``：一个够用的假 Playwright，让 `auth.ensure_login`
的交互与落盘路径可以在没有浏览器的环境下被测到（多个测试文件都要用，故放这里）。
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

_COOKIES: list[dict[str, Any]] = [
    {"name": "SESSIONID", "value": "v", "domain": ".xjtu.edu.cn", "path": "/"}
]


class FakePage:
    def goto(self, url: str, **kwargs: Any) -> None:
        pass


class FakeContext:
    def __init__(self) -> None:
        self.pages: list[FakePage] = []
        self.closed = False

    def new_page(self) -> FakePage:
        return FakePage()

    def cookies(self) -> list[dict[str, Any]]:
        return list(_COOKIES)

    def storage_state(self) -> dict[str, Any]:
        return {"cookies": list(_COOKIES), "origins": []}

    def close(self) -> None:
        self.closed = True


class FakePlaywright:
    """用作 ``sync_playwright()`` 返回的上下文管理器。"""

    def __init__(self, context: FakeContext) -> None:
        self._context = context

    class _Chromium:
        def __init__(self, context: FakeContext) -> None:
            self._context = context

        def launch_persistent_context(self, **kwargs: Any) -> FakeContext:
            return self._context

    def __enter__(self) -> FakePlaywright:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    @property
    def chromium(self) -> _Chromium:
        return self._Chromium(self._context)


@pytest.fixture
def fake_playwright(monkeypatch: pytest.MonkeyPatch) -> FakeContext:
    """把 ``playwright.sync_api`` 换成假模块，返回那个假的浏览器上下文。"""
    context = FakeContext()
    module = types.ModuleType("playwright.sync_api")
    module.sync_playwright = lambda: FakePlaywright(context)  # type: ignore[attr-defined]
    package = types.ModuleType("playwright")
    package.sync_api = module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", module)
    return context
