"""CourseMeeting -> CalendarEvent -> iCalendar (.ics)

设计要点
--------
**不使用 RRULE 压缩整学期课程。**
原因：一次 ``FREQ=WEEKLY;COUNT=16`` 的重复规则只携带一个 DTSTART，
一旦学期中途切换冬/夏季作息，后续周次的实际钟点变化无法表达。
学校还可能存在单双周、不连续周、节假日停课、调课补课、换教室等情况。
因此本模块**为每一次实际上课生成一个独立 VEVENT**。
一学期几百个事件对现代日历应用完全无压力，换来的是逻辑可靠性。

**UID 必须稳定。** 同一门课的同一节课反复导出应得到同一个 UID，
这样才能做后续更新与去重。UID 由
``sha256(semester + course_key + 日期 + 节次)`` 派生。
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Sequence
from datetime import date, datetime

from .academic_calendar import AcademicCalendar
from .errors import CalendarExportError
from .models import CalendarEvent, CourseMeeting, UnsupportedAdjustment
from .periods import format_periods
from .schedules import ScheduleTable
from .timeutil import TZ_XIAN, now_local
from .weeks import format_weeks

__all__ = [
    "PRODID",
    "build_events",
    "collect_unsupported",
    "make_uid",
    "render_ics",
    "summarize",
]

PRODID = "-//xjtu-timetable-calendar//XJTU Personal Timetable Export//CN"

#: UID 的域名后缀，便于在日历客户端中识别来源
UID_DOMAIN = "xjtu-timetable-calendar"

#: 生产环境默认的日历名称
DEFAULT_CALENDAR_NAME = "西安交通大学课表"

_WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


def make_uid(
    semester_key: str,
    meeting: CourseMeeting,
    day: str,
) -> str:
    """为「某学期、某课程的某一次实际上课」生成稳定 UID。

    Parameters
    ----------
    semester_key:
        学期标识，例如 ``"2026-fall"``。
    meeting:
        课程安排。
    day:
        实际上课日期（ISO 字符串）。

    Notes
    -----
    刻意**不把时间和地点纳入哈希**：作息调整或换教室时，同一节课应当被视为
    「同一事件的新版本」，由日历客户端原地更新，而不是产生重复事件。

    Examples
    --------
    >>> from xjtu_calendar.models import CourseMeeting
    >>> m = CourseMeeting("DEMO-1001", "示例课程甲", 5, [1, 2], [1, 2, 3])
    >>> a = make_uid("2026-fall", m, "2026-09-11")
    >>> b = make_uid("2026-fall", m, "2026-09-11")
    >>> a == b
    True
    """
    periods_token = ",".join(str(p) for p in sorted(set(meeting.periods)))
    payload = "|".join(
        [
            semester_key.strip(),
            meeting.stable_course_key.strip(),
            meeting.course_name.strip(),
            day.strip(),
            periods_token,
        ]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{digest}@{UID_DOMAIN}"


def _build_description(meeting: CourseMeeting, week: int) -> str:
    """构造 DESCRIPTION：结构化罗列，不把信息塞进 SUMMARY。"""
    lines: list[str] = []
    if meeting.teacher:
        lines.append(f"教师：{meeting.teacher}")
    lines.append(f"教学周：{meeting.raw_week_text or format_weeks(meeting.weeks) + '周'}")
    lines.append(f"节次：{meeting.raw_period_text or format_periods(meeting.periods)}")
    lines.append(f"本周为本学期第 {week} 教学周")
    if meeting.course_id:
        lines.append(f"课程编号：{meeting.course_id}")
    return "\n".join(lines)


def _build_makeup_description(
    meeting: CourseMeeting,
    source_week: int,
    source_weekday: int,
    source_date: date,
) -> str:
    """构造调课事件的 DESCRIPTION。

    与普通事件的关键差异：不能写「本周为第 N 教学周」——target date 所在
    教学周与课程来源周通常不同（例如第 1 周周日上第 4 周周二的课），
    误导性的周次说明比没有说明更糟。
    """
    lines: list[str] = []
    if meeting.teacher:
        lines.append(f"教师：{meeting.teacher}")
    lines.append(f"教学周：{meeting.raw_week_text or format_weeks(meeting.weeks) + '周'}")
    lines.append(f"节次：{meeting.raw_period_text or format_periods(meeting.periods)}")
    lines.append(
        f"调课：本日按第 {source_week} 教学周星期{_WEEKDAY_NAMES[source_weekday - 1]}"
        f"（{source_date.isoformat()}）的课表上课"
    )
    if meeting.course_id:
        lines.append(f"课程编号：{meeting.course_id}")
    return "\n".join(lines)


def build_events(
    meetings: Sequence[CourseMeeting],
    calendar: AcademicCalendar,
    schedules: ScheduleTable,
    *,
    with_override_notes: bool = True,
) -> list[CalendarEvent]:
    """把课程安排展开成逐次上课的日历事件。

    处理流程：

    **第一遍（原生展开）**，对每个 :class:`CourseMeeting` 的每个教学周：

    0. **周次校验（fail-closed）**：``week < 1`` 或（``total_weeks`` 有值时）
       ``week > total_weeks`` 一律抛 :class:`CalendarExportError` 终止导出，
       **绝不静默跳过**。``total_weeks is None`` 时不做上限过滤，
       完全按 ``meeting.weeks`` 原样展开；
    1. ``week + weekday`` -> 具体日期（:class:`AcademicCalendar`）；
    2. 若该日期在停课集合中 -> 丢弃；
    3. 若该日期存在覆盖规则且命中本课程 -> 丢弃
       （调课日 ``source_date`` 隐含整日取消，原课程在此让位）；
    4. ``date`` -> 生效作息表（:class:`ScheduleTable`）；
    5. ``periods`` -> 实际起止时刻；
    6. 生成事件，UID 稳定可复现。

    **第二遍（调课展开）**，对每条 ``source_date`` 调课规则：

    7. 课程来源是 **source_date 所在教学周 + 星期** 的课程安排
       （不是 target date 自然星期——单双周 / 位掩码按 source 周解释）；
    8. 事件日期写 target date，钟点按 **target date 当天适用作息** 解析
       （例如 10-10 补 10-07 的课，下午第一节用冬春季作息 14:00）；
    9. UID 与普通事件同一规则（semester + course + 日期 + 节次），稳定可复现。

    两遍共用同一个 UID 去重集合，target date 不会出现重复事件。

    Parameters
    ----------
    meetings:
        标准化后的课程安排列表。
    calendar:
        教学日历（周 -> 日期映射 + 停课 / 调课规则）。
    schedules:
        作息表（日期 -> 作息 -> 节次钟点）。
    with_override_notes:
        是否把覆盖规则的备注写入 DESCRIPTION。

    Returns
    -------
    list[CalendarEvent]
        按开始时间升序排列的事件列表。
    """
    semester = calendar.semester
    total_weeks = semester.total_weeks
    events: list[CalendarEvent] = []
    seen_uids: set[str] = set()

    for meeting in meetings:
        sorted_periods = sorted(set(meeting.periods))

        for week in sorted(set(meeting.weeks)):
            # 周次合法性：绝不静默跳过。
            # 静默丢弃的后果是「ICS 生成成功、但悄悄缺课」——
            # 用户会拿着一份看起来正常、实则少了几周的日历去上课，
            # 这比直接报错危险得多。
            if week < 1:
                raise CalendarExportError(
                    f"课程「{meeting.course_name}」出现非法周次 {week}："
                    f"教学周必须 >= 1。请检查课表原始数据或 parser 的周次解析。"
                )
            if total_weeks is not None and week > total_weeks:
                raise CalendarExportError(
                    f"课程「{meeting.course_name}」的周次 {week} 超出学期总周数 "
                    f"{total_weeks}（学期：{semester.key}）。\n"
                    f"  可能原因：校历的 total_weeks 填小了，或课表确实包含超出该范围的周次。\n"
                    f"  处理方式：核对校历后填写正确的 total_weeks；"
                    f"若无法确认，把 total_weeks 留空（省略该字段），"
                    f"导出将完全按课表自身周次展开。"
                )

            day = calendar.week_to_date(week, meeting.weekday)

            if calendar.is_excluded(day):
                continue

            override = calendar.override_for(day)
            if override is not None and override.skips(meeting):
                continue

            try:
                start, end = schedules.resolve_period_time(day, sorted_periods)
            except Exception as exc:
                raise CalendarExportError(
                    f"解析 {day.isoformat()} 第 {sorted_periods} 节的上课时间失败："
                    f"{exc}（课程：{meeting.course_name}）"
                ) from exc

            uid = make_uid(semester.key, meeting, day.isoformat())
            if uid in seen_uids:
                # 同一课程同一天重复导入时去重，避免日历里出现双份
                continue
            seen_uids.add(uid)

            description = _build_description(meeting, week)
            if with_override_notes and override is not None and override.note:
                description = f"{description}\n备注：{override.note}"

            location = meeting.full_location
            if with_override_notes and override is not None and override.location:
                location = override.location

            events.append(
                CalendarEvent(
                    uid=uid,
                    summary=meeting.course_name.strip(),
                    start=start,
                    end=end,
                    location=location,
                    description=description,
                    meeting=meeting,
                )
            )

    # ---- 第二遍：调课日（source_date）展开 ----
    for day, override in calendar.makeup_rules():
        source = override.source_date
        if source is None:  # pragma: no cover - makeup_rules 已过滤
            continue
        source_week = calendar.date_to_week(source)
        source_weekday = source.isoweekday()
        # 防御性校验：from_dict 已挡住越界，这里兜底防止绕过加载器构造的对象。
        if source_week < 1:
            raise CalendarExportError(
                f"调课规则 {day.isoformat()} 的 source_date={source.isoformat()} "
                f"早于第 1 教学周星期一，无法换算教学周。"
            )
        total = semester.total_weeks
        if total is not None and source_week > total:
            raise CalendarExportError(
                f"调课规则 {day.isoformat()} 的 source_date={source.isoformat()} "
                f"落在第 {source_week} 教学周，超出学期总周数 {total}。"
            )

        for meeting in meetings:
            if meeting.weekday != source_weekday:
                continue
            if source_week not in meeting.weeks:
                continue

            sorted_periods = sorted(set(meeting.periods))
            try:
                start, end = schedules.resolve_period_time(day, sorted_periods)
            except Exception as exc:
                raise CalendarExportError(
                    f"解析调课日 {day.isoformat()} 第 {sorted_periods} 节的上课时间失败："
                    f"{exc}（课程：{meeting.course_name}）"
                ) from exc

            uid = make_uid(semester.key, meeting, day.isoformat())
            if uid in seen_uids:
                continue
            seen_uids.add(uid)

            description = _build_makeup_description(meeting, source_week, source_weekday, source)
            if with_override_notes and override.note:
                description = f"{description}\n备注：{override.note}"

            events.append(
                CalendarEvent(
                    uid=uid,
                    summary=meeting.course_name.strip(),
                    start=start,
                    end=end,
                    location=meeting.full_location,
                    description=description,
                    meeting=meeting,
                )
            )

    events.sort(key=lambda e: (e.start, e.summary))
    return events


def collect_unsupported(
    calendar: AcademicCalendar,
    events: Sequence[CalendarEvent],
) -> list[UnsupportedAdjustment]:
    """找出落在导出范围内、但本工具无法表达的调课安排。

    判定逻辑：声明了 ``unsupported_adjustments`` 且其日期落在
    本次事件的日期跨度内（事件为空时视为全部命中）。

    返回值交给调用方决定处置：默认 fail-closed，
    显式允许后转为显著警告。**本函数只报告，不虚构任何事件** ——
    不会为了「让日历看起来完整」而生成占位事件。
    """
    adjustments = calendar.unsupported_adjustments
    if not adjustments:
        return []

    if not events:
        return list(adjustments)

    lo = min(e.start.date() for e in events)
    hi = max(e.start.date() for e in events)
    return [a for a in adjustments if lo <= a.date <= hi]


def render_ics(
    events: Iterable[CalendarEvent],
    *,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    prodid: str = PRODID,
    dtstamp: datetime | None = None,
) -> str:
    """把事件序列渲染成符合 RFC 5545 的 iCalendar 文本。

    Parameters
    ----------
    events:
        日历事件。
    calendar_name:
        ``X-WR-CALNAME``，日历客户端中显示的名字。
    dtstamp:
        覆盖 DTSTAMP（测试用；生产环境留 ``None`` 即可）。

    Returns
    -------
    str
        以 ``\\r\\n`` 为行结束符的 ICS 文本（RFC 5545 要求 CRLF）。

    Notes
    -----
    使用成熟的 ``icalendar`` 库完成序列化与折行，不手写拼接器——
    ICS 的折行规则（75 字节）、转义规则（逗号、分号、反斜杠、换行）
    极易出错。库缺失时给出明确安装提示。
    """
    try:
        from icalendar import Calendar, Event
    except ImportError as exc:  # pragma: no cover
        raise CalendarExportError(
            "缺少 icalendar 依赖，请安装：pip install icalendar"
        ) from exc

    stamp = dtstamp or now_local()

    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", calendar_name)
    cal.add("x-wr-timezone", TZ_XIAN.tzname(None) or "Asia/Shanghai")

    for item in events:
        component = Event()
        component.add("uid", item.uid)
        component.add("dtstamp", stamp)
        component.add("dtstart", item.start)
        component.add("dtend", item.end)
        component.add("summary", item.summary)
        if item.location:
            component.add("location", item.location)
        if item.description:
            component.add("description", item.description)
        cal.add_component(component)

    # icalendar 未随包提供类型标注（mypy 视其为 Any），显式标注收窄返回值类型。
    # 运行时无任何变化：Calendar.to_ical() 恒返回 bytes。
    raw: bytes = cal.to_ical()
    return raw.decode("utf-8")


def summarize(meetings: Sequence[CourseMeeting], events: Sequence[CalendarEvent]) -> dict[str, object]:
    """汇总导出结果，供 CLI 展示。"""
    course_keys = {(m.course_id or m.course_name) for m in meetings}
    if events:
        first = min(e.start for e in events)
        last = max(e.end for e in events)
        date_range = f"{first.date().isoformat()} ~ {last.date().isoformat()}"
    else:
        date_range = "（无事件）"

    return {
        "courses": len(course_keys),
        "meetings": len(meetings),
        "events": len(events),
        "date_range": date_range,
    }
