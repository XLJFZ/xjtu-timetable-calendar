"""`_fetch_exams` 的落盘行为（设计文档 §6.2 / §7）。

核心断言只有一条：**状态未知时既有快照的字节不许变**。
"""

from __future__ import annotations

import json
import os
import stat
import time
from datetime import datetime
from pathlib import Path

import pytest
from exam_support import SEMESTER, captured_logs, exam_payload, exam_row

from xjtu_calendar import fetcher
from xjtu_calendar.cli import _fetch_exams, main
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import AuthenticationExpired
from xjtu_calendar.exams import exam_snapshot_lag_days
from xjtu_calendar.logging_setup import redact
from xjtu_calendar.subscribe import snapshot_age_days


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    return cfg


def _endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="exam_schedule",
        method="POST",
        path="/jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do",
        description="我的考试安排",
    )


def _endpoints(**over: object) -> dict[str, fetcher.Endpoint]:
    table: dict[str, fetcher.Endpoint] = {"exam_schedule": _endpoint()}
    table.update(over)  # 传 exam_schedule=None 再由调用方 pop 可模拟缺端点
    return table


def _write_old_snapshot(cfg: Settings) -> str:
    """预置一份"上次抓到的"快照，用来验证未知态不会覆盖它。"""
    payload = exam_payload([exam_row(KCM="旧课程")])
    cfg.raw_exams_path(SEMESTER).write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return cfg.raw_exams_path(SEMESTER).read_text(encoding="utf-8")


def _patch(monkeypatch: pytest.MonkeyPatch, impl) -> None:
    monkeypatch.setattr(fetcher, "fetch_via_http", impl)


def test_fetch_exams_saves_snapshot_when_scheduled(home: Settings, monkeypatch) -> None:
    calls = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        calls.append((endpoint.name, params))
        return exam_payload([exam_row()])

    _patch(monkeypatch, fake)
    path = _fetch_exams(_endpoints(), home, SEMESTER)

    assert path == home.raw_exams_path(SEMESTER)
    assert path is not None and path.is_file()
    assert calls == [("exam_schedule", {"XNXQDM": SEMESTER})]  # 参数由调用方传，不写进端点表


@pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 POSIX 权限位语义")
def test_exam_snapshot_is_owner_only(home: Settings, monkeypatch) -> None:
    """§8：考试快照落盘必须 0600。权限位断言按 test_fileutil.py:69 的同款 skipif 走。"""
    _patch(monkeypatch, lambda *a, **k: exam_payload([exam_row()]))
    path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_state_keeps_existing_snapshot_byte_for_byte(home, monkeypatch) -> None:
    before = _write_old_snapshot(home)
    _patch(monkeypatch, lambda *a, **k: exam_payload([], code=0, msg="查询失败"))  # 实测未排考形态
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    assert home.raw_exams_path(SEMESTER).read_text(encoding="utf-8") == before
    assert any("不覆盖" in rec.getMessage() for rec in records)


def test_expired_session_is_unknown_not_empty(home: Settings, monkeypatch) -> None:
    before = _write_old_snapshot(home)

    def boom(*a, **k):
        raise AuthenticationExpired("会话已过期")

    _patch(monkeypatch, boom)
    assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    assert home.raw_exams_path(SEMESTER).read_text(encoding="utf-8") == before


