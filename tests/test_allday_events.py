"""0.9-P1-01 全天事件基础设施（先红用例）。

规格来自 `docs/design/2026-10-15-academic-calendar-source.md` §6.1 + §12.1 + §13 第 1 笔：
**"补一下 `parse_baseline`"不足以让 `DATE` 全天事件接进来**——现管线里有六处按 `datetime` 写死，
本文件把六处各自钉一条用例，并额外钉住两条既有不变量（Golden 字节、既有 DATE-TIME 行为）。

写本文件时对现状做过一次直接探针（结论也写进各条 docstring）：

- `render_ics([全天事件])` 当场崩在 `sequence.py:88` 的 `value.tzinfo`
  （`AttributeError: 'datetime.date' object has no attribute 'tzinfo'`）——**渲染这一步都到不了**；
- `sequence.py:145` 对非 `datetime` 的 `dtstart`/`dtend` 直接 `continue` ⇒ 全天事件进不了基线；
- `exporter.py:381` 的 `events.sort(key=lambda e: (e.start, e.summary))` 在 `date` 与 `datetime`
  混排时抛 `TypeError: can't compare datetime.datetime to datetime.date`；
- `exporter.py:555`（`summarize`）与 `exporter.py:634`（`_in_range`，即 `--from-date/--to-date`）
  都调 `event.start.date()` ⇒ `AttributeError`；
- `models.py:295-299` 的 `__post_init__` 用 `end <= start` 比较，混合类型同样 `TypeError`；
- `cancellation.py` 的台账只收 `datetime`（保留期要和 aware 墙钟比，spec D9/D22）⇒ 全天事件
  必须被**明确拒绝**，不许静默丢进 `LedgerEntry`。这是 §5「校历事件不接撤销通路」的落点。

改造同时**反转**了一条既有断言：`tests/test_sequence.py` 里原 `test_parse_baseline_skips_all_day_events`
记录的是缺口而非契约，现改为「全天事件进基线」，另立新的一条用例专门守住"起止形态混用仍跳过"。

语义裁定（§12.1，别在实现时改口径）：配置层是**闭区间**，`CalendarEvent` 与 ICS 一律是
**半开区间**（RFC 5545 的 `DTEND` 不含结束边界）。所以"寒假 1/18–2/28"进 ICS 是
`DTSTART;VALUE=DATE:20270118` + `DTEND;VALUE=DATE:20270301`，单日事件是 `D → D+1`（不是 `D → D`）。
"""

from __future__ import annotations

from datetime import date

import pytest
from icalendar import Calendar

from xjtu_calendar.cancellation import render_ledger
from xjtu_calendar.errors import CalendarExportError
from xjtu_calendar.exporter import _in_range, render_ics, sort_key, summarize
from xjtu_calendar.models import CalendarEvent
from xjtu_calendar.schedules import combine
from xjtu_calendar.sequence import event_fingerprint, parse_baseline

#: 钉死的 DTSTAMP（渲染类用例都要它，否则 DTSTAMP/LAST-MODIFIED 每次都变）。
#: 时刻一律走 `schedules.combine`（它强制 `Asia/Shanghai`）——用 `fromisoformat("+08:00")`
#: 造出来的是固定偏移 tz，icalendar 渲染出的 TZID 与真实产物不同，测试会自己骗自己。
STAMP = combine(date(2026, 9, 1), "00:00")
TIMED_START = combine(date(2026, 9, 14), "08:00")
TIMED_END = combine(date(2026, 9, 14), "09:40")


def _timed(uid: str = "timed@xjtu-timetable-calendar") -> CalendarEvent:
    return CalendarEvent(uid=uid, summary="示例课程甲", start=TIMED_START, end=TIMED_END)


def _all_day(
    uid: str = "cal-2026-2027-1-winter@xjtu-timetable-calendar",
    start: date = date(2027, 1, 18),
    end_exclusive: date = date(2027, 3, 1),
) -> CalendarEvent:
    return CalendarEvent(uid=uid, summary="寒假", start=start, end=end_exclusive)


# --------------------------------------------------------------------------- #
# 1. 模型：date 与 date 合法，date 与 datetime 混着用必须被挡在构造期
# --------------------------------------------------------------------------- #


def test_calendar_event_accepts_pure_dates() -> None:
    """一条 `date` 事件应当能构造出来（现状已能，这条守的是改造之后不许退化）。"""
    event = _all_day()
    assert event.start == date(2027, 1, 18) and event.end == date(2027, 3, 1)


def test_single_day_event_uses_exclusive_end_not_equal_dates() -> None:
    """§12.1：注册 9/12 单日 ⇒ `20260912 → 20260913`，**不是** start == end。

    打破红：把 `__post_init__` 放宽成允许 `end == start`，本用例照样绿——所以断言的是
    "构造得出一条合法的单日事件"而不是"相等也行"；相等的那条由下一条用例挡。
    """
    one_day = CalendarEvent(
        uid="x@y", summary="报到", start=date(2026, 9, 12), end=date(2026, 9, 13)
    )
    assert one_day.end > one_day.start


