"""课表快照比对（diff_meetings）单元测试。

身份模型：课程按 stable_course_key 配对；课程内部按（星期 + 节次区间）配对时段，
配对上的时段逐项比较 周次 / 教室 / 教师，配不上的进新增/取消时段。
整门课新增/删除时只报课程级，避免和时段级重复计数。
"""

from __future__ import annotations

from xjtu_calendar.diff import diff_meetings
from xjtu_calendar.models import CourseMeeting


def _m(
    course: str,
    cid: str,
    weekday: int = 5,
    periods: tuple[int, ...] = (1, 2),
    weeks: tuple[int, ...] = (1, 2, 3),
    location: str | None = "A-1001",
    teacher: str | None = "教师甲",
    campus: str | None = None,
) -> CourseMeeting:
    return CourseMeeting(
        cid,
        course,
        weekday,
        list(periods),
        list(weeks),
        teacher=teacher,
        location=location,
        campus=campus,
    )


def test_identical_lists_are_empty_diff() -> None:
    d = diff_meetings([_m("示例课程甲", "D-1")], [_m("示例课程甲", "D-1")])
    assert d.is_empty


def test_whole_course_added_and_removed() -> None:
    d = diff_meetings([_m("示例课程甲", "D-1")], [_m("示例课程乙", "D-2")])
    assert d.added_courses == ["示例课程乙"]
    assert d.removed_courses == ["示例课程甲"]
    # 整门课的新增/删除不再按时段重复报一遍
    assert d.added_slots == [] and d.removed_slots == []


def test_slot_added_within_existing_course() -> None:
    old = [_m("示例课程甲", "D-1", weekday=5)]
    new = [_m("示例课程甲", "D-1", weekday=5), _m("示例课程甲", "D-1", weekday=3, periods=(7, 8))]
    d = diff_meetings(old, new)
    assert [s.weekday for s in d.added_slots] == [3]
    assert d.removed_slots == []


def test_slot_removed_within_existing_course() -> None:
    old = [_m("示例课程甲", "D-1", weekday=5), _m("示例课程甲", "D-1", weekday=3)]
    new = [_m("示例课程甲", "D-1", weekday=5)]
    d = diff_meetings(old, new)
    assert [s.weekday for s in d.removed_slots] == [3]


def test_weeks_change_is_reported() -> None:
    old = [_m("示例课程甲", "D-1", weeks=(1, 2, 3, 4))]
    new = [_m("示例课程甲", "D-1", weeks=(1, 2, 3))]
    d = diff_meetings(old, new)
    assert len(d.changes) == 1
    change = d.changes[0]
    assert (change.field, change.old, change.new) == ("周次", "1-4", "1-3")
    assert change.course_name == "示例课程甲"


def test_location_change_uses_full_location() -> None:
    old = [_m("示例课程甲", "D-1", location="A-1001", campus="创新港校区")]
    new = [_m("示例课程甲", "D-1", location="A-2002", campus="创新港校区")]
    d = diff_meetings(old, new)
    assert [(c.field, c.old, c.new) for c in d.changes] == [
        ("教室", "创新港校区 A-1001", "创新港校区 A-2002")
    ]


def test_teacher_change_reported() -> None:
    old = [_m("示例课程甲", "D-1", teacher="教师甲")]
    new = [_m("示例课程甲", "D-1", teacher="教师乙")]
    d = diff_meetings(old, new)
    assert d.changes[0].field == "教师"


def test_multiple_fields_in_one_slot_each_reported() -> None:
    old = [_m("示例课程甲", "D-1", weeks=(1, 2), location="A-1001")]
    new = [_m("示例课程甲", "D-1", weeks=(1, 2, 3), location="A-2002")]
    d = diff_meetings(old, new)
    assert {c.field for c in d.changes} == {"周次", "教室"}


def test_course_renamed_same_id_reported_as_name_change() -> None:
    """同一 course_id 改名：课程级可见，且不误报成整门新增/删除。"""
    d = diff_meetings([_m("大学英语（2）", "D-1")], [_m("大学英语Ⅱ", "D-1")])
    assert d.added_courses == [] and d.removed_courses == []
    assert any(c.field == "课程名" for c in d.changes)
