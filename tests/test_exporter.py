"""ICS 导出测试（Phase 5）。

重点覆盖用户明确要求的五条：

1. 相同课程事件重新生成 -> UID 不变
2. 不同日期 -> UID 不同
3. DTSTART / DTEND 正确
4. LOCATION 正确
5. 时区 = Asia/Shanghai

外加：不使用 RRULE、逐次上课生成独立 VEVENT、DESCRIPTION 结构、RFC 5545 合规。
"""

from __future__ import annotations

from datetime import date, datetime

import pytest
from icalendar import Calendar

from xjtu_calendar.academic_calendar import AcademicCalendar, DateOverride
from xjtu_calendar.errors import CalendarExportError
from xjtu_calendar.exporter import (
    PRODID,
    UID_DOMAIN,
    build_events,
    collect_unsupported,
    make_uid,
    render_ics,
    summarize,
)
from xjtu_calendar.models import CourseMeeting, Semester, UnsupportedAdjustment
from xjtu_calendar.schedules import ScheduleTable
from xjtu_calendar.timeutil import TZ_NAME

WEEK1_MONDAY = date(2026, 9, 7)


# --------------------------------------------------------------------------- #
# 固件
# --------------------------------------------------------------------------- #
@pytest.fixture
def calendar() -> AcademicCalendar:
    return AcademicCalendar(
        Semester(
            key="2026-fall",
            name="2026-2027 学年秋季学期",
            first_week_monday=WEEK1_MONDAY,
            total_weeks=16,
            start_date=WEEK1_MONDAY,
            end_date=date(2026, 12, 27),
        )
    )


@pytest.fixture
def schedules() -> ScheduleTable:
    return ScheduleTable.from_dict(
        {
            "profiles": {
                "summer": {
                    "name": "夏季作息",
                    "periods": {
                        "1": ["08:00", "08:50"],
                        "2": ["09:00", "09:50"],
                        "5": ["14:00", "14:50"],
                        "6": ["15:00", "15:50"],
                    },
                },
                "winter": {
                    "name": "冬季作息",
                    "periods": {
                        "1": ["08:30", "09:20"],
                        "2": ["09:30", "10:20"],
                        "5": ["14:30", "15:20"],
                        "6": ["15:30", "16:20"],
                    },
                },
            },
            "periods": [
                {"start": "2026-09-07", "end": "2026-10-11", "profile": "summer"},
                {"start": "2026-10-12", "end": "2027-01-10", "profile": "winter"},
            ],
        }
    )


@pytest.fixture
def schedules_wide() -> ScheduleTable:
    """覆盖到第 20 周之后的作息表，供「无上限展开」用例使用。

    上面的 ``schedules`` 固件只覆盖到 2027-01-10，第 20 周（2027-01-15 起）
    会命中「作息未配置」而报错 —— 那是正确行为，但会掩盖本用例要验证的点。
    """
    return ScheduleTable.from_dict(
        {
            "profiles": {
                "all-year": {
                    "name": "全年作息",
                    "periods": {
                        "1": ["08:00", "08:50"],
                        "2": ["09:00", "09:50"],
                    },
                }
            },
            "periods": [
                {"start": "2026-09-01", "end": "2027-03-31", "profile": "all-year"}
            ],
        }
    )


@pytest.fixture
def sample_meeting() -> CourseMeeting:
    """示例课程安排。课程名/教师/地点/课程编号均为虚构占位值，不含真实个人信息。"""
    return CourseMeeting(
        course_id="DEMO-1001",
        course_name="示例课程甲",
        teacher="某教师",
        location="A-1001",
        campus="创新港校区",
        weekday=5,             # 星期五
        periods=[1, 2],        # 1-2 节
        weeks=[1, 2, 3, 4, 5, 6, 7, 8],
        raw_week_text="1-8周",
        raw_period_text="1-2节",
    )


