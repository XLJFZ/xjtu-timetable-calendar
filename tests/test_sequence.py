"""SEQUENCE / LAST-MODIFIED 版本管理测试。

覆盖三类场景（对应日历客户端的更新判定）：

1. 无基线 -> 新事件：``SEQUENCE: 0``、``LAST-MODIFIED`` = 本次导出时刻（UTC）
2. 内容未变 -> 原样保留 ``SEQUENCE`` 与 ``LAST-MODIFIED``（客户端跳过更新）
3. 内容有变 -> ``SEQUENCE + 1``、``LAST-MODIFIED`` 刷新

外加：基线解析的容错与 fail-closed、指纹归一化（时区表示不同不算变化）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from icalendar import Calendar

from xjtu_calendar.errors import CalendarExportError
from xjtu_calendar.exporter import render_ics
from xjtu_calendar.models import CalendarEvent
from xjtu_calendar.sequence import (
    EventBaseline,
    event_fingerprint,
    parse_baseline,
    resolve_sequence,
    sequence_stats,
)
from xjtu_calendar.timeutil import TZ_XIAN

STAMP = datetime(2026, 9, 28, 12, 0, tzinfo=TZ_XIAN)
STAMP_2 = datetime(2026, 10, 5, 12, 0, tzinfo=TZ_XIAN)


def make_event(**overrides: object) -> CalendarEvent:
    defaults: dict[str, object] = {
        "uid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa@example",
        "summary": "示例课程甲",
        "start": datetime(2026, 9, 11, 8, 0, tzinfo=TZ_XIAN),
        "end": datetime(2026, 9, 11, 9, 50, tzinfo=TZ_XIAN),
        "location": "A-1001",
        "description": "教师：某教师",
    }
    defaults.update(overrides)
    return CalendarEvent(**defaults)  # type: ignore[arg-type]


def parse_event(ics_text: str, uid: str) -> dict[str, object]:
    cal = Calendar.from_ical(ics_text)
    for component in cal.walk("VEVENT"):
        if str(component.get("uid")) == uid:
            return dict(component)
    raise AssertionError(f"ICS 中找不到 UID {uid}")  # pragma: no cover


# --------------------------------------------------------------------------- #
# 无基线：全部按新增
# --------------------------------------------------------------------------- #
def test_render_without_baseline_assigns_sequence_zero() -> None:
    event = make_event()
    ics = render_ics([event], dtstamp=STAMP)
    props = parse_event(ics, event.uid)
    assert int(props["SEQUENCE"]) == 0


def test_render_last_modified_is_utc() -> None:
    """LAST-MODIFIED 必须是 UTC（RFC 5545），且等于本次导出时刻。"""
    event = make_event()
    ics = render_ics([event], dtstamp=STAMP)
    props = parse_event(ics, event.uid)
    last_modified = props["LAST-MODIFIED"].dt  # type: ignore[attr-defined]
    assert last_modified.tzinfo is not None
    assert last_modified.utcoffset().total_seconds() == 0
    assert last_modified == STAMP.astimezone(UTC)


# --------------------------------------------------------------------------- #
# 基线闭环：render -> parse -> 再 render
# --------------------------------------------------------------------------- #
def test_unchanged_event_preserves_sequence_and_last_modified() -> None:
    event = make_event()
    first = render_ics([event], dtstamp=STAMP)
    baseline = parse_baseline(first)

    second = render_ics([event], dtstamp=STAMP_2, baseline=baseline)
    props = parse_event(second, event.uid)
    assert int(props["SEQUENCE"]) == 0
    assert props["LAST-MODIFIED"].dt == STAMP.astimezone(UTC)  # type: ignore[attr-defined]


def test_changed_event_bumps_sequence_and_refreshes_last_modified() -> None:
    event = make_event()
    baseline = parse_baseline(render_ics([event], dtstamp=STAMP))

    moved = make_event(location="B-2002")  # 换教室：内容变了
    second = render_ics([moved], dtstamp=STAMP_2, baseline=baseline)
    props = parse_event(second, event.uid)
    assert int(props["SEQUENCE"]) == 1
    assert props["LAST-MODIFIED"].dt == STAMP_2.astimezone(UTC)  # type: ignore[attr-defined]


def test_reexported_baseline_keeps_sequence_monotonic() -> None:
    """连续两轮变更：SEQUENCE 逐次递增，不重置。"""
    event = make_event()
    first = parse_baseline(render_ics([event], dtstamp=STAMP))
    second = parse_baseline(
        render_ics([make_event(location="B-2002")], dtstamp=STAMP_2, baseline=first)
    )
    third_ics = render_ics(
        [make_event(location="B-2002", description="教师：另一位")],
        dtstamp=STAMP_2,
        baseline=second,
    )
    props = parse_event(third_ics, event.uid)
    assert int(props["SEQUENCE"]) == 2


# --------------------------------------------------------------------------- #
# 指纹与判定
# --------------------------------------------------------------------------- #
def test_fingerprint_ignores_timezone_representation() -> None:
    """同一时刻的上海表示与 UTC 表示必须算作未变。"""
    a = event_fingerprint(
        "示例课程甲", "A-1001", None,
        datetime(2026, 9, 11, 8, 0, tzinfo=TZ_XIAN),
        datetime(2026, 9, 11, 9, 50, tzinfo=TZ_XIAN),
    )
    b = event_fingerprint(
        "示例课程甲", "A-1001", None,
        datetime(2026, 9, 11, 0, 0, tzinfo=UTC),
        datetime(2026, 9, 11, 1, 50, tzinfo=UTC),
    )
    assert a == b


def test_resolve_sequence_content_change_bumps() -> None:
    event = make_event()
    baseline = {
        event.uid: EventBaseline(
            sequence=3,
            last_modified=datetime(2026, 9, 1, tzinfo=UTC),
            fingerprint=event_fingerprint(
                "不同内容", None, None, event.start, event.end
            ),
        )
    }
    sequence, last_modified = resolve_sequence(event, baseline, STAMP)
    assert sequence == 4
    assert last_modified == STAMP.astimezone(UTC)


def test_resolve_sequence_unchanged_keeps_old_state() -> None:
    event = make_event()
    fingerprint = event_fingerprint(
        event.summary, event.location, event.description, event.start, event.end
    )
    old_lm = datetime(2026, 9, 1, tzinfo=UTC)
    baseline = {
        event.uid: EventBaseline(sequence=7, last_modified=old_lm, fingerprint=fingerprint)
    }
    sequence, last_modified = resolve_sequence(event, baseline, STAMP)
    assert (sequence, last_modified) == (7, old_lm)


def test_sequence_stats_counts_three_buckets() -> None:
    kept = make_event()
    changed = make_event(uid="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb@example", location="B-2002")
    baseline = parse_baseline(render_ics([kept, changed], dtstamp=STAMP))

    # kept 未变；changed 再换教室（更新）；新增一条
    events = [kept, make_event(uid="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb@example", location="C-3003"),
              make_event(uid="cccccccccccccccccccccccccccccccc@example", summary="示例课程乙")]
    stats = sequence_stats(events, baseline)
    assert stats == {"preserved": 1, "updated": 1, "added": 1}


# --------------------------------------------------------------------------- #
# 基线解析：容错与 fail-closed
# --------------------------------------------------------------------------- #
def test_parse_baseline_skips_events_without_uid() -> None:
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//test//CN",
            "BEGIN:VEVENT",
            "DTSTART:20260911T080000Z",
            "DTEND:20260911T095000Z",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    assert parse_baseline(ics) == {}


def test_parse_baseline_skips_all_day_events() -> None:
    """DATE（非 DATE-TIME）形态不是本项目生成的，跳过而非报错。"""
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//test//CN",
            "BEGIN:VEVENT",
            "UID:allday@example",
            "DTSTART;VALUE=DATE:20260911",
            "DTEND;VALUE=DATE:20260912",
            "SUMMARY:全天事件",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    assert parse_baseline(ics) == {}


def test_parse_baseline_rejects_garbage() -> None:
    with pytest.raises(CalendarExportError):
        parse_baseline("这不是 ICS")


def test_parse_baseline_missing_last_modified_is_none() -> None:
    ics = "\r\n".join(
        [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//test//CN",
            "BEGIN:VEVENT",
            "UID:no-lm@example",
            "SEQUENCE:5",
            "DTSTART:20260911T000000Z",
            "DTEND:20260911T015000Z",
            "SUMMARY:旧事件",
            "END:VEVENT",
            "END:VCALENDAR",
            "",
        ]
    )
    baseline = parse_baseline(ics)
    assert baseline["no-lm@example"].sequence == 5
    assert baseline["no-lm@example"].last_modified is None


def test_baseline_ignores_dtstamp_only_differences() -> None:
    """两次导出只有 DTSTAMP 不同（元数据），内容未变必须判定为 preserved。"""
    event = make_event()
    a = render_ics([event], dtstamp=STAMP)
    baseline = parse_baseline(a)
    b = render_ics([event], dtstamp=STAMP_2, baseline=baseline)
    props = parse_event(b, event.uid)
    assert int(props["SEQUENCE"]) == 0
    # LAST-MODIFIED 保留第一轮的值，而不是被刷新
    assert props["LAST-MODIFIED"].dt == STAMP.astimezone(UTC)  # type: ignore[attr-defined]


def test_shanghai_zone_loaded() -> None:
    """守卫：本模块的时区假设依赖 Asia/Shanghai 可加载。"""
    assert ZoneInfo("Asia/Shanghai") is TZ_XIAN or str(TZ_XIAN)
