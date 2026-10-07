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
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from .academic_calendar import AcademicCalendar
from .config import Settings
from .errors import (
    CalendarExportError,
    ScheduleNotConfigured,
    SemesterNotConfigured,
    UnsupportedAdjustmentError,
    XjtuCalendarError,
)
from .logging_setup import get_logger
from .models import CalendarEvent, CourseMeeting, UnsupportedAdjustment
from .parser import TimetableParser
from .periods import format_periods
from .schedules import ScheduleTable
from .sequence import EventBaseline, parse_baseline, resolve_sequence, sequence_stats
from .timeutil import TZ_XIAN, now_local
from .weeks import DEFAULT_EXPANSION_LIMIT, format_weeks

__all__ = [
    "PRODID",
    "ExportResult",
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

    **``course_name`` 参与身份是刻意的向后兼容选择**（legacy UID contract）：
    ``course_id`` 已稳定时重复包含课程名在模型上并不理想，但 UID 算法自
    v0.1.0 起已随正式版发布，改动会让已导入用户的整个学期被识别成新事件。
    已知边界：同一 ``course_id`` 的课程被改名时，其事件会被视为新事件。
    未来若做 UID v2，必须配显式迁移方案，不在普通 minor release 里直接改。

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
            if uid in seen_uids:
                continue
            seen_uids.add(uid)

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

    events.sort(key=lambda e: (e.start, e.summary))
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
        component.add("summary", item.summary)
        if item.location:
            component.add("location", item.location)
        if item.description:
            component.add("description", item.description)
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


@dataclass
class ExportResult:
    """build_ics_for_semester 的返回：渲染文本 + 汇总数据（CLI 打印用）。"""

    ics: str
    info: dict[str, object]
    sequence_stats: dict[str, int] | None


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


def build_ics_for_semester(
    cfg: Settings,
    semester: str,
    *,
    input_path: str | None = None,
    calendar_config: str | None = None,
    schedule_config: str | None = None,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    from_date: str | None = None,
    to_date: str | None = None,
    allow_unsupported_adjustments: bool = False,
    sequence_from: str | None = None,
    baseline_probe: str | None = None,
    no_sequence: bool = False,
    dtstamp: datetime | None = None,
) -> ExportResult:
    """按学期构建 RFC 5545 文本。export 与 subscribe push 共用的唯一管线。

    ``baseline_probe``：SEQUENCE 自动探测的「旧版本」路径（CLI export 传 -o
    输出路径；subscribe 传上次发布留底）。显式 ``sequence_from`` 优先。

    ``dtstamp`` 为 ``None`` 时默认取**数据源快照的 mtime**（见
    :func:`_stamp_from_snapshot`），保证同快照重建字节一致——subscribe
    的「内容无变化→跳过」判定全靠这一点。
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
            hint="请参考 examples/academic_calendar.example.json 创建该文件。"
            "注意：第 1 教学周的星期一等日期必须来自官方校历，不要凭空填写。",
        )
    academic = AcademicCalendar.from_file(calendar_path)
    logger.info("教学日历：%s", academic.semester.name)

    # --- 作息表 ---
    schedule_path = Path(schedule_config) if schedule_config else cfg.schedule_config_path()
    if not schedule_path.is_file():
        raise ScheduleNotConfigured(
            f"未找到作息表配置：{schedule_path}",
            hint="请参考 examples/schedule.example.json 创建该文件。"
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

    # --- 展开 ---
    events = build_events(meetings, academic, schedules)
    logger.info("已展开：%d 次实际上课", len(events))

    # --- 可选日期过滤 ---
    # 先把边界解析出来：它同时决定「事件过滤」与「unsupported 调课的范围判定」，
    # 两处必须用同一组边界，否则会出现「事件被裁掉、缺课告警却没报」。
    lower = date.fromisoformat(from_date) if from_date else None
    upper = date.fromisoformat(to_date) if to_date else None
    if lower is not None or upper is not None:
        events = [
            e
            for e in events
            if (lower is None or e.start.date() >= lower)
            and (upper is None or e.start.date() <= upper)
        ]
        logger.info("按日期过滤后剩余 %d 次上课", len(events))

    if not events:
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

    stamp = dtstamp or _stamp_from_snapshot(stamp_source or cfg.raw_timetable_path(semester))
    ics = render_ics(events, calendar_name=calendar_name, dtstamp=stamp, baseline=baseline)

    # --- 汇总 ---
    info = summarize(meetings, events)
    # semester_name 供 CLI 打印「Semester:」块；写文件由调用方负责。
    info["semester_name"] = academic.semester.name
    stats: dict[str, int] | None = None
    if baseline is not None:
        stats = sequence_stats(events, baseline)

    return ExportResult(ics=ics, info=info, sequence_stats=stats)