def test_mixed_date_and_datetime_bounds_are_rejected() -> None:
    """`date` 配 `datetime` 的半吊子事件必须在构造期报错，而不是留到排序时 `TypeError`。

    诚实交代现状：这条**今天就是绿的**——`__post_init__` 里 `end <= start` 一比就抛
    `TypeError`，是"侥幸挡住"而不是"有意校验"。改造后 `start/end` 要放宽成 `date | datetime`，
    那时挡住它的必须是显式的同类性检查（错误信息要点名是哪一半是 `date`），本用例继续守着这个行为。
    """
    with pytest.raises((TypeError, ValueError)):
        CalendarEvent(uid="x@y", summary="混", start=date(2026, 9, 12), end=TIMED_END)
    with pytest.raises((TypeError, ValueError)):
        CalendarEvent(uid="x@y", summary="混", start=TIMED_START, end=date(2026, 9, 13))


# --------------------------------------------------------------------------- #
# 2. 序列化：VALUE=DATE，不带 TZID，不该凭空把 VTIMEZONE 拽进来
# --------------------------------------------------------------------------- #


def test_render_ics_writes_value_date_without_tzid() -> None:
    """全天事件渲染成 `DTSTART;VALUE=DATE:YYYYMMDD`（现状：崩在 `norm_dt` 的 `.tzinfo`）。"""
    ics = render_ics([_all_day()], dtstamp=STAMP)
    assert "DTSTART;VALUE=DATE:20270118" in ics
    assert "DTEND;VALUE=DATE:20270301" in ics
    assert "TZID" not in ics
    # 全份产物只有一条 DATE 事件 ⇒ 没有任何事件引用 TZID，VTIMEZONE 不该出现
    # （既有规则：空日历不引用 TZID 也不应造 VTIMEZONE，exporter.py:537-539）。
    assert "BEGIN:VTIMEZONE" not in ics


def test_render_ics_keeps_datetime_events_untouched() -> None:
    """同一条改造不许改动 DATE-TIME 那一路的字节（既有 Golden 之外的第二道看守）。"""
    ics = render_ics([_timed()], dtstamp=STAMP)
    assert "DTSTART;TZID=Asia/Shanghai:20260914T080000" in ics
    assert "BEGIN:VTIMEZONE" in ics


# --------------------------------------------------------------------------- #
# 3. 指纹与基线：全天事件必须进基线，改日期要 SEQUENCE+1
# --------------------------------------------------------------------------- #


def test_event_fingerprint_supports_date_values() -> None:
    """`event_fingerprint` 得能吃 `date`：同一日期稳定、不同日期不同。"""
    same = event_fingerprint("寒假", None, None, date(2027, 1, 18), date(2027, 3, 1))
    again = event_fingerprint("寒假", None, None, date(2027, 1, 18), date(2027, 3, 1))
    moved = event_fingerprint("寒假", None, None, date(2027, 1, 22), date(2027, 3, 5))
    assert same == again
    assert same != moved


def test_parse_baseline_keeps_all_day_events() -> None:
    """现状 `sequence.py:145` 直接 `continue` ⇒ 全天事件"进不了基线、客户端永不更新"。"""
    ics = render_ics([_all_day()], dtstamp=STAMP)
    baseline = parse_baseline(ics)
    uid = "cal-2026-2027-1-winter@xjtu-timetable-calendar"
    assert uid in baseline, f"基线里没有全天事件：{sorted(baseline)}"
    assert baseline[uid].sequence == 0


def _sequences(ics: str) -> list[int]:
    """产物里所有 VEVENT 的 `SEQUENCE`（按出现顺序）。

    用 `splitlines()` 而不是 `^SEQUENCE:1$` + MULTILINE：RFC 5545 要求 **CRLF** 换行，
    `$` 只认 `\n`，`\r` 会让一条本来成立的断言永远匹配不上（写本用例时实测踩过）。
    """
    return [int(line.split(":")[1]) for line in ics.splitlines() if line.startswith("SEQUENCE:")]


def test_sequence_bumps_when_an_all_day_event_moves_and_title_only_change_does_not() -> None:
    """§5/§12.3 的身份承诺：改日期 ⇒ SEQUENCE+1；只改标题也 ⇒ SEQUENCE+1（标题进指纹）。

    反过来说，**内容一字不改地重发布**必须 SEQUENCE 不动——那才是客户端原地更新的依据。
    """
    original = render_ics([_all_day()], dtstamp=STAMP)
    baseline = parse_baseline(original)
    assert _sequences(original) == [0], "无基线时全天事件应从 SEQUENCE:0 起算"

    moved = render_ics(
        [_all_day(start=date(2027, 1, 22), end_exclusive=date(2027, 3, 5))],
        dtstamp=STAMP,
        baseline=baseline,
    )
    assert _sequences(moved) == [1], "改日期没升 SEQUENCE（多半是没进基线）"

    renamed = render_ics(
        [_replace_summary(_all_day(), "2026-2027 学年寒假")],
        dtstamp=STAMP,
        baseline=baseline,
    )
    assert _sequences(renamed) == [1]

    identical = render_ics([_all_day()], dtstamp=STAMP, baseline=baseline)
    assert _sequences(identical) == [0], "内容未变却升了 SEQUENCE"


