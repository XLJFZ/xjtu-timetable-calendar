"""xjtu-timetable-calendar —— 西安交通大学 eHall 个人课表 -> iCalendar。

流水线::

    eHall -> 登录/会话 -> 原始课表 JSON -> Parser
          -> 标准化 Course / CourseMeeting
          -> AcademicCalendar（教学周 -> 实际日期）
          -> ScheduleTable（日期 -> 冬/夏季作息 -> 节次钟点）
          -> CalendarEvent（逐次上课）
          -> iCalendar (.ics)

设计红线见 README：课程安排只保存「星期 + 节次 + 教学周」，
任何具体钟点都必须由作息规则在导出阶段现算。
"""

from __future__ import annotations

import tomllib
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

#: distribution 名称，必须与 ``pyproject.toml`` 的 ``project.name`` 一致
DISTRIBUTION_NAME = "xjtu-timetable-calendar"


def _version_from_pyproject() -> str | None:
    """源码 checkout（未安装）时的回退：直接读 ``pyproject.toml``。

    版本号仍然只有 **一个真源** —— ``pyproject.toml``。已安装时走
    ``importlib.metadata``，未安装时解析同一份文件，不需要第二处常量。
    """
    # __init__.py -> src/xjtu_calendar -> src -> <repo root>
    candidate = Path(__file__).resolve().parent.parent.parent / "pyproject.toml"
    try:
        payload = tomllib.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return None
    value = payload.get("project", {}).get("version")
    return str(value) if value else None


def _resolve_version() -> str:
    try:
        return version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        return _version_from_pyproject() or "0.0.0"


__version__ = _resolve_version()

from .models import (  # noqa: E402 - 必须先算出 __version__ 再导出公共 API
    CAMPUS_UNKNOWN,
    CalendarEvent,
    Course,
    CourseMeeting,
    SchedulePeriod,
    ScheduleProfile,
    Semester,
)

__all__ = [
    "CAMPUS_UNKNOWN",
    "DISTRIBUTION_NAME",
    "CalendarEvent",
    "Course",
    "CourseMeeting",
    "SchedulePeriod",
    "ScheduleProfile",
    "Semester",
    "__version__",
]
