"""课程侧三道判据（docs/design/2026-10-12-course-cancellation-uid-v2.md CD12/CD13）。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xjtu_calendar.cancellation import LedgerEntry
from xjtu_calendar.courses import (
    assess_courses,
    mass_cancellation_check,
    migration_candidates,
    upcoming_ledger_count,
)
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


@pytest.mark.parametrize(
    ("candidates", "ledger_total", "blocks"),
    [
        (10, 30, True),  # 恰好触到最小事件数（10/30 ≈ 33% 同时过比例关）⇒ 熔断
        (12, 40, True),  # 恰好 30% ⇒ 熔断（放行条件是 ratio < 0.30，等号不放过）
        (9, 20, False),  # 恰好少一条最小事件数 ⇒ 不熔断
        (299, 1000, False),  # 比例 0.299 ⇒ 不熔断
        (50, 0, False),  # 分母 0 ⇒ 不熔断（除零防线）
    ],
)
def test_gate2_boundaries(candidates: int, ledger_total: int, blocks: bool) -> None:
    """CD12 双条件的四个等号/邻界钉死：`==10`、`==30%`、`==9`、`<30%`、分母 `==0`。"""
    got = mass_cancellation_check(candidates=candidates, ledger_total=ledger_total)
    assert (got is not None) is blocks


def test_gate2_denominator_only_counts_upcoming_entries() -> None:
    """门 2 分母（终审裁定）：`start >= now` 的条目才进分母——与分子 `deliverable` 同域。

    旧口径数整本台账：学期中段 50 条里 40 条已过期时，"抓漏只剩 10 条"算出 10/50 = 20%，
    熔断恰好在该响的时候不响。通路级反证见 `test_exporter_course_cancellation` 同名用例。
    """
    ledger = {f"e{i}": _entry(f"e{i}", hours=-240 + i) for i in range(40)}  # 40 条全部在 NOW 之前
    ledger.update({f"u{i}": _entry(f"u{i}", hours=10 + i) for i in range(10)})
    assert upcoming_ledger_count(ledger, NOW) == 10
    assert upcoming_ledger_count({}, NOW) == 0


def test_gate2_denominator_excludes_already_cancelled_uids() -> None:
    """分母与分子同域的第二半：基线里已 CANCELLED 的墓碑 UID 要从分母一并抹掉。

    只排分子会在学期末重新打开分母稀释的洞：10 条未过期 live 候选 + 80 条墓碑
    ⇒ 10/90 ≈ 11% < 30%，熔断又漏响。排除后 10/10 = 100%。
    """
    ledger = {f"u{i}": _entry(f"u{i}", hours=10 + i) for i in range(10)}
    ledger.update({f"t{i}": _entry(f"t{i}", hours=20 + i) for i in range(80)})
    tombstones = {f"t{i}" for i in range(80)}
    assert upcoming_ledger_count(ledger, NOW) == 90
    assert upcoming_ledger_count(ledger, NOW, exclude_uids=tombstones) == 10
    # 默认参数保持既有调用形状（关键字默认，不破坏兼容）。
    assert upcoming_ledger_count(ledger, NOW, exclude_uids=()) == 90
    assert upcoming_ledger_count(ledger, NOW, exclude_uids=frozenset(ledger)) == 0


def _entry(uid: str, *, hours: int) -> LedgerEntry:
    start = datetime(2026, 10, 1, 8, tzinfo=UTC) + timedelta(hours=hours)
    return LedgerEntry(uid=uid, start=start, end=start + timedelta(hours=2), summary="示例课程甲")


NOW = datetime(2026, 10, 1, 0, tzinfo=UTC)


def test_migration_cancels_every_stale_published_uid() -> None:
    published = {u: _entry(u, hours=10 + i) for i, u in enumerate(["a", "b", "c"])}
    got = migration_candidates(
        published=published, live_uids={"b"}, normal_cancel_uids=set(), now=NOW
    )
    assert [e.uid for e in got] == ["a", "c"]


def test_migration_skips_entries_already_cancelled_this_run() -> None:
    """常态分支已经要撤的 UID，迁移分支不许再出一份（同 UID 双条目）。"""
    published = {"a": _entry("a", hours=10)}
    got = migration_candidates(
        published=published, live_uids=set(), normal_cancel_uids={"a"}, now=NOW
    )
    assert got == []


def test_migration_skips_expired_entries() -> None:
    """CD10：留底里含"上次已撤销"的条目，它们不在任何台账里；
    不过期筛就会把上学期的旧事件反复撤销、无限续命。

    （简报原例的 hours=-5 换算后是 03:00，仍在 NOW=00:00 之后、并没有过期；
    按 CD10 的语义把过期条目改成真正落在 now 之前的时刻。）
    """
    published = {"old": _entry("old", hours=-10), "new": _entry("new", hours=5)}
    got = migration_candidates(
        published=published, live_uids=set(), normal_cancel_uids=set(), now=NOW
    )
    assert [e.uid for e in got] == ["new"]


def test_migration_output_is_fully_ordered() -> None:
    published = {u: _entry(u, hours=20 - i) for i, u in enumerate(["x", "y", "z"])}
    got = migration_candidates(published, set(), set(), NOW)  # 位置参数也要能吃
    assert [e.uid for e in got] == ["z", "y", "x"]
