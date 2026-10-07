"""认证与会话管理。

设计红线
--------
- **不要求用户把账号密码写进项目，也不硬编码任何凭据。**
- 登录由用户本人在浏览器窗口里完成统一身份认证，程序只负责保存会话状态。
- 会话数据只落本地（``~/.xjtu-timetable-calendar/session/``），且已在 .gitignore 排除。
- 401 / 403 是终态，**不做重试**，直接给出「登录失效 / 无权限」的明确提示。

两种会话载体
------------
1. **Playwright persistent context**（首选）
   把 cookie 写进自己的 profile 目录，登录态跨进程重启保留。适合本机使用。
2. **storage_state JSON**
   Playwright 导出的 cookie 快照。适合需要把会话交给纯 HTTP 客户端复用的场景。
   .. warning:: 该文件等价于凭据，绝不可提交或分享。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .config import Settings, find_browser
from .config import settings as default_settings
from .errors import AuthenticationRequired
from .fileutil import atomic_write_text
from .logging_setup import get_logger

__all__ = ["SessionInfo", "ensure_login", "has_session", "load_cookies"]

logger = get_logger()

#: 判定登录成功的标志：eHall 会在 cookie 里放这些会话标识
_SESSION_COOKIE_HINTS = (
    "jsessionid",
    "ehall",
    "session",
    "sso",
    "ticket",
    "ids",
    "wengine",
)


class SessionInfo:
    """本地会话状态摘要（不含任何凭据内容）。"""

    def __init__(
        self,
        *,
        has_storage_state: bool,
        has_profile: bool,
        cookie_count: int,
        saved_at: datetime | None,
    ) -> None:
        self.has_storage_state = has_storage_state
        self.has_profile = has_profile
        self.cookie_count = cookie_count
        self.saved_at = saved_at

    @property
    def usable(self) -> bool:
        return self.has_storage_state or self.has_profile

    def describe(self) -> str:
        if not self.usable:
            return "未登录（本地无会话状态）"
        parts = []
        if self.has_storage_state:
            parts.append(f"storage_state 含 {self.cookie_count} 个 cookie")
        if self.has_profile:
            parts.append("浏览器 profile 已存在")
        if self.saved_at:
            parts.append(f"保存于 {self.saved_at.astimezone().strftime('%Y-%m-%d %H:%M')}")
        return "、".join(parts)


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def inspect_session(cfg: Settings | None = None) -> SessionInfo:
    """检查本地会话状态（不发起任何网络请求）。"""
    cfg = cfg or default_settings
    state_path = cfg.state_path()
    profile_dir = cfg.profile_dir()

    cookie_count = 0
    saved_at: datetime | None = None

    if state_path.is_file():
        try:
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            cookie_count = len(payload.get("cookies") or [])
            raw_time = payload.get("_saved_at")
            if raw_time:
                saved_at = datetime.fromisoformat(raw_time)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("会话文件无法解析，将视为未登录：%s", exc)
            cookie_count = 0

    return SessionInfo(
        has_storage_state=state_path.is_file() and cookie_count > 0,
        has_profile=profile_dir.is_dir(),
        cookie_count=cookie_count,
        saved_at=saved_at,
    )


def has_session(cfg: Settings | None = None) -> bool:
    """本地是否存在可用会话。"""
    return inspect_session(cfg).usable


def ensure_login(cfg: Settings | None = None, *, force: bool = False) -> SessionInfo:
    """确保本地存在会话；不存在时打开浏览器让用户手动登录。

    Parameters
    ----------
    cfg:
        配置。
    force:
        ``True`` 时即使已有会话也重新登录（用于会话失效后的修复）。

    Raises
    ------
    AuthenticationRequired
        缺少 playwright，或用户未完成登录。
    """
    cfg = cfg or default_settings
    cfg.ensure_dirs()

    info = inspect_session(cfg)
    if info.usable and not force:
        logger.info("已检测到本地会话：%s", info.describe())
        return info

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthenticationRequired(
            "登录功能需要 playwright（仅本机浏览器自动化用，不涉及凭据上传）",
            hint="请安装：pip install playwright（使用系统已装的 Edge/Chrome 时无需再跑 playwright install）",
        ) from exc

    executable = find_browser()
    if not executable:
        logger.warning("未探测到系统浏览器，将尝试使用 playwright 自带 Chromium")

    print()
    print("=" * 68)
    print("  需要先登录西安交通大学统一身份认证")
    print("=" * 68)
    print(f"  入口：{cfg.select_role_url}")
    print()
    print("  请在打开的浏览器窗口里完成以下操作：")
    print("    1) 由你本人输入账号密码完成统一身份认证")
    print("       （本程序不读取、不记录、不上传任何凭据）")
    print("    2) 进入「我的课表」页面，并切换到你要导出的学期")
    print("    3) 确认页面已正常显示课表后，回到终端按 Enter")
    print()
    print("  会话将保存到本地：")
    print(f"    {cfg.session_dir}")
    print("  该目录已在 .gitignore 中排除，不会被提交。")
    print("=" * 68)
    print()

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(cfg.profile_dir()),
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if executable:
        launch_kwargs["executable_path"] = executable

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()

        # 启动 URL 不带 #/，避免 SPA 路由被破坏而卡死
        page.goto(cfg.select_role_url, wait_until="domcontentloaded", timeout=90_000)

        try:
            input(">>> 完成登录并确认课表已显示后，按 Enter 继续... ")
        except (EOFError, KeyboardInterrupt):
            context.close()
            raise AuthenticationRequired("用户中止了登录流程") from None

        cookies = context.cookies()
        storage = context.storage_state()

        context.close()

    if not cookies:
        raise AuthenticationRequired(
            "未获取到任何 cookie，登录可能未完成",
            hint="请重新运行 login 子命令，并确保在浏览器里完成统一身份认证。",
        )

    state = {
        "cookies": storage.get("cookies", []),
        "origins": storage.get("origins", []),
        "_saved_at": _iso_now(),
        "_note": "本文件等价于登录凭据，请勿提交或分享。",
    }
    # 凭据等价文件：原子替换 + 属主-only 权限（POSIX 0600）。
    # 用户往往只有这一份会话，直接覆盖时进程中途崩溃会留下截断的 JSON。
    atomic_write_text(
        cfg.state_path(),
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        private=True,
    )

    logger.info("登录会话已保存（%d 个 cookie）", len(cookies))
    return inspect_session(cfg)


def _cookie_matches_target(domain: str, host: str) -> bool:
    """cookie 的 Domain 属性是否覆盖目标主机（RFC 6265 的域匹配语义）。

    缺 Domain 字段的旧快照按宽容处理（保留）——宁可多发不外漏，
    升级不该把已有用户的好端端会话打断。
    """
    cdom = (domain or "").lstrip(".").lower()
    if not cdom or not host:
        return True
    host = host.lower()
    return host == cdom or host.endswith(f".{cdom}")


def load_cookies(cfg: Settings | None = None) -> dict[str, str]:
    """从 storage_state 里提取**属于 eHall 主机**的 cookie，供 HTTP 客户端复用。

    按域过滤是刻意的：Playwright 快照里同时存着 CAS 登录域、其他站点的
    cookie；把它们压平全量发给接口，既可能覆盖同名会话键，也无谓扩大
    凭据暴露面。

    Raises
    ------
    AuthenticationRequired
        本地不存在会话文件。
    """
    cfg = cfg or default_settings
    state_path = cfg.state_path()
    if not state_path.is_file():
        raise AuthenticationRequired(f"未找到会话文件：{state_path}")

    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise AuthenticationRequired(f"会话文件损坏：{state_path}（{exc}）") from exc

    host = urlparse(cfg.ehall_base).hostname or ""
    cookies: dict[str, str] = {}
    for item in payload.get("cookies") or []:
        name = item.get("name")
        value = item.get("value")
        if not name or not value:
            continue
        if not _cookie_matches_target(str(item.get("domain") or ""), host):
            logger.debug("跳过其他域的 cookie：%s", name)
            continue
        if name in cookies and cookies[name] != value:
            logger.warning(
                "eHall 域范围内出现同名 cookie：%s（以最后一份为准，建议重新登录）", name
            )
        cookies[str(name)] = str(value)
    if not cookies:
        raise AuthenticationRequired(f"会话文件中没有可用 cookie：{state_path}")

    if not any(hint in name.lower() for name in cookies for hint in _SESSION_COOKIE_HINTS):
        logger.warning("未在 cookie 中发现常见会话标识，登录状态可能不完整")

    return cookies


def storage_state_dict(cfg: Settings | None = None) -> dict[str, Any]:
    """返回可直接传给 Playwright 的 ``storage_state`` 结构。"""
    cfg = cfg or default_settings
    state_path = cfg.state_path()
    if not state_path.is_file():
        raise AuthenticationRequired(f"未找到会话文件：{state_path}")
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    return {"cookies": payload.get("cookies", []), "origins": payload.get("origins", [])}
