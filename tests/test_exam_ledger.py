"""考试台账与撤销事件的单元层：渲染条件化（T1）、writer/reader（T3/T4）、候选算法（T5/T6）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from exam_support import STAMP

from xjtu_calendar.exporter import render_ics
from xjtu_calendar.models import CalendarEvent
from xjtu_calendar.schedules import combine


def test_status_line_only_appears_when_set():
    """`status` 默认 None ⇒ 产物里没有 STATUS 行；给了才出现。"""
    event = CalendarEvent(
        uid="a@xjtu-timetable-calendar",
        summary="示例课程甲（结课考试）",
        start=combine(date(2030, 6, 17), "15:00"),
        end=combine(date(2030, 6, 17), "17:30"),
    )
    plain = render_ics([event], calendar_name="课表", dtstamp=STAMP)
    assert "STATUS" not in plain

    cancelled = replace(event, status="CANCELLED")
    out = render_ics([cancelled], calendar_name="课表", dtstamp=STAMP)
    assert "STATUS:CANCELLED" in out


def test_empty_summary_writes_no_summary_line():
    """实测：`add("summary", "")` 会写出字面 `SUMMARY:` 空值行，必须靠不调用 add 来省略。

    spec D3 的「撤销条目无标题」只有这条成立；若将来有人改成 `summary or " "`
    之类，本用例先红。
    """
    event = CalendarEvent(
        uid="b@xjtu-timetable-calendar",
        summary="",
        start=combine(date(2030, 6, 17), "15:00"),
        end=combine(date(2030, 6, 17), "17:30"),
        status="CANCELLED",
    )
    out = render_ics([event], calendar_name="课表", dtstamp=STAMP)
    assert "SUMMARY" not in out
    assert "STATUS:CANCELLED" in out