@pytest.fixture(params=("unknown", "expired"))
def degraded(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """把 `fetch_via_http` 打成两种「拿不到考试但绝不许动本地快照」的形态之一。

    §7:376 要求这两条降级分支都报出旧数据的日期，所以每条都要过一遍 F1 的两个用例。
    返回形态名，供断言失败时连同消息一起打出来。
    """
    if request.param == "unknown":
        _patch(monkeypatch, lambda *a, **k: exam_payload([], code=0, msg="查询失败"))
    else:

        def boom(*a, **k):
            raise AuthenticationExpired("会话已过期")

        _patch(monkeypatch, boom)
    return str(request.param)


def _snapshot_day(cfg: Settings) -> str:
    """告警应当报出的日期 = 快照 mtime 那天（与实现的取法一致）。"""
    return datetime.fromtimestamp(cfg.raw_exams_path(SEMESTER).stat().st_mtime).date().isoformat()


def test_degraded_warns_with_existing_snapshot_date(home: Settings, degraded: str) -> None:
    """§7:376「有旧快照则沿用并**提示其日期**」：告警里必须出现那一天，而不是空口"沿用"。"""
    _write_old_snapshot(home)
    expected = _snapshot_day(home)
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    messages = [rec.getMessage() for rec in records]
    assert any(expected in message for message in messages), (degraded, messages)


def test_degraded_without_snapshot_says_local_copy_is_missing(
    home: Settings, degraded: str
) -> None:
    """没有本地快照时那句"沿用已有快照"是**谎话**：必须改成明说不含考试。"""
    assert not home.raw_exams_path(SEMESTER).exists()
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    messages = [rec.getMessage() for rec in records]
    assert any("没有" in message for message in messages), (degraded, messages)
    assert not any("沿用" in message for message in messages), (degraded, messages)


def test_confirmed_empty_writes_empty_snapshot(home: Settings, monkeypatch) -> None:
    """只有 code==1 + 空 rows（确认无考试）才允许把旧快照顶掉。"""
    _write_old_snapshot(home)
    _patch(monkeypatch, lambda *a, **k: exam_payload([], code=1, msg="操作成功"))
    path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["datas"]["wdksap"]["rows"] == []


def test_missing_endpoint_degrades_without_raising(home: Settings) -> None:
    with captured_logs() as records:
        assert _fetch_exams({}, home, SEMESTER) is None  # 没有 exam_schedule 键
    assert any("跳过考试安排" in rec.getMessage() for rec in records)
    assert not home.raw_exams_path(SEMESTER).exists()


def test_pagination_guard_warns_but_still_saves(home: Settings, monkeypatch) -> None:
    """§6.4「翻页护栏」：行数与 totalSize 不符只 warning，**照常落盘**（裁决 3）。

    实测 pageSize=999、totalSize 最大 7，今天一页够用；但一旦某学期超过一页，
    护栏保证「静默丢考试」不会发生——它只报警，绝不据此拒绝保存。
    """
    payload = exam_payload([exam_row()])
    payload["datas"]["wdksap"]["totalSize"] = 42  # 伪造「服务端说有更多行」
    _patch(monkeypatch, lambda *a, **k: payload)
    with captured_logs() as records:
        path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None and path.is_file()  # 只告警，不阻断落盘
    assert any("totalSize" in rec.getMessage() for rec in records)

    # 同族接口的数字经常是字符串形态（这份额外层 `code` 就是 "0"）：护栏不能因此静默关闭。
    payload["datas"]["wdksap"]["totalSize"] = "42"
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is not None
    assert any("totalSize" in rec.getMessage() and "42" in rec.getMessage() for rec in records)


def test_cmd_fetch_still_returns_zero_when_exams_explode(home: Settings, monkeypatch) -> None:
    """课表是主功能：考试侧任何异常都不能让 cmd_fetch 非零退出（§7 末条）。"""
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    monkeypatch.setattr(fetcher, "fetch_via_http", _timetable_then_boom)

    assert main(["fetch", "--semester", SEMESTER, "--source", "http"]) == 0
    assert not home.raw_exams_path(SEMESTER).exists()  # 考试失败没落任何半个快照


def test_cmd_fetch_no_exams_flag_never_requests_exams(home: Settings, monkeypatch) -> None:
    """`--no-exams`：三条分支一律不抓考试——连考试请求都不发，本地快照不动。

    只断言可观测行为（请求了哪些端点、有没有落盘），不抓日志：`main()` 会经
    `setup_logging` 清空 `xjtu_calendar` logger 的 handler，`captured_logs` 在 `main`
    路径里收不到记录（brief 的 `main()` 用例同样不依赖日志断言，即为此故）。
    """
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    requested: list[str] = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        requested.append(endpoint.name)
        return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}

    _patch(monkeypatch, fake)
    assert main(["fetch", "--semester", SEMESTER, "--source", "http", "--no-exams"]) == 0
    assert requested == ["timetable"]  # 关掉后连考试请求都不发
    assert not home.raw_exams_path(SEMESTER).exists()


