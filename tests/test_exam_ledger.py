"""考试台账与撤销事件的单元层：渲染条件化（T1）、writer/reader（T3/T4）、候选算法（T5/T6）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from exam_support import STAMP, captured_logs

from xjtu_calendar.exams import LedgerEntry, load_exam_ledger, render_exam_ledger
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


def entry(uid: str, day: date, start: str = "15:00", end: str = "17:30") -> LedgerEntry:
    return LedgerEntry(
        uid=uid,
        start=combine(day, start),
        end=combine(day, end),
        summary=f"示例课程{uid}（结课考试）",
        location="兴庆 A-1001",
        description="座位号：NN",
    )


def test_render_exam_ledger_is_minimal_but_parseable():
    text = render_exam_ledger([], [entry("a", date(2030, 6, 17))], dtstamp=STAMP)
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" in text
    # spec D10/D22：台账不是发布产物，不许带这些字段
    assert "STATUS" not in text
    assert "SEQUENCE" not in text
    assert "LAST-MODIFIED" not in text
    assert "METHOD" not in text
    assert "X-WR-CALNAME" not in text
    # 台账必须自带 VTIMEZONE：读取端要拿回 aware datetime（Task 4 的 naive 门槛靠它）
    assert "BEGIN:VTIMEZONE" in text
    assert "TZID=Asia/Shanghai" in text


def test_render_exam_ledger_dedupes_with_live_winning():
    live = CalendarEvent(
        uid="dup",
        summary="新的 live 形态",
        start=combine(date(2030, 6, 18), "09:00"),
        end=combine(date(2030, 6, 18), "11:00"),
    )
    text = render_exam_ledger(
        [live],
        [
            LedgerEntry(
                uid="dup",
                start=combine(date(2030, 6, 17), "15:00"),
                end=combine(date(2030, 6, 17), "17:30"),
                summary="旧形态",
            )
        ],
        dtstamp=STAMP,
    )
    assert text.count("UID:dup") == 1
    assert "新的 live 形态" in text
    assert "旧形态" not in text


def test_render_exam_ledger_orders_by_start_then_uid():
    entries = [entry("b", date(2030, 6, 20)), entry("a", date(2030, 6, 17))]
    text = render_exam_ledger([], entries, dtstamp=STAMP)
    assert text.index("UID:a") < text.index("UID:b")


def test_ledger_round_trip_keeps_uids_and_aware_times():
    entries = [entry("a", date(2030, 6, 17)), entry("b", date(2030, 6, 20))]
    back = load_exam_ledger(render_exam_ledger([], entries, dtstamp=STAMP))
    assert sorted(back) == ["a", "b"]
    assert back["a"].start.tzinfo is not None, "读回 naive 会让 D9 的比较 TypeError"
    assert back["a"].start == entries[0].start
    assert back["a"].location == "兴庆 A-1001"


def test_load_exam_ledger_drops_naive_entries():
    """手工造一条 `DTSTART;VALUE=DATE` 式的无 tz 行：必须丢弃并 warning，不许抛。"""
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
        "UID:naive\r\nDTSTART:20300617T150000\r\nDTEND:20300617T173000\r\n"
        "SUMMARY:无 tz\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with captured_logs() as records:
        got = load_exam_ledger(text)
    assert got == {}
    assert any("naive" in rec.getMessage() or "时区" in rec.getMessage() for rec in records)


def test_load_exam_ledger_survives_garbage_text():
    """spec D15：台账坏文本 ⇒ 当作没有台账，一条 warning，绝不炸穿导出。"""
    with captured_logs() as records:
        assert load_exam_ledger("BEGIN:VCALENDAR\r\n\xff\xfe 不是 ICS") == {}
    assert any("台账" in rec.getMessage() for rec in records)


def test_load_exam_ledger_keeps_first_of_duplicate_uids():
    one = render_exam_ledger([], [entry("a", date(2030, 6, 17))], dtstamp=STAMP)
    two = one.replace("示例课程a", "后来的形态")
    merged = one.replace("\r\nEND:VCALENDAR\r\n", "") + two.replace(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n", ""
    )
    got = load_exam_ledger(merged)
    assert len(got) == 1
    # 计划稿这里写的是 `== "示例课程a"`，但固件 `entry()` 的 summary 带「（结课考试）」后缀；
    # 按本用例声明的不变量「保留第一条」断言完整的第一条 summary。
    assert got["a"].summary == "示例课程a（结课考试）"