# --------------------------------------------------------------------------- #
# UID 稳定性（用户明确要求）
# --------------------------------------------------------------------------- #
def test_uid_is_stable_across_regeneration(sample_meeting: CourseMeeting) -> None:
    """同一门课的同一节课反复导出应得到同一个 UID。"""
    first = make_uid("2026-fall", sample_meeting, "2026-09-11")
    second = make_uid("2026-fall", sample_meeting, "2026-09-11")
    assert first == second
    assert first.endswith(f"@{UID_DOMAIN}")


def test_uid_differs_by_date(sample_meeting: CourseMeeting) -> None:
    """不同日期必须得到不同 UID，否则日历会把这些课当成同一事件覆盖掉。"""
    a = make_uid("2026-fall", sample_meeting, "2026-09-11")
    b = make_uid("2026-fall", sample_meeting, "2026-09-18")
    assert a != b


def test_uid_differs_by_semester_and_course(sample_meeting: CourseMeeting) -> None:
    base = make_uid("2026-fall", sample_meeting, "2026-09-11")
    assert base != make_uid("2027-spring", sample_meeting, "2026-09-11")

    other = CourseMeeting(
        course_id="OTHER-1",
        course_name="另一门课",
        weekday=5,
        periods=[1, 2],
        weeks=[1],
    )
    assert base != make_uid("2026-fall", other, "2026-09-11")


def test_uid_ignores_location_and_time_changes(sample_meeting: CourseMeeting) -> None:
    """换教室不应产生新事件，只应更新原事件——因此 UID 不含地点。"""
    moved = CourseMeeting(
        course_id=sample_meeting.course_id,
        course_name=sample_meeting.course_name,
        teacher=sample_meeting.teacher,
        location="完全不同的地点",
        campus=sample_meeting.campus,
        weekday=sample_meeting.weekday,
        periods=sample_meeting.periods,
        weeks=sample_meeting.weeks,
    )
    assert make_uid("2026-fall", sample_meeting, "2026-09-11") == make_uid(
        "2026-fall", moved, "2026-09-11"
    )


def test_uid_differs_by_periods(sample_meeting: CourseMeeting) -> None:
    shifted = CourseMeeting(
        course_id=sample_meeting.course_id,
        course_name=sample_meeting.course_name,
        weekday=sample_meeting.weekday,
        periods=[3, 4],
        weeks=sample_meeting.weeks,
    )
    assert make_uid("2026-fall", sample_meeting, "2026-09-11") != make_uid(
        "2026-fall", shifted, "2026-09-11"
    )


def test_uid_stable_when_course_id_missing() -> None:
    """course_id 缺失时降级为课程名，UID 依然稳定。"""
    m = CourseMeeting(None, "无编号课程", 3, [5, 6], [1, 2])
    assert make_uid("2026-fall", m, "2026-09-16") == make_uid("2026-fall", m, "2026-09-16")


