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
import os
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
    load_raw,
    require_endpoint,
    save_raw,
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
                "cookies": [{"name": "JSESSIONID", "value": "fake", "domain": "ehall.xjtu.edu.cn"}],
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


def transport(
    status: int, body: str, content_type: str = "application/json"
) -> httpx.MockTransport:
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
                    {"name": "timetable", "method": "GET", "path": "/REPLACE_WITH_OBSERVED_PATH"}
                ]
            }
        ),
        encoding="utf-8",
    )
    assert load_endpoints(path) == {}


def test_load_endpoints_reads_confirmed_path(tmp_path: Path) -> None:
    path = tmp_path / "endpoints.json"
    path.write_text(
        json.dumps({"endpoints": [{"name": "timetable", "method": "POST", "path": "/x/kb"}]}),
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
            endpoint(),
            cfg=cfg,
            transport=transport(200, LOGIN_HTML, "text/html; charset=utf-8"),
        )
    assert exc.value.exit_code == 3
    assert "登录" in str(exc.value)


def test_fetch_plain_html_is_fetch_error_not_auth_error(cfg: Settings) -> None:
    with pytest.raises(TimetableFetchError) as exc:
        fetch_via_http(
            endpoint(),
            cfg=cfg,
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


def test_classify_body_detects_login_markers_late_in_long_page() -> None:
    """登录页标记出现在 4000 字符窗口之外时也必须识别为 login-html。

    eHall 会话失效常以 200 + 登录页 HTML 响应；若标记词落在扫描窗口外，
    会被误判成普通「不是 JSON」的获取错误，用户看不到「该重新登录」的提示。
    """
    filler = "<meta property='x' content='noise'/>" * 400  # >19000 字符
    text = f"<html><head>{filler}<title>统一身份认证</title></head><body>请登录</body></html>"
    assert len(filler) > 4000
    assert classify_body(text) == "login-html"


def test_save_raw_rotates_previous_snapshot(cfg: Settings) -> None:
    """fetch 落盘前把上一份快照轮转为 .prev.json（diff 的比较基线，单代）。

    没有轮转时用户想比对变化就得手工复制缓存文件——「调课检测」的
    前提是把「上一次」自动留住。旧快照不存在时不造空 prev。
    """
    current = cfg.raw_timetable_path("2026-fall")
    previous = cfg.raw_timetable_prev_path("2026-fall")

    save_raw({"v": 1}, cfg, "2026-fall")
    assert current.is_file() and not previous.exists()

    save_raw({"v": 2}, cfg, "2026-fall")
    assert json.loads(previous.read_text(encoding="utf-8")) == {"v": 1}
    assert json.loads(current.read_text(encoding="utf-8")) == {"v": 2}

    save_raw({"v": 3}, cfg, "2026-fall")
    assert json.loads(previous.read_text(encoding="utf-8")) == {"v": 2}
    assert json.loads(current.read_text(encoding="utf-8")) == {"v": 3}


def test_save_raw_kind_rotates_only_its_own_stream(tmp_path):
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    save_raw({"a": 1}, cfg, "2026-2027-1")
    save_raw({"exams": "v1"}, cfg, "2026-2027-1", kind="exams")
    save_raw({"exams": "v2"}, cfg, "2026-2027-1", kind="exams")
    assert cfg.raw_timetable_prev_path("2026-2027-1").exists() is False  # 课表只写过一次
    assert cfg.raw_exams_prev_path("2026-2027-1").read_text(encoding="utf-8").find("v1") >= 0
    assert load_raw(cfg, "2026-2027-1", kind="exams") == {"exams": "v2"}


def test_exam_snapshot_is_owner_only(tmp_path):
    """§8 的用户可见结果：考试快照落盘 0600。

    这条**不足以**看守 `private=True`（见下面那条 kwargs 用例的说明）：
    `atomic_write_text` 走 `tempfile.mkstemp` + `os.replace`，出来的文件本来就是
    0600，把 `private=True` 删掉它也不红。它钉的是"用户拿到的权限位"，
    §8 的"必须显式收紧"由 `test_both_snapshot_kinds_request_the_private_write` 钉。
    """
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    path = save_raw({"exams": 1}, cfg, "2026-2027-1", kind="exams")
    mode = path.stat().st_mode & 0o077
    if os.name != "posix":
        pytest.skip("Windows 不执行 POSIX 权限位")
    assert mode == 0


def test_both_snapshot_kinds_request_the_private_write(tmp_path, monkeypatch):
    """§8 的可证伪看守：`private=True` 必须**作为关键字参数**传到 `atomic_write_text`。

    为什么光有上面的 mode 断言不够：`fileutil.atomic_write_text` 用
    `tempfile.mkstemp` 造临时文件（0600，umask 只会收窄不会放宽），再用
    `os.replace` 同 inode 改名 —— 所以**删掉 `private=True` 之后落盘权限位分毫不变**，
    两条 mode 用例在任何平台上都照样绿（Windows 上还整条 skip）。
    形状照 `test_auth_session.py:142` 的 `ensure_login` 间谍：monkeypatch 包住
    **`fetcher` 里实际引用的那个名字**（`from .fileutil import atomic_write_text`
    是模块属性绑定，打桩 `fileutil` 上的同名函数收不到任何调用 → 假绿），
    替身转调真实实现，写入照常发生。
    """
    from xjtu_calendar import fetcher as fetcher_module

    captured: list[dict[str, object]] = []
    real_write = fetcher_module.atomic_write_text

    def spy(path: Path, text: str, **kwargs: object) -> None:
        captured.append(kwargs)
        real_write(path, text, **kwargs)  # 照常完成写入，否则测不到真行为

    monkeypatch.setattr(fetcher_module, "atomic_write_text", spy)

    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    save_raw({"a": 1}, cfg, "2026-2027-1")  # 默认 kind：课表快照（Task 6 一并收紧）
    timetable_kwargs = captured[-1]
    exam_path = save_raw({"exams": 1}, cfg, "2026-2027-1", kind="exams")

    assert timetable_kwargs.get("private") is True
    assert captured[-1].get("private") is True
    # 替身没把写入吞掉：快照内容确实落到了考试路径上
    assert json.loads(exam_path.read_text(encoding="utf-8")) == {"exams": 1}
    assert len(captured) == 2  # 两条路径各一次，没有偷偷多写第三份


def test_load_raw_missing_exams_snapshot_hint(tmp_path):
    cfg = Settings(home=tmp_path)
    with pytest.raises(TimetableFetchError, match="fetch"):
        load_raw(cfg, "2026-2027-1", kind="exams")


def test_unknown_snapshot_kind_is_rejected(tmp_path):
    """`_raw_paths` 收到未知 kind 必须**直接抛**（终审 F7，`fetcher` 这一侧）。

    `"exam"` 是 `"exams"` 的常见笔误。少了这条 `ValueError`，`kind` 判断会一路落到
    课表分支返回课表路径：`load_raw(kind="exam")` 于是**读错文件**，把课表信封交给
    考试解析，得到 0 行还理直气壮地报"本学期无考试"—— 静默的错数据比抛异常难查得多。
    所以这里先把课表快照写上：回落真的会"成功"，用例才谈得上证伪。
    `subscribe.snapshot_age_days` 那一侧的同类用例见 Task 11。
    """
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    save_raw({"kbList": []}, cfg, "2026-2027-1")
    with pytest.raises(ValueError, match="未知的快照类型"):
        load_raw(cfg, "2026-2027-1", kind="exam")
