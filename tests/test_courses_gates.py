"""课程侧三道判据（docs/design/2026-10-12-course-cancellation-uid-v2.md CD12/CD13）。"""

from __future__ import annotations

from xjtu_calendar.courses import assess_courses, mass_cancellation_check
from xjtu_calendar.models import CourseMeeting


def _meetings(n: int) -> list[CourseMeeting]:
    return [
        CourseMeeting(f"DEMO-{i}", f"示例课程{'甲乙丙丁'[i % 4]}", 1, [1], [1]) for i in range(n)
    ]


def test_gate1_untrusted_when_no_records() -> None:
    got = assess_courses(total_candidates=0, skipped=[], meetings=[])
    assert not got.trusted and "没有" in (got.reason or "")


def test_gate1_untrusted_when_any_row_skipped() -> None:
    """有行读不懂 ⇒ 这份数据**不完整**，不能拿它判"某门课消失了"。"""
    got = assess_courses(
        total_candidates=9, skipped=["第 3 行：无法识别星期"], meetings=_meetings(8)
    )
    assert not got.trusted and got.reason


def test_gate1_trusted_when_everything_represented() -> None:
    assert assess_courses(total_candidates=9, skipped=[], meetings=_meetings(9)).trusted


def test_gate2_ratio_saves_localized_anomaly() -> None:
    """大日历的局部异常：候选数够，但 500 条里撤 20 条 ≈ 4%，比例条件不满足 ⇒ 不拦。"""
    assert mass_cancellation_check(candidates=20, ledger_total=500) is None


def test_gate2_small_ledger_high_ratio_triggers() -> None:
    """双条件按 CD12 字面判定：12/30 同时满足 ≥10 与 ≥30% ⇒ 熔断。

    简报原例把"30 里撤 12"写作不误伤对象，与 CD12 的形式判据冲突；以规格为准
    （小台账高比例本就危险，出口是 --allow-mass-cancellation 与退出码 6，CD4）。
    """
    got = mass_cancellation_check(candidates=12, ledger_total=30)
    assert got is not None and got.ratio > 0.30


def test_gate2_triggers_on_both_conditions() -> None:
    got = mass_cancellation_check(candidates=40, ledger_total=120)
    assert got is not None and got.ratio > 0.30


def test_gate2_below_min_events_never_triggers() -> None:
    assert mass_cancellation_check(candidates=9, ledger_total=20) is None


def test_gate2_empty_ledger_never_triggers() -> None:
    assert mass_cancellation_check(candidates=50, ledger_total=0) is None