# --------------------------------------------------------------------------- #
# 事件展开
# --------------------------------------------------------------------------- #
def test_build_events_one_vevent_per_actual_class(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """8 个教学周 -> 8 个独立事件（而非 1 个 RRULE）。"""
    events = build_events([sample_meeting], calendar, schedules)
    assert len(events) == 8


def test_build_events_dates_are_in_week_order(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    days = [e.start.date() for e in events]
    assert days == [
        date(2026, 9, 11),
        date(2026, 9, 18),
        date(2026, 9, 25),
        date(2026, 10, 2),
        date(2026, 10, 9),
        date(2026, 10, 16),
        date(2026, 10, 23),
        date(2026, 10, 30),
    ]


def test_build_events_dtstart_dtend_correct(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    first = events[0]

    # 第 1 周周五 = 2026-09-11，处在夏季区间 -> 1-2 节 = 08:00-09:50
    assert first.start == datetime(2026, 9, 11, 8, 0, tzinfo=first.start.tzinfo)
    assert first.end == datetime(2026, 9, 11, 9, 50, tzinfo=first.start.tzinfo)


def test_build_events_respects_season_switch(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """第 5 周（10/9）仍在夏季，第 6 周（10/16）已进入冬季。

    这正是「不能用单个 RRULE」的原因：同样的 1-2 节，钟点变了。
    """
    events = build_events([sample_meeting], calendar, schedules)
    week5 = events[4]
    week6 = events[5]

    assert week5.start.date() == date(2026, 10, 9)
    assert (week5.start.hour, week5.start.minute) == (8, 0)

    assert week6.start.date() == date(2026, 10, 16)
    assert (week6.start.hour, week6.start.minute) == (8, 30)


def test_build_events_timezone_is_asia_shanghai(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    for event in events:
        assert str(event.start.tzinfo) == TZ_NAME
        assert str(event.end.tzinfo) == TZ_NAME
        assert event.start.utcoffset().total_seconds() == 8 * 3600


def test_build_events_location(sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    assert events[0].location == "创新港校区 A-1001"


def test_build_events_summary_is_course_name_only(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """SUMMARY 只放课程名，不塞教师/周次等信息。"""
    events = build_events([sample_meeting], calendar, schedules)
    assert events[0].summary == "示例课程甲"


def test_build_events_description_structure(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    description = events[0].description or ""
    assert "教师：某教师" in description
    assert "教学周：1-8周" in description
    assert "节次：1-2节" in description
    assert "课程编号：DEMO-1001" in description


def test_build_events_sorted_by_start(
    calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    meetings = [
        CourseMeeting("B", "晚点的课", 5, [5, 6], [1]),
        CourseMeeting("A", "早点的课", 5, [1, 2], [1]),
    ]
    events = build_events(meetings, calendar, schedules)
    assert [e.summary for e in events] == ["早点的课", "晚点的课"]


# --------------------------------------------------------------------------- #
# 停课 / 调课
# --------------------------------------------------------------------------- #
def test_build_events_skips_excluded_dates(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """第 3 周周五（2026-09-25）停课 -> 事件数减一，且该日无事件。"""
    calendar.excluded_dates.add(date(2026, 9, 25))
    events = build_events([sample_meeting], calendar, schedules)
    assert len(events) == 7
    assert date(2026, 9, 25) not in {e.start.date() for e in events}


def test_build_events_honours_cancel_override(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    day = date(2026, 10, 2)
    calendar.overrides[day] = DateOverride(day=day, cancel=True)
    events = build_events([sample_meeting], calendar, schedules)
    assert day not in {e.start.date() for e in events}


def test_build_events_honours_location_and_note_override(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    day = date(2026, 10, 2)
    calendar.overrides[day] = DateOverride(
        day=day, location="临时教室 A-101", note="调课"
    )
    events = build_events([sample_meeting], calendar, schedules)
    target = next(e for e in events if e.start.date() == day)
    assert target.location == "临时教室 A-101"
    assert "备注：调课" in (target.description or "")


def test_build_events_can_disable_override_notes(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    day = date(2026, 10, 2)
    calendar.overrides[day] = DateOverride(day=day, note="调课")
    events = build_events([sample_meeting], calendar, schedules, with_override_notes=False)
    target = next(e for e in events if e.start.date() == day)
    assert "备注" not in (target.description or "")


def test_build_events_fails_closed_on_out_of_range_week(
    calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """total_weeks 有值 + 出现越界周次 -> 显式报错，绝不静默丢周。

    这是 P0 的核心契约：旧行为是 ``continue`` 跳过，会产出
    「生成成功但悄悄缺课」的 ICS，比报错危险得多。
    """
    long_meeting = CourseMeeting("LONG", "超长课程", 5, [1, 2], list(range(1, 21)))

    with pytest.raises(CalendarExportError) as excinfo:
        build_events([long_meeting], calendar, schedules)

    message = str(excinfo.value)
    assert "17" in message            # 第一个越界周次
    assert "16" in message            # 学期总周数
    assert "超长课程" in message       # 必须指名是哪门课，方便定位
    assert "total_weeks" in message   # 必须给出处理指引


def test_build_events_expands_all_weeks_when_total_weeks_is_none(
    schedules_wide: ScheduleTable,
) -> None:
    """total_weeks 为 None -> 完全按 meeting.weeks 原样展开，不做上限过滤。"""
    open_calendar = AcademicCalendar(
        Semester(
            key="2026-fall",
            name="2026-2027 学年秋季学期",
            first_week_monday=WEEK1_MONDAY,
            total_weeks=None,
        )
    )
    long_meeting = CourseMeeting("LONG", "超长课程", 5, [1, 2], list(range(1, 21)))

    events = build_events([long_meeting], open_calendar, schedules_wide)
    assert len(events) == 20          # 20 周一次不落，包括超出 16 的部分


def test_build_events_raises_on_non_positive_week(
    calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """周次 < 1 是非法数据，同样显式报错而非跳过。"""
    broken = CourseMeeting("BAD", "错误周次课程", 5, [1, 2], [0, 1, 2])

    with pytest.raises(CalendarExportError) as excinfo:
        build_events([broken], calendar, schedules)

    assert "错误周次课程" in str(excinfo.value)


def test_build_events_never_silently_drops_weeks(
    calendar: AcademicCalendar, schedules_wide: ScheduleTable
) -> None:
    """回归守卫：越界时要么报错，要么全量展开，不允许第三种（静默丢弃）。

    前半段断言「有上限时报错」，后半段断言「无上限时全出」，
    两者共同把「生成成功但周数变少」这条路堵死。
    """
    long_meeting = CourseMeeting("LONG", "超长课程", 5, [1, 2], list(range(1, 21)))

    with pytest.raises(CalendarExportError):
        build_events([long_meeting], calendar, schedules)

    open_calendar = AcademicCalendar(
        Semester("2026-fall", "2026-2027 学年秋季学期", WEEK1_MONDAY, total_weeks=None)
    )
    # 展开结果必须与课表自身周次集合严格一一对应
    assert len(build_events([long_meeting], open_calendar, schedules_wide)) == len(
        set(long_meeting.weeks)
    )


def test_build_events_raises_on_unconfigured_schedule(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar
) -> None:
    """作息表未覆盖时明确报错，绝不静默跳过或猜测时间。"""
    empty = ScheduleTable(profiles={}, periods=[])
    with pytest.raises(CalendarExportError, match="解析"):
        build_events([sample_meeting], calendar, empty)


def test_build_events_deduplicates_same_uid(
    calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """同一课程在导入数据里重复出现时只保留一个事件。"""
    m = CourseMeeting("DUP", "重复课程", 5, [1, 2], [1])
    duplicate = CourseMeeting("DUP", "重复课程", 5, [1, 2], [1])
    events = build_events([m, duplicate], calendar, schedules)
    assert len(events) == 1


# --------------------------------------------------------------------------- #
# unsupported_adjustments：已知但无法表达的调课（fail-closed 的数据来源）
# --------------------------------------------------------------------------- #
def _calendar_with_adjustments(
    base: AcademicCalendar, *adjustments: UnsupportedAdjustment
) -> AcademicCalendar:
    return AcademicCalendar(
        semester=base.semester,
        excluded_dates=base.excluded_dates,
        overrides=base.overrides,
        unsupported_adjustments=tuple(adjustments),
    )


def test_collect_unsupported_reports_only_in_range(
    calendar: AcademicCalendar, schedules: ScheduleTable, sample_meeting: CourseMeeting
) -> None:
    """声明了的调课若落在事件日期跨度内则报告；范围外的不报告。"""
    events = build_events([sample_meeting], calendar, schedules)
    cal_with = _calendar_with_adjustments(
        calendar,
        UnsupportedAdjustment(date(2026, 9, 20), "按 10-06 课表上课"),
        UnsupportedAdjustment(date(2026, 10, 10), "按 10-07 课表上课"),
        UnsupportedAdjustment(date(2025, 3, 2), "上学期的事，不应命中"),
    )

    got = collect_unsupported(cal_with, events)
    dates = {a.date for a in got}
    assert dates == {date(2026, 9, 20), date(2026, 10, 10)}


def test_collect_unsupported_empty_when_not_declared(
    calendar: AcademicCalendar, schedules: ScheduleTable, sample_meeting: CourseMeeting
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    assert collect_unsupported(calendar, events) == []


def test_collect_unsupported_all_hit_when_no_events(calendar: AcademicCalendar) -> None:
    """事件为空时全部命中 —— 空日历同样需要用户知情。"""
    cal_with = _calendar_with_adjustments(
        calendar, UnsupportedAdjustment(date(2026, 9, 20), "按 10-06 课表上课")
    )
    assert len(collect_unsupported(cal_with, [])) == 1


def test_written_ics_keeps_rfc5545_crlf(
    tmp_path: object, calendar: AcademicCalendar, schedules: ScheduleTable,
    sample_meeting: CourseMeeting,
) -> None:
    """写文件时不得二次翻译行尾：render_ics 产出 CRLF，落盘必须仍是 CRLF。

    Windows 上 `write_text` 默认 newline=None 会把 \\n 再翻译成 os.linesep，
    得到 ``\\r\\r\\n``。cli 侧用 ``newline=""`` 写入 —— 这里锁定该行为。
    """
    ics = render_ics(build_events([sample_meeting], calendar, schedules))
    assert "\r\n" in ics
    assert "\r\r\n" not in ics

    path = tmp_path / "timetable.ics"  # type: ignore[operator]
    path.write_text(ics, encoding="utf-8", newline="")  # 与 cli 写法一致
    raw = path.read_bytes()
    assert raw.count(b"\r\r\n") == 0
    assert raw.count(b"\r\n") == raw.count(b"\n")


# --------------------------------------------------------------------------- #
# ICS 渲染
# --------------------------------------------------------------------------- #
def test_render_ics_has_required_properties(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    ics = render_ics(events)

    assert "BEGIN:VCALENDAR" in ics
    assert "VERSION:2.0" in ics
    assert "CALSCALE:GREGORIAN" in ics
    assert PRODID in ics


def test_render_ics_event_has_required_fields(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    ics = render_ics(events)
    cal = Calendar.from_ical(ics)

    vevents = list(cal.walk("VEVENT"))
    assert len(vevents) == 8

    for component in vevents:
        for field in ("UID", "DTSTAMP", "DTSTART", "DTEND", "SUMMARY"):
            assert component.get(field) is not None, f"缺少必需属性 {field}"

    first = vevents[0]
    assert str(first.get("LOCATION")) == "创新港校区 A-1001"


def test_render_ics_does_not_use_rrule(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """核心设计约束：不使用 RRULE 表达整学期课程。"""
    events = build_events([sample_meeting], calendar, schedules)
    ics = render_ics(events)
    assert "RRULE" not in ics
    assert ics.count("BEGIN:VEVENT") == 8


def test_render_ics_dtstart_round_trip(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    cal = Calendar.from_ical(render_ics(events))
    vevents = sorted(cal.walk("VEVENT"), key=lambda c: c.get("DTSTART").dt)

    start = vevents[0].get("DTSTART").dt
    end = vevents[0].get("DTEND").dt
    assert start == datetime(2026, 9, 11, 8, 0, tzinfo=start.tzinfo)
    assert end == datetime(2026, 9, 11, 9, 50, tzinfo=start.tzinfo)
    assert str(start.tzinfo) == TZ_NAME


def test_render_ics_uses_crlf(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """RFC 5545 要求 CRLF 行结束符。"""
    events = build_events([sample_meeting], calendar, schedules)
    ics = render_ics(events)
    assert "\r\n" in ics
    # 不应出现裸 LF（除 CRLF 之外）
    assert "\n" not in ics.replace("\r\n", "")


def test_render_ics_is_deterministic_except_dtstamp(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """固定 DTSTAMP 时，同样输入必须产出逐字节相同的 ICS。"""
    events = build_events([sample_meeting], calendar, schedules)
    stamp = datetime(2026, 1, 1, 0, 0, tzinfo=None)
    a = render_ics(events, dtstamp=stamp)
    b = render_ics(events, dtstamp=stamp)
    assert a == b


def test_render_ics_uids_stable_across_runs(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """重新生成时必须得到同一组 UID——这是后续能做去重与更新的前提。"""
    first = render_ics(build_events([sample_meeting], calendar, schedules))
    second = render_ics(build_events([sample_meeting], calendar, schedules))

    uids_a = sorted(str(c.get("UID")) for c in Calendar.from_ical(first).walk("VEVENT"))
    uids_b = sorted(str(c.get("UID")) for c in Calendar.from_ical(second).walk("VEVENT"))
    assert uids_a == uids_b
    assert len(set(uids_a)) == 8


def test_render_ics_empty_calendar() -> None:
    ics = render_ics([])
    assert "BEGIN:VCALENDAR" in ics
    assert "BEGIN:VEVENT" not in ics


def test_render_ics_calendar_name(
    sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    ics = render_ics(events, calendar_name="我的课表")
    assert "我的课表" in ics


def test_render_ics_special_characters_escaped() -> None:
    """课程名含逗号/分号时 icalendar 必须做转义，不能破坏 ICS 结构。"""
    meeting = CourseMeeting(
        "ESC", "带,逗号;分号的课程", 3, [1, 2], [1], location="A,B;1"
    )
    cal = AcademicCalendar(
        Semester("t", "测试", WEEK1_MONDAY, total_weeks=16)
    )
    table = ScheduleTable.from_dict(
        {
            "profiles": {"summer": {"name": "夏季", "periods": {"1": ["08:00", "08:50"], "2": ["09:00", "09:50"]}}},
            "periods": [{"start": "2026-09-01", "end": "2026-12-31", "profile": "summer"}],
        }
    )
    ics = render_ics(build_events([meeting], cal, table))
    parsed = Calendar.from_ical(ics)
    component = next(iter(parsed.walk("VEVENT")))
    assert str(component.get("SUMMARY")) == "带,逗号;分号的课程"
    assert str(component.get("LOCATION")) == "A,B;1"


# --------------------------------------------------------------------------- #
# 汇总
# --------------------------------------------------------------------------- #
def test_summarize(sample_meeting: CourseMeeting, calendar: AcademicCalendar, schedules: ScheduleTable) -> None:
    events = build_events([sample_meeting], calendar, schedules)
    info = summarize([sample_meeting], events)

    assert info["courses"] == 1
    assert info["meetings"] == 1
    assert info["events"] == 8
    assert info["date_range"] == "2026-09-11 ~ 2026-10-30"


def test_summarize_empty() -> None:
    info = summarize([], [])
    assert info["courses"] == 0
    assert info["events"] == 0
    assert "无事件" in str(info["date_range"])


# --------------------------------------------------------------------------- #
# 模型不变式
# --------------------------------------------------------------------------- #
def test_course_meeting_rejects_invalid_weekday() -> None:
    with pytest.raises(ValueError):
        CourseMeeting("X", "课程", 0, [1], [1])
    with pytest.raises(ValueError):
        CourseMeeting("X", "课程", 8, [1], [1])


def test_course_meeting_rejects_empty_periods_or_weeks() -> None:
    with pytest.raises(ValueError):
        CourseMeeting("X", "课程", 1, [], [1])
    with pytest.raises(ValueError):
        CourseMeeting("X", "课程", 1, [1], [])


def test_course_meeting_has_no_time_fields() -> None:
    """设计红线：CourseMeeting 不得出现任何具体钟点字段。"""
    fields = set(CourseMeeting.__dataclass_fields__)
    assert "start_time" not in fields
    assert "end_time" not in fields
    assert "start" not in fields
    assert "end" not in fields
    assert {"weekday", "periods", "weeks"} <= fields


def test_full_location_handles_duplicate_campus() -> None:
    m = CourseMeeting("X", "课程", 1, [1], [1], location="创新港", campus="创新港校区")
    assert m.full_location == "创新港校区"


def test_full_location_none_when_empty() -> None:
    m = CourseMeeting("X", "课程", 1, [1], [1])
    assert m.full_location is None
