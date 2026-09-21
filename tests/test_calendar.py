"""教学日历与作息解析器测试（Phase 3 + Phase 4）。

覆盖：
- 教学周 -> 日期换算（含星期一/星期日边界）
- 冬/夏季作息区间解析
- 节次 -> 实际时间
- 停课 / 调课覆盖规则
- 未配置时的明确报错（不猜时间）
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from xjtu_calendar.academic_calendar import AcademicCalendar, DateOverride
from xjtu_calendar.errors import ParseError, ScheduleNotConfigured
from xjtu_calendar.models import Semester
from xjtu_calendar.schedules import ScheduleTable
from xjtu_calendar.timeutil import TZ_NAME

# --------------------------------------------------------------------------- #
# 固件：刻意使用「第 1 周周一 = 2026-09-07」这类构造数据，
# 它是测试用的**假设**，不代表西安交大真实校历。
# --------------------------------------------------------------------------- #
WEEK1_MONDAY = date(2026, 9, 7)


def make_semester(**kwargs: object) -> Semester:
    defaults: dict[str, object] = {
        "key": "test-fall",
        "name": "测试学期",
        "first_week_monday": WEEK1_MONDAY,
        "total_weeks": 16,
    }
    defaults.update(kwargs)
    return Semester(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def calendar() -> AcademicCalendar:
    return AcademicCalendar(make_semester())


@pytest.fixture
def schedules() -> ScheduleTable:
    """两套作息，用于验证「同一节次在不同日期得到不同钟点」。"""
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


# --------------------------------------------------------------------------- #
# 日期换算
# --------------------------------------------------------------------------- #
def test_week_to_date_first_week_monday(calendar: AcademicCalendar) -> None:
    assert calendar.week_to_date(1, 1) == date(2026, 9, 7)


def test_week_to_date_sunday_is_day_seven(calendar: AcademicCalendar) -> None:
    assert calendar.week_to_date(1, 7) == date(2026, 9, 13)


@pytest.mark.parametrize(
    ("week", "weekday", "expected"),
    [
        (1, 1, date(2026, 9, 7)),   # 第 1 周周一
        (1, 5, date(2026, 9, 11)),  # 第 1 周周五
        (2, 5, date(2026, 9, 18)),  # 第 2 周周五
        (3, 2, date(2026, 9, 22)),  # 第 3 周周二
        (8, 3, date(2026, 10, 28)), # 第 8 周周三
        (16, 6, date(2026, 12, 26)),# 第 16 周周六（12/21 周一 + 5 天）
    ],
)
def test_week_to_date_table(
    calendar: AcademicCalendar, week: int, weekday: int, expected: date
) -> None:
    assert calendar.week_to_date(week, weekday) == expected


def test_week_to_date_rejects_bad_input(calendar: AcademicCalendar) -> None:
    with pytest.raises(ValueError):
        calendar.week_to_date(0, 1)
    with pytest.raises(ValueError):
        calendar.week_to_date(1, 0)
    with pytest.raises(ValueError):
        calendar.week_to_date(1, 8)


def test_semester_requires_monday_anchor() -> None:
    """第 1 教学周锚点必须是星期一，否则后续所有换算都会系统性错位。"""
    with pytest.raises(ValueError, match="星期一"):
        Semester("t", "测试", date(2026, 9, 8))


def test_last_week_sunday(calendar: AcademicCalendar) -> None:
    assert calendar.semester.last_week_sunday == date(2026, 12, 27)


def test_date_to_week_round_trip(calendar: AcademicCalendar) -> None:
    for week in (1, 5, 16):
        for weekday in (1, 4, 7):
            day = calendar.week_to_date(week, weekday)
            assert calendar.date_to_week(day) == week


# --------------------------------------------------------------------------- #
# 作息解析
# --------------------------------------------------------------------------- #
def test_resolve_schedule_profile_summer_and_winter(
    calendar: AcademicCalendar, schedules: ScheduleTable
) -> None:
    """夏季日期 -> summer，冬季日期 -> winter。"""
    assert schedules.resolve_schedule_profile(date(2026, 9, 7)).key == "summer"
    assert schedules.resolve_schedule_profile(date(2026, 10, 11)).key == "summer"
    assert schedules.resolve_schedule_profile(date(2026, 10, 12)).key == "winter"
    assert schedules.resolve_schedule_profile(date(2026, 12, 1)).key == "winter"


def test_resolve_schedule_profile_unknown_date_raises(schedules: ScheduleTable) -> None:
    """未覆盖的日期必须明确报错，绝不回退到某个默认作息。"""
    with pytest.raises(ScheduleNotConfigured):
        schedules.resolve_schedule_profile(date(2030, 1, 1))


def test_resolve_period_time_summer(schedules: ScheduleTable) -> None:
    start, end = schedules.resolve_period_time(date(2026, 9, 8), [1, 2])
    assert (start.hour, start.minute) == (8, 0)
    assert (end.hour, end.minute) == (9, 50)


def test_resolve_period_time_winter(schedules: ScheduleTable) -> None:
    start, end = schedules.resolve_period_time(date(2026, 10, 13), [1, 2])
    assert (start.hour, start.minute) == (8, 30)
    assert (end.hour, end.minute) == (10, 20)


def test_same_period_different_clock_across_seasons(schedules: ScheduleTable) -> None:
    """核心设计验证：同样的 1-2 节，随季节得到不同钟点。"""
    summer_start, _ = schedules.resolve_period_time(date(2026, 9, 8), [1, 2])
    winter_start, _ = schedules.resolve_period_time(date(2026, 10, 13), [1, 2])
    assert summer_start != winter_start
    assert summer_start.hour == 8 and winter_start.hour == 8
    assert summer_start.minute == 0 and winter_start.minute == 30


def test_resolve_period_time_spans_gap_between_periods(schedules: ScheduleTable) -> None:
    """连排节次取首节开始、末节结束，中间的课间包含在内。"""
    start, end = schedules.resolve_period_time(date(2026, 9, 8), [1, 2])
    assert start == datetime(2026, 9, 8, 8, 0, tzinfo=start.tzinfo)
    assert end == datetime(2026, 9, 8, 9, 50, tzinfo=start.tzinfo)


def test_resolve_period_time_ignores_order_and_duplicates(schedules: ScheduleTable) -> None:
    a = schedules.resolve_period_time(date(2026, 9, 8), [2, 1])
    b = schedules.resolve_period_time(date(2026, 9, 8), [1, 1, 2])
    assert a == b


def test_resolve_period_time_missing_period_raises(schedules: ScheduleTable) -> None:
    with pytest.raises(ScheduleNotConfigured, match="未定义节次"):
        schedules.resolve_period_time(date(2026, 9, 8), [1, 9])


def test_resolve_period_time_empty_raises(schedules: ScheduleTable) -> None:
    with pytest.raises(ValueError):
        schedules.resolve_period_time(date(2026, 9, 8), [])


# --------------------------------------------------------------------------- #
# 时区
# --------------------------------------------------------------------------- #
def test_resolved_time_is_timezone_aware_xian(schedules: ScheduleTable) -> None:
    """事件时间必须带 Asia/Shanghai 时区，不受运行机器本地时区影响。"""
    start, end = schedules.resolve_period_time(date(2026, 9, 8), [1, 2])
    assert start.tzinfo is not None
    assert end.tzinfo is not None
    assert str(start.tzinfo) == TZ_NAME
    assert start.utcoffset() is not None
    assert start.utcoffset().total_seconds() == 8 * 3600


# --------------------------------------------------------------------------- #
# 配置校验与错误处理
# --------------------------------------------------------------------------- #
def test_validate_reports_overlapping_periods(schedules: ScheduleTable) -> None:
    assert schedules.validate() == []

    overlapping = ScheduleTable.from_dict(
        {
            "profiles": {"summer": {"name": "夏季", "periods": {"1": ["08:00", "08:50"]}}},
            "periods": [
                {"start": "2026-09-07", "end": "2026-10-11", "profile": "summer"},
                {"start": "2026-10-01", "end": "2026-10-20", "profile": "summer"},
            ],
        }
    )
    problems = overlapping.validate()
    assert any("重叠" in p for p in problems)


def test_validate_reports_undefined_profile() -> None:
    table = ScheduleTable.from_dict(
        {
            "profiles": {},
            "periods": [{"start": "2026-09-07", "end": "2026-10-11", "profile": "ghost"}],
        }
    )
    problems = table.validate()
    assert any("未定义" in p for p in problems)


def test_schedule_rejects_bad_time_format() -> None:
    with pytest.raises(ValueError):
        ScheduleTable.from_dict(
            {
                "profiles": {"summer": {"name": "夏季", "periods": {"1": ["8点", "09:00"]}}},
                "periods": [],
            }
        )


def test_schedule_accepts_dict_style_period_entry() -> None:
    table = ScheduleTable.from_dict(
        {
            "profiles": {
                "summer": {
                    "name": "夏季",
                    "periods": {"1": {"start": "08:00", "end": "08:50"}},
                }
            },
            "periods": [{"start": "2026-09-07", "end": "2026-10-11", "profile": "summer"}],
        }
    )
    assert table.profiles["summer"].time_of(1) == ("08:00", "08:50")


def test_schedule_rejects_reversed_interval() -> None:
    with pytest.raises(ValueError):
        ScheduleTable.from_dict(
            {
                "profiles": {"summer": {"name": "夏季", "periods": {"1": ["08:00", "08:50"]}}},
                "periods": [{"start": "2026-10-11", "end": "2026-09-07", "profile": "summer"}],
            }
        )


# --------------------------------------------------------------------------- #
# 停课 / 调课覆盖
# --------------------------------------------------------------------------- #
def test_is_excluded_by_date_set(calendar: AcademicCalendar) -> None:
    calendar.excluded_dates.add(date(2026, 10, 1))
    assert calendar.is_excluded(date(2026, 10, 1)) is True
    assert calendar.is_excluded(date(2026, 10, 2)) is False


def test_is_excluded_by_cancel_override(calendar: AcademicCalendar) -> None:
    day = date(2026, 10, 10)
    calendar.overrides[day] = DateOverride(day=day, cancel=True)
    assert calendar.is_excluded(day) is True


# --------------------------------------------------------------------------- #
# 从字典构造
# --------------------------------------------------------------------------- #
def test_academic_calendar_from_dict_round_trip() -> None:
    payload = {
        "semester": {
            "key": "2026-fall",
            "name": "2026-2027 学年秋季学期",
            "first_week_monday": "2026-09-07",
            "total_weeks": 16,
            "start_date": "2026-09-07",
            "end_date": "2026-12-27",
        },
        "excluded_dates": ["2026-10-01", "2026-10-02"],
        "overrides": {
            "2026-10-10": {"note": "国庆调休补课", "location": "A-1001"},
            "2026-11-15": {"cancel": True},
        },
    }
    cal = AcademicCalendar.from_dict(payload)

    assert cal.semester.key == "2026-fall"
    assert cal.semester.first_week_monday == date(2026, 9, 7)
    assert cal.semester.total_weeks == 16
    assert date(2026, 10, 1) in cal.excluded_dates
    assert cal.is_excluded(date(2026, 10, 2)) is True
    assert cal.is_excluded(date(2026, 11, 15)) is True

    oct10 = cal.override_for(date(2026, 10, 10))
    assert oct10 is not None
    assert oct10.note == "国庆调休补课"
    assert oct10.location == "A-1001"
    assert oct10.cancel is False


def test_academic_calendar_total_weeks_omitted_is_none() -> None:
    """total_weeks 可省略：省略 = 不做周次上限过滤。

    没有官方校历依据时宁可留空 —— 填一个猜来的数字会让合法周次被判越界。
    """
    cal = AcademicCalendar.from_dict(
        {"semester": {"key": "x", "first_week_monday": "2026-09-07"}}
    )
    assert cal.semester.total_weeks is None


def test_academic_calendar_total_weeks_empty_string_is_none() -> None:
    cal = AcademicCalendar.from_dict(
        {"semester": {"key": "x", "first_week_monday": "2026-09-07", "total_weeks": ""}}
    )
    assert cal.semester.total_weeks is None


def test_academic_calendar_bad_total_weeks_raises() -> None:
    with pytest.raises(ParseError, match="total_weeks"):
        AcademicCalendar.from_dict(
            {"semester": {"key": "x", "first_week_monday": "2026-09-07",
                          "total_weeks": "十六"}}
        )


def test_semester_last_week_sunday_requires_total_weeks() -> None:
    """total_weeks 留空时学期末日无定义，必须明确报错而不是给个默认值。"""
    semester = Semester("x", "测试", WEEK1_MONDAY, total_weeks=None)
    with pytest.raises(ValueError, match="total_weeks"):
        _ = semester.last_week_sunday


def test_academic_calendar_missing_semester_raises() -> None:
    with pytest.raises(ParseError, match="semester"):
        AcademicCalendar.from_dict({})


# --------------------------------------------------------------------------- #
# unsupported_adjustments：显式声明「已知但无法表达」的调课
# --------------------------------------------------------------------------- #
def test_unsupported_adjustments_omitted_is_empty() -> None:
    cal = AcademicCalendar.from_dict(
        {"semester": {"key": "x", "first_week_monday": "2026-09-07"}}
    )
    assert cal.unsupported_adjustments == ()


def test_unsupported_adjustments_parsed() -> None:
    cal = AcademicCalendar.from_dict(
        {
            "semester": {"key": "x", "first_week_monday": "2026-09-07"},
            "unsupported_adjustments": [
                {"date": "2026-09-20", "description": "按 10-06（第 4 周周二）课表上课"},
                {"date": "2026-10-10", "description": "按 10-07（第 4 周周三）课表上课"},
            ],
        }
    )
    assert [a.date for a in cal.unsupported_adjustments] == [
        date(2026, 9, 20),
        date(2026, 10, 10),
    ]
    assert "10-06" in cal.unsupported_adjustments[0].description


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-list",                                   # 非数组
        [{"description": "没有日期"}],                    # 缺 date
        [{"date": "2026-09-20"}],                       # 缺 description
        [{"date": "2026-09-20", "description": "  "}],  # 空白 description
    ],
)
def test_unsupported_adjustments_bad_shape_raises(raw: object) -> None:
    """没有说明的「无法表达」只会让人困惑 —— description 必填。"""
    with pytest.raises(ParseError):
        AcademicCalendar.from_dict(
            {
                "semester": {"key": "x", "first_week_monday": "2026-09-07"},
                "unsupported_adjustments": raw,
            }
        )


def test_academic_calendar_bad_date_raises() -> None:
    with pytest.raises(ParseError):
        AcademicCalendar.from_dict(
            {
                "semester": {
                    "key": "x",
                    "first_week_monday": "not-a-date",
                    "total_weeks": 16,
                }
            }
        )
