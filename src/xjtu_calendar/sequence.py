"""VEVENT 的 SEQUENCE / LAST-MODIFIED 管理（RFC 5545 §3.8.7.2 / §3.8.7.3）。

设计动机
--------
日历客户端（Apple 日历、Google 日历、Outlook）在「重新导入一份 ICS」时，
靠 ``UID + SEQUENCE`` 判断一条事件是「没变，跳过」还是「变了，更新」。
只靠 UID 而没有 SEQUENCE，部分客户端会忽略更新——用户改了教室、
调了时间，重新导出导入后日历却纹丝不动。

本项目的事件是**逐次生成**的（无 RRULE），UID 稳定可复现，因此可以
做到精确的版本管理：

- **内容未变**（摘要 / 时间 / 地点 / 描述全同）→ 保留原 ``SEQUENCE`` 与
  ``LAST-MODIFIED``，客户端据此跳过，不产生无意义的「已更新」噪音；
- **内容有变** → ``SEQUENCE + 1``，``LAST-MODIFIED`` 刷新为本次导出时刻；
- **新事件** → ``SEQUENCE: 0``，``LAST-MODIFIED`` 为本次导出时刻。

基线从哪来？上一次导出的 ICS 文件（默认就是输出文件自身的旧版本）。
基线解析失败时**绝不静默吞掉**：显式指定的基线报错终止，
自动探测的基线降级为警告 + 空基线（等价于全部按新事件处理）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from .errors import CalendarExportError
from .models import CalendarEvent

__all__ = [
    "EventBaseline",
    "event_fingerprint",
    "parse_baseline",
    "resolve_sequence",
    "sequence_stats",
]

#: 指纹字段分隔符。控制字符不可能出现在正常的 ICS 文本里，避免拼接歧义。
_SEP = "\x1f"


@dataclass(frozen=True)
class EventBaseline:
    """从旧 ICS 里读出的一条事件的版本状态。

    Attributes
    ----------
    sequence:
        旧 ``SEQUENCE`` 值；旧文件缺失该属性时为 ``0``。
    last_modified:
        旧 ``LAST-MODIFIED``；旧文件缺失该属性时为 ``None``
        （此时内容未变的事件会拿到本次导出时刻作为新的 LAST-MODIFIED，
        SEQUENCE 仍保持不变——对客户端而言这是合法的「元数据刷新」）。
    fingerprint:
        内容指纹，用于与本次导出的事件逐一比对。
    """

    sequence: int
    last_modified: datetime | None
    fingerprint: str


def event_fingerprint(
    summary: str,
    location: str | None,
    description: str | None,
    start: datetime,
    end: datetime,
) -> str:
    """计算事件**内容指纹**。

    参与比对的字段 = 日历客户端真正展示的内容：摘要、起止时间、地点、描述。
    刻意**不含** DTSTAMP / SEQUENCE / LAST-MODIFIED 这类元数据——
    它们变化不代表事件本身变了。

    时间统一折算成 UTC 再比较，避免「同一时刻、不同时区表示」造成假差异。
    """
    def norm_dt(value: datetime) -> str:
        if value.tzinfo is None:
            return value.isoformat()
        return value.astimezone(UTC).isoformat()

    return _SEP.join(
        [
            summary.strip(),
            (location or "").strip(),
            (description or "").strip(),
            norm_dt(start),
            norm_dt(end),
        ]
    )


def parse_baseline(ics_text: str) -> dict[str, EventBaseline]:
    """从旧 ICS 文本解析出 ``UID -> EventBaseline`` 映射。

    Raises
    ------
    CalendarExportError
        文本不是合法 ICS（无法解析出日历组件）时抛出。
        调用方应区分场景处理：显式指定的基线直接终止；
        自动探测的基线降级为警告 + 空基线。
    """
    try:
        from icalendar import Calendar
    except ImportError as exc:  # pragma: no cover
        raise CalendarExportError(
            "缺少 icalendar 依赖，请安装：pip install icalendar"
        ) from exc

    try:
        calendar = Calendar.from_ical(ics_text)
    except ValueError as exc:
        raise CalendarExportError(f"基线 ICS 无法解析：{exc}") from exc

    baselines: dict[str, EventBaseline] = {}
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("uid") or "").strip()
        if not uid:
            # 没有 UID 的事件无法对应到新导出，跳过（不视为错误——
            # 基线可能是别的工具生成的）。
            continue

        raw_sequence = component.get("sequence")
        try:
            sequence = int(raw_sequence) if raw_sequence is not None else 0
        except (TypeError, ValueError):
            sequence = 0

        last_modified_prop = component.get("last-modified")
        last_modified: datetime | None = getattr(last_modified_prop, "dt", None)
        if last_modified is not None and not isinstance(last_modified, datetime):
            # LAST-MODIFIED 按规范必须是 DATE-TIME；遇到异常值时丢弃而非报错。
            last_modified = None

        dtstart = getattr(component.get("dtstart"), "dt", None)
        dtend = getattr(component.get("dtend"), "dt", None)
        if not isinstance(dtstart, datetime) or not isinstance(dtend, datetime):
            # 全天事件（DATE 值）等本项目不会生成的形态：指纹无从谈起，跳过。
            continue

        fingerprint = event_fingerprint(
            summary=str(component.get("summary") or ""),
            location=str(component.get("location") or "") or None,
            description=str(component.get("description") or "") or None,
            start=dtstart,
            end=dtend,
        )
        baselines[uid] = EventBaseline(
            sequence=sequence,
            last_modified=last_modified,
            fingerprint=fingerprint,
        )
    return baselines


def resolve_sequence(
    event: CalendarEvent,
    baseline: Mapping[str, EventBaseline] | None,
    stamp: datetime,
) -> tuple[int, datetime]:
    """决定一条事件的 ``SEQUENCE`` 与 ``LAST-MODIFIED``。

    Parameters
    ----------
    event:
        本次导出的事件。
    baseline:
        旧 ICS 解析出的基线；``None`` 或空表示没有基线（全部按新事件）。
    stamp:
        本次导出时刻（``DTSTAMP`` 同源）。

    Returns
    -------
    tuple[int, datetime]
        ``(sequence, last_modified)``。``last_modified`` 恒为 UTC——
        RFC 5545 要求 LAST-MODIFIED 必须是 UTC 的 DATE-TIME。
    """
    fingerprint = event_fingerprint(
        summary=event.summary,
        location=event.location,
        description=event.description,
        start=event.start,
        end=event.end,
    )
    previous = baseline.get(event.uid) if baseline else None
    if previous is None:
        return 0, _to_utc(stamp)
    if previous.fingerprint == fingerprint:
        preserved = previous.last_modified or stamp
        return previous.sequence, _to_utc(preserved)
    return previous.sequence + 1, _to_utc(stamp)


def sequence_stats(
    events: list[CalendarEvent],
    baseline: Mapping[str, EventBaseline] | None,
) -> dict[str, int]:
    """统计本次导出相对基线的变更构成，供 CLI 展示。"""
    preserved = bumped = added = 0
    for event in events:
        previous = baseline.get(event.uid) if baseline else None
        if previous is None:
            added += 1
        elif event_fingerprint(
            summary=event.summary,
            location=event.location,
            description=event.description,
            start=event.start,
            end=event.end,
        ) == previous.fingerprint:
            preserved += 1
        else:
            bumped += 1
    return {"preserved": preserved, "updated": bumped, "added": added}


def _to_utc(moment: datetime) -> datetime:
    return moment.astimezone(UTC)
