"""撤销机制：考试与课程**共用**的那一份（docs/design/2026-10-12-course-cancellation-uid-v2.md §6.1）。

权威在这里，不在 `exams.py`。`exams.py` 保留原名 re-export，既有调用点与测试的
import 路径一律不改（CD9：零回归面）。课程侧独有的判据（门 1/门 2/迁移候选）
在 `courses.py`；**一旦某段逻辑需要按考试/课程分叉，就把它挪出本模块**，
别往这里塞 if 分支。
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .exporter import PRODID
from .models import CalendarEvent

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 台账（spec §8）：记录「曾以 live 形态发布」的考试，供后续渲染撤销事件
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class LedgerEntry:
    """台账里的一条：某场**曾以 live 形态发布**的考试，保留最后一次发布的字段。

    只在本地存在（spec §8），`start`/`end` 必须是带 tz 的 datetime —— 保留期要和 aware 的
    墙钟比较（spec D9/D22）。
    """

    uid: str
    start: datetime
    end: datetime
    summary: str
    location: str | None = None
    description: str | None = None


def render_ledger(
    live: Sequence[CalendarEvent],
    pending: Sequence[LedgerEntry],
    *,
    dtstamp: datetime,
) -> str:
    """渲染台账：``live`` 的全字段 ∪ ``pending`` 的原字段，按 ``(start, uid)`` 全序。

    台账不是发布产物（spec D10/D22）：**不写** STATUS / SEQUENCE / LAST-MODIFIED /
    METHOD / X-WR-CALNAME，也不走 :func:`xjtu_calendar.exporter.render_ics`（那个函数
    无条件写 DTSTAMP/SEQUENCE，会把 D10 立刻推翻）。同 UID 时 live 形态胜出。
    ``add_missing_timezones()`` 必须调用：读取端要靠 TZID 拿回 aware datetime。
    """
    from icalendar import Calendar, Event

    chosen: dict[str, LedgerEntry] = {}
    for item in pending:
        chosen.setdefault(item.uid, item)
    for event in live:
        chosen[event.uid] = LedgerEntry(
            uid=event.uid,
            start=event.start,
            end=event.end,
            summary=event.summary,
            location=event.location,
            description=event.description,
        )

    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    for item in sorted(chosen.values(), key=lambda e: (e.start, e.uid)):
        component = Event()
        component.add("uid", item.uid)
        component.add("dtstamp", dtstamp)
        component.add("dtstart", item.start)
        component.add("dtend", item.end)
        if item.summary:
            component.add("summary", item.summary)
        if item.location:
            component.add("location", item.location)
        if item.description:
            component.add("description", item.description)
        cal.add_component(component)
    if chosen:
        cal.add_missing_timezones()
    raw: bytes = cal.to_ical()
    return raw.decode("utf-8")


def _optional_text(value: object) -> str | None:
    """icalendar 属性值 → 可选文本（空值一律 ``None``，与渲染端的条件化对齐）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def load_ledger(text: str, *, label: str = "台账") -> dict[str, LedgerEntry]:
    """台账文本 → ``{UID: LedgerEntry}``。

    **不抛异常**（spec D15），但两条降级路径要分清：

    - 整篇不可解析 ⇒ 一条 warning + 空 dict，调用方按"没有台账"继续；
    - 单条 VEVENT 读不出 ⇒ 一条 warning + **只丢那一条**，其余照常返回。
      这一条不是可选优化：本版本的 icalendar 把属性值解码推迟到访问时，
      结构合法的文档也能在 ``component.get("dtstart").dt`` 上抛
      ``BrokenCalendarProperty``，所以逐条读取整体包在 try 里。

    两条路径的 warning 都只报**失败种类**（异常类名），绝不带台账原文
    ——spec §8「日志不打印台账内容」，而 icalendar 的报错信息会逐字引用原文。
    丢弃：UID 缺失或不是单个文本、起止缺失或相等、**起止为 naive datetime**
    （spec D22：D9 要和 aware 墙钟比较，naive 值会 ``TypeError``）、重复 UID（保留第一条）。

    ``label`` 只用于日志措辞（考试侧传「考试台账」，课程侧传「课程台账」）；
    考试侧经 `exams.load_exam_ledger`（`partial`）进入，逐字节保持既有文案。
    """
    from icalendar import Calendar

    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:  # 截断、编码坏、结构走样都归这一类
        # 只报类名：``str(exc)`` 会把台账原文（含课程名/地点）整段带进日志。
        logger.warning("%s整篇不可解析，本次按没有台账处理（%s）", label, type(exc).__name__)
        return {}

    entries: dict[str, LedgerEntry] = {}
    for component in cal.walk("VEVENT"):
        uid = ""
        try:
            raw_uid = component.get("uid")
            # 一个 VEVENT 写了两条 `UID` 时 icalendar 返回 list，`str()` 出来是
            # `[vText(b'one'), vText(b'two')]` 这种垃圾键；非单个文本与没有 UID 同罪。
            if not isinstance(raw_uid, str):
                logger.warning("%s里有一条的 UID 不是单个文本，已丢弃", label)
                continue
            uid = raw_uid.strip()
            if not uid:
                logger.warning("%s里有一条没有 UID，已丢弃", label)
                continue
            if uid in entries:
                logger.warning("%s里 UID 重复：%s，保留第一条", label, uid)
                continue
            start = getattr(component.get("dtstart"), "dt", None)
            end = getattr(component.get("dtend"), "dt", None)
            if not isinstance(start, datetime) or not isinstance(end, datetime):
                logger.warning("%s条目 %s 的起止不是 DATE-TIME，已丢弃", label, uid)
                continue
            if start.tzinfo is None or end.tzinfo is None:
                logger.warning(
                    "%s条目 %s 的起止没有时区，已丢弃（否则保留期比较会失败）", label, uid
                )
                continue
            if end <= start:
                logger.warning("%s条目 %s 的结束不晚于开始，已丢弃", label, uid)
                continue
            entries[uid] = LedgerEntry(
                uid=uid,
                start=start,
                end=end,
                summary=str(component.get("summary") or ""),
                location=_optional_text(component.get("location")),
                description=_optional_text(component.get("description")),
            )
        except Exception as exc:
            # 属性延迟解码才炸（BrokenCalendarProperty 等）：丢这一条，别的照留。
            # 同样只报类名 + UID，UID 之外的台账内容一个字都不进日志（spec §8）。
            if uid:
                logger.warning("%s条目 %s 读不出（%s），已丢弃", label, uid, type(exc).__name__)
            else:
                logger.warning("%s里有一条读不出（%s），已丢弃", label, type(exc).__name__)
    return entries