def _replace_summary(event: CalendarEvent, summary: str) -> CalendarEvent:
    from dataclasses import replace

    return replace(event, summary=summary)


# --------------------------------------------------------------------------- #
# 4. 排序与过滤：混合类型不许炸
# --------------------------------------------------------------------------- #


def test_mixed_all_day_and_timed_events_sort_all_day_first() -> None:
    """`exporter.py:381` 的 `events.sort(key=(e.start, e.summary))` 混排会 `TypeError`。

    裁定：同一天里**全天事件排在前面**（客户端的"整天"块通常置顶，且这样产物稳定可读）。

    断言打在 `sort_key` 上而不是 `render_ics` 的输出顺序上——排序发生在
    :func:`~xjtu_calendar.exporter.build_events`（第 381 行），`render_ics` 只按传入顺序渲染；
    拿渲染结果断言顺序会测到一条不存在的契约。
    """
    timed_on_winter_day = CalendarEvent(
        uid="timed-on-winter@xjtu-timetable-calendar",
        summary="示例课程乙",
        start=combine(date(2027, 1, 18), "08:00"),
        end=combine(date(2027, 1, 18), "09:40"),
    )
    mixed = [timed_on_winter_day, _all_day()]

    # 旧键的现状：混排当场炸（这条就是本用例的"红"依据）。
    with pytest.raises(TypeError):
        sorted(mixed, key=lambda e: (e.start, e.summary))

    assert [event.uid for event in sorted(mixed, key=sort_key)] == [
        "cal-2026-2027-1-winter@xjtu-timetable-calendar",
        "timed-on-winter@xjtu-timetable-calendar",
    ]

    # 纯计时事件的排序必须与旧键**逐位一致**——否则既有产物的字节会变。
    timed_only = [
        CalendarEvent(
            uid="b",
            summary="乙",
            start=combine(date(2026, 9, 14), "10:00"),
            end=combine(date(2026, 9, 14), "11:40"),
        ),
        _timed(),
        CalendarEvent(
            uid="a",
            summary="乙",
            start=combine(date(2026, 9, 14), "08:00"),
            end=combine(date(2026, 9, 14), "09:40"),
        ),
    ]
    assert [event.uid for event in sorted(timed_only, key=sort_key)] == [
        event.uid for event in sorted(timed_only, key=lambda e: (e.start, e.summary))
    ]


def test_in_range_predicate_handles_date_events() -> None:
    """`_in_range`（`--from-date/--to-date` 走的那条）现状调 `start.date()` ⇒ `AttributeError`。"""
    event = _all_day()
    assert _in_range(event, date(2027, 1, 1), date(2027, 2, 1)) is True
    assert _in_range(event, date(2027, 4, 1), date(2027, 5, 1)) is False
    assert _in_range(_timed(), date(2026, 9, 14), date(2026, 9, 14)) is True


def test_summarize_reports_date_range_for_date_events() -> None:
    """`summarize`（`exporter.py:555`）同样炸在 `.date()`；混合时也要能给出范围。"""
    result = summarize([], [_all_day()])
    assert result["date_range"] == "2027-01-18 ~ 2027-02-28"
    mixed = summarize([], [_timed(), _all_day()])
    assert mixed["date_range"].startswith("2026-09-14") and mixed["date_range"].endswith(
        "2027-02-28"
    )


def test_round_trip_of_all_day_event_is_lossless() -> None:
    """写出去再读回来，起止必须还是 `date`（半开区间的值本身，不做 +/-1 二次换算）。"""
    ics = render_ics([_all_day()], dtstamp=STAMP)
    event = next(iter(Calendar.from_ical(ics).walk("VEVENT")))
    assert event["dtstart"].dt == date(2027, 1, 18)
    assert event["dtend"].dt == date(2027, 3, 1)


def test_cancellation_ledger_refuses_all_day_events() -> None:
    """全天事件**不进**撤销通路（§5 裁定：0.9.0 校历事件不参与熔断与撤销）。

    台账的保留期要和 aware 墙钟比较（spec D9/D22），`date` 没有那一刻，所以这条通路
    必须"失败要响"，而不是静默丢掉一条本该撤销的事件。
    """
    with pytest.raises(CalendarExportError, match="UID=cal-2026-2027-1-winter"):
        render_ledger([_all_day()], [], dtstamp=STAMP)

    # 计时事件照旧走得通——守卫不能把既有通路一起挡死。
    assert "UID:timed@xjtu-timetable-calendar" in render_ledger([_timed()], [], dtstamp=STAMP)
