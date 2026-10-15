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
import json
from collections.abc import Iterable, Mapping, MutableSet, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .academic_calendar import AcademicCalendar
from .config import Settings
from .errors import (
    CalendarExportError,
    MassCancellationBlocked,
    ScheduleNotConfigured,
    SemesterNotConfigured,
    TimetableFetchError,
    UnsupportedAdjustmentError,
    XjtuCalendarError,
)
from .logging_setup import get_logger
from .models import CalendarEvent, CourseMeeting, UnsupportedAdjustment
from .naming import DEFAULT_CALENDAR_NAME, calendar_title
from .parser import ParseReport, TimetableParser
from .periods import format_periods
from .schedules import ScheduleTable
from .sequence import EventBaseline, parse_baseline, resolve_sequence, sequence_stats
from .timeutil import TZ_XIAN, now_local
from .weeks import DEFAULT_EXPANSION_LIMIT, format_weeks

if TYPE_CHECKING:  # 仅为注解：exams.py 顶层 import exporter，运行时反向 import 会成环（R5）
    from .exams import LedgerEntry

__all__ = [
    "PRODID",
    "ExportResult",
    "MigrationSummary",
    "build_events",
    "build_ics_for_semester",
    "collect_unsupported",
    "make_uid",
    "render_ics",
    "summarize",
]

logger = get_logger()

PRODID = "-//xjtu-timetable-calendar//XJTU Personal Timetable Export//CN"

#: UID 的域名后缀，便于在日历客户端中识别来源
UID_DOMAIN = "xjtu-timetable-calendar"

# 默认日历名称与标题推导口径集中在 naming 模块（见 .naming 导入），
# exporter 只在渲染时取用；DEFAULT_CALENDAR_NAME 仍可从本模块导入，
# 以兼容既有 CLI / 测试引用。

_WEEKDAY_NAMES = ("一", "二", "三", "四", "五", "六", "日")


#: 课程 UID 的配方版本。v2（0.8.0）去掉了 `course_name`；v1 的形状只保留在文档里，
#: 代码不再能产出它——迁移读的是发布留底，不需要重算旧 UID（CD15）。
#: `subscribe` 状态文件的 `uid_recipe` 字段与 `doctor` 都读这个常量，别写第二处字面量。
UID_RECIPE_VERSION = 2


def make_uid(
    semester_key: str,
    meeting: CourseMeeting,
    day: str,
) -> str:
    """为「某学期、某课程的某一次实际上课」生成稳定 UID（配方 v2）。

    payload = ``semester_key | stable_course_key | day | periods_token``。
    刻意**不纳入**时间、地点与课程名：作息调整、换教室、**改名**都应当被视作
    「同一事件的新版本」由客户端原地更新（v0.8.0 起改名不再产生新事件，CD2）。

    v1 配方（≤ 0.7.x，形状存档于此，代码里已无对应实现）在 ``stable_course_key``
    之后多了一项 ``course_name.strip()``，即
    ``semester | key | name | day | periods``。v1→v2 的一次性迁移见
    docs/design/2026-10-12-course-cancellation-uid-v2.md §6.4：旧 UID 从发布留底
    里读，不重算。

    已知边界（CD16）：``course_id`` 缺失时 ``stable_course_key`` 退回
    ``f"name:{course_name}"``（models.py:263），那种行**仍**对改名敏感。

    Parameters
    ----------
    semester_key:
        学期标识，例如 ``"2026-fall"``。
    meeting:
        课程安排。
    day:
        实际上课日期（ISO 字符串）。

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
    collision_sink: MutableSet[str] | None = None,
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
    collision_sink:
        向后兼容的出参（默认 ``None``，行为与旧签名完全一致）：收集「重复 UID 却对应
        不同课程名」而被去重丢弃的那个 UID。v2 配方不含课程名，分层/分项教学班这类
        形状会同课程号、同天、同节次、异名撞出同一个 UID；行为仍与今天一致——
        **保留首个、丢弃后者**（产物字节不变），但碰撞必须冒给调用方，否则课程撤销
        通路会把"分不清哪行是哪门课"当成常态继续算候选。

    Returns
    -------
    list[CalendarEvent]
        按开始时间升序排列的事件列表。
    """
    semester = calendar.semester
    total_weeks = semester.total_weeks
    events: list[CalendarEvent] = []
    #: UID → 首个事件的 summary。命中重复 UID 且课程名不同 ⇒ 记入 collision_sink
    #: （纯重复导入同名去重不算碰撞），处置仍是保留首个、丢弃后者。
    seen_summaries: dict[str, str] = {}

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
            if uid in seen_summaries:
                # 同一课程同一天重复导入时去重，避免日历里出现双份。同名异名的处置一样
                # （保留首个、丢弃后者）；异名 ⇒ 碰撞，冒给 collision_sink（终审裁定的
                # 短路判据），同名 ⇒ 纯重复，不惊动任何通路。
                if (
                    seen_summaries[uid] != meeting.course_name.strip()
                    and collision_sink is not None
                ):
                    collision_sink.add(uid)
                continue
            seen_summaries[uid] = meeting.course_name.strip()

            description = _build_description(meeting, week)
            if with_override_notes and override is not None and override.note:
                description = f"{description}\n备注：{override.note}"

            # location 是业务事实，不受 with_override_notes 影响 ——
            # 那个开关只管 DESCRIPTION 里要不要追加备注，绝不能顺手关掉换教室。
            location = meeting.full_location
            if override is not None and override.location:
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
            if uid in seen_summaries:
                if (
                    seen_summaries[uid] != meeting.course_name.strip()
                    and collision_sink is not None
                ):
                    collision_sink.add(uid)
                continue
            seen_summaries[uid] = meeting.course_name.strip()

            description = _build_makeup_description(meeting, source_week, source_weekday, source)
            if with_override_notes and override.note:
                description = f"{description}\n备注：{override.note}"

            # 调课事件与普通事件使用同一套 location 语义：
            # overrides[target].location 是「这一天在哪里上课」的业务事实，
            # 必须覆盖 meeting 自带的地点（临时换教室正是调课最常见的场景）。
            location = meeting.full_location
            if override.location:
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

    events.sort(key=sort_key)
    return events


def collect_unsupported(
    calendar: AcademicCalendar,
    *,
    lower: date | None = None,
    upper: date | None = None,
) -> list[UnsupportedAdjustment]:
    """找出落在**导出范围**内、但本工具无法表达的调课安排。

    范围语义（关键修正）
    --------------------
    判定范围必须是「用户请求导出的日期范围」，而**不是**「最终生成出来的
    事件跨度」。旧实现从 ``events`` 取 ``min/max`` 日期当边界，会漏掉
    「首次上课之前」的特殊安排：例如学期 09-07 开始、第一门课 09-11 才上，
    那么 09-08 的调课声明落在事件跨度之外，会被静默漏报 —— 而漏报一条
    已知的调课，用户拿到的就是一份悄悄缺课的日历。

    Parameters
    ----------
    calendar:
        教学日历（提供 ``unsupported_adjustments`` 与学期边界）。
    lower / upper:
        调用方已知的导出边界（对应 CLI 的 ``--from-date`` / ``--to-date``）。
        为 ``None`` 的一侧回退到 :meth:`Semester.export_date_range`；
        仍无法确定时视为无界——**不确定范围时多报，不能漏报**。

    Returns
    -------
    list[UnsupportedAdjustment]
        落在范围内的声明；两侧都无界时返回全部。

    Notes
    -----
    本函数只报告，不虚构任何事件 —— 不会为了「让日历看起来完整」
    而生成占位事件。
    """
    adjustments = calendar.unsupported_adjustments
    if not adjustments:
        return []

    if lower is None or upper is None:
        semester_lower, semester_upper = calendar.semester.export_date_range()
        if lower is None:
            lower = semester_lower
        if upper is None:
            upper = semester_upper

    if lower is None and upper is None:
        # 学期边界不可知：safe-side，全部报出交用户判断。
        return list(adjustments)

    return [
        a
        for a in adjustments
        if (lower is None or a.date >= lower) and (upper is None or a.date <= upper)
    ]