def cancel_candidates(
    *,
    ledger: Mapping[str, LedgerEntry],
    live_uids: Collection[str],
    baseline_uids: Collection[str],
    now: datetime,
) -> tuple[list[LedgerEntry], list[LedgerEntry]]:
    """台账 − live ⇒ ``(可下发的撤销候选, 无法安全下发的候选)``，都按 ``(start, uid)`` 全序。

    三道筛子对应设计文档的三条决策：

    - ``live_uids``：**未经日期过滤**的考试 UID（D19）。过滤后的集合会把窗口外考试
      当成"消失"，一次局部导出就剪掉全局订阅状态。
    - ``now``：墙钟。原定开始时刻已过 ⇒ 既不下发也不保留（D2/D9），条目就此退出台账。
    - ``baseline_uids``：上一次发布产物的 UID 集合。不在其中的候选进 ``unresolvable``：
      ``resolve_sequence`` 会给 ``SEQUENCE:0``，而客户端对更低序号应当忽略 ⇒ 发了等于
      没发，还骗自己"撤销过了"（D20）。调用方要把 ``unresolvable`` 记 warning 并**从
      台账剪掉**。

    同日两场考试是实测见过的（spec §4.1），所以排序必须是全序。
    """
    deliverable: list[LedgerEntry] = []
    unresolvable: list[LedgerEntry] = []
    for item in ledger.values():
        if item.uid in live_uids:
            continue
        if item.start < now:
            continue
        if item.uid not in baseline_uids:
            unresolvable.append(item)
            continue
        deliverable.append(item)
    key = lambda e: (e.start, e.uid)  # noqa: E731 —— 两处排序同一口径，不提公共函数
    return sorted(deliverable, key=key), sorted(unresolvable, key=key)


#: 撤销事件的 ``STATUS`` 值。台账里**不**写这个字段（spec D10）。
STATUS_CANCELLED = "CANCELLED"

#: 撤销条目的固定标题（v0.6.1，spec D3 的修订）。
#:
#: 为什么要有标题：忽略 ``STATUS:CANCELLED`` 的客户端（Outlook.com、ColorOS 系统日历，
#: 2026-10 真机实测）会把撤销条目当普通事件显示，没有 ``SUMMARY`` 就只剩一条「无主题」
#: 日程——用户既认不出它是废弃条目，也不知道该不该删。
#: 为什么是**固定文案**：标题恒定 ⇒ :func:`xjtu_calendar.sequence.event_fingerprint`
#: 的输入恒定 ⇒ 同一份产物重渲染时 SEQUENCE 不漂移（D8）；同时这个字符串里不含课程名、
#: 考场、座位、教师中的任何一项，保留期内不会重新公开它们。
CANCELLATION_SUMMARY = "已取消的活动"


def build_cancellation_events(entries: Sequence[LedgerEntry]) -> list[CalendarEvent]:
    """台账条目 → 待发布的撤销事件（最小字段，spec D3）。

    UID **照抄不重算**：撤销的全部前提就是复用已发布出去的那个 UID，客户端按 UID 匹配
    才谈得上删除。``summary`` 取 :data:`CANCELLATION_SUMMARY` 那个固定文案，**不引用**
    台账里的任何原文；``location`` / ``description`` 一律置空。
    顺序保持调用方给的全序，本函数不重排。
    """
    return [
        CalendarEvent(
            uid=item.uid,
            summary=CANCELLATION_SUMMARY,
            start=item.start,
            end=item.end,
            location=None,
            description=None,
            meeting=None,
            status=STATUS_CANCELLED,
        )
        for item in entries
    ]
