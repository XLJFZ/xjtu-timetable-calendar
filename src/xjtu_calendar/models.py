"""标准化数据模型。

**核心设计约束（务必遵守）**

课表原始事实只有三样：

    weekday（星期） + periods（节次） + weeks（教学周）

``08:00`` / ``09:50`` / ``14:00`` 这类具体钟点**不属于课表事实**，
而属于「该日期当天学校执行的是哪套作息规则」这一独立问题。

因此 :class:`CourseMeeting` 里**不存在** ``start_time`` 字段。钟点由
:class:`AcademicCalendar` + :class:`ScheduleProfile` 在导出阶段现算，
只有这样，冬/夏作息切换才不会破坏数据模型。

（注意：这里的季节作息≠时区 DST。时区恒为 ``Asia/Shanghai``，
变化的是「第 N 节课对应的实际钟点」。）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

__all__ = [
    "CAMPUS_UNKNOWN",
    "CalendarEvent",
    "Course",
    "CourseMeeting",
    "SchedulePeriod",
    "ScheduleProfile",
    "Semester",
]

#: 无法识别校区时使用的占位值
CAMPUS_UNKNOWN = "未知校区"


@dataclass(frozen=True)
class Semester:
    """一个学期（与一套教学日历绑定）。

    Attributes
    ----------
    key:
        学期标识，用于配置文件索引与 UID 生成，例如 ``"2026-fall"``。
    name:
        人类可读名称，例如 ``"2026-2027 学年秋季学期"``。
    first_week_monday:
        第 1 教学周**星期一**的日期。这是教学周换算成具体日期的唯一锚点。

        .. note::
            第 1 教学周的星期一不一定等于开学报到日，也不一定是学期首日。
            这里存的必须是从校历确认过的「第 1 周的周一」。
    total_weeks:
        学期总周数，用于裁剪周次与展开裸「单周/双周」。
    start_date / end_date:
        学期整体起止日期，仅用于展示与校验，不参与逐事件计算。
    """

    key: str
    name: str
    first_week_monday: date
    total_weeks: int = 20
    start_date: date | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        if self.first_week_monday.weekday() != 0:
            raise ValueError(
                f"first_week_monday 必须是星期一，实际是 "
                f"{self.first_week_monday.isoformat()}（weekday={self.first_week_monday.weekday()}）"
            )
        if self.total_weeks <= 0:
            raise ValueError(f"total_weeks 必须为正整数，实际为 {self.total_weeks}")

    @property
    def last_week_sunday(self) -> date:
        """最后一个教学周的星期日。"""
        return self.week_to_date(self.total_weeks, 7)

    def week_to_date(self, week: int, weekday: int) -> date:
        """把「第 N 教学周 + 星期几」换算为具体日期。

        Parameters
        ----------
        week:
            教学周，从 1 开始。
        weekday:
            星期几，``1`` = 星期一 …… ``7`` = 星期日。

        Returns
        -------
        datetime.date

        Examples
        --------
        >>> from datetime import date
        >>> s = Semester("t", "测试", date(2026, 9, 7), total_weeks=16)
        >>> s.week_to_date(1, 1)
        datetime.date(2026, 9, 7)
        >>> s.week_to_date(3, 2)
        datetime.date(2026, 9, 22)
        """
        if week < 1:
            raise ValueError(f"教学周必须 >= 1，实际为 {week}")
        if not 1 <= weekday <= 7:
            raise ValueError(f"星期必须位于 1..7，实际为 {weekday}")
        return self.first_week_monday + _timedelta(weeks=week - 1, days=weekday - 1)


def _timedelta(*, weeks: int = 0, days: int = 0) -> timedelta:
    return timedelta(weeks=weeks, days=days)


@dataclass(frozen=True)
class Course:
    """一门课程（与具体上课时间无关的元信息）。"""

    course_id: str | None
    name: str
    teacher: str | None = None
    credits: float | None = None
    category: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @property
    def display_name(self) -> str:
        return self.name.strip() or "（未命名课程）"


@dataclass(frozen=True)
class CourseMeeting:
    """一次「课程安排」：某门课在某个星期、某些节次、某些教学周上课。

    这是本项目最重要的数据模型。它刻意**不包含任何钟点信息**。

    Attributes
    ----------
    course_id:
        课程稳定标识。``None`` 表示学校接口未提供，此时降级用课程名做标识。
    course_name:
        课程名称。
    teacher:
        教师姓名，可能是多位教师用顿号/逗号分隔的字符串。
    location:
        上课地点原始文本，例如 ``"A-1001"``。
    campus:
        校区，例如 ``"创新港校区"``。
    weekday:
        星期，``1`` = 星期一 …… ``7`` = 星期日。
    periods:
        节次序号列表，例如 ``[1, 2]``。**不是时间，是序号。**
    weeks:
        教学周列表，例如 ``[1, 2, 3, 4, 5, 6, 7, 8]``。
    raw_week_text / raw_period_text:
        原始文本留档，便于排查解析偏差，并用于 DESCRIPTION 回显。
    """

    course_id: str | None
    course_name: str
    weekday: int
    periods: list[int]
    weeks: list[int]
    teacher: str | None = None
    location: str | None = None
    campus: str | None = None
    raw_week_text: str | None = None
    raw_period_text: str | None = None

    def __post_init__(self) -> None:
        if not 1 <= self.weekday <= 7:
            raise ValueError(f"weekday 必须位于 1..7，实际为 {self.weekday}")
        if not self.periods:
            raise ValueError("periods 不能为空")
        if not self.weeks:
            raise ValueError("weeks 不能为空")

    @property
    def stable_course_key(self) -> str:
        """用于生成 UID 的课程标识，保证 course_id 缺失时仍稳定。"""
        return self.course_id or f"name:{self.course_name.strip()}"

    @property
    def full_location(self) -> str | None:
        """拼接后的完整地点，例如 ``"创新港校区 A-1001"``。"""
        parts = [p for p in (self.campus, self.location) if p]
        if not parts:
            return None
        # 避免「创新港校区」和「创新港」重复
        if len(parts) == 2 and (parts[0] in parts[1] or parts[1] in parts[0]):
            return parts[0] if len(parts[0]) >= len(parts[1]) else parts[1]
        return " ".join(parts)


@dataclass(frozen=True)
class CalendarEvent:
    """一次「实际上课」，已经带上了具体日期与时间。"""

    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str | None = None
    description: str | None = None
    #: 溯源信息，便于调试与调课比对（不进 ICS 正文）
    meeting: CourseMeeting | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError(
                f"事件结束时间必须晚于开始时间：{self.start.isoformat()} -> {self.end.isoformat()}"
            )


@dataclass(frozen=True)
class ScheduleProfile:
    """一套作息表：节次序号 -> 当天该节课的起止钟点。

    ``periods`` 的键是节次序号字符串（与 JSON 配置一致），值为 ``("HH:MM", "HH:MM")``。

    .. warning::
        本项目**不内置**任何未经校方来源确认的作息时间。配置由用户或
        后续的官方数据抓取填充。缺少配置时抛出
        :class:`~xjtu_calendar.errors.ScheduleNotConfigured`，而不是猜测。
    """

    key: str
    name: str
    periods: dict[int, tuple[str, str]]

    def __post_init__(self) -> None:
        normalized: dict[int, tuple[str, str]] = {}
        for raw_key, value in self.periods.items():
            idx = int(raw_key)
            start, end = value
            _validate_hhmm(start, f"{self.key} 第 {idx} 节 start")
            _validate_hhmm(end, f"{self.key} 第 {idx} 节 end")
            normalized[idx] = (start, end)
        object.__setattr__(self, "periods", normalized)

    def covers(self, period: int) -> bool:
        return period in self.periods

    def missing_periods(self, periods: list[int]) -> list[int]:
        return [p for p in periods if p not in self.periods]

    def time_of(self, period: int) -> tuple[str, str]:
        try:
            return self.periods[period]
        except KeyError as exc:  # pragma: no cover - 由调用方先行校验
            raise KeyError(f"作息表 {self.key!r} 中缺少第 {period} 节") from exc


@dataclass(frozen=True)
class SchedulePeriod:
    """作息表生效区间：``[start, end]`` 闭区间内使用 ``profile``。"""

    start: date
    end: date
    profile: str

    def __post_init__(self) -> None:
        if self.end < self.start:
            raise ValueError(
                f"作息区间结束日期早于开始日期：{self.start.isoformat()} -> {self.end.isoformat()}"
            )

    def contains(self, day: date) -> bool:
        return self.start <= day <= self.end


def _validate_hhmm(value: str, label: str) -> None:
    """校验 ``"HH:MM"`` 格式并确保是合法时刻。"""
    import re

    if not isinstance(value, str) or not re.fullmatch(r"\d{1,2}:\d{2}", value.strip()):
        raise ValueError(f"{label} 必须是 'HH:MM' 格式，实际为 {value!r}")
    hour, minute = (int(x) for x in value.strip().split(":"))
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{label} 不是合法时刻：{value!r}")
