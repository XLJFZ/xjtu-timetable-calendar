"""课表数据抓取。

两条路径
--------
1. **HTTP 路径（首选，轻量）** —— 复用 :func:`xjtu_calendar.auth.load_cookies`
   拿到的 cookie，用 ``httpx`` 直接请求课表接口。需要已知端点
   （由 Phase 1 的接口分析确定，写入 ``config/ehall_endpoints.json``）。
2. **浏览器路径（兜底）** —— 用 Playwright 在已登录的持久化 profile 里
   打开课表页，拦截页面自身发出的结构化 JSON 响应。

安全行为（严格遵守 README「安全与网络行为」）
-------------------------------------------
- 只访问当前账号正常有权限的接口
- 不绕过认证、不绕过权限、不枚举学号、不枚举课程、不抓他人数据
- 不并发：所有请求串行
- 401/403 立即终止且不重试
- 仅对**可能自愈**的错误（超时 / 5xx / 连接重置）做有限次指数退避
- 不把 cookie / token 写进日志
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings, find_browser
from .config import settings as default_settings
from .errors import (
    AuthenticationExpired,
    AuthenticationRequired,
    EndpointNotConfigured,
    PermissionDenied,
    TimetableFetchError,
)
from .logging_setup import get_logger, redact, redact_url

__all__ = [
    "Endpoint",
    "classify_body",
    "fetch_current_semester",
    "fetch_via_browser",
    "fetch_via_http",
    "load_endpoints",
    "require_endpoint",
]

logger = get_logger()

#: 端点配置文件名（放在项目 ``config/`` 下，随仓库分发）
ENDPOINTS_FILE = "ehall_endpoints.json"

#: 可重试的 HTTP 状态码
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})

#: 模板占位路径。出现即视为「尚未完成真实探测」，**绝不能拿去发请求**。
PLACEHOLDER_MARKER = "REPLACE_WITH_OBSERVED_PATH"

#: 判定「这其实是个登录页」的 HTML 特征
#: （eHall 会话失效后，接口常以 200 + 登录页 HTML 的方式返回，而不是 401）
LOGIN_PAGE_MARKERS = (
    "统一身份认证", "统一身份", "账号登录", "用户登录",
    "login.xjtu.edu.cn", "cas.xjtu.edu.cn", "ids.xjtu.edu.cn",
    "请输入账号", "请输入密码",
)


@dataclass(frozen=True)
class Endpoint:
    """一个课表相关接口的描述。

    Attributes
    ----------
    name:
        逻辑名称，如 ``"semester_list"`` / ``"timetable"``。
    method:
        HTTP 方法。
    path:
        相对于 eHall 根地址的路径。
    description:
        人类可读说明。
    required:
        缺少该端点时是否阻断导出。
    """

    name: str
    method: str
    path: str
    description: str = ""
    required: bool = False

    def url(self, cfg: Settings) -> str:
        path = self.path if self.path.startswith("/") else f"/{self.path}"
        return f"{cfg.ehall_base}{path}"


def classify_body(text: str) -> str:
    """判断响应体属于哪一类，返回 ``"json"`` / ``"login-html"`` / ``"html"`` / ``"text"`` / ``"empty"``。

    .. important::
        **会话失效时的登录页必须被单独识别出来**，不能当成「JSON 解析失败」。
        eHall 在会话过期后常常以 ``200 + 登录页 HTML`` 的形式响应接口请求
        （尤其是 ``follow_redirects=True`` 跟到了统一身份认证页之后）。
        若笼统报「响应不是合法 JSON」，用户会以为是程序 bug，
        而真正该做的是重新 ``login``。
    """
    if not text or not text.strip():
        return "empty"

    stripped = text.strip()
    if stripped[0] in "[{":
        try:
            json.loads(stripped)
        except json.JSONDecodeError:
            pass
        else:
            return "json"

    head = text[:4000]
    if head.lstrip()[:6].lower().startswith("<html") or "<html" in head.lower()[:200]:
        if any(marker in head for marker in LOGIN_PAGE_MARKERS):
            return "login-html"
        return "html"
    if any(marker in head for marker in LOGIN_PAGE_MARKERS):
        return "login-html"
    return "text"


def require_endpoint(endpoints: Mapping[str, Endpoint], name: str) -> Endpoint:
    """取出指定端点；缺失时给出**明确**的「尚未探测」提示。

    Raises
    ------
    EndpointNotConfigured
        端点不存在，或其路径仍是未填写的占位符。
    """
    endpoint = endpoints.get(name)
    if endpoint is None:
        raise EndpointNotConfigured(f"接口定义中缺少 `{name}` 端点")
    if PLACEHOLDER_MARKER in endpoint.path:
        raise EndpointNotConfigured(
            f"接口 `{name}` 的路径仍是占位符（{endpoint.path}），尚未由真实探测填写"
        )
    return endpoint


def load_endpoints(path: Path | str | None = None, cfg: Settings | None = None) -> dict[str, Endpoint]:
    """加载接口定义。

    Parameters
    ----------
    path:
        显式指定配置文件；``None`` 时依次查找
        ``<repo>/config/ehall_endpoints.json`` 与 ``<home>/endpoints.json``。
    cfg:
        配置。

    Returns
    -------
    dict[str, Endpoint]
        逻辑名称 -> 端点。

    Notes
    -----
    **接口路径必须来自真实网络请求观测**（Phase 1 产物），不得猜测。
    若配置文件缺失或不含 ``timetable`` 端点，返回空字典，
    调用方应回退到浏览器路径或提示用户先完成接口分析。
    """
    cfg = cfg or default_settings

    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    else:
        candidates.append(Path(__file__).resolve().parent.parent.parent / "config" / ENDPOINTS_FILE)
        candidates.append(cfg.home / ENDPOINTS_FILE)

    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("接口定义文件无法解析：%s（%s）", candidate, exc)
            continue

        endpoints: dict[str, Endpoint] = {}
        for item in payload.get("endpoints") or []:
            try:
                endpoint = Endpoint(
                    name=str(item["name"]),
                    method=str(item.get("method", "GET")).upper(),
                    path=str(item["path"]),
                    description=str(item.get("description", "")),
                    required=bool(item.get("required", False)),
                )
            except KeyError as exc:
                logger.warning("接口定义缺少字段：%s", exc)
                continue
            if PLACEHOLDER_MARKER in endpoint.path:
                # 占位符不是「已确认的接口」。放进去等于拿模板去发真实请求。
                logger.warning(
                    "接口 `%s` 的路径仍是占位符，已忽略（必须先由真实探测确认）",
                    endpoint.name,
                )
                continue
            endpoints[endpoint.name] = endpoint

        if endpoints:
            logger.debug("已从 %s 载入 %d 个接口定义", candidate, len(endpoints))
            return endpoints

    return {}


# --------------------------------------------------------------------------- #
# HTTP 路径
# --------------------------------------------------------------------------- #
def fetch_current_semester(endpoint: Endpoint, *, cfg: Settings | None = None) -> str:
    """通过学期发现接口取得当前学期代码（如 ``"2026-2027-1"``）。

    真实响应信封为 ``datas.<模块名>.rows[0]``，学期代码在 ``DM`` 键
    （2026-09-20 观测）。这里按**结构**取值，不绑定模块名。

    Raises
    ------
    TimetableFetchError
        响应里找不到学期代码（schema 变化时给出可诊断的错误，
        而不是笼统的「获取失败」）。
    """
    payload = fetch_via_http(endpoint, cfg=cfg)
    datas = payload.get("datas") if isinstance(payload, Mapping) else None
    if isinstance(datas, Mapping):
        for module in datas.values():
            rows = module.get("rows") if isinstance(module, Mapping) else None
            if rows and isinstance(rows[0], Mapping):
                code = rows[0].get("DM") or rows[0].get("XNXQDM")
                if code:
                    return str(code)
    raise TimetableFetchError(
        f"学期接口 {endpoint.name} 的响应中没有学期代码（DM/XNXQDM）",
        hint="响应 schema 可能变化。请运行 python -m xjtu_calendar inspect "
             "查看脱敏结构，并核对 scripts/probe_ehall.py 的最新观测。",
    )


def fetch_via_http(
    endpoint: Endpoint,
    *,
    cfg: Settings | None = None,
    params: dict[str, Any] | None = None,
    transport: Any = None,
) -> Any:
    """用 httpx 复用本地会话请求接口。

    Raises
    ------
    AuthenticationRequired
        未安装 httpx 或本地无会话。
    AuthenticationExpired
        服务端返回 401，**或返回了统一身份认证的登录页 HTML**（会话已失效）。
    PermissionDenied
        服务端返回 403。
    TimetableFetchError
        网络错误、空响应体，或响应确实不是 JSON（且不是登录页）。

    Parameters
    ----------
    transport:
        仅测试使用的 httpx transport 注入点；生产调用保持 ``None``。
    """
    cfg = cfg or default_settings

    try:
        import httpx
    except ImportError as exc:
        raise TimetableFetchError(
            "HTTP 路径需要 httpx", hint="请安装：pip install httpx；或改用浏览器路径（--browser）"
        ) from exc

    from .auth import load_cookies

    cookies = load_cookies(cfg)
    url = endpoint.url(cfg)

    logger.debug("请求 %s %s", endpoint.method, redact_url(url))

    last_error: Exception | None = None
    attempts = max(1, cfg.max_retries)

    with httpx.Client(
        cookies=cookies,
        timeout=cfg.request_timeout,
        follow_redirects=True,
        transport=transport,
        headers={
            # 明确声明我们只要 JSON，避免被返回 HTML 登录页
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "XMLHttpRequest",
        },
    ) as client:
        for attempt in range(1, attempts + 1):
            try:
                if endpoint.method == "POST":
                    response = client.post(url, data=params or {})
                else:
                    response = client.get(url, params=params or {})
            except Exception as exc:  # 网络层异常
                last_error = exc
                if attempt >= attempts:
                    break
                delay = cfg.retry_backoff_base ** attempt
                logger.warning("请求失败（第 %d/%d 次），%.1fs 后重试", attempt, attempts, delay)
                time.sleep(delay)
                continue

            # --- 终态：身份与权限问题，绝不重试 ---
            if response.status_code == 401:
                raise AuthenticationExpired(f"接口返回 401：{endpoint.name}")
            if response.status_code == 403:
                raise PermissionDenied(
                    f"接口返回 403：{endpoint.name}",
                    hint="当前账号没有访问该接口的权限。本工具只导出本人有权限的课表。",
                )

            # --- 可能自愈的错误：有限次退避重试 ---
            if response.status_code in RETRYABLE_STATUS:
                last_error = TimetableFetchError(
                    f"接口 {endpoint.name} 返回 {response.status_code}"
                )
                if attempt >= attempts:
                    break
                delay = cfg.retry_backoff_base ** attempt
                logger.warning(
                    "服务端返回 %d（第 %d/%d 次），%.1fs 后重试",
                    response.status_code, attempt, attempts, delay,
                )
                time.sleep(delay)
                continue

            if response.status_code != 200:
                raise TimetableFetchError(
                    f"接口 {endpoint.name} 返回异常状态码 {response.status_code}"
                )

            # --- 解析：先分清「登录页」和「真的不是 JSON」 ---
            text = response.text
            kind = classify_body(text)

            if kind == "login-html":
                # 会话失效后 eHall 会用 200 + 登录页 HTML 响应接口。
                # 这是**认证问题**，不是解析问题。
                raise AuthenticationExpired(
                    f"接口 {endpoint.name} 返回的是统一身份认证登录页，登录态已失效"
                )

            if kind == "empty":
                raise TimetableFetchError(
                    f"接口 {endpoint.name} 返回了空响应体"
                )

            if kind != "json":
                raise TimetableFetchError(
                    f"接口 {endpoint.name} 的响应不是 JSON（实际为 {kind}）。"
                    f"响应前 200 字符：{text[:200]!r}"
                )

            payload = json.loads(text)

            logger.debug("响应结构（已脱敏）：%s", redact(payload))
            return payload

    raise TimetableFetchError(
        f"请求接口 {endpoint.name} 失败，已尝试 {attempts} 次：{last_error}"
    )


# --------------------------------------------------------------------------- #
# 浏览器路径
# --------------------------------------------------------------------------- #
def fetch_via_browser(
    *,
    cfg: Settings | None = None,
    navigate_url: str | None = None,
    hint_keywords: tuple[str, ...] = (
        "timetable", "schedule", "course", "kcb", "semester", "term",
    ),
    settle_seconds: float = 8.0,
) -> list[dict[str, Any]]:
    """在已登录的持久化 profile 里打开课表页，拦截页面自身发出的 JSON。

    这是**兜底路径**：不猜接口，只观察前端真实请求。

    Parameters
    ----------
    cfg:
        配置。
    navigate_url:
        要打开的页面；默认课表应用入口。
    hint_keywords:
        用于筛选目标响应的 URL 关键词。
    settle_seconds:
        页面加载后等待前端发起请求的时间。

    Returns
    -------
    list[dict]
        每个元素为 ``{"url": ..., "path": ..., "method": ..., "payload": ...}``。
    """
    cfg = cfg or default_settings

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise AuthenticationRequired(
            "浏览器路径需要 playwright", hint="请安装：pip install playwright"
        ) from exc

    from .auth import storage_state_dict

    url = navigate_url or cfg.select_role_url
    executable = find_browser()
    captured: list[dict[str, Any]] = []

    launch_kwargs: dict[str, Any] = {
        "user_data_dir": str(cfg.profile_dir()),
        "headless": True,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    if executable:
        launch_kwargs["executable_path"] = executable

    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(**launch_kwargs)

        # 把已保存的 cookie 注入（profile 与 storage_state 双保险）
        try:
            state = storage_state_dict(cfg)
            if state.get("cookies"):
                context.add_cookies(state["cookies"])
        except AuthenticationRequired:
            pass

        page = context.pages[0] if context.pages else context.new_page()

        def on_response(response: Any) -> None:
            try:
                lowered = response.url.lower()
                if not any(k in lowered for k in hint_keywords):
                    return
                ctype = (response.headers or {}).get("content-type", "")
                if "json" not in ctype.lower():
                    return
                payload = json.loads(response.text())
            except Exception:
                return
            captured.append(
                {
                    "url": response.url,
                    "path": response.url.split("?")[0],
                    "method": response.request.method,
                    "payload": payload,
                }
            )

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        page.wait_for_timeout(int(settle_seconds * 1000))

        context.close()

    if not captured:
        logger.warning(
            "未捕获到任何课表相关 JSON 响应。"
            "可能是页面需要手动点击「我的课表」才会发起请求，"
            "建议改用 scripts/probe_ehall.py 手动浏览一次。"
        )

    return captured


def save_raw(payload: Any, cfg: Settings, semester_key: str) -> Path:
    """把原始课表 JSON 缓存到本地。

    .. danger::
        该文件含个人信息，**必须留在 .gitignore 排除目录内**，绝不可提交。
    """
    cfg.ensure_dirs()
    path = cfg.raw_timetable_path(semester_key)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.debug("原始课表已缓存到 %s（含个人信息，请勿提交）", path)
    return path


def load_raw(cfg: Settings, semester_key: str) -> Any:
    """读取本地缓存的原始课表 JSON。"""
    path = cfg.raw_timetable_path(semester_key)
    if not path.is_file():
        raise TimetableFetchError(
            f"本地没有 {semester_key} 的原始课表缓存：{path}",
            hint="请先运行 fetch 子命令获取课表。",
        )
    return json.loads(path.read_text(encoding="utf-8"))