def render_ics(
    events: Iterable[CalendarEvent],
    *,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    prodid: str = PRODID,
    dtstamp: datetime | None = None,
    baseline: Mapping[str, EventBaseline] | None = None,
) -> str:
    """把事件序列渲染成符合 RFC 5545 的 iCalendar 文本。

    Parameters
    ----------
    events:
        日历事件。可以是生成器 —— 函数入口会立刻物化，后续要遍历两次。
    calendar_name:
        ``X-WR-CALNAME``，日历客户端中显示的名字。
    dtstamp:
        覆盖 DTSTAMP（测试用；生产环境留 ``None`` 即可）。
    baseline:
        旧 ICS 解析出的 ``UID -> EventBaseline``（见 :mod:`xjtu_calendar.sequence`）。
        提供后，内容未变的事件保留原 ``SEQUENCE`` / ``LAST-MODIFIED``，
        内容变化的事件 ``SEQUENCE`` 递增——日历客户端据此正确执行原地更新。
        ``None`` 时全部事件按新增处理（``SEQUENCE: 0``）。

    Returns
    -------
    str
        以 ``\\r\\n`` 为行结束符的 ICS 文本（RFC 5545 要求 CRLF）。

    Notes
    -----
    1. 使用成熟的 ``icalendar`` 库完成序列化与折行，不手写拼接器——
       ICS 的折行规则（75 字节）、转义规则（逗号、分号、反斜杠、换行）
       极易出错。库缺失时给出明确安装提示。
    2. **时区定义（VTIMEZONE）**：RFC 5545 §3.2.19 规定

           "An individual 'VTIMEZONE' calendar component MUST be specified
            for each unique 'TZID' parameter value specified in the
            iCalendar object."

       本项目的事件引用 ``TZID=Asia/Shanghai``，因此必须内嵌对应的 VTIMEZONE；
       ``X-WR-TIMEZONE`` 只是给客户端的提示，**不能**替代它。
       定义由 ``icalendar`` 官方的 :meth:`Calendar.add_missing_timezones`
       生成，不手写（偏移与缩写极易写错）。该 API 需要 ``icalendar>=6.1.0``。
    """
    try:
        from icalendar import Calendar, Event
    except ImportError as exc:  # pragma: no cover
        raise CalendarExportError("缺少 icalendar 依赖，请安装：pip install icalendar") from exc

    # 入口处显式物化：调用方传 generator 时，第二次遍历会得到空集合
    # （事件一个都不写、VTIMEZONE 也因「无引用」被静默跳过）。
    items = list(events)

    stamp = dtstamp or now_local()

    cal = Calendar()
    cal.add("prodid", prodid)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", calendar_name)
    cal.add("x-wr-timezone", TZ_XIAN.tzname(None) or "Asia/Shanghai")

    for item in items:
        component = Event()
        component.add("uid", item.uid)
        component.add("dtstamp", stamp)
        sequence, last_modified = resolve_sequence(item, baseline, stamp)
        component.add("sequence", sequence)
        component.add("last-modified", last_modified)
        component.add("dtstart", item.start)
        component.add("dtend", item.end)
        if item.summary or item.status is None:
            # 守卫的是「空值行」而不是「撤销条目」：实测 `add("summary", "")` 会写出字面
            # `SUMMARY:` 空值行，所以只能不调用 add。判据是结构性的（终审 F5a），不是
            # "summary 恰好为空"——parser 允许纯空白 KCM 被 strip 成 ``""``、CLI 也存
            # ``course_name.strip()``，若只按 truthiness 判，课程侧"永远有 SUMMARY"就只是
            # 数据假设而非代码保证。
            # v0.6.1 之后撤销条目自带固定标题（`exams.CANCELLATION_SUMMARY`），走的是
            # `item.summary` 为真这一支；本条件因此与撤销无关，纯粹挡住任何空标题。
            component.add("summary", item.summary)
        if item.location:
            component.add("location", item.location)
        if item.description:
            component.add("description", item.description)
        if item.status:
            component.add("status", item.status)
        cal.add_component(component)

    # 所有 VEVENT 就位后补齐 VTIMEZONE（必须在 to_ical() 之前）。
    # 采用库默认的全量定义（1970~2038），**不按事件范围裁剪**：
    # Asia/Shanghai 自 1991 年起恒为 +08:00，组件只有一个 STANDARD，裁剪
    # 省不下什么；而个别客户端按 TZID 全局缓存时区定义，一份「只在 X~Y
    # 有效」的裁剪定义反而可能污染其他日历。
    # 空日历不引用任何 TZID，也就不需要（也不应凭空造出）VTIMEZONE。
    if items:
        cal.add_missing_timezones()

    # icalendar 未随包提供类型标注（mypy 视其为 Any），显式标注收窄返回值类型。
    # 运行时无任何变化：Calendar.to_ical() 恒返回 bytes。
    raw: bytes = cal.to_ical()
    return raw.decode("utf-8")


def summarize(
    meetings: Sequence[CourseMeeting], events: Sequence[CalendarEvent]
) -> dict[str, object]:
    """汇总导出结果，供 CLI 展示。

    起止按**日历日**给（`date_range` 只是展示，不进任何产物字节）。全天事件的 ``end``
    是不含的边界（RFC 5545 的 `DTEND`），所以最后一天要减一天才打得出"寒假到 2/28"；
    计时事件的 `end` 本来就落在最后一天那天上，直接取日期。
    """
    course_keys = {(m.course_id or m.course_name) for m in meetings}
    if events:
        first = min(event.start_date for event in events)
        last = max(_last_day(event) for event in events)
        date_range = f"{first.isoformat()} ~ {last.isoformat()}"
    else:
        date_range = "（无事件）"

    return {
        "courses": len(course_keys),
        "meetings": len(meetings),
        "events": len(events),
        "date_range": date_range,
    }


@dataclass(frozen=True)
class MigrationSummary:
    """v1→v2 迁移那一次全量撤销的规模（CD11），经 `ExportResult.migration_summary` 交给 CLI。

    迁移是配方切换而不是数据缺失，规模天然达到整学期量级——所以它**豁免**门 2 的熔断
    （喂进去会把升级永远掐死），但必须让 CLI 有机会打印一行预告（「自动但不静默」）。
    只报数量与占比，不含任何课程名（§8）。
    """

    candidates: int
    ratio: float


