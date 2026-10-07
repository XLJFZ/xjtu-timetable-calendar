"""新旧课表快照比对（调课检测）。

比对口径
--------
- **课程级**：按 ``stable_course_key``（course_id，缺失时退回 ``name:课程名``）配对；
  只在一侧出现 -> 整门新增 / 删除，不再按时段重复报。
- **时段级**：同一课程内按（星期 + 节次区间）配对；配不上的进新增/取消上课时段。
- **字段级**：配对成功的时段逐项比较 周次 / 教室 / 教师；同一 key 出现多个时段
  （分组课常见）按属性排序后逐位配对，数量变化多出的部分报新增/取消。
- **课程改名**：同一 course_id 改名按时段变化报（``field="课程名"``），
  提醒这正是 UID 已知边界会当成新事件的情形。

本模块只做纯数据比对，不读写文件、不发网络；展示格式化由 CLI 负责
（:func:`describe_slot` / :func:`describe_periods` 为共用出口，避免 CLI 里再造一套）。
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field

from .models import CourseMeeting
from .weeks import format_weeks

__all__ = [
    "CourseSlot",
    "SlotChange",
    "TimetableDiff",
    "describe_periods",
    "describe_slot",
    "diff_meetings",
]

_WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


@dataclass(frozen=True)
class CourseSlot:
    """一条可比对、可展示的上课时段。"""

    course_key: str
    course_name: str
    weekday: int
    periods: tuple[int, ...]
    weeks: tuple[int, ...]
    location: str | None
    teacher: str | None


@dataclass(frozen=True)
class SlotChange:
    """配对时段上的一个字段变化。"""

    course_name: str
    weekday: int
    periods: tuple[int, ...]
    field: str
    old: str
    new: str


@dataclass
class TimetableDiff:
    added_courses: list[str] = field(default_factory=list)
    removed_courses: list[str] = field(default_factory=list)
    added_slots: list[CourseSlot] = field(default_factory=list)
    removed_slots: list[CourseSlot] = field(default_factory=list)
    changes: list[SlotChange] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (
            self.added_courses
            or self.removed_courses
            or self.added_slots
            or self.removed_slots
            or self.changes
        )


def describe_periods(periods: Sequence[int]) -> str:
    """``(1, 2)`` -> ``"第1-2节"``；非连续 -> ``"第1,2,5节"``。"""
    ordered = tuple(periods)
    if not ordered:
        return "第?节"
    if all(b - a == 1 for a, b in itertools.pairwise(ordered)):
        return f"第{ordered[0]}-{ordered[-1]}节" if len(ordered) > 1 else f"第{ordered[0]}节"
    return "第" + ",".join(str(p) for p in ordered) + "节"


def describe_slot(slot: CourseSlot) -> str:
    """一行人类可读的时段描述（CLI 展示用）。"""
    location = slot.location or "地点未知"
    teacher = f"，{slot.teacher}" if slot.teacher else ""
    return (
        f"{slot.course_name} 星期{_WEEKDAY_NAMES[slot.weekday - 1]} "
        f"{describe_periods(slot.periods)}（周次 {format_weeks(list(slot.weeks))}，{location}{teacher}）"
    )


def _to_slot(meeting: CourseMeeting) -> CourseSlot:
    return CourseSlot(
        course_key=meeting.stable_course_key,
        course_name=meeting.course_name.strip(),
        weekday=meeting.weekday,
        periods=tuple(sorted(set(meeting.periods))),
        weeks=tuple(sorted(set(meeting.weeks))),
        location=meeting.full_location,
        teacher=meeting.teacher,
    )


def _slot_sort_key(slot: CourseSlot) -> tuple[tuple[int, ...], str, str]:
    return (slot.weeks, slot.location or "", slot.teacher or "")


def _group(meetings: Sequence[CourseMeeting]) -> dict[str, list[CourseSlot]]:
    groups: dict[str, list[CourseSlot]] = {}
    for meeting in meetings:
        slot = _to_slot(meeting)
        groups.setdefault(slot.course_key, []).append(slot)
    for slots in groups.values():
        slots.sort(key=_slot_sort_key)
    return groups


def _field_changes(old: CourseSlot, new: CourseSlot) -> list[SlotChange]:
    pairs = (
        ("周次", format_weeks(list(old.weeks)), format_weeks(list(new.weeks))),
        ("教室", old.location or "地点未知", new.location or "地点未知"),
        ("教师", old.teacher or "未署名", new.teacher or "未署名"),
    )
    return [
        SlotChange(
            course_name=new.course_name,
            weekday=new.weekday,
            periods=new.periods,
            field=label,
            old=before,
            new=after,
        )
        for label, before, after in pairs
        if before != after
    ]


def diff_meetings(
    old_meetings: Sequence[CourseMeeting], new_meetings: Sequence[CourseMeeting]
) -> TimetableDiff:
    """比对两份解析好的课程安排列表（旧 -> 新）。"""
    old_groups = _group(old_meetings)
    new_groups = _group(new_meetings)
    diff = TimetableDiff()

    for key in sorted(set(old_groups) | set(new_groups)):
        old_slots = old_groups.get(key)
        new_slots = new_groups.get(key)

        if old_slots is None and new_slots is not None:
            diff.added_courses.append(new_slots[0].course_name)
            continue
        if new_slots is None and old_slots is not None:
            diff.removed_courses.append(old_slots[0].course_name)
            continue
        if old_slots is None or new_slots is None:  # pragma: no cover - 上面两分支已覆盖
            continue

        # 改名按时段变化报（同 course_id —— UID 已知边界的可见提醒）
        if old_slots[0].course_name != new_slots[0].course_name:
            representative = new_slots[0]
            diff.changes.append(
                SlotChange(
                    course_name=representative.course_name,
                    weekday=representative.weekday,
                    periods=representative.periods,
                    field="课程名",
                    old=old_slots[0].course_name,
                    new=new_slots[0].course_name,
                )
            )

        def slotmap(slots: list[CourseSlot]) -> dict[tuple[int, tuple[int, ...]], list[CourseSlot]]:
            grouped: dict[tuple[int, tuple[int, ...]], list[CourseSlot]] = {}
            for slot in slots:
                grouped.setdefault((slot.weekday, slot.periods), []).append(slot)
            return grouped

        old_by_slot = slotmap(old_slots)
        new_by_slot = slotmap(new_slots)

        for slot_key in sorted(set(old_by_slot) | set(new_by_slot)):
            olds = old_by_slot.get(slot_key, [])
            news = new_by_slot.get(slot_key, [])
            for old_slot, new_slot in zip(olds, news, strict=False):
                diff.changes.extend(_field_changes(old_slot, new_slot))
            diff.added_slots.extend(news[len(olds) :])
            diff.removed_slots.extend(olds[len(news) :])

    diff.added_slots.sort(key=lambda s: (s.course_name, s.weekday, s.periods))
    diff.removed_slots.sort(key=lambda s: (s.course_name, s.weekday, s.periods))
    diff.changes.sort(key=lambda c: (c.course_name, c.weekday, c.periods, c.field))
    return diff