def test_cmd_fetch_http_branch_fetches_exams_after_timetable(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正向接线（评审 F3）：课表抓完之后**真的**去抓考试，并把考试快照落了盘。

    上面两条 `main()` 用例断言的全是否定式（退出码 0／没落盘／没发请求），
    把 `cmd_fetch` 里的整个考试块删掉它们依然全绿——本任务最关键的那根线在测试里
    是不可见的。这条钉住请求顺序与落盘，是头号回归护栏。
    """
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    requested: list[str] = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        requested.append(endpoint.name)
        if endpoint.name == "exam_schedule":
            return exam_payload([exam_row()])
        return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}

    _patch(monkeypatch, fake)
    assert main(["fetch", "--semester", SEMESTER, "--source", "http"]) == 0
    assert requested == ["timetable", "exam_schedule"]
    assert home.raw_exams_path(SEMESTER).exists()


def _timetable_endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="timetable", method="POST", path="/jwapp/sys/xskcb/xskcb.do", description="我的课表"
    )


def _timetable_then_boom(endpoint, *, cfg=None, params=None, transport=None):
    """按端点分流：课表正常返回一份 `kbList`，考试直接抛未预期异常。"""
    if endpoint.name == "exam_schedule":
        raise RuntimeError("考试接口炸了")
    return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}


# --------------------------------------------------------------------------- #
# Task 11：陈旧口径（考试快照 vs 课表快照）与打码名单
# --------------------------------------------------------------------------- #


def _touch(path: Path, *, days_ago: float) -> None:
    """把 mtime 拨到 N 天前；跨平台都用 os.utime，不靠 sleep。"""
    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def test_snapshot_age_days_defaults_to_timetable_kind(tmp_path: Path) -> None:
    """既有调用点（cli.py 的两处 publish/status 调用）不传 kind，行为必须与改造前逐字一致。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    assert snapshot_age_days(cfg, SEMESTER) is None  # 还没 fetch 过

    cfg.raw_timetable_path(SEMESTER).write_text('{"kbList": []}', encoding="utf-8")
    assert snapshot_age_days(cfg, SEMESTER) < 1
    assert snapshot_age_days(cfg, SEMESTER, kind="exams") is None  # 考试侧仍为空


def test_exam_kind_reads_the_exam_snapshot(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    _touch(exam_path, days_ago=3)

    assert 2.9 < snapshot_age_days(cfg, SEMESTER, kind="exams") < 3.1
    assert snapshot_age_days(cfg, SEMESTER, kind="timetable") is None


def test_lag_is_exam_snapshot_behind_timetable_snapshot(tmp_path: Path) -> None:
    """§7 选定口径：考试快照落后于**课表快照**多少天，不是「距今几天」。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    timetable = cfg.raw_timetable_path(SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=10)
    _touch(timetable, days_ago=1)

    assert 8.9 < exam_snapshot_lag_days(cfg, SEMESTER) < 9.1


def test_lag_is_none_when_either_snapshot_is_missing(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    assert exam_snapshot_lag_days(cfg, SEMESTER) is None
    cfg.raw_exams_path(SEMESTER).write_text("{}{}", encoding="utf-8")  # 只有考试，没有课表
    assert exam_snapshot_lag_days(cfg, SEMESTER) is None


def test_exam_ahead_of_timetable_is_not_stale(tmp_path: Path) -> None:
    """考试比课表新（先 fetch 考试再动课表）不该报警：滞后为负按 0 处理。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    timetable = cfg.raw_timetable_path(SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=1)
    _touch(timetable, days_ago=5)

    assert exam_snapshot_lag_days(cfg, SEMESTER) == 0.0


def test_teacher_and_log_id_keys_are_redacted() -> None:
    """redact 是**按键**打码，不是按值：喂 dict 而不是喂 json 字符串。"""
    assert redact({"ZJJSXM": "张三"})["ZJJSXM"] == "***"
    assert redact({"SJBH": "007"})["SJBH"] == "***"
    assert redact({"XM": "李四"})["XM"] == "***"  # 既有能力，回归护栏
    assert redact({"KCM": "示例课程甲"})["KCM"] == "示例课程甲"  # 课程名不敏感
