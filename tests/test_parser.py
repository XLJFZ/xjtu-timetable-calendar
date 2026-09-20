"""Parser 测试（Phase 2 的字段映射层）。

所有固件均为**手工构造的脱敏数据**，不含任何真实学生信息。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xjtu_calendar.errors import ParseError
from xjtu_calendar.parser import (
    TimetableParser,
    parse_weekday,
    resolve_campus,
)

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_payload() -> dict:
    return json.loads((FIXTURES / "timetable_sample.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 星期解析
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1, 1), (5, 5), (7, 7),
        ("1", 1), ("5", 5), ("7", 7),
        ("星期一", 1), ("星期五", 5), ("星期日", 7), ("星期天", 7),
        ("周一", 1), ("周五", 5), ("周日", 7), ("周天", 7),
        ("礼拜一", 1), ("礼拜天", 7),
        ("一", 1), ("五", 5), ("日", 7), ("天", 7),
        ("Mon", 1), ("Friday", 5), ("fri", 5), ("Sunday", 7),
        (" friday ", 5),
        ("星期五下午", 5),
    ],
)
def test_parse_weekday(value: object, expected: int) -> None:
    assert parse_weekday(value) == expected


@pytest.mark.parametrize("bad", [None, "", "  ", 0, 8, 99, "星期八", "abc", True, False])
def test_parse_weekday_rejects_invalid(bad: object) -> None:
    assert parse_weekday(bad) is None


# --------------------------------------------------------------------------- #
# 整体解析
# --------------------------------------------------------------------------- #
def test_parse_returns_courses_and_meetings(sample_payload: dict) -> None:
    courses, meetings = TimetableParser(max_week=16).parse(sample_payload)

    assert len(courses) == 4
    assert len(meetings) == 4
    assert [c.name for c in courses] == [
        "示例课程甲",
        "示例课程乙",
        "示例课程丙",
        "示例课程丁",
    ]


def test_parse_course_metadata(sample_payload: dict) -> None:
    courses, _ = TimetableParser(max_week=16).parse(sample_payload)
    first = next(c for c in courses if c.course_id == "DEMO-1001")

    assert first.name == "示例课程甲"
    assert first.teacher == "示例教师甲"
    assert first.credits == 3.0


def test_parse_meeting_facts(sample_payload: dict) -> None:
    """核心：CourseMeeting 只保留星期 + 节次 + 周次，不含钟点。"""
    _, meetings = TimetableParser(max_week=16).parse(sample_payload)
    first = next(m for m in meetings if m.course_id == "DEMO-1001")

    assert first.weekday == 5
    assert first.periods == [1, 2]
    assert first.weeks == [1, 2, 3, 4, 5, 6, 7, 8]
    assert first.location == "A-1001"
    assert first.campus == "创新港校区"
    assert first.full_location == "创新港校区 A-1001"
    assert first.raw_period_text == "1-2节"
    assert first.raw_week_text == "1-8周"


def test_parse_discrete_weeks(sample_payload: dict) -> None:
    """``2,5-8周`` -> ``[2,5,6,7,8]``（用户点名的用例）。"""
    _, meetings = TimetableParser(max_week=16).parse(sample_payload)
    discrete = next(m for m in meetings if m.course_id == "DEMO-1003")
    assert discrete.weeks == [2, 5, 6, 7, 8]


def test_parse_odd_weeks(sample_payload: dict) -> None:
    _, meetings = TimetableParser(max_week=16).parse(sample_payload)
    odd = next(m for m in meetings if m.course_id == "DEMO-1004")
    assert odd.weeks == [1, 3, 5, 7, 9, 11, 13, 15]


def test_parse_report_is_clean(sample_payload: dict) -> None:
    parser = TimetableParser(max_week=16)
    parser.parse(sample_payload)
    assert parser.report.parsed == 4
    assert parser.report.skipped == []


# --------------------------------------------------------------------------- #
# 字段名变体
# --------------------------------------------------------------------------- #
def test_parser_handles_alternative_field_names() -> None:
    """候选键名机制：真实键（KCM/SKXQ/SKZC…）优先，拼音变体兼容。

    .. note::
        2026-09-20 校准说明：原测试把 ``xm`` 当教师字段——这在西交大
        真实响应里是**学生姓名**（教师是 ``SKJS``），属于会向事件里
        写入学生姓名的错误映射，已删除。
    """
    payload = {
        "kbList": [
            {
                "kch": "K001",
                "kcmc": "示例课程甲",
                "teacher": "教师A",
                "cdmc": "2-101",
                "xqj": "3",
                "jcs": "1-2节",
                "zcd": "1-8周",
            }
        ]
    }
    _courses, meetings = TimetableParser(max_week=16).parse(payload)
    assert len(meetings) == 1
    meeting = meetings[0]
    assert meeting.course_name == "示例课程甲"
    assert meeting.course_id == "K001"
    assert meeting.teacher == "教师A"
    assert meeting.location == "2-101"
    assert meeting.weekday == 3
    assert meeting.periods == [1, 2]
    assert meeting.weeks == [1, 2, 3, 4, 5, 6, 7, 8]
    # 未提供 campus 字段时应从地点推断或留空
    assert meeting.campus is None


def test_parser_prefers_real_xjtu_keys() -> None:
    """真实西交大键名（2026-09-20 观测）必须被直接识别。"""
    payload = [
        {
            "KCM": "示例课程甲",
            "KCH": "TEST400001",
            "SKJS": "教师A",
            "JASMC": "A-0001",
            "SKXQ": "6",
            "KSJC": "1",
            "JSJC": "4",
            "ZCMC": "1-8周",
            "SKZC": "1111111100000000",
            "XNXQDM": "2026-2027-1",
            "XXXQDM_DISPLAY": "创新港校区",
        }
    ]
    courses, meetings = TimetableParser(max_week=18).parse(payload)
    assert len(courses) == 1 and len(meetings) == 1
    m = meetings[0]
    assert m.course_id == "TEST400001"
    assert m.weekday == 6
    assert m.periods == [1, 2, 3, 4]
    assert m.weeks == [1, 2, 3, 4, 5, 6, 7, 8]
    assert m.campus == "创新港校区"


def test_parser_week_mask_wins_over_display_text() -> None:
    """SKZC 位掩码与 ZCMC 展示串同时存在时，以结构化掩码为准。

    展示串可能被学校简化（例如把「3-16 双」写成「3-16周」），
    掩码才是教务系统内部的权威表达。
    """
    payload = [
        {
            "courseName": "课程A",
            "weekday": 1,
            "KSJC": "1",
            "JSJC": "2",
            "ZCMC": "1-4周",
            "SKZC": "1010101000000000",
        }
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)
    assert meetings[0].weeks == [1, 3, 5, 7]
    assert meetings[0].raw_week_text == "1-4周"  # 原始展示串仍被保留


def test_parser_handles_bare_list_payload() -> None:
    payload = [
        {
            "courseName": "课程A",
            "weekday": 1,
            "periods": "1-2节",
            "weeks": "1-4周",
        }
    ]
    courses, meetings = TimetableParser(max_week=16).parse(payload)
    assert len(courses) == 1
    assert len(meetings) == 1


def test_parser_handles_numeric_periods_and_weeks() -> None:
    """字段直接是数字/列表时的处理。"""
    payload = [
        {
            "courseName": "课程B",
            "weekday": "2",
            "periods": [1, 2],
            "weeks": "1-3周",
        }
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)
    assert meetings[0].periods == [1, 2]
    assert meetings[0].weeks == [1, 2, 3]


def test_parser_handles_dict_style_period_field() -> None:
    payload = [
        {
            "courseName": "课程C",
            "weekday": 4,
            "periods": {"period": "5-6"},
            "weeks": {"week": "1-2"},
        }
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)
    assert meetings[0].periods == [5, 6]
    assert meetings[0].weeks == [1, 2]


# --------------------------------------------------------------------------- #
# 容错
# --------------------------------------------------------------------------- #
def test_parser_skips_record_without_name() -> None:
    payload = [{"weekday": 1, "periods": "1-2节", "weeks": "1-4周"}]
    parser = TimetableParser(max_week=16)
    _, meetings = parser.parse(payload)
    assert meetings == []
    assert any("缺少名称" in s for s in parser.report.skipped)


def test_parser_skips_record_with_bad_weekday() -> None:
    payload = [{"courseName": "课程", "weekday": "星期八", "periods": "1-2节", "weeks": "1-4周"}]
    parser = TimetableParser(max_week=16)
    _, meetings = parser.parse(payload)
    assert meetings == []
    assert any("星期" in s for s in parser.report.skipped)


def test_parser_skips_record_with_bad_weeks() -> None:
    payload = [{"courseName": "课程", "weekday": 1, "periods": "1-2节", "weeks": "待定"}]
    parser = TimetableParser(max_week=16)
    _, meetings = parser.parse(payload)
    assert meetings == []
    assert any("待定" in s or "解析" in s for s in parser.report.skipped)


def test_parser_skips_record_with_bad_periods() -> None:
    payload = [{"courseName": "课程", "weekday": 1, "periods": "待定", "weeks": "1-4周"}]
    parser = TimetableParser(max_week=16)
    _, meetings = parser.parse(payload)
    assert meetings == []


def test_parser_partial_success_keeps_valid_records() -> None:
    """一条坏数据不应导致整批失败。"""
    payload = [
        {"courseName": "好课程", "weekday": 1, "periods": "1-2节", "weeks": "1-4周"},
        {"courseName": "坏课程", "weekday": "?", "periods": "1-2节", "weeks": "1-4周"},
    ]
    parser = TimetableParser(max_week=16)
    _, meetings = parser.parse(payload)
    assert len(meetings) == 1
    assert meetings[0].course_name == "好课程"


def test_parser_raises_on_empty_payload() -> None:
    with pytest.raises(ParseError):
        TimetableParser().parse(None)


def test_parser_raises_when_no_list_found() -> None:
    with pytest.raises(ParseError, match="无法在课表响应中定位课程列表"):
        TimetableParser().parse({"foo": 1, "bar": "baz"})


def test_parser_rejects_scalar_payload() -> None:
    with pytest.raises(ParseError):
        TimetableParser().parse("not json structure")


def test_parser_clips_weeks_to_semester_length() -> None:
    payload = [{"courseName": "长课程", "weekday": 1, "periods": "1-2节", "weeks": "1-20周"}]
    _, meetings = TimetableParser(max_week=16).parse(payload)
    assert meetings[0].weeks == list(range(1, 17))


# --------------------------------------------------------------------------- #
# 校区推断
# --------------------------------------------------------------------------- #
def test_resolve_campus_prefers_explicit() -> None:
    assert resolve_campus("雁塔校区", "创新港 A-1001") == "雁塔校区"


def test_resolve_campus_infers_from_location() -> None:
    assert resolve_campus(None, "创新港 A-1001") == "创新港校区"
    assert resolve_campus(None, "兴庆 A-1002") == "兴庆校区"


def test_resolve_campus_falls_back() -> None:
    result = resolve_campus(None, "某个未知地点")
    assert result == "未知校区"


def test_parser_does_not_leak_personal_data_structure() -> None:
    """解析结果不得包含原始记录里的敏感字段（如学号 / 姓名）。"""
    payload = [
        {
            "courseName": "课程",
            "weekday": 1,
            "periods": "1-2节",
            "weeks": "1-4周",
            "xh": "0000000000",
            "xm": "某学生",
        }
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)
    # CourseMeeting 是冻结的 dataclass，字段集固定，不携带原始记录
    fields = set(meetings[0].__dataclass_fields__)
    assert "xh" not in fields
    assert "xm" not in fields
    assert not hasattr(meetings[0], "xh")


# --------------------------------------------------------------------------- #
# 真实课表中常见的多段结构
# --------------------------------------------------------------------------- #
def test_parser_keeps_multiple_meetings_of_same_course() -> None:
    """同一门课一周多次（如理论课 + 习题课）必须各生成一条 meeting。"""
    payload = [
        {"courseName": "数据结构", "weekday": 1, "periods": "1-2节", "weeks": "1-16周"},
        {"courseName": "数据结构", "weekday": 3, "periods": "3-4节", "weeks": "1-16周"},
    ]
    courses, meetings = TimetableParser(max_week=16).parse(payload)

    assert len(courses) == 1  # 元信息去重，不因出现两次而重复建课
    assert len(meetings) == 2
    assert {m.weekday for m in meetings} == {1, 3}


def test_parser_same_course_different_room_by_weeks() -> None:
    """同一门课不同周在不同教室：每条 meeting 保留自己的周次与地点。

    这是「每段独立记录」设计的关键场景——如果实现按课程名合并，
    教室切换的信息会被静默丢失。
    """
    payload = [
        {"courseName": "建筑设计", "weekday": 2, "periods": "1-4节",
         "weeks": "1-8周", "location": "东楼 A301"},
        {"courseName": "建筑设计", "weekday": 2, "periods": "1-4节",
         "weeks": "9-16周", "location": "东楼 B502"},
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)

    assert len(meetings) == 2
    by_room = {m.location: m for m in meetings}
    assert by_room["东楼 A301"].weeks == list(range(1, 9))
    assert by_room["东楼 B502"].weeks == list(range(9, 17))


def test_parser_single_double_week_parity_meetings() -> None:
    """单双周交替（如体育课）：单周上 A 场地、双周上 B 场地，两条记录各自独立。"""
    payload = [
        {"courseName": "体育", "weekday": 4, "periods": "5-6节",
         "weeks": "单周", "location": "田径场"},
        {"courseName": "体育", "weekday": 4, "periods": "5-6节",
         "weeks": "双周", "location": "体育馆"},
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)

    assert len(meetings) == 2
    weeks_by_room = {m.location: m.weeks for m in meetings}
    assert all(w % 2 == 1 for w in weeks_by_room["田径场"])
    assert all(w % 2 == 0 for w in weeks_by_room["体育馆"])


def test_parser_period_range_and_disjoint_weeks() -> None:
    """节次区间与非连续周次并存（如 5-6 节、第 1,3,5,7 周）。"""
    payload = [
        {"courseName": "讲座", "weekday": 5, "periods": "5-6节", "weeks": "1,3,5,7周"}
    ]
    _, meetings = TimetableParser(max_week=16).parse(payload)

    assert meetings[0].periods == [5, 6]
    assert meetings[0].weeks == [1, 3, 5, 7]
    # 冻结模型：meeting 上不存在钟点字段，钟点由 ScheduleTable 在导出时解析
    fields = set(meetings[0].__dataclass_fields__)
    assert not fields & {"start_time", "end_time", "start", "end", "datetime"}
