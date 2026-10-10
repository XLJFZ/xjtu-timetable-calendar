"""考试台账与撤销事件的单元层：渲染条件化（T1）、writer/reader（T3/T4）、候选算法（T5/T6）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from exam_support import STAMP, captured_logs

from xjtu_calendar.exams import (
    EXAM_STATUS_CANCELLED,
    LedgerEntry,
    build_cancellation_events,
    cancel_candidates,
    load_exam_ledger,
    render_exam_ledger,
)
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


#: 合成"私密"标记（不是真实课程名）：日志泄漏用例靠它判断 warning 有没有复读台账原文。
PRIVATE_MARKER = "私密课程名ZZ"


def ledger_doc(*event_bodies: str) -> str:
    """把若干 VEVENT 体（不含 `BEGIN/END:VEVENT` 行）拼进同一个 VCALENDAR。

    喂给读取端的坏数据要**结构合法**才走得到逐条丢弃那条路，所以这里只拼外壳。
    """
    parts = ["BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\n"]
    for body in event_bodies:
        parts.append(f"BEGIN:VEVENT\r\n{body}END:VEVENT\r\n")
    parts.append("END:VCALENDAR\r\n")
    return "".join(parts)


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


def test_load_exam_ledger_drops_poisoned_entry_and_keeps_the_rest():
    """spec D15 的另一半：属性值是**延迟解码**的 ⇒ `from_ical` 不炸、读 `.dt` 才炸。

    这种条目必须逐条丢弃（其余照常返回），既不许抛异常，也不许把整本台账降级成 `{}`。
    """
    text = ledger_doc(
        f"UID:bad\r\nDTSTART;VALUE=DATE-TIME:{PRIVATE_MARKER}\r\n",
        "UID:good\r\nDTSTART:20300617T150000Z\r\nDTEND:20300618T150000Z\r\nSUMMARY:ok\r\n",
    )
    with captured_logs() as records:
        got = load_exam_ledger(text)
    assert sorted(got) == ["good"], "一条坏数据不该带走整本台账"
    assert got["good"].start.tzinfo is not None
    assert any("bad" in rec.getMessage() for rec in records), "丢弃要能定位到 UID"


def test_load_exam_ledger_drops_entry_with_multiple_uid_lines():
    """两条 `UID` 行 ⇒ icalendar 返回 list，`str()` 出来是 `[vText(b'one'), ...]` 式的垃圾键。

    非单个文本的 UID 与没有 UID 同罪：丢弃 + warning，不许留成假条目。
    """
    text = ledger_doc(
        "UID:one\r\nUID:two\r\nDTSTART:20300617T150000Z\r\nDTEND:20300618T150000Z\r\n"
    )
    with captured_logs() as records:
        got = load_exam_ledger(text)
    assert got == {}
    assert any("UID" in rec.getMessage() for rec in records)


def test_load_exam_ledger_warnings_hide_ledger_text():
    """spec §8「日志不打印台账内容」：icalendar 的报错逐字引用原文，日志不能跟着复读。

    两条降级路径（整篇不可解析 / 单条读不出）都要喂进 `PRIVATE_MARKER`，
    断言抓到的每条 warning 渲染结果里都没有它，也没带 traceback。
    """
    whole_doc = f"BEGIN:VCALENDAR\r\n\xff\xfe 不是 ICS {PRIVATE_MARKER}"
    entry_doc = ledger_doc(f"UID:bad\r\nDTSTART;VALUE=DATE-TIME:{PRIVATE_MARKER}\r\n")
    with captured_logs() as records:
        assert load_exam_ledger(whole_doc) == {}
        assert load_exam_ledger(entry_doc) == {}
    assert records, "两条降级路径都该留下 warning"
    assert [rec.getMessage() for rec in records if PRIVATE_MARKER in rec.getMessage()] == []
    assert not any(rec.exc_info for rec in records), "exc_info 会把原文带进 traceback"


NOW = combine(date(2030, 6, 1), "08:00")  # 所有 2030-06-1x 的考试都还没到


def ledger_of(*uids_and_days: tuple[str, date]) -> dict[str, LedgerEntry]:
    return {uid: entry(uid, day) for uid, day in uids_and_days}


def test_candidate_only_when_absent_from_live():
    ledger = ledger_of(("a", date(2030, 6, 17)), ("b", date(2030, 6, 20)))
    deliverable, _ = cancel_candidates(
        ledger=ledger, live_uids={"b"}, baseline_uids={"a", "b"}, now=NOW
    )
    assert [e.uid for e in deliverable] == ["a"]


def test_expired_entries_are_not_cancelled():
    """spec D2/D9：原定时刻已过 ⇒ 什么都不发，也不留在结果里。"""
    ledger = ledger_of(("past", date(2030, 5, 1)))
    deliverable, unresolved = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids={"past"}, now=NOW
    )
    assert deliverable == [] and unresolved == []


def test_expired_and_baseline_missing_lands_in_neither_list():
    """筛子顺序是承重的：过期判定必须在基线判定**之前**。

    既已过原定时刻（相对 ``NOW`` 是 2030-05-01）又不在基线里的条目，两个返回列表
    都不许出现 ⇒ 它安静地退出台账，永远不会被发布。若把基线检查挪到过期检查前面，
    这条会进 ``unresolvable``：调用方按 D20 给它记「无法安全下发」的 warning，
    而 spec 对过期条目的口径（D2/D9）是「什么都不发」。上面两个用例各自只踩中
    一道筛子（past 在基线里、ghost 未过期），都拦不住这次换位 —— 本用例是唯一的钉子。
    """
    ledger = ledger_of(("gone", date(2030, 5, 1)))
    deliverable, unresolved = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids=set(), now=NOW
    )
    assert deliverable == []
    assert unresolved == []


def test_missing_from_baseline_is_unresolvable_not_emitted():
    """spec D20：基线里没有该 UID ⇒ resolve_sequence 会给 0，宁可不撤销。

    喂两条同日条目且**逆序**插入台账 dict：``unresolvable`` 必须和 ``deliverable``
    一样按 ``(start, uid)`` 全序排好，否则发布产物的字节不稳定（台账 dict 按插入序
    返回，删掉那处 ``sorted`` 时只有本用例会红）。
    """
    ledger = ledger_of(("ghost-b", date(2030, 6, 17)), ("ghost-a", date(2030, 6, 17)))
    deliverable, unresolved = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids=set(), now=NOW
    )
    assert deliverable == []
    assert [e.uid for e in unresolved] == ["ghost-a", "ghost-b"]


def test_total_order_by_start_then_uid():
    ledger = ledger_of(("b", date(2030, 6, 17)), ("a", date(2030, 6, 17)), ("c", date(2030, 6, 10)))
    deliverable, _ = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids={"a", "b", "c"}, now=NOW
    )
    assert [e.uid for e in deliverable] == ["c", "a", "b"]  # 同日按 uid 升序，全序


def test_cancellation_event_copies_uid_and_times_verbatim():
    src = entry("a", date(2030, 6, 17))
    (event,) = build_cancellation_events([src])
    assert event.uid == src.uid
    assert event.start == src.start and event.end == src.end
    assert event.status == EXAM_STATUS_CANCELLED
    # spec D3：最小字段。考场/座位/教师都不再公开。
    assert event.summary == ""
    assert event.location is None
    assert event.description is None


def test_cancellation_events_keep_input_order():
    entries = [entry("b", date(2030, 6, 20)), entry("a", date(2030, 6, 17))]
    assert [e.uid for e in build_cancellation_events(entries)] == ["b", "a"]
