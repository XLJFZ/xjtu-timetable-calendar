"""订阅审阅回归：组合状态仍须全量核对，恢复与发布须使用同一输入。"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, payload_row
from test_cli_subscribe_recovery import _git, _init, _live_uids_by_course
from test_cli_subscribe_recovery import env as env

from xjtu_calendar import subscribe
from xjtu_calendar.cancellation import LedgerEntry, render_ledger
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings


@pytest.mark.parametrize("mismatch", ["exam_ledger", "exam_ledger_keep", "uid_recipe", None])
def test_course_pruning_does_not_skip_remaining_checks(
    tmp_path: Path, mismatch: str | None
) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    cfg.subscribe_dir.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 10, 27, tzinfo=UTC)
    course = LedgerEntry(
        "past-course",
        datetime(2026, 10, 1, tzinfo=UTC),
        datetime(2026, 10, 1, 1, tzinfo=UTC),
        "示例课程",
    )
    exam = LedgerEntry(
        "future-exam",
        datetime(2026, 12, 1, tzinfo=UTC),
        datetime(2026, 12, 1, 1, tzinfo=UTC),
        "示例考试",
    )
    published_course = render_ledger([], [course], dtstamp=now)
    published_exam = render_ledger([], [exam], dtstamp=now)
    cfg.exam_ledger_path(SEMESTER).write_bytes(published_exam.encode())
    state = subscribe.SubscriptionState(
        SEMESTER,
        "file:///unused",
        "cal",
        "a" * 32,
        "https://example.test",
        uid_recipe=2,
    )
    ics = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    state.last_push = subscribe.PushRecord(
        now.isoformat(),
        hashlib.sha256(ics.encode()).hexdigest(),
        state.token,
    )
    subscribe.write_journal(
        cfg,
        semester=SEMESTER,
        token=state.token,
        branch="cal",
        ics_text=ics,
        course_ledger_text=published_course,
        exam_ledger_text=None if mismatch == "exam_ledger_keep" else published_exam,
        uid_recipe=2,
        revive_salts={},
    )
    subscribe.promote_pending(cfg, SEMESTER)
    (cfg.subscribe_dir / f"last-{SEMESTER}.ics").write_bytes(ics.encode())
    cfg.course_ledger_path(SEMESTER).write_bytes(published_course.encode())
    assert subscribe.check_local_compatibility(cfg, state, SEMESTER, now=now).ok
    cfg.course_ledger_path(SEMESTER).write_bytes(render_ledger([], [], dtstamp=now).encode())
    if mismatch in {"exam_ledger", "exam_ledger_keep"}:
        cfg.exam_ledger_path(SEMESTER).unlink()
    elif mismatch == "uid_recipe":
        state.uid_recipe = None
    result = subscribe.check_local_compatibility(cfg, state, SEMESTER, now=now)
    if mismatch is None:
        assert result.ok and result.reason == "ledger_pruned_by_time", result
    else:
        assert not result.ok and result.reason == mismatch, result


@pytest.mark.parametrize("cached", [True, False])
@pytest.mark.parametrize("by_name", [True, False])
def test_revive_uses_explicit_input_through_rotate_and_no_change(
    env: tuple[Path, Path],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cached: bool,
    by_name: bool,
) -> None:
    home, origin = env
    assert _init(origin) == 0
    cfg = Settings(home=home)
    if not cached:
        cfg.raw_timetable_path(SEMESTER).unlink()
    row = payload_row(KCH="EXPLICIT", KCM="示例导入课程", ZCMC="13-15周")
    source = tmp_path / "explicit.json"
    payload = {"kbList": [row]}
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    args = ["subscribe", "push", "--semester", SEMESTER, "--input", str(source)]
    assert main(args) == 0
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    before = _live_uids_by_course(_git("show", f"cal:{state.token}.ics", cwd=origin))
    capsys.readouterr()
    assert main([*args, "--revive", row["KCM"] if by_name else "EXPLICIT"]) == 0
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.revive_salts == {"EXPLICIT": 1}
    revived = _live_uids_by_course(_git("show", f"cal:{state.token}.ics", cwd=origin))
    assert revived["EXPLICIT"] != before["EXPLICIT"]
    capsys.readouterr()
    assert main(args) == 0
    assert "无变化" in capsys.readouterr().out
    # rotate 无 --input 入口；模拟下一次 fetch 使缓存与已发布课表一致。
    shutil.copy2(source, cfg.raw_timetable_path(SEMESTER))
    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    assert _live_uids_by_course(_git("show", f"cal:{state.token}.ics", cwd=origin)) == revived
    capsys.readouterr()
    assert main(args) == 0
    assert "无变化" in capsys.readouterr().out