@dataclass
class ExportResult:
    """build_ics_for_semester 的返回：渲染文本 + 汇总数据（CLI 打印用）。

    ``info`` 中与考试相关的键有两个：``exam_events`` 是**进了产物**的 live 考试事件数
    （已过日期过滤与全局 UID 断言）；``exam_cancellations`` 是本次实际下发的撤销条数
    ——撤销条目**不进** ``sequence_stats``（spec D23），数量只在这里体现。
    ``events`` 与 ``date_range`` 的口径始终只算课程事件，见设计文档 §6.5 第 1 条。
    ``info["course_cancellations"]`` 与 ``exam_cancellations`` 同口径（D23/CD14）：
    只数**真正进了产物**的课程撤销条目（含迁移支），不计撞 UID 被丢弃与 unresolvable 的。
    """

    ics: str
    info: dict[str, object]
    sequence_stats: dict[str, int] | None
    #: 本次应回写的考试台账文本；空值口径见 spec §6.6：``None`` ⟺ 撤销门槛没过
    #: （D18，`--no-exams` 同形）、或既没读到台账也没有任何 live 考试 —— 此时调用方
    #: **不得创建也不得改写**该文件。读到过台账就返回文本（可能已把过期条目剪光），
    #: 不存在"算出空台账但不回写"的中间状态。live 侧取**未经日期过滤**的考试集（D19）。
    #: 本字段只**产出**文本；落盘是 Task 9/10 的 CLI 职责（exporter 绝不写文件）。
    exam_ledger_text: str | None = None
    #: 课程台账文本，与 ``exam_ledger_text`` 同形（CD8 的两份台账各自独立回写）：
    #: ``None`` ⟺ 门 1（CD13）没过；live 侧取**未经日期过滤**的课程事件集（CD14）。
    #: 同样只产出文本，落盘是 CLI 的职责。
    course_ledger_text: str | None = None
    #: 迁移分支的规模（CD11）；``None`` ⟺ 本次没走迁移（CLI 没传留底，或留底里没有
    #: 可撤的旧 UID）。构建层**只算不写**——`uid_recipe` 标记的回写只发生在发布成功
    #: 之后（CD7），那是 Task 8/9 的 CLI 职责。
    migration_summary: MigrationSummary | None = None


def _stamp_from_snapshot(path: Path) -> datetime:
    """数据源文件的 mtime（aware UTC）作为默认 DTSTAMP；不可读时回退挂钟。

    subscribe publish 的幂等跳过（spec §7「内容无变化→跳过」）依赖**内容哈希
    跨进程稳定**：若 DTSTAMP 取 ``now_local()``，同一快照每次重建字节都不同，
    跳过分支在生产中永远命中不了。收敛到快照 mtime 后，「同一快照 + 同一配置
    → 字节一致」成立。:func:`render_ics` 里所有非确定字段（DTSTAMP、以及
    :func:`~xjtu_calendar.sequence.resolve_sequence` 给新事件的 LAST-MODIFIED）
    都由这一个 stamp 派生，别处无需再钉时钟。
    """
    try:
        return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except OSError:  # 快照在 stat 前消失等极端情形：回退挂钟，渲染仍 fail-open
        return now_local()


def sort_key(event: CalendarEvent) -> tuple[date, int, date | datetime, str]:
    """产物内的统一事件顺序：**同一天里全天事件排在计时事件之前**。

    为什么不能沿用 `(e.start, e.summary)`：`date` 与 `datetime` 之间**不可比较**
    （`TypeError: can't compare datetime.datetime to datetime.date`），一旦课程/考试
    （DATE-TIME）与校历事件（DATE）同批进产物，原写法会当场炸。
    三段键的构造保证同一形态内部比较项仍是原类型：

    1. 开始那天（两种形态都取得出来）；
    2. 全天 = 0、计时 = 1 ⇒ 同一天里整天块置顶，这也符合客户端惯例；
    3. 组内比较项：**计时事件用 `start`（与旧键 `(e.start, e.summary)` 完全同序）**，
       全天事件用不含的 `end`（同日起点相同的长事件之间才有稳定次序）。两组的第 2 位不同，
       元组比较在第 2 位就分出胜负、**永远不会拿 `date` 去和 `datetime` 比**。

    ⚠️ 对既有的纯 DATE-TIME 产物，这个键与 `(e.start, e.summary)` 给出**完全相同**的顺序
    ⇒ 不改动任何已发布字节，由两支 Golden 用例守着。
    """
    return (
        event.start_date,
        0 if event.is_all_day else 1,
        event.end if event.is_all_day else event.start,
        event.summary,
    )


def _last_day(event: CalendarEvent) -> date:
    """事件实际占用的**最后一天**。

    全天事件的 ``end`` 是不含的边界（§12.1），所以要减一天；计时事件的 `end` 就是
    下课那一刻，日期本身即最后一天。判据用 `isinstance(..., datetime)` 而不是
    `event.is_all_day`：两者由构造期的"起止同形态"约束保证等价，而这一条不依赖别的字段。
    """
    end = event.end
    if isinstance(end, datetime):
        return end.date()
    return end - timedelta(days=1)


def _in_range(event: CalendarEvent, lower: date | None, upper: date | None) -> bool:
    """事件的日期是否落在 ``[lower, upper]`` 闭区间内（与旧内联条件等价）。

    抽成模块级谓词的唯一理由：课程事件与考试事件现在**共用同一组边界**
    （§6.5 第 4 条：``--from-date``/``--to-date`` 连考试一起裁），
    两处各写一遍边界判断迟早会漂移。
    """
    day = event.start_date
    return (lower is None or day >= lower) and (upper is None or day <= upper)


def _cancellation_baseline_uids(baseline: dict[str, EventBaseline] | None) -> set[str]:
    """D20 可发性判定用的「上次发布产物 UID 集」（§6.5:210-212、§7:267）。

    只有**真读出来过一份基线**（``baseline`` 非 ``None``）时，才谈得上"这条考试确实发布过、
    能安全地给它发一条撤销"。``baseline`` 为 ``None``——压根没配基线来源、配了却没读出来
    （``--no-sequence``、留底缺失/不可解析）、或上次发布来自 ``--no-exams``——一律返回**空集**：
    没有任何发布凭据 ⇒ 每个候选都进 ``unresolvable``、一条都不撤（终审 F2）。

    早先"没配基线来源就以台账为凭据全可下发"的分支是**错**的：台账如今也被 ``export`` 写，
    一次 ``subscribe init`` + 首屏 ``push``（没有 ``last_push``、也没有留底可读）会拿本地导出
    攒下的台账，给一个从没收到过该考试的订阅者发一条无标题的 ``STATUS:CANCELLED``。没有基线
    可对照，就不是"发布过"的证据。生产里 ``export`` 恒传 ``-o`` 路径、push/rotate 恒传留底，
    所以本口径只影响"确实没得对照"的场景。
    """
    if baseline is not None:
        return set(baseline)
    return set()


