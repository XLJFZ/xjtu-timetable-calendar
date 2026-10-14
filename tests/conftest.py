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

#: 假浏览器的默认 cookie：**走完最后一步的健康会话**。
#: 门户侧一条 + 教务侧（``/jwapp``）一条，后者是 `ensure_login` 自检"登录走没走完"
#: 的判据（2026-10-14 实测：缺它就是三个 ``.do`` 接口一律 403）。
#: 需要模拟半截会话的用例请自行覆盖 ``FakeContext.cookies``。
_COOKIES: list[dict[str, Any]] = [
    {"name": "SESSIONID", "value": "v", "domain": ".xjtu.edu.cn", "path": "/"},
    {"name": "_WEU", "value": "v", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp"},
]


class FakePage:
    def __init__(self, title: str = "") -> None:
        self._title = title

    def goto(self, url: str, **kwargs: Any) -> None:
        pass

    def title(self) -> str:
        return self._title


class FakeContext:
    def __init__(self, cookies: list[dict[str, Any]] | None = None) -> None:
        self.pages: list[FakePage] = [FakePage()]
        self.closed = False
        self._cookies = list(_COOKIES if cookies is None else cookies)

    def new_page(self) -> FakePage:
        return FakePage()

    def cookies(self) -> list[dict[str, Any]]:
        return list(self._cookies)

    def set_cookies(self, cookies: list[dict[str, Any]]) -> None:
        """替换当前 cookie：模拟用户在窗口里又点了一步之后状态变了。

        `storage_state()` 必须跟着变 —— 它是**真正落盘**的那份，而
        `cookies()` 只被自检逻辑读；两者不一致会让测试通过却测不到真实路径。
        """
        self._cookies = list(cookies)

    def storage_state(self) -> dict[str, Any]:
        return {"cookies": list(self._cookies), "origins": []}

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


@pytest.fixture(autouse=True)
def isolate_calendar_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """**每条用例**默认把 `XJTU_CALENDAR_HOME` 指到 tmp。

    2026-10-14 实测事故：一条只用了 `tmp_path` 却忘了 setenv 的登录测试，把
    `ensure_login` 的落盘目标指到了**真实用户 home**，直接覆盖了那份可用会话。
    `Settings` 在构造时读环境变量，所以只要环境里没设过，就会写到 `~/.xjtu-...`。
    默认隔离比"每个测试自己记得"可靠得多；需要真实 home 的用例请显式
    `@pytest.mark.usefixtures("allow_real_home")`（目前没有）。
    """
    if "allow_real_home" in request.keywords:
        return
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path / "_isolated_home"))


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
