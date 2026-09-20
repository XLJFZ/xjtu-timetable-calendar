"""真实结构（脱敏 fixture）的 parser 测试。

fixture：``tests/fixtures/ehall_timetable_real_sanitized.json``
来自 2026-09-20 对 ``POST /jwapp/sys/wdkb/modules/xskcb/xskcb.do`` 真实响应的
脱敏副本：层级、键名、类型、``SKZC`` 周次位掩码均为真实结构，
姓名/学号/教师/教室/课程名均为替换值。

校准基准（2026-09-20 观测）：
- 18 行记录、9 门课程（KCH 去重）、18 个教学班（JXBID 去重）
- 结构化字段：SKXQ(星期) / KSJC+JSJC(节次区间) / SKZC(周次位掩码)
- 同课程多 meeting、同课程换教室、非连续周均真实存在
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xjtu_calendar.parser import TimetableParser

FIXTURES = Path(__file__).parent / "fixtures"
REAL_FIXTURE = FIXTURES / "ehall_timetable_real_sanitized.json"


@pytest.fixture(scope="module")
def real_payload() -> dict:
    return json.loads(REAL_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def real_parsed(real_payload: dict) -> tuple[list, list, TimetableParser]:
    parser = TimetableParser(max_week=18)
    courses, meetings = parser.parse(real_payload)
    return courses, meetings, parser


def test_parse_real_ehall_fixture(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """真实结构应完整解析：0 跳过、课程/会议数与 raw 一致。"""
    courses, meetings, parser = real_parsed
    assert parser.report.parsed == 18
    assert parser.report.skipped == []
    assert len(meetings) == 18
    assert len(courses) == 9  # KCH 去重（与真实观测一致）


def test_real_fixture_multiple_meetings(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """同一门课多个上课时间必须各自成 meeting，不得合并。"""
    _, meetings, _ = real_parsed
    by_course: dict[str, list] = {}
    for m in meetings:
        by_course.setdefault(m.course_id, []).append(m)
    counts = sorted(len(v) for v in by_course.values())
    assert max(counts) >= 3  # 观测中存在一门课占多个时段
    assert sum(counts) == 18


def test_real_fixture_week_mapping(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """周次以 SKZC 位掩码为准：第 i 位为 1 ⇔ 第 i+1 周上课。"""
    _, meetings, _ = real_parsed
    m = next(m for m in meetings if m.raw_week_text == "1-8周"
             and m.periods == [1, 2, 3, 4] and m.weekday == 6)
    assert m.weeks == [1, 2, 3, 4, 5, 6, 7, 8]

    # 非连续周（真实数据中存在 "1-2周,5周" 形态，且有两条不同时段的记录）
    disjoint = [m for m in meetings if m.weeks == [1, 2, 5]]
    assert len(disjoint) == 2
    assert {m.weekday for m in disjoint} == {2, 4}


def test_real_fixture_period_mapping(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """节次来自结构化 KSJC/JSJC，而不是解析 YPSJDD 展示串。"""
    _, meetings, _ = real_parsed
    expected = {(1, 4), (5, 8), (5, 6), (3, 4), (1, 8), (2, 4), (5, 7), (7, 8)}
    got = {(m.periods[0], m.periods[-1]) for m in meetings}
    assert got <= expected
    # 每条 meeting 的节次必须是连续区间
    for m in meetings:
        assert m.periods == list(range(m.periods[0], m.periods[-1] + 1))


def test_real_fixture_location_mapping(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """同课程不同周不同教室：教室信息必须逐 meeting 保留。"""
    _, meetings, _ = real_parsed
    by_course: dict[str, list] = {}
    for m in meetings:
        by_course.setdefault(m.course_id, []).append(m)
    multi_room = [ms for ms in by_course.values()
                  if len({m.location for m in ms}) >= 2]
    assert multi_room, "观测数据中存在同课程多教室，解析后不应丢失"
    for ms in multi_room:
        rooms = {m.location for m in ms}
        assert all(m.location for m in ms)
        assert len(rooms) == len({m.location for m in ms})


def test_real_fixture_empty_optional_fields(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """真实响应里大量 BY1-BY10 为 null：解析不得因此报错或误匹配。"""
    courses, _, _ = real_parsed
    for c in courses:
        assert c.name  # 课程名一定存在
        assert c.category is None  # 真实 xskcb 响应没有类别字段
    # 每条 meeting 的核心事实齐备
    _, meetings, _ = real_parsed
    for m in meetings:
        assert 1 <= m.weekday <= 7
        assert m.periods and m.weeks
        assert m.location


def test_real_fixture_no_time_fields_leak(real_parsed: tuple[list, list, TimetableParser]) -> None:
    """架构红线：即使真实接口携带展示串 YPSJDD，meeting 也不得含钟点字段。"""
    _, meetings, _ = real_parsed
    for m in meetings:
        fields = set(m.__dataclass_fields__)
        assert not fields & {"start_time", "end_time", "start", "end", "datetime"}
