"""`_fetch_exams` 的落盘行为（设计文档 §6.2 / §7）。

核心断言只有一条：**状态未知时既有快照的字节不许变**。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from exam_support import SEMESTER, captured_logs, exam_payload, exam_row

from xjtu_calendar import fetcher
from xjtu_calendar.cli import _fetch_exams, main
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import AuthenticationExpired


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


def _timetable_endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="timetable", method="POST", path="/jwapp/sys/xskcb/xskcb.do", description="我的课表"
    )


def _timetable_then_boom(endpoint, *, cfg=None, params=None, transport=None):
    """按端点分流：课表正常返回一份 `kbList`，考试直接抛未预期异常。"""
    if endpoint.name == "exam_schedule":
        raise RuntimeError("考试接口炸了")
    return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}