def _append_cancellation_events(
    events: Sequence[CalendarEvent],
    pending: Sequence[LedgerEntry],
    render_events: list[CalendarEvent],
) -> tuple[list[CalendarEvent], list[LedgerEntry], list[CalendarEvent]]:
    """撤销条目并入产物前的全局 UID 唯一性断言（课程支用；§9 第 15 条）。

    与考试撤销块里的内层循环**同形**：撞车的条目丢弃 + warning，且**不进**台账 pending
    （撞车下轮照撞，留下就是兑现不了的承诺）。事件与条目按位置配对（
    `build_cancellation_events` 保持调用方全序），``strict=True`` 给配对上锁。
    返回 ``(保留的撤销事件, 对应台账条目, 新的 render_events)``。
    """
    seen_uids = {event.uid for event in render_events}
    kept_events: list[CalendarEvent] = []
    kept_pending: list[LedgerEntry] = []
    for event, entry in zip(events, pending, strict=True):
        if event.uid in seen_uids:
            logger.warning("撤销事件 UID 与已有事件冲突，已丢弃：%s", event.uid)
            continue
        seen_uids.add(event.uid)
        kept_events.append(event)
        kept_pending.append(entry)
    return kept_events, kept_pending, [*render_events, *kept_events]


def build_ics_for_semester(
    cfg: Settings,
    semester: str,
    *,
    input_path: str | None = None,
    calendar_config: str | None = None,
    schedule_config: str | None = None,
    calendar_name: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    allow_unsupported_adjustments: bool = False,
    sequence_from: str | None = None,
    baseline_probe: str | None = None,
    no_sequence: bool = False,
    dtstamp: datetime | None = None,
    include_exams: bool = True,
    exams_payload: dict[str, Any] | None = None,
    cancel_expiry_at: datetime | None = None,
    allow_mass_cancellation: bool = False,
    published_baseline_text: str | None = None,
) -> ExportResult:
    """按学期构建 RFC 5545 文本。export 与 subscribe push 共用的唯一管线。

    ``calendar_name``：``None`` 表示由课表数据自动推导日历标题
    （「西安交通大学课表 · 大三-上」，见 :mod:`xjtu_calendar.naming`）；显式给定
    则原样使用、不追加任何后缀。标题只落在 ``X-WR-CALNAME``，不进 UID/VEVENT，
    因此订阅端不会因标题变化而重收事件。

    ``baseline_probe``：SEQUENCE 自动探测的「旧版本」路径（CLI export 传 -o
    输出路径；subscribe 传上次发布留底）。显式 ``sequence_from`` 优先。

    ``dtstamp`` 为 ``None`` 时默认取**数据源快照的 mtime**（见
    :func:`_stamp_from_snapshot`），保证同快照重建字节一致——subscribe
    的「内容无变化→跳过」判定全靠这一点。

    ``include_exams``：是否把同学期的考试安排并进**同一份** .ics（D1/D2，默认开）。
    关掉只影响导出，**不删**本地考试快照。考试侧任何失败都只降级（少一批事件 +
    留下一条日志），既不改变课程事件的口径，也绝不让导出非零退出（§7）。

    ``exams_payload``：直接给定考试响应、跳过读快照文件——§6.5 点名的注入接缝，
    当前主要供测试与后续任务使用；``include_exams=False`` 时一并忽略。
    注意 ``input_path`` **只喂课表 payload**，考试数据永远不从它读（§6.5 第 3 条）。

    ``cancel_expiry_at``：撤销保留期比较用的墙钟；``None`` 取 ``datetime.now(TZ_XIAN)``
    （D9）。只有测试注入，**不暴露成 CLI 旗标**，也不参与 ``--from-date/--to-date``
    窗口过滤（撤销条目本就在过滤之后单独注入，见 §6.6）。

    ``allow_mass_cancellation``：门 2（CD12）的显式放行旗标，对应 CLI 的
    ``--allow-mass-cancellation``。默认 ``False`` 时课程侧熔断候选达标会抛
    :class:`~xjtu_calendar.errors.MassCancellationBlocked`（退出码 6、零写盘），
    **不会**被考试撤销块那种「任何异常降级为不撤销」的口径吞掉（CD4 fail-closed）。

    ``published_baseline_text``：发布留底（上次发布产物）的**文本**——CLI 从
    ``last-<学期>.ics`` 读出后，仅当订阅状态缺 ``uid_recipe``（升级后第一次 push）时
    传入；``export`` 路径永远不传（CD3：迁移撤销只随 push 下发）。传了 ⇒ 走迁移分支
    （§5：候选 = 留底 UID − live − 本次常态撤销，再过 D2 过期筛，CD10），该分支
    **豁免**门 2 但规模经 ``migration_summary`` 冒出来给 CLI 打印预告（CD11）。
    exporter 不读写任何文件：留底读取与状态标记回写都是 CLI 的职责。
    """
    from .fetcher import load_raw

    # --- 课表数据 ---
    if input_path:
        source = Path(input_path)
        if not source.is_file():
            raise XjtuCalendarError(f"课表文件不存在：{source}")
        payload = json.loads(source.read_text(encoding="utf-8"))
        stamp_source: Path | None = source
    else:
        payload = load_raw(cfg, semester)
        # load_raw 的落盘位置就是默认 DTSTAMP 的来源；若 fetcher 口径变动需同步。
        stamp_source = cfg.raw_timetable_path(semester)

    # --- 教学日历 ---
    calendar_path = Path(calendar_config) if calendar_config else cfg.semester_config_path(semester)
    if not calendar_path.is_file():
        raise SemesterNotConfigured(
            f"未找到学期 {semester} 的教学日历：{calendar_path}",
            hint="请参考 README「必须由你配置的内容 → 1. 教学日历（校历）」小节的模板创建该文件。"
            "注意：第 1 教学周的星期一等日期必须来自官方校历，不要凭空填写。",
        )
    academic = AcademicCalendar.from_file(calendar_path)
    logger.info("教学日历：%s", academic.semester.name)

    # --- 作息表 ---
    schedule_path = Path(schedule_config) if schedule_config else cfg.schedule_config_path()
    if not schedule_path.is_file():
        raise ScheduleNotConfigured(
            f"未找到作息表配置：{schedule_path}",
            hint="请参考 README「必须由你配置的内容 → 2. 作息表」小节的模板创建该文件。"
            "注意：本项目不内置任何未经官方确认的作息时间，必须由你提供。",
        )
    schedules = ScheduleTable.from_file(schedule_path)

    problems = schedules.validate()
    for problem in problems:
        logger.warning("作息表配置问题：%s", problem)

    logger.info("作息表：%d 套作息、%d 个生效区间", len(schedules.profiles), len(schedules.periods))

    # --- 解析 ---
    # total_weeks 可省略：校历没给就用解析侧的默认安全上限。
    # 注意别把「解析边界」和「导出校验」混为一谈 ——
    # 导出侧的越界判定在 build_events 里独立进行，且是 fail-closed。
    parser = TimetableParser(
        expansion_limit=academic.semester.total_weeks or DEFAULT_EXPANSION_LIMIT
    )
    courses, meetings = parser.parse(payload)
    logger.info("已解析：%d 门课程、%d 条课程安排", len(courses), len(meetings))
    logger.info("解析报告：%s", parser.report.summary())
    for warning in parser.report.warnings:
        logger.warning("%s", warning)
    for skip in parser.report.skipped:
        logger.warning("跳过：%s", skip)

    if not meetings and parser.report.total_candidates == 0:
        # 「响应里根本没有课程记录」与「有记录但字段没对上」是两件事：
        # 前者是该学期真的没课，后者是适配问题，不能笼统报同一个错。
        logger.warning("课表为空：响应中没有找到任何课程记录，将生成不含事件的日历")
    elif not meetings:
        raise XjtuCalendarError(
            f"响应里有 {parser.report.total_candidates} 条候选记录，但没有一条能解析出课程安排",
            hint="字段映射很可能与实际响应不符。请运行 inspect 子命令查看脱敏结构，"
            "并把真实字段名补进 parser.py 的 FIELD_CANDIDATES。",
        )

    # --- 展开（课程）---
    #: v2 配方不含课程名：同课程号+同一天+同节次而课程名不同 ⇒ 同 UID（分层/分项教学班
    #: 形状）。碰撞意味着"分不清哪行是哪门课"，此时任何撤销判断都不可信——收集起来，
    #: 下面用它把整条课程撤销通路短路成不撤销。调课第二遍在 build_events 内共用同一个 sink。
    course_uid_collisions: set[str] = set()
    events = build_events(meetings, academic, schedules, collision_sink=course_uid_collisions)
    logger.info("已展开：%d 次实际上课", len(events))

    # --- 考试事件（并进**日期过滤之前**；§6.5 第 1 条）---
    # 失败一律降级（§7:387「绝不因为考试失败而让 export 非零退出」）：
    # 课程是主功能，考试是增量，增量出问题时产物必须等于「没有增量」。
    exam_events: list[CalendarEvent] = []
    #: 撤销通路的准入门槛（spec D18）：只有"快照读到、解析没炸、且不是一批全失败"才可信。
    #: `live = ∅` 在撤销语义下是"取消整学期"，所以这里必须是**白名单**而不是默认放开。
    exams_trusted = False
    if include_exams:
        # 只能在函数体内 import：exams.py 顶层 `from .exporter import UID_DOMAIN`，
        # 反向的模块级 import 会成环（计划的 Global Constraint；`load_raw` 同此写法）。
        from .exams import build_exam_events, campus_names_from_timetable, parse_exam_rows

        try:
            try:
                exams_source: dict[str, Any] | None = (
                    exams_payload
                    if exams_payload is not None
                    else load_raw(cfg, semester, kind="exams")
                )
            except TimetableFetchError:
                # 从没抓过考试不是错误（D2「静默跳过，只在日志说明」——
                # 是「不报警」，不是「一个字都不说」）。
                exams_source = None
                logger.info("本地没有 %s 的考试快照，本次日历不含考试事件", semester)
            if exams_source is not None:
                report = ParseReport()
                parsed = parse_exam_rows(
                    exams_source,
                    campus_names=campus_names_from_timetable(payload),
                    report=report,
                )
                for line in report.skipped:
                    # 逐条打印的是**已在源头脱敏**的文案（只有行号 + 字段名，§8）：
                    # 保留逐条是因为用户要按行号回本地快照定位坏掉的是哪一场。
                    # 看守：`tests/test_exporter_exams.py::test_exam_log_lines_never_carry_original_text`。
                    logger.warning("考试安排跳过一条：%s", line)
                for line in report.warnings:  # 星期与日期不一致这类"保留但可疑"的提示
                    logger.warning("考试安排提醒：%s", line)
                exam_events = build_exam_events(parsed, semester)
                # 撤销通路的可信判据（spec D18 + 终审 F1）：不是"读到了且没炸"就够，
                # 而是"快照里的**每一行**都被产物代表"。`parse_exam_rows`（缺 KCM / 时间
                # 文本解析不出 / 缺 KSRQ）与 `build_exam_events`（`date.fromisoformat` 抛）
                # 都**逐行**跳过，所以"某一场考试的行本次坏了、其它场正常"时那一场会缺席
                # `live_exam_uids` ⇒ 旧实现把它当成"消失"发撤销 ⇒ 从所有订阅者日历里删掉一条
                # 仍然存在的考试，比这条特性要消除的 v0.5 ghost 更糟。判据：`report.skipped`
                # 非空 或 产物考试事件数 < 原始行数 ⇒ 已解析的事件照发，但**本次关闭撤销通路**。
                # NO_EXAMS（`total_candidates == 0`）不触发（0 < 0 = False、无 skip）仍可信。
                exams_trusted = not report.skipped and len(exam_events) >= report.total_candidates
                if not exams_trusted:
                    # 只报计数，绝不带考试名/考场/座位（spec §8）。
                    if not exam_events and report.total_candidates:
                        logger.warning(
                            "考试快照有 %d 行但一条都没解析出来，本次不启用撤销通路",
                            report.total_candidates,
                        )
                    else:
                        logger.warning(
                            "考试快照有 %d 行、只构建了 %d 场考试（跳过 %d 行），"
                            "本次不启用撤销通路",
                            report.total_candidates,
                            len(exam_events),
                            len(report.skipped),
                        )
                elif not exam_events:
                    logger.info("本学期暂无考试安排（考试快照为空）")
        except Exception as exc:  # 快照半截损坏、结构走样等一切意外
            # 计划稿只包 `load_raw`，与它自己「失败一律静默降级」的注释不符：
            # `load_raw` 里的 `json.loads` 抛 JSONDecodeError（ValueError 子类），
            # 不是 TimetableFetchError，照样能炸穿导出。整段兜住才对得上 §7:387。
            # 只记异常类名，不记 `str(exc)`：icalendar/`json.loads`/`UnicodeDecodeError` 的
            # 消息会逐字引用坏字节或考试原文，属 §8 禁止进日志的来源数据（与台账各路径同口径）。
            logger.warning("考试快照无法处理，本次日历不含考试事件（%s）", type(exc).__name__)
            exam_events = []
            exams_trusted = False  # D18：炸了就收回撤销权

    # --- 可选日期过滤 ---
    #: 撤销候选要用**未经日期过滤**的 live 集（spec D19）：下面的 `--from-date/--to-date`
    #: 会连考试一起裁，用过滤后的集合就会把"窗口外"当成"已消失"而撤销并剪台账。
    #: 台账回写也用同一份**未过滤**的事件列表，两处必须同源（Task 8 的 `live=` 参数）。
    pre_filter_exam_events = list(exam_events)
    live_exam_uids = {event.uid for event in exam_events}
    #: 课程撤销候选与台账回写都必须吃**未经日期过滤**的事件（CD14，D19 的对应物）。
    #: 裁剪是课程侧的常态操作，用过滤后的集合会把窗口外的课当成"消失"并撤销、
    #: 还从台账剪掉——一次局部导出动了全局订阅状态。
    pre_filter_course_events = list(events)
    live_course_uids = {event.uid for event in events}
    # 先把边界解析出来：它同时决定「事件过滤」与「unsupported 调课的范围判定」，
    # 两处必须用同一组边界，否则会出现「事件被裁掉、缺课告警却没报」。
    lower = date.fromisoformat(from_date) if from_date else None
    upper = date.fromisoformat(to_date) if to_date else None
    if lower is not None or upper is not None:
        # v1 决定（§6.5 第 4 条）：考试与课程用**同一组**边界一起裁，不特殊放行。
        events = [e for e in events if _in_range(e, lower, upper)]
        exam_events = [e for e in exam_events if _in_range(e, lower, upper)]
        logger.info("按日期过滤后剩余 %d 次上课", len(events))

    # --- 全局 UID 唯一性断言（§6.5 第 2 条）---
    # `build_events` 里的 `seen_uids` 只在课程侧生效（:213），管不到从外面并进来的
    # 考试事件；而 `render_ics` 是直接 `add_component`，重复 UID 会原样写进 .ics，
    # 违反 RFC 5545 且客户端行为不可预期。处置：丢弃后来者 + warning，**不中止导出**。
    kept_exam: list[CalendarEvent] = []
    if exam_events:
        seen = {event.uid for event in events}
        for event in exam_events:
            if event.uid in seen:
                # 只报 UID（§8，与课程侧撤销的丢弃日志同口径）：`event.summary` 是考试原名。
                logger.warning("考试事件 UID 与已有事件冲突，已丢弃：%s", event.uid)
                continue
            seen.add(event.uid)
            kept_exam.append(event)

    render_events = [*events, *kept_exam]
    if not render_events:
        # 判空看的是**合并后**的集合：日期窗口只框住考试时课程为零，但产物并不空，
        # 这时候甩一句「没有生成任何事件」就是假警告。
        logger.warning("没有生成任何事件（可能全部落在停课日期或被日期过滤排除）")

    # --- 无法表达的调课：fail-closed（显式允许后转为显著警告） ---
    # 范围来自「用户请求导出的范围」，不是 events 的日期跨度：
    # 首次上课之前的特殊安排同样必须被捕获。
    unsupported = collect_unsupported(academic, lower=lower, upper=upper)
    if unsupported:
        lines = [f"  {a.date.isoformat()}：{a.description}" for a in unsupported]
        if not allow_unsupported_adjustments:
            raise UnsupportedAdjustmentError(
                f"教学日历声明了 {len(unsupported)} 条本工具无法表达的调课，"
                f"它们落在本次导出范围内：\n" + "\n".join(lines) + "\n"
                "继续导出将得到一份**缺少这些时段**的日历。"
            )
        for line in lines:
            logger.warning("⚠️ 未表达的调课（该时段事件缺失）：%s", line)

    # --- 渲染 ---
    # SEQUENCE / LAST-MODIFIED 基线：
    #   显式 --sequence-from 指定；否则自动探测输出文件的旧版本；
    #   --no-sequence 关闭。基线让日历客户端能区分「没变」与「变了」，
    #   避免重新导入时更新被忽略或产生全量「已更新」噪音。
    baseline: dict[str, EventBaseline] | None = None
    if not no_sequence:
        explicit = Path(sequence_from) if sequence_from else None
        auto = Path(baseline_probe) if baseline_probe else None
        baseline_path = explicit or auto
        if baseline_path is not None and baseline_path.is_file():
            if explicit is not None:
                baseline = parse_baseline(baseline_path.read_text(encoding="utf-8"))
            else:
                try:
                    baseline = parse_baseline(baseline_path.read_text(encoding="utf-8"))
                except XjtuCalendarError as exc:
                    # 自动探测的基线解析失败：降级为空基线（全部按新增），
                    # 不阻断导出——ICS 内容本身不会因此出错。
                    logger.warning("自动探测的基线 ICS 不可用（%s），事件将全部按新增处理", exc)
                    baseline = None
            if baseline:
                logger.info("SEQUENCE 基线：%s（%d 个事件）", baseline_path, len(baseline))

    # DTSTAMP/LAST-MODIFIED 的来源 stamp 先算出来：撤销通路（下面的 try）里的
    # `render_exam_ledger` 与产物渲染都吃同一个 stamp，两处必须同源。
    stamp = dtstamp or _stamp_from_snapshot(stamp_source or cfg.raw_timetable_path(semester))

    # --- 撤销注入 + 台账文本（docs/design/2026-10-09-exam-cancellation.md §6.5）---
    # 门槛（D18/F1）没过、或 `--no-exams`（D4/D21）时整段跳过：不读台账、不算撤销、
    # 不产出台账文本。所有跨分支读取的标志先在块外初始化，保证之后的
    # `ExportResult` / `info` 读它们时永远有值（评审 R4）。
    # 整段（候选数学 + 台账文本序列化）都包在 try/except 里：读台账的 OSError/
    # UnicodeDecodeError 早已就地兜住，但把一份手改/外来台账交给 icalendar 序列化时冒出的
    # 意外（发不出的 DTSTART 时区、折不了的属性）会从这里冒出 `build_ics_for_semester`，
    # 把一个考试侧问题变成 export/push 的非零退出——违反红线（spec §1、§7）。任何此类异常
    # ⇒ 只记 `type(exc).__name__`、撤销条目与台账文本清空、`render_events` 退回撤销前（终审 F4）。
    cancellation_events: list[CalendarEvent] = []
    pending_entries: list[LedgerEntry] = []
    ledger_exists = False
    exam_ledger_text: str | None = None
    if include_exams and exams_trusted:
        pre_cancellation_render_events = render_events
        try:
            # 运行时 import 留在函数体内：exams.py 顶层 `from .exporter import ...`，反向成环
            # （循环依赖规则）。类型注解用的 `LedgerEntry` 由文件顶部 TYPE_CHECKING 分支提供（R5）。
            from .exams import build_cancellation_events, cancel_candidates, load_exam_ledger

            ledger_path = cfg.exam_ledger_path(semester)
            ledger_exists = ledger_path.is_file()
            ledger: dict[str, LedgerEntry] = {}
            if ledger_exists:
                try:
                    ledger = load_exam_ledger(ledger_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError) as exc:
                    # 台账是二进制/坏编码字节时 `read_text` 抛 UnicodeDecodeError，目录权限问题时
                    # 抛 OSError；这一段**不在**外层考试事件 try 的保护范围内，不就地兜住就会冒出
                    # `build_ics_for_semester` ⇒ 一个考试侧问题把 export 变成非零退出（spec D15/§7
                    # 红线）。就地降级成"当作没有台账"：`ledger` 留空 ⇒ 本次不撤销任何考试。但
                    # `ledger_exists` 仍为 True，下面的空值口径照旧用本次 live 考试产出
                    # `exam_ledger_text`，让调用方**覆盖这份读不出的坏台账**（§7：损坏 ⇒ warning +
                    # 当作无台账、产物正常；覆盖正是期望的修复动作）。warning 只报学期与失败种类，
                    # 绝不带台账内容（spec §8「日志不打印台账内容」，而异常原文会逐字引用坏字节）。
                    logger.warning(
                        "考试台账 %s 读不出（%s），本次当作无台账处理并用当前考试覆盖它",
                        semester,
                        type(exc).__name__,
                    )
            else:
                logger.info("本地还没有 %s 的考试台账，本次不撤销任何已发布考试", semester)
            if ledger:
                deliverable, unresolvable = cancel_candidates(
                    ledger=ledger,
                    live_uids=live_exam_uids,
                    baseline_uids=_cancellation_baseline_uids(baseline),
                    now=cancel_expiry_at or now_local(),
                )
                # D20：留底/基线里没有该 UID ⇒ 客户端握着更高序号会忽略 `SEQUENCE:0` 的撤销，
                # 发了等于没发；宁可不撤销。只报 UID，不带个人数据。文案说明**本次渲染**的决定，
                # 且诚实交代两半：本次没发、但**留在台账里**等它自己时刻过去（终审 F3——判定
                # "不可下发"的是这一次的调用方，一次没发布的渲染不该永久毁掉下次 push 撤销它的能力）。
                for item in unresolvable:
                    logger.warning(
                        "考试 %s 无法安全下发撤销（发布留底里没有这个 UID，序号只能从 0 起，"
                        "客户端会忽略更低的序号），本次不随产物下发这条撤销，"
                        "留在台账里直到它原定时刻自然过去",
                        item.uid,
                    )
                cancellation_events = build_cancellation_events(deliverable)
                # 全局 UID 唯一性：撤销条目**不豁免**，处置与考试事件一致（丢弃后来者 + warning），
                # 写法与本函数上面的 `kept_exam` 同形。
                seen_uids = {event.uid for event in render_events}
                kept_cancellations: list[CalendarEvent] = []
                kept_pending: list[LedgerEntry] = []
                # `build_cancellation_events` 保持调用方的全序、不重排（exams.build_cancellation_events），
                # 所以事件与条目可以按位置配对；`strict=True` 给配对上锁。
                for event, entry in zip(cancellation_events, deliverable, strict=True):
                    if event.uid in seen_uids:
                        logger.warning("撤销事件 UID 与已有事件冲突，已丢弃：%s", event.uid)
                        continue
                    seen_uids.add(event.uid)
                    kept_cancellations.append(event)
                    kept_pending.append(entry)
                cancellation_events = kept_cancellations
                # 台账 pending = **实际下发**的撤销 + **unresolvable**（终审 F3）：撞车被丢弃的撤销
                # 随丢弃退场（撞车下轮照撞，留在台账是兑现不了的承诺）；但 unresolvable 要留着排队，
                # 下一次有基线可对照的渲染就能真正撤销它。两类都自限：各自的 DTSTART 一过就被 D2 剪掉。
                pending_entries = [*kept_pending, *unresolvable]
                render_events = [*render_events, *cancellation_events]

            #: 台账文本（§6.6 空值口径）：门槛过了（`exams_trusted` 蕴含 `include_exams`）**且**
            #: 要么本地有台账文件、要么本次有 live 考试 —— 就要产出文本。全新订阅者（有 live 考试、
            #: 还没有台账文件）也必须拿到文本，否则 Task 9/10 只在 `text is not None` 时落盘建文件，
            #: 撤销通路对首屏用户直接死掉。反过来：门槛没过、`--no-exams`、既没读到台账又没 live 考试
            #: ⇒ None，调用方不得建文件。live 侧用**未经日期过滤**的 `pre_filter_exam_events`
            #: （D19，与候选同源），pending 侧用可下发候选 + unresolvable；本函数**不落盘**。
            if ledger_exists or pre_filter_exam_events:
                from .exams import render_exam_ledger

                exam_ledger_text = render_exam_ledger(
                    pre_filter_exam_events, pending_entries, dtstamp=stamp
                )
        except Exception as exc:
            # 红线：撤销通路是考试侧增量，任何意外都降级成"没有撤销"，绝不改变 export/push 的
            # 退出码。只记异常类名（`str(exc)` 可能逐字引用手改台账里的原文，spec §8 禁止台账
            # 内容进日志）。撤销条目与台账文本一律清空、`render_events` 退回撤销前，产物照常。
            logger.warning(
                "考试撤销通路本次降级为不撤销（%s），撤销条目与台账文本都不产出，导出照常",
                type(exc).__name__,
            )
            cancellation_events = []
            pending_entries = []
            exam_ledger_text = None
            render_events = pre_cancellation_render_events

    # --- 课程侧撤销注入 + 课程台账文本（docs/design/2026-10-12-course-cancellation-uid-v2.md §6.5）---
    # 门 1（CD13）：不信任 ⇒ 整块跳过——不读台账、不撤销、不产出课程台账文本（照 D18/D21），
    # 且与 `--no-exams` 无关（课程撤销不受考试开关影响，§7 表倒数第 3 行）。
    # 异常处理口径与考试支**不同**：门 2 的熔断 `MassCancellationBlocked` 必须冒到 CLI
    # （CD4 fail-closed：退出码 6、零写盘），它不在考试支的 try 里，也必须在下面的
    # 兜底 `except Exception` 之前单独 re-raise——保险丝不许被"降级为不撤销"吞掉。
    # 其余意外（手改台账喂给 icalendar 之类）仍降级为"没有增量"，不改导出退出码。
    # 运行时 import 留在函数体内：cancellation 顶层 import exporter 的 PRODID，courses
    # 又 import cancellation，模块级反向 import 会成环（与 `load_raw`/`exams` 同法）。
    from .courses import assess_courses

    courses_trust = assess_courses(
        total_candidates=parser.report.total_candidates,
        skipped=parser.report.skipped,
        meetings=meetings,
    )
    course_ledger_text: str | None = None
    migration_summary: MigrationSummary | None = None
    course_cancellation_events: list[CalendarEvent] = []
    if not courses_trust.trusted:
        # `reason` 是类别词（无记录/有跳过行/零门课），本身不含课程名（§8）。
        logger.warning("课程撤销通路本次不启用：%s", courses_trust.reason)
    if course_uid_collisions:
        # UID 碰撞 ⇒ 台账行与课表行的对应关系本身不可信，整段撤销逻辑（读台账、算候选、
        # 熔断、迁移、台账文本）本次一并跳过：零撤销、不迁移、不写课程台账文本，产物照常
        # （第一不变量：任何降级只会让撤销变少）。只报数量，不带课程名/教师/教室/UID
        # 之外的信息（§8）。考试侧成果不受牵连；熔断异常与这道短路互不相干。
        logger.warning(
            "本次有 %d 个课程 UID 对应了不同课程名（课程号+日期+节次撞在一起），"
            "课程撤销通路本次降级为不撤销",
            len(course_uid_collisions),
        )
    if courses_trust.trusted and not course_uid_collisions:
        from .cancellation import (
            STATUS_CANCELLED,
            build_cancellation_events,
            cancel_candidates,
            load_ledger,
            render_ledger,
        )
        from .courses import mass_cancellation_check, migration_candidates, upcoming_ledger_count

        pre_course_cancellation_render_events = render_events
        try:
            now = cancel_expiry_at or now_local()
            course_ledger: dict[str, LedgerEntry] = {}
            course_ledger_exists = cfg.course_ledger_path(semester).is_file()
            if course_ledger_exists:
                try:
                    course_ledger = load_ledger(
                        cfg.course_ledger_path(semester).read_text(encoding="utf-8"),
                        label="课程台账",
                    )
                except (OSError, UnicodeDecodeError) as exc:
                    # 就地降级成"当作没有台账"（D15）：本次不撤销任何课程，但
                    # `course_ledger_exists` 仍为 True ⇒ 下方照旧产出文本，让调用方
                    # 用当前课表**覆盖**这份读不出的坏台账（§7 的修复动作）。只报种类。
                    logger.warning(
                        "课程台账 %s 读不出（%s），本次当作无台账处理并用当前课表覆盖它",
                        semester,
                        type(exc).__name__,
                    )
            else:
                logger.info("本地还没有 %s 的课程台账，本次不撤销任何已发布课程事件", semester)
            deliverable, unresolvable = cancel_candidates(
                ledger=course_ledger,
                live_uids=live_course_uids,
                baseline_uids=_cancellation_baseline_uids(baseline),
                now=now,
            )
            # 门 2 数的是**本次新撤销**，不是「这次产物里带着几条撤销」。
            # 现场证据（2026-10-12 真数据彩排）：v0.8 迁移跑完后课程台账里有 171 条
            # （96 live + 75 墓碑），台账按 D10 不带 STATUS ⇒ 墓碑与活课在 `cancel_candidates`
            # 的三道筛里长得一模一样、再次被算成 deliverable ⇒ 75/150 = 50% ⇒ 之后每一次普通
            # `push` 都被自己的保险丝误杀，直到那批原定时刻自然过去。
            # 「上次已经发布过这条撤销」的权威在留底（带 STATUS），不在台账。
            # 墓碑照旧继续携带（产物形态不变）；固定标题让墓碑内容逐次恒定 ⇒ 重复携带只会
            # 在迁移那一次把 SEQUENCE 从 0 抬到 1，之后每次发布指纹相同、SEQUENCE 不再漂移。
            already_cancelled = {
                uid for uid, item in (baseline or {}).items() if item.status == STATUS_CANCELLED
            }
            blocked = (
                None
                if allow_mass_cancellation
                else mass_cancellation_check(
                    candidates=len([c for c in deliverable if c.uid not in already_cancelled]),
                    ledger_total=upcoming_ledger_count(
                        course_ledger, now, exclude_uids=already_cancelled
                    ),
                )
            )
            if blocked is not None:
                # 只报数量与占比，不列课程名（§8）。此刻 render_events 尚未被改动，冒到
                # CLI ⇒ 退出码 6、留底/台账/状态文件字节不变（零写盘由"不产出"保证）。
                # 分母是**未过期**台账条目（upcoming_ledger_count）——与分子 deliverable
                # 同域，否则学期中段的比例会被过期条目稀释到阈值以下、熔断恰好漏响。
                # 两侧还都排除基线里已 CANCELLED 的墓碑，理由见上面那段现场证据。
                raise MassCancellationBlocked(
                    f"本次将撤销 {blocked.candidates} 条课程事件，"
                    f"占尚未过去的已发布事件的 {blocked.ratio:.0%}——疑似课表没抓全"
                )
            # D20/F3 的课程侧对应物：留底里没有的候选不下发，但留在台账里排队。
            for item in unresolvable:
                logger.warning(
                    "课程 %s 无法安全下发撤销（发布留底里没有这个 UID，序号只能从 0 起，"
                    "客户端会忽略更低的序号），本次不随产物下发这条撤销，"
                    "留在课程台账里直到它原定时刻自然过去",
                    item.uid,
                )
            course_cancellation_events, course_pending, render_events = _append_cancellation_events(
                build_cancellation_events(deliverable), deliverable, render_events
            )
            # 台账 pending = 实际下发 + unresolvable（与考试支同形，都自限到 DTSTART 过去）。
            course_pending = [*course_pending, *unresolvable]
            #: 迁移分支（§5/CD7/CD10/CD11）：留底文本由 CLI 在状态缺 `uid_recipe` 时传进来
            #: （export 路径永不传，CD3）。候选 = 留底 − live（课程 ∪ 考试）− 本次常态撤销，
            #: 再过 D2 过期筛。**豁免门 2**：配方切换天然整学期量级，喂进保险丝等于掐死升级；
            #: 但规模必须经 `migration_summary` 交给 CLI 打印预告——构建层不写状态标记（CD7）。
            if published_baseline_text is not None:
                published = load_ledger(published_baseline_text, label="发布留底")
                migration = migration_candidates(
                    published=published,
                    live_uids=live_course_uids | live_exam_uids,
                    normal_cancel_uids={event.uid for event in course_cancellation_events}
                    | {event.uid for event in cancellation_events},
                    now=now,
                )
                if migration:
                    migration_summary = MigrationSummary(
                        candidates=len(migration),
                        ratio=len(migration) / max(1, len(published)),
                    )
                    kept, kept_pending, render_events = _append_cancellation_events(
                        build_cancellation_events(migration), migration, render_events
                    )
                    course_cancellation_events = [*course_cancellation_events, *kept]
                    course_pending = [*course_pending, *kept_pending]
            #: 课程台账文本与考试侧同形（§6.6 空值口径）：读到过台账文件或本次有 live
            #: 课程事件 ⇒ 产出；live 侧用**未经日期过滤**的 `pre_filter_course_events`
            #: （CD14，与候选同源），pending 侧含迁移条目（自限到原定时刻过去）。本函数**不落盘**。
            if course_ledger_exists or pre_filter_course_events:
                course_ledger_text = render_ledger(
                    pre_filter_course_events, course_pending, dtstamp=stamp
                )
        except MassCancellationBlocked:
            # 熔断不是"课程侧的小毛病"，是 fail-closed 的拒绝发布信号：原样冒出去。
            raise
        except Exception as exc:
            # 其余一切意外 ⇒ 降级为"没有撤销"（不变量 1：任何降级只会让撤销变少）。
            # 只记异常类名：`str(exc)` 可能逐字引用坏台账原文（§8 禁止进日志）。
            # 回滚只退到**课程撤销前**——考试支的成果（若有）不受牵连。
            logger.warning(
                "课程撤销通路本次降级为不撤销（%s），撤销条目与课程台账文本都不产出，导出照常",
                type(exc).__name__,
            )
            course_cancellation_events = []
            course_ledger_text = None
            migration_summary = None
            render_events = pre_course_cancellation_render_events

    if calendar_name is None:
        # 标题从课表数据推导；年级缺失/并列时 calendar_title 自己会退回基础名。
        calendar_name = calendar_title(semester, (m.grade_year for m in meetings))
        logger.info("日历标题：%s", calendar_name)
    ics = render_ics(render_events, calendar_name=calendar_name, dtstamp=stamp, baseline=baseline)

    # --- 汇总 ---
    # 口径分工（§6.5 第 1 条）：`Events:` 与 `Date range` **只吃课程事件**，
    # 否则考试周会把「课表覆盖范围」莫名拉长到 6 月；考试数量单独走 exam_events。
    info = summarize(meetings, events)
    # semester_name 供 CLI 打印「Semester:」块；写文件由调用方负责。
    info["semester_name"] = academic.semester.name
    # 计数口径 = 真正进了产物的考试（既过了日期过滤，也过了上面的全局 UID 断言）。
    info["exam_events"] = len(kept_exam)
    # 撤销数量单独报（§6.5 D23：撤销不进 sequence_stats，只在这个计数里体现）。
    info["exam_cancellations"] = len(cancellation_events)
    # 课程侧同口径（CD14/§9-17）：只数真正进了产物的条目（含迁移支），被 UID 断言
    # 丢弃的与 unresolvable 的不算。
    info["course_cancellations"] = len(course_cancellation_events)
    stats: dict[str, int] | None = None
    if baseline is not None:
        # stats 口径（spec D23）：必须含「课程 + live 考试」——否则考试的 SEQUENCE/新增
        # 统计失真（`test_sequence_stats_see_exam_events` 看守）；但**不含**撤销条目：
        # 它们的 UID 在基线里以 live 形态存在，指纹必变，混进来会凭空抬高 `updated`。
        # 撤销数量单独走 `info["exam_cancellations"]`。
        stats = sequence_stats([*events, *kept_exam], baseline)

    return ExportResult(
        ics=ics,
        info=info,
        sequence_stats=stats,
        exam_ledger_text=exam_ledger_text,
        course_ledger_text=course_ledger_text,
        migration_summary=migration_summary,
    )
