"""调课日（source_date / makeup day）语义测试。

核心语义（依据学校调休通知的普遍含义）：

    「target_date 上，按 source_date 所在教学周 + 星期的课程安排上课。」

必须锁死的行为：

- 课程来源看 source_date 的**教学周 + 星期**，不是 target_date 的自然星期
  （否则单双周 / SKZC 位掩码会被偷换周次）；
- 事件日期是 target_date；钟点按 target_date 当天适用作息解析
  （冬春季切换后补课必须用新作息）；
- target date 自身的原生课程不生成；source date 是假期，也不生成事件；
- UID 稳定；普通展开与调课展开不产生重复事件。
"""

from __future__ import annotations

from datetime import date

import pytest

from xjtu_calendar.academic_calendar import AcademicCalendar
from xjtu_calendar.errors import ParseError
from xjtu_calendar.exporter import build_events
from xjtu_calendar.models import CourseMeeting
from xjtu_calendar.schedules import ScheduleTable

# --------------------------------------------------------------------------- #
# 公共夹具：2026-2027-1 学期语义的等比缩小版
# --------------------------------------------------------------------------- #

SEMESTER_PAYLOAD: dict = {
    "semester": {
        "key": "makeup-test",
        "name": "调课测试学期",
        "first_week_monday": "2026-09-14",
        "total_weeks": 18,
        "start_date": "2026-09-14",
        "end_date": "2027-01-17",
    },
    "excluded_dates": [
        "2026-09-25",
        "2026-09-26",
        "2026-09-27",
        "2026-10-01",
        "2026-10-02",
        "2026-10-03",
        "2026-10-04",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
    ],
    "overrides": {
        # 第 1 周周日 上 第 4 周周二的课
        "2026-09-20": {"source_date": "2026-10-06", "note": "调休补课"},
        # 第 4 周周六 上 第 4 周周三的课
        "2026-10-10": {"source_date": "2026-10-07", "note": "调休补课"},
    },
}

SCHEDULE_PAYLOAD: dict = {
    "profiles": {
        "summer": {
            "name": "夏秋季作息",
            "periods": {
                "1": ["08:00", "08:50"],
                "2": ["09:00", "09:50"],
                "3": ["10:10", "11:00"],
                "4": ["11:10", "12:00"],
                "5": ["14:30", "15:20"],
                "6": ["15:30", "16:20"],
            },
        },
        "winter": {
            "name": "冬春季作息",
            "periods": {
                "1": ["08:00", "08:50"],
                "2": ["09:00", "09:50"],
                "3": ["10:00", "10:50"],
                "4": ["11:00", "11:50"],
                "5": ["14:00", "14:50"],
                "6": ["15:00", "15:50"],
            },
        },
    },
    "periods": [
        {"start": "2026-09-14", "end": "2026-09-30", "profile": "summer"},
        {"start": "2026-10-01", "end": "2027-01-17", "profile": "winter"},
    ],
}


def _make_meetings() -> list[CourseMeeting]:
    """覆盖关键组合：周二/周三 × 单周/双周 + 原生周六/周日课程。"""
    return [
        # 周二、双周（第 4 周）——09-20 应承接它
        CourseMeeting("DEMO-1001", "示例课程甲", 2, [5, 6], [2, 4, 6], teacher="教师甲"),
        # 周二、单周（第 4 周 ∉ weeks）——09-20 不应承接它
        CourseMeeting("DEMO-1002", "示例课程乙", 2, [3, 4], [1, 3, 5], teacher="教师乙"),
        # 周三、双周（第 4 周）——10-10 应承接它
        CourseMeeting("DEMO-1003", "示例课程丙", 3, [5, 6], [2, 4], teacher="教师丙"),
        # 周三、单周（第 4 周 ∉ weeks）——10-10 不应承接它
        CourseMeeting("DEMO-1004", "示例课程丁", 3, [1, 2], [1, 3, 5], teacher="教师丁"),
        # 原生周六课程——10-10 的自身安排，必须停上
        CourseMeeting("DEMO-1005", "示例课程戊", 6, [5, 6], list(range(1, 19)), teacher="教师戊"),
        # 原生周日课程——09-20 的自身安排，必须停上
        CourseMeeting("DEMO-1006", "示例课程己", 7, [1, 2], list(range(1, 19)), teacher="教师己"),
    ]


def _build(payload: dict | None = None) -> tuple[AcademicCalendar, ScheduleTable]:
    calendar = AcademicCalendar.from_dict(payload or SEMESTER_PAYLOAD)
    schedules = ScheduleTable.from_dict(SCHEDULE_PAYLOAD)
    return calendar, schedules


def _events_on(events, day: str):
    return [e for e in events if e.start.date() == date.fromisoformat(day)]


def _dates_of(events, day: str) -> list[str]:
    return sorted(e.start.date().isoformat() for e in _events_on(events, day))


# --------------------------------------------------------------------------- #
# 1. 普通停课
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "day",
    ["2026-09-25", "2026-09-26", "2026-09-27", "2026-10-01", "2026-10-06", "2026-10-07"],
)
def test_holidays_produce_no_events(day: str) -> None:
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    assert _events_on(events, day) == []


# --------------------------------------------------------------------------- #
# 2. 09-20 调课：上第 4 周周二的课
# --------------------------------------------------------------------------- #


def test_0920_makeup_runs_tuesday_week4_courses() -> None:
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    on_day = _events_on(events, "2026-09-20")
    assert [e.summary for e in on_day] == ["示例课程甲"]
    # 事件日期必须是 09-20 本身
    assert _dates_of(events, "2026-09-20") == ["2026-09-20"]


def test_0920_own_sunday_courses_cancelled() -> None:
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    assert all(e.summary != "示例课程己" for e in _events_on(events, "2026-09-20"))


