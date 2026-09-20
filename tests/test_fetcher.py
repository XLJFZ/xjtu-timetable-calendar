"""fetcher 的响应分类与错误处理测试。

设计原则
--------
- **不发真实网络请求**：用 ``httpx.MockTransport`` 注入假响应。
- 每条测试只验证一种失败原因，且断言具体到异常类型与退出码，
  不允许「catch 所有异常后统一报获取失败」。
- 端点路径只使用**测试自造**的假路径；真实路径仍必须由探测确认。

.. note::
    本文件**不**包含真实接口结构测试——真实 fixture
    （``tests/fixtures/ehall_timetable_real_sanitized.json``）要等
    ``scripts/probe_ehall.py`` 真正捕获到课表响应之后才能建立。
    在那之前不得凭猜测构造「真实结构」固件。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from xjtu_calendar.config import Settings
from xjtu_calendar.errors import (
    AuthenticationExpired,
    AuthenticationRequired,
    EndpointNotConfigured,
    PermissionDenied,
    TimetableFetchError,
)
from xjtu_calendar.fetcher import (
    Endpoint,
    classify_body,
    fetch_via_http,
    load_endpoints,
    require_endpoint,
)

#: 会话失效后 eHall 常见的响应形态：**200 + 登录页 HTML**，而不是 401
LOGIN_HTML = (
    "<html><head><title>西安交通大学统一身份认证</title></head>"
    "<body><form>请输入账号<input name='username'></form></body></html>"
)

PLAIN_HTML = "<html><head><title>系统维护</title></head><body>维护中</body></html>"


@pytest.fixture()
def cfg(tmp_path: Path) -> Settings:
    """带一个假会话的临时配置。"""
    home = tmp_path / "home"
    session = home / "session"
    session.mkdir(parents=True)
    (session / "storage_state.json").write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "JSESSIONID", "value": "fake", "domain": "ehall.xjtu.edu.cn"}
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    return Settings(home=home, max_retries=2, retry_backoff_base=0.001)


@pytest.fixture()
def no_session_cfg(tmp_path: Path) -> Settings:
    return Settings(home=tmp_path / "empty-home")


def endpoint() -> Endpoint:
    return Endpoint(name="timetable", method="GET", path="/fake/timetable")


def transport(status: int, body: str, content_type: str = "application/json") -> httpx.MockTransport:
    return httpx.MockTransport(
        lambda request: httpx.Response(status, text=body, headers={"content-type": content_type})
    )


# --------------------------------------------------------------------------- #
# classify_body
# --------------------------------------------------------------------------- #
def test_classify_body_json_object() -> None:
    assert classify_body('{"a": 1}') == "json"


def test_classify_body_json_array() -> None:
    assert classify_body("[1, 2, 3]") == "json"


def test_classify_body_login_html() -> None:
    assert classify_body(LOGIN_HTML) == "login-html"


def test_classify_body_plain_html_is_not_login() -> None:
    assert classify_body(PLAIN_HTML) == "html"


def test_classify_body_text() -> None:
    assert classify_body("not json at all") == "text"


def test_classify_body_empty() -> None:
    assert classify_body("") == "empty"
    assert classify_body("   ") == "empty"


def test_classify_body_broken_json_is_not_json() -> None:
    # 以 { 开头但解析失败：不能因为「看起来像 JSON」就当作 JSON
    assert classify_body('{"a": 1,}') == "text"


# --------------------------------------------------------------------------- #
# 端点配置：绝不拿占位符发请求
# --------------------------------------------------------------------------- #
def test_load_endpoints_ignores_placeholder(tmp_path: Path) -> None:
    path = tmp_path / "endpoints.json"
    path.write_text(
        json.dumps(
            {
                "endpoints": [
                    {"name": "timetable", "method": "GET",
                     "path": "/REPLACE_WITH_OBSERVED_PATH"}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_endpoints(path) == {}


def test_load_endpoints_reads_confirmed_path(tmp_path: Path) -> None:
    path = tmp_path / "endpoints.json"
    path.write_text(
        json.dumps(
            {"endpoints": [{"name": "timetable", "method": "POST", "path": "/x/kb"}]}
        ),
        encoding="utf-8",
    )
    endpoints = load_endpoints(path)
    assert endpoints["timetable"].method == "POST"
    assert endpoints["timetable"].path == "/x/kb"


def test_load_endpoints_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_endpoints(tmp_path / "nope.json") == {}


def test_require_endpoint_missing_raises() -> None:
    with pytest.raises(EndpointNotConfigured):
        require_endpoint({}, "timetable")


def test_require_endpoint_rejects_placeholder() -> None:
    with pytest.raises(EndpointNotConfigured):
        require_endpoint(
            {"timetable": Endpoint("timetable", "GET", "/REPLACE_WITH_OBSERVED_PATH")},
            "timetable",
        )


# --------------------------------------------------------------------------- #
# fetch_via_http：正常与各类失败
# --------------------------------------------------------------------------- #
def test_fetch_returns_payload(cfg: Settings) -> None:
    payload = fetch_via_http(
        endpoint(), cfg=cfg, transport=transport(200, '{"rows": [{"kcmc": "x"}]}')
    )
    assert payload == {"rows": [{"kcmc": "x"}]}


def test_fetch_without_session_raises_authentication_required(no_session_cfg: Settings) -> None:
    with pytest.raises(AuthenticationRequired) as exc:
        fetch_via_http(endpoint(), cfg=no_session_cfg, transport=transport(200, "{}"))
    assert exc.value.exit_code == 2


def test_fetch_401_raises_authentication_expired(cfg: Settings) -> None:
    with pytest.raises(AuthenticationExpired) as exc:
        fetch_via_http(endpoint(), cfg=cfg, transport=transport(401, "{}"))
    assert exc.value.exit_code == 3


def test_fetch_403_raises_permission_denied(cfg: Settings) -> None:
    with pytest.raises(PermissionDenied) as exc:
        fetch_via_http(endpoint(), cfg=cfg, transport=transport(403, "{}"))
    assert exc.value.exit_code == 4


def test_fetch_login_html_is_auth_problem_not_parse_error(cfg: Settings) -> None:
    """会话失效时 eHall 常返回 200 + 登录页 HTML。

    这条是核心回归点：**不能报成 JSON 解析错误**，
    否则用户会以为是程序 bug 而不是去重新 login。
    """
    with pytest.raises(AuthenticationExpired) as exc:
        fetch_via_http(
            endpoint(), cfg=cfg,
            transport=transport(200, LOGIN_HTML, "text/html; charset=utf-8"),
        )
    assert exc.value.exit_code == 3
    assert "登录" in str(exc.value)


def test_fetch_plain_html_is_fetch_error_not_auth_error(cfg: Settings) -> None:
    with pytest.raises(TimetableFetchError) as exc:
        fetch_via_http(
            endpoint(), cfg=cfg,
            transport=transport(200, PLAIN_HTML, "text/html; charset=utf-8"),
        )
    assert not isinstance(exc.value, AuthenticationExpired)


def test_fetch_non_json_text_raises_fetch_error(cfg: Settings) -> None:
    with pytest.raises(TimetableFetchError):
        fetch_via_http(endpoint(), cfg=cfg, transport=transport(200, "service unavailable"))


def test_fetch_empty_body_raises_fetch_error(cfg: Settings) -> None:
    with pytest.raises(TimetableFetchError):
        fetch_via_http(endpoint(), cfg=cfg, transport=transport(200, ""))


def test_fetch_500_retries_then_raises(cfg: Settings) -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    with pytest.raises(TimetableFetchError):
        fetch_via_http(endpoint(), cfg=cfg, transport=httpx.MockTransport(handler))
    assert calls["n"] == cfg.max_retries


def test_fetch_403_is_not_retried(cfg: Settings) -> None:
    """401/403 是终态，重试只会浪费时间并可能触发风控。"""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(403, text="{}")

    with pytest.raises(PermissionDenied):
        fetch_via_http(endpoint(), cfg=cfg, transport=httpx.MockTransport(handler))
    assert calls["n"] == 1


def test_fetch_unexpected_status_raises(cfg: Settings) -> None:
    with pytest.raises(TimetableFetchError):
        fetch_via_http(endpoint(), cfg=cfg, transport=transport(404, "{}"))
