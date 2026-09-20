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

__version__ = "0.1.0"

from .models import (
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
    "CalendarEvent",
    "Course",
    "CourseMeeting",
    "SchedulePeriod",
    "ScheduleProfile",
    "Semester",
    "__version__",
]