def test_0920_uses_summer_schedule() -> None:
    """09-20 在 10-01 作息切换之前，下午第 5 节是 14:30。"""
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    event = _events_on(events, "2026-09-20")[0]
    assert event.start.hour == 14 and event.start.minute == 30


def test_makeup_description_states_source_semantics() -> None:
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    event = _events_on(events, "2026-09-20")[0]
    assert "调课：本日按第 4 教学周星期二（2026-10-06）的课表上课" in event.description
    # 不能误导为「本周是第 4 教学周」——09-20 实际是第 1 教学周
    assert "本周为本学期第 4 教学周" not in event.description


# --------------------------------------------------------------------------- #
# 3. 10-10 调课：上第 4 周周三的课
# --------------------------------------------------------------------------- #


def test_1010_makeup_runs_wednesday_week4_courses() -> None:
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    on_day = _events_on(events, "2026-10-10")
    assert [e.summary for e in on_day] == ["示例课程丙"]
    assert _dates_of(events, "2026-10-10") == ["2026-10-10"]


def test_1010_own_saturday_courses_cancelled() -> None:
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    assert all(e.summary != "示例课程戊" for e in _events_on(events, "2026-10-10"))


def test_1010_uses_winter_schedule() -> None:
    """作息 10-01 起切冬春季：10-10 补课的下午第 5 节必须是 14:00 而非 14:30。"""
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    event = _events_on(events, "2026-10-10")[0]
    assert event.start.hour == 14 and event.start.minute == 0
    assert event.end.hour == 15 and event.end.minute == 50


# --------------------------------------------------------------------------- #
# 4. 单双周 / SKZC 位掩码按 source 教学周解释
# --------------------------------------------------------------------------- #


def test_week_parity_follows_source_week_not_target_week() -> None:
    """09-20 在第 1 教学周（单周），但来源是第 4 教学周（双周）：
    单周课程不得因 target 周次是单周而混入。"""
    calendar, schedules = _build()
    events = build_events(_make_meetings(), calendar, schedules)
    assert [e.summary for e in _events_on(events, "2026-09-20")] == ["示例课程甲"]
    assert [e.summary for e in _events_on(events, "2026-10-10")] == ["示例课程丙"]


# --------------------------------------------------------------------------- #
# 5. UID 稳定 + 防重复
# --------------------------------------------------------------------------- #


def test_makeup_uids_stable_across_runs() -> None:
    calendar, schedules = _build()
    meetings = _make_meetings()
    first = build_events(meetings, calendar, schedules)
    second = build_events(meetings, calendar, schedules)
    assert [e.uid for e in first] == [e.uid for e in second]
    makeup_uids = {e.uid for e in first if e.start.date() in (date(2026, 9, 20), date(2026, 10, 10))}
    assert len(makeup_uids) == 2  # 两条调课事件都有稳定 UID


def test_no_duplicate_events_on_target_date() -> None:
    """同一门课既有被调走的周六安排、又有被调入的周三安排时，
    target date 只能出现一份事件（普通展开与调课展开共用 UID 去重）。"""
    calendar, schedules = _build()
    meetings = [
        CourseMeeting("DEMO-2001", "示例课程庚", 6, [5, 6], [4]),  # 周六第 4 周：被停
        CourseMeeting("DEMO-2001", "示例课程庚", 3, [5, 6], [4]),  # 周三第 4 周：调入 10-10
    ]
    events = build_events(meetings, calendar, schedules)
    on_day = _events_on(events, "2026-10-10")
    assert len(on_day) == 1
    assert len({e.uid for e in events}) == len(events)


# --------------------------------------------------------------------------- #
# 6. 配置校验（fail-closed）
# --------------------------------------------------------------------------- #


def _payload_with(overrides: dict) -> dict:
    payload = {**SEMESTER_PAYLOAD, "overrides": overrides}
    return payload


def test_source_date_equal_to_day_rejected() -> None:
    with pytest.raises(ParseError, match="不能等于该日期自身"):
        _build(_payload_with({"2026-10-10": {"source_date": "2026-10-10"}}))


def test_source_date_before_semester_rejected() -> None:
    with pytest.raises(ParseError, match="早于第 1 教学周"):
        _build(_payload_with({"2026-10-10": {"source_date": "2026-08-01"}}))


def test_source_date_beyond_total_weeks_rejected() -> None:
    with pytest.raises(ParseError, match="超出学期总周数"):
        _build(_payload_with({"2026-10-10": {"source_date": "2027-02-10"}}))


def test_makeup_target_in_excluded_dates_rejected() -> None:
    payload = _payload_with({"2026-10-02": {"source_date": "2026-10-06"}})
    with pytest.raises(ParseError, match="只能保留其一"):
        _build(payload)


def test_stale_unsupported_declaration_rejected() -> None:
    """调课已被 overrides 表达后，残留的 unsupported_adjustments 声明必须报错，
    否则导出阶段会对同一日期同时「已生成」和「声称缺失」。"""
    payload = {
        **SEMESTER_PAYLOAD,
        "unsupported_adjustments": [
            {"date": "2026-10-10", "description": "过时声明：按 10-07 课表上课"}
        ],
    }
    with pytest.raises(ParseError, match=r"过时声明|unsupported_adjustments"):
        _build(payload)


def test_cancel_only_override_unaffected() -> None:
    """回归：不带 source_date 的 cancel 覆盖行为不变。"""
    payload = _payload_with({"2026-09-16": {"cancel": True, "note": "临时停课"}})
    calendar, schedules = _build(payload)
    meetings = [CourseMeeting("DEMO-3001", "示例课程辛", 3, [1, 2], [1])]
    events = build_events(meetings, calendar, schedules)
    assert _events_on(events, "2026-09-16") == []
