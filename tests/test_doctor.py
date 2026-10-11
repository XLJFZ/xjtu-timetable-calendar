"""`doctor` 判据核心：每条检查都要有成功 / 警告 / 阻塞三态，且全程只读。

夹具直接用 `subscribe_support.make_home`——它是仓库里"配置齐全的离线 home"的
唯一权威搭法（校历 / 作息 / raw 快照三件套），自己发明 JSON 形状只会造成 schema 漂移。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar.config import Settings
from xjtu_calendar.doctor import (
    DOCTOR_EXIT_BLOCKED,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    Check,
    doctor_exit_code,
    run_checks,
    worst_status,
)


def _write_session(cfg: Settings, *, age_days: int = 0) -> None:
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    saved = datetime.now(UTC) - timedelta(days=age_days)
    (cfg.session_dir / "storage_state.json").write_text(
        json.dumps(
            {
                "cookies": [{"name": "JSESSIONID", "value": "v", "domain": ".example.edu.cn"}],
                "_saved_at": saved.isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


# --------------------------------------------------------------------------- #
# 整体形状：齐全的环境不该有阻塞项
# --------------------------------------------------------------------------- #


def test_configured_home_has_no_blocked_items(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    checks = run_checks(cfg, SEMESTER)
    assert doctor_exit_code(checks) == 0
    assert worst_status(checks) == STATUS_WARN  # 没配订阅是警告，不是失败
    assert doctor_exit_code(run_checks(cfg, SEMESTER, verify=False)) == 0


def test_every_check_name_appears_once(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    names = [c.name for c in run_checks(cfg, SEMESTER)]
    assert names == [
        "version",
        "session",
        "semester_config",
        "schedule_config",
        "snapshot",
        "exam_snapshot",
        "render",
        "subscription",
    ]


# --------------------------------------------------------------------------- #
# 逐项三态
# --------------------------------------------------------------------------- #


def test_missing_session_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    check = by_name(run_checks(cfg, SEMESTER))["session"]
    assert check.status == STATUS_FAIL
    assert "login" in check.detail


def test_fresh_session_is_ok(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    assert by_name(run_checks(cfg, SEMESTER))["session"].status == STATUS_OK


def test_stale_session_warns_without_claiming_expiry(tmp_path: Path) -> None:
    """会话旧只警告，**不许**说它过期——旧 ≠ 失效，误报会把人推向错误的修复动作。"""
    cfg = make_home(tmp_path)
    _write_session(cfg, age_days=45)
    check = by_name(run_checks(cfg, SEMESTER))["session"]
    assert check.status == STATUS_WARN
    assert "过期" not in check.detail
    assert "login --force" in check.detail


def test_missing_semester_config_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.semester_config_path(SEMESTER).unlink()
    check = by_name(run_checks(cfg, SEMESTER))["semester_config"]
    assert check.status == STATUS_FAIL
    assert "学期校历缺失" in check.detail


def test_missing_schedule_config_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.schedule_config_path().unlink()
    check = by_name(run_checks(cfg, SEMESTER))["schedule_config"]
    assert check.status == STATUS_FAIL
    assert "作息表缺失" in check.detail


def test_missing_snapshot_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.raw_timetable_path(SEMESTER).unlink()
    check = by_name(run_checks(cfg, SEMESTER))["snapshot"]
    assert check.status == STATUS_FAIL
    assert "fetch" in check.detail


def test_no_exam_snapshot_is_warn_not_fail(tmp_path: Path) -> None:
    """未排考时本来就没有考试快照——这是常态，判成阻塞项会逼用户去查不存在的问题。"""
    cfg = make_home(tmp_path)
    _write_session(cfg)
    check = by_name(run_checks(cfg, SEMESTER))["exam_snapshot"]
    assert check.status == STATUS_WARN
    assert "未排考" in check.detail


def test_render_reports_event_and_exam_counts(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    check = by_name(run_checks(cfg, SEMESTER))["render"]
    assert check.status == STATUS_OK
    assert "个事件" in check.detail
    assert "考试" in check.detail


def test_subscription_reads_the_ledger_without_newline_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """留底必须按 bytes 读。

    `read_text` 的通用换行翻译会把 CRLF 折成 LF，于是内容 sha 永远对不上，
    doctor 会**永久误报**"有未发布变更"——真机上第一个跑出来的 bug 就是这个。
    """
    import xjtu_calendar.doctor as doctor_module
    from xjtu_calendar.subscribe import PushRecord, SubscriptionState

    cfg = make_home(tmp_path)
    _write_session(cfg)
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://example.invalid/r.git",
        branch="cal",
        token="t" * 32,
        url_base="https://example.invalid/cal",
        last_push=PushRecord(pushed_at="2026-10-11T00:00:00Z", content_sha256="x", token="t" * 32),
    )
    monkeypatch.setattr(doctor_module, "load_state", lambda *a, **k: state)

    seen: dict[str, str] = {}

    def _spy(_cfg: object, _state: object, text: str) -> bool:
        seen["text"] = text
        return False

    monkeypatch.setattr(doctor_module, "has_unpublished_changes", _spy)

    cfg.subscribe_dir.mkdir(parents=True, exist_ok=True)
    (cfg.subscribe_dir / f"last-{SEMESTER}.ics").write_bytes(
        b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    )

    check = by_name(run_checks(cfg, SEMESTER))["subscription"]
    assert "\r\n" in seen["text"]
    assert check.status == STATUS_OK


def test_render_failure_is_fail_and_does_not_raise(tmp_path: Path) -> None:
    """校历坏掉时 build 会抛错；doctor 必须把它折成一条 fail，而不是让命令炸掉。"""
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.semester_config_path(SEMESTER).write_text("{ 坏 JSON", encoding="utf-8")
    checks = run_checks(cfg, SEMESTER)
    assert by_name(checks)["render"].status == STATUS_FAIL


def test_unconfigured_subscription_is_warn(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    check = by_name(run_checks(cfg, SEMESTER))["subscription"]
    assert check.status == STATUS_WARN
    assert "未配置订阅" in check.detail


# --------------------------------------------------------------------------- #
# 两条全局承诺
# --------------------------------------------------------------------------- #


def test_doctor_is_read_only(tmp_path: Path) -> None:
    """只读承诺：跑完 doctor 后 home 下不得多出一个文件、也不得改动任何一个。"""
    cfg = make_home(tmp_path)
    _write_session(cfg)
    before = {p: p.stat().st_mtime_ns for p in cfg.home.rglob("*") if p.is_file()}
    run_checks(cfg, SEMESTER)
    after = {p: p.stat().st_mtime_ns for p in cfg.home.rglob("*") if p.is_file()}
    assert after == before


def test_exit_code_and_worst_status_are_pure() -> None:
    """退出码与最差状态是纯映射，不依赖夹具的真实渲染结果。"""
    assert worst_status([]) == STATUS_OK
    assert worst_status([Check("a", STATUS_OK, ""), Check("b", STATUS_WARN, "")]) == STATUS_WARN
    assert worst_status([Check("a", STATUS_OK, ""), Check("b", STATUS_FAIL, "")]) == STATUS_FAIL
    assert doctor_exit_code([Check("a", STATUS_WARN, "")]) == 0
    assert doctor_exit_code([Check("a", STATUS_FAIL, "")]) == DOCTOR_EXIT_BLOCKED


def test_verify_flag_adds_the_url_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 `--verify` 才联网；默认一次 HTTP 都不发（由 test_cli_doctor 再兜一层）。"""
    import xjtu_calendar.doctor as doctor_module

    cfg = make_home(tmp_path)
    _write_session(cfg)
    state = _FakeState()
    monkeypatch.setattr(doctor_module, "load_state", lambda *a, **k: state)

    names = [c.name for c in run_checks(cfg, SEMESTER, verify=True)]
    assert "url" in names
    assert "url" not in [c.name for c in run_checks(cfg, SEMESTER, verify=False)]


class _FakeState:
    subscription_url = "https://example.invalid/cal.ics"
    token = "t"
    last_push = None
