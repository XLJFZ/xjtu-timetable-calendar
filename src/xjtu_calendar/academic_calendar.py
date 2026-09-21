"""教学日历：教学周 -> 实际日期。

职责边界
--------
:class:`AcademicCalendar` 只回答一个问题：

    「第 N 教学周的星期 X 是哪一天？」

它**不回答**「那天上第几节、几点上」——那是 :mod:`xjtu_calendar.schedules` 的事。

同时它承载两类可扩展的教学安排干预：

- :attr:`excluded_dates` —— 全校停课日（节假日），生成事件时直接丢弃。
- :attr:`overrides` —— 对特定日期的显式覆盖（补课、调课、换教室）。

第一版不自动推断节假日，只提供机制；数据由校历配置提供。
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .errors import ParseError, SemesterNotConfigured
from .models import CourseMeeting, Semester, UnsupportedAdjustment

__all__ = ["AcademicCalendar", "DateOverride"]


@dataclass(frozen=True)
class DateOverride:
    """对某个具体日期的教学安排覆盖。

    用法（按优先级从高到低）：

    1. ``source_date`` —— **调课日**：该日原课程停上（等效于整日取消），
       改为按 ``source_date`` **所在教学周 + 星期** 的课程安排生成事件。
       事件的日期是本日（不是 source_date），钟点按**本日**适用作息解析。
       这正是学校调休通知的语义："某日上原本某日的课"。
    2. ``cancel=True`` —— 该日不上课，等效于把该日期加入排除集合。
    3. ``skip_meeting_keys`` —— 该日仅跳过指定的课程安排（按 ``course_key`` 匹配）。
    4. ``location`` / ``note`` —— 不改变是否上课，只覆盖地点或附加备注。

    Attributes
    ----------
    day:
        被覆盖的日期。
    cancel:
        是否整日停课（``source_date`` 存在时隐含生效，无需重复声明）。
    source_date:
        调课来源日期。该日的课程来源是 ``source_date`` 的**教学周 + 星期**，
        而不是本日在自然日历上的星期 —— 单双周 / ``SKZC`` 位掩码语义
        也按 source 教学周解释（复现源教学日原本应上的那一套课）。
    skip_meeting_keys:
        该日需要跳过的课程标识集合（``CourseMeeting.stable_course_key``）。
    location:
        覆盖上课地点（换教室场景）。
    note:
        附加到 DESCRIPTION 的说明，例如 ``"调课补课"``。
    """

    day: date
    cancel: bool = False
    source_date: date | None = None
    skip_meeting_keys: frozenset[str] = frozenset()
    location: str | None = None
    note: str | None = None

    def skips(self, meeting: CourseMeeting) -> bool:
        """该日是否跳过 ``meeting`` 的**原生**安排。

        调课日（``source_date`` 存在）隐含整日取消：原课程让位给
        source 课表，否则 target date 会出现「自身课程 + 调入课程」双份事件。
        """
        if self.cancel or self.source_date is not None:
            return True
        return meeting.stable_course_key in self.skip_meeting_keys


@dataclass
class AcademicCalendar:
    """教学日历 = 学期 + 干预规则。

    Attributes
    ----------
    semester:
        绑定的学期，提供 ``first_week_monday`` 锚点。
    excluded_dates:
        全校停课日集合。
    overrides:
        以日期为键的覆盖规则。
    """

    semester: Semester
    excluded_dates: set[date] = field(default_factory=set)
    overrides: dict[date, DateOverride] = field(default_factory=dict)
    unsupported_adjustments: tuple[UnsupportedAdjustment, ...] = field(default_factory=tuple)

    # ------------------------------------------------------------------ #
    # 教学周 -> 日期
    # ------------------------------------------------------------------ #
    def week_to_date(self, week: int, weekday: int) -> date:
        """换算教学周 + 星期为具体日期。

        >>> from datetime import date
        >>> from xjtu_calendar.models import Semester
        >>> cal = AcademicCalendar(Semester("t", "测试", date(2026, 9, 7), total_weeks=16))
        >>> cal.week_to_date(3, 2).isoformat()
        '2026-09-22'
        """
        return self.semester.week_to_date(week, weekday)

    def date_to_week(self, day: date) -> int:
        """反向换算：某日期落在第几教学周。不在学期范围内时返回 0 或负数由调用方判断。"""
        delta = (day - self.semester.first_week_monday).days
        return delta // 7 + 1

    def semester_dates(self) -> Iterable[date]:
        """按日遍历整个学期。"""
        current = self.semester.first_week_monday
        end = self.semester.last_week_sunday
        while current <= end:
            yield current
            current += timedelta(days=1)

    # ------------------------------------------------------------------ #
    # 干预规则
    # ------------------------------------------------------------------ #
    def is_excluded(self, day: date) -> bool:
        """该日是否全校停课。"""
        if day in self.excluded_dates:
            return True
        override = self.overrides.get(day)
        return bool(override and override.cancel)

    def override_for(self, day: date) -> DateOverride | None:
        return self.overrides.get(day)

    def makeup_rules(self) -> list[tuple[date, DateOverride]]:
        """返回所有调课规则 ``[(target_date, override), ...]``（按日期升序）。

        只包含声明了 ``source_date`` 的覆盖；纯停课 / 换教室等规则不在此列。
        """
        return sorted(
            ((day, o) for day, o in self.overrides.items() if o.source_date is not None),
            key=lambda item: item[0],
        )

    # ------------------------------------------------------------------ #
    # 序列化
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> AcademicCalendar:
        """从字典构造。

        期望结构（与 ``examples/academic_calendar.example.json`` 一致）::

            {
              "semester": {
                "key": "2026-fall",
                "name": "2026-2027 学年秋季学期",
                "first_week_monday": "2026-09-07",
                "total_weeks": 16,
                "start_date": "2026-09-07",
                "end_date": "2026-12-27"
              },
              "excluded_dates": ["2026-10-01"],
              "overrides": {
                "2026-10-10": {"note": "国庆调休补课", "location": "A-1001"}
              }
            }
        """
        try:
            raw_semester = payload["semester"]
        except (KeyError, TypeError) as exc:
            raise ParseError("教学日历缺少 'semester' 字段") from exc

        semester = _semester_from_dict(raw_semester)

        excluded = {_parse_date(item, "excluded_dates") for item in payload.get("excluded_dates", [])}

        overrides: dict[date, DateOverride] = {}
        for key, value in (payload.get("overrides") or {}).items():
            day = _parse_date(key, "overrides")
            value = value or {}
            skip_keys = frozenset(
                value.get("skip_courses") or value.get("skip_meeting_keys") or []
            )
            source_date: date | None = None
            if value.get("source_date"):
                source_date = _parse_date(value["source_date"], f"overrides[{key}].source_date")
            overrides[day] = DateOverride(
                day=day,
                cancel=bool(value.get("cancel", False)),
                source_date=source_date,
                skip_meeting_keys=skip_keys,
                location=value.get("location"),
                note=value.get("note"),
            )
            overrides[day] = _validate_override(overrides[day], semester, excluded)

        adjustments = _unsupported_adjustments_from_dict(
            payload.get("unsupported_adjustments")
        )

        # 调课日不能再同时声明为「无法表达」：同一日期两种互相矛盾的声明，
        # 几乎总是「机制升级后忘了删旧声明」——保留会让导出侧误报缺课。
        makeup_days = {day for day, o in overrides.items() if o.source_date is not None}
        for a in adjustments:
            if a.date in makeup_days:
                raise ParseError(
                    f"{a.date.isoformat()} 同时出现在 unsupported_adjustments 与"
                    f" overrides[source_date] 中：该调课已可用 overrides 表达，"
                    f"请从 unsupported_adjustments 中删除这条过时声明。"
                )

        return cls(
            semester=semester,
            excluded_dates=excluded,
            overrides=overrides,
            unsupported_adjustments=adjustments,
        )

    @classmethod
    def from_file(cls, path: str | Path) -> AcademicCalendar:
        """从 JSON 文件加载教学日历。"""
        file_path = Path(path)
        if not file_path.is_file():
            raise SemesterNotConfigured(f"教学日历文件不存在：{file_path}")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ParseError(f"教学日历不是合法 JSON：{file_path}（{exc}）") from exc
        return cls.from_dict(payload)


def _validate_override(
    override: DateOverride,
    semester: Semester,
    excluded: set[date],
) -> DateOverride:
    """校验单条覆盖规则的跨字段一致性（fail-closed）。

    调课声明里的错误如果等到导出阶段才炸，用户会面对一份「看起来生成
    成功实则错乱」的日历；在配置加载时就拒绝，才能把问题挡在最早一步。
    """
    if override.source_date is None:
        return override

    if override.source_date == override.day:
        raise ParseError(
            f"overrides[{override.day.isoformat()}].source_date 不能等于该日期自身："
            f"「按当天的课表上课」不是调课，请删除 source_date。"
        )

    if override.day in excluded:
        raise ParseError(
            f"{override.day.isoformat()} 同时出现在 excluded_dates 与"
            f" overrides[source_date] 中：调课目标日不能又是全校停课日，"
            f"两者只能保留其一。"
        )

    # source_date 只用于推导「第几教学周 + 星期几」，不需要它自己可上课；
    # 但它必须落在学期教学周范围内，否则周次换算没有意义。
    source_week = (override.source_date - semester.first_week_monday).days // 7 + 1
    if override.source_date < semester.first_week_monday or source_week < 1:
        raise ParseError(
            f"overrides[{override.day.isoformat()}].source_date="
            f"{override.source_date.isoformat()} 早于第 1 教学周星期一"
            f"（{semester.first_week_monday.isoformat()}），无法换算教学周。"
        )
    if semester.total_weeks is not None and source_week > semester.total_weeks:
        raise ParseError(
            f"overrides[{override.day.isoformat()}].source_date="
            f"{override.source_date.isoformat()} 落在第 {source_week} 教学周，"
            f"超出学期总周数 {semester.total_weeks}。"
        )

    return override


def _semester_from_dict(raw: dict[str, Any]) -> Semester:
    try:
        key = str(raw["key"])
        name = str(raw.get("name") or key)
        first_monday = _parse_date(raw["first_week_monday"], "semester.first_week_monday")
    except KeyError as exc:
        raise ParseError(f"学期配置缺少字段：{exc}") from exc

    return Semester(
        key=key,
        name=name,
        first_week_monday=first_monday,
        total_weeks=_optional_int(raw.get("total_weeks"), "semester.total_weeks"),
        start_date=_optional_date(raw.get("start_date")),
        end_date=_optional_date(raw.get("end_date")),
    )


def _unsupported_adjustments_from_dict(
    raw: Any,
) -> tuple[UnsupportedAdjustment, ...]:
    """解析 ``unsupported_adjustments`` 配置（可为空 / 缺省）。

    期望结构::

        "unsupported_adjustments": [
            {"date": "2026-09-20", "description": "按 2026-10-06 的课表上课"},
            ...
        ]

    这是「已知但无法表达」的显式声明，与 ``overrides`` 的区别：
    ``overrides`` 是工具**能**表达的业务规则；本字段是工具**不能**表达、
    但配置者知道存在的事实。导出阶段会据此决定 fail 还是警告。
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ParseError(
            f"unsupported_adjustments 必须是数组，实际为 {type(raw).__name__}"
        )

    adjustments: list[UnsupportedAdjustment] = []
    for index, item in enumerate(raw):
        label = f"unsupported_adjustments[{index}]"
        if not isinstance(item, dict):
            raise ParseError(f"{label} 必须是对象（含 date / description）")
        if "date" not in item:
            raise ParseError(f"{label} 缺少 date 字段")
        day = _parse_date(item["date"], f"{label}.date")
        description = str(item.get("description") or "").strip()
        if not description:
            raise ParseError(
                f"{label} 缺少 description —— 没有说明的「无法表达」只会让人困惑，"
                f"请写清楚该日期应按什么安排上课"
            )
        adjustments.append(UnsupportedAdjustment(date=day, description=description))

    return tuple(adjustments)


def _optional_int(value: Any, label: str) -> int | None:
    """把配置值转成 int；缺省（``None`` / ``""``）时返回 ``None``。

    ``total_weeks`` 是可选的：留空表示「不做周次上限过滤」。
    填错比留空更危险 —— 越界周次会被判为脏数据在导出阶段报错终止。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ParseError(f"{label} 必须是正整数或留空，实际为 {value!r}") from exc


def _parse_date(value: Any, label: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ParseError(f"{label} 不是合法 ISO 日期：{value!r}") from exc
    raise ParseError(f"{label} 期望 ISO 日期字符串，实际为 {type(value).__name__}")


def _optional_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    return _parse_date(value, "date")
