"""考试安排（`studentWdksapApp`）的解析、导出与变更比对。设计文档：docs/design/2026-10-08-exam-schedule.md。"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from .config import Settings
from .exporter import PRODID, UID_DOMAIN  # 顶层导入；exporter 反向只在函数内 import（避免循环）
from .models import CalendarEvent, ExamSchedule
from .parser import ParseReport
from .schedules import combine

logger = logging.getLogger(__name__)

#: 日期前缀：`-` / `.` / `/` 三种连接符都见过或可能见到，必须先剥掉，
#: 否则 `2030.06.17` 里的 `30.06` 会被下面那条时刻正则吃掉。
_DATE_PREFIX = re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")

#: 两组「时:分」；分隔符实测见过 ``:``／全角 ``：``／半角点 ``.``／全角点 ``．`` 四种。
_HHMM = re.compile(r"(\d{1,2})[:：.．](\d{2})")


def parse_exam_time_text(text: str | None) -> tuple[str, str] | None:
    """从 ``KSSJMS`` 抓出起止时刻，返回零补齐的 ``("HH:MM", "HH:MM")``。

    抓不出两组合法时刻、或结束不晚于开始时返回 ``None`` —— 调用方**跳过该条**，
    绝不退化成 00:00 的假事件（设计文档 §6.4）。输出交给
    :func:`xjtu_calendar.schedules.combine` 落地成带 ``Asia/Shanghai`` 的 datetime。
    """
    if not text:
        return None
    body = _DATE_PREFIX.sub("", text)
    found: list[str] = []
    for hour_text, minute_text in _HHMM.findall(body):
        hour, minute = int(hour_text), int(minute_text)
        if hour > 23 or minute > 59:
            return None
        found.append(f"{hour:02d}:{minute:02d}")
        if len(found) == 2:
            break
    if len(found) < 2 or found[1] <= found[0]:
        return None
    return found[0], found[1]


#: 考试行的字段候选表。**不复用 parser.FIELD_CANDIDATES**：那张表按课程写
#: （teacher 认 SKJS，考试行是 ZJJSXM），且报错文案写死「课程」。
EXAM_FIELDS: dict[str, tuple[str, ...]] = {
    "course_id": ("KCH", "courseId"),
    "course_name": ("KCM", "courseName"),
    "exam_name": ("KSMC",),
    "date": ("KSRQ",),
    "time_text": ("KSSJMS", "KSSJ"),
    "location": ("JASMC",),
    "campus_code": ("XXXQDM",),
    "seat": ("ZWH", "KSWZ"),
    "credits": ("XF",),
    "teacher": ("ZJJSXM",),
    "row_id": ("WID",),
    "task_id": ("KSRWID",),
    "exam_code": ("KSDM",),
}


def _text(record: Mapping[str, Any], key: str) -> str:
    for candidate in EXAM_FIELDS[key]:
        value = record.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def iter_exam_rows(payload: Any) -> list[dict[str, Any]]:
    """按结构找出考试行；**不硬编码模块名**，也不看 rows 是否为空（空 rows 交给三态判定）。

    判据故意比 :func:`_exam_module` 宽（只看 `rows`，不看 `extParams`），因为课表快照的
    `xskcb` 模块实测就没有 `extParams`；考试侧的解析请走 :func:`_rows_for_parse`，
    别把这里收紧。
    """
    datas = payload.get("datas") if isinstance(payload, Mapping) else None
    if not isinstance(datas, Mapping):
        return []
    for module in datas.values():
        if isinstance(module, Mapping) and isinstance(module.get("rows"), list):
            # `isinstance(row, dict)` 而非 `Mapping`：真实 JSON 行必是 dict，
            # 也让返回类型 `list[dict[str, Any]]` 在 mypy strict 下成立。
            return [row for row in module["rows"] if isinstance(row, dict)]
    return []


class ExamState(Enum):
    """§7 三态。`UNKNOWN` 的判据是"能不能确定地写出结论"，不是"看起来像不像空"。"""

    HAS_EXAMS = "has_exams"
    NO_EXAMS = "no_exams"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExamOutcome:
    state: ExamState
    rows: tuple[Mapping[str, Any], ...] = ()
    msg: str = ""
    code: Any = None


def _exam_module(payload: Any) -> Mapping[str, Any] | None:
    """返回含 `rows` 与 `extParams` 的那个 module（模块名不硬编码）。"""
    datas = payload.get("datas") if isinstance(payload, Mapping) else None
    if not isinstance(datas, Mapping):
        return None
    for module in datas.values():
        # 三个条件合成一个 `if`（而非计划稿的嵌套写法）：ruff 的 SIM102 不接受嵌套。
        if (
            isinstance(module, Mapping)
            and isinstance(module.get("rows"), list)
            and isinstance(module.get("extParams"), Mapping)
        ):
            return module
    return None


def classify_exam_payload(payload: Any) -> ExamOutcome:
    """判据**只**看 `datas.<模块>.extParams`；外层 `code` 恒为字符串 ``"0"``，不作依据。

    网络层失败（非 200 / 401 / 登录页 / 非 JSON）在 `fetch_via_http` 里就已经抛异常，
    走不到这里 —— 所以本函数返回 `UNKNOWN` 只代表"响应到了但内容不可判定"。
    """
    module = _exam_module(payload)
    if module is None:
        return ExamOutcome(ExamState.UNKNOWN, msg="响应缺少 datas.<module>.extParams")
    ext = module["extParams"]
    code, msg = ext.get("code"), str(ext.get("msg") or "")
    rows = tuple(row for row in module["rows"] if isinstance(row, Mapping))
    if code == 1 and rows:
        return ExamOutcome(ExamState.HAS_EXAMS, rows, msg, code)
    if code == 1:
        return ExamOutcome(ExamState.NO_EXAMS, (), msg, code)
    return ExamOutcome(ExamState.UNKNOWN, (), msg, code)


def campus_names_from_timetable(timetable_payload: Any) -> dict[str, str]:
    """同学期课表快照里的 ``XXXQDM -> XXXQDM_DISPLAY`` 对照（考试行没有 DISPLAY 字段）。"""
    out: dict[str, str] = {}
    for row in iter_exam_rows(timetable_payload):
        code, name = row.get("XXXQDM"), row.get("XXXQDM_DISPLAY")
        if isinstance(code, str) and isinstance(name, str) and code and name:
            out[code] = name
    return out


def _rows_for_parse(payload: Any) -> list[dict[str, Any]]:
    """``parse_exam_rows`` 的行来源：与 classify **同源**（终审 F2 的统一）。

    判据顺序刻意是"先严后宽"：

    1. :func:`_exam_module`（``rows`` + ``extParams``）—— 与 :func:`classify_exam_payload`、
       `cli._exam_total_size` 同一个 module，Task 4 的裁定「以 classify 选中的模块为准」
       由此落到代码上；多模块响应里不会出现「fetch 侧按 B 告警并落盘、导出侧解析 A 的行」；
    2. 退回 :func:`iter_exam_rows`（rows-only）—— 旧快照／`extParams` 缺失时行为与收紧前
       逐字一致，绝不因为判据变严而把一份能用的快照解析成零行。

    课表侧的 :func:`campus_names_from_timetable` **不走这里**：实测 `xskcb` 模块没有
    `extParams`，判据收紧会让校区对照表整体失效。
    """
    module = _exam_module(payload)
    if module is not None:
        return [row for row in module["rows"] if isinstance(row, dict)]
    return iter_exam_rows(payload)


def parse_exam_rows(
    payload: Any,
    *,
    campus_names: Mapping[str, str] | None = None,
    report: ParseReport | None = None,
) -> list[ExamSchedule]:
    """把考试行转成 :class:`ExamSchedule`。不可用的行跳过并计入 ``report``，绝不造 00:00 假事件。"""
    rep = report or ParseReport()
    out: list[ExamSchedule] = []
    for row in _rows_for_parse(payload):
        rep.total_candidates += 1
        name = _text(row, "course_name")
        raw_date = _text(row, "date")
        pair = parse_exam_time_text(_text(row, "time_text"))
        if not name:
            rep.skip("考试行缺少课程名（KCM），已跳过")
            continue
        if pair is None:
            rep.skip(f"考试「{name}」的时间描述无法解析：{_text(row, 'time_text')!r}")
            continue
        if len(raw_date) < 10:
            rep.skip(f"考试「{name}」缺少考试日期（KSRQ），已跳过")
            continue
        day = raw_date[:10]
        _check_weekday_consistency(rep, name, day, _text(row, "time_text"))
        code = _text(row, "campus_code")
        credits_raw = _text(row, "credits")
        out.append(
            ExamSchedule(
                course_id=_text(row, "course_id") or None,
                course_name=name,
                exam_name=_text(row, "exam_name") or None,
                date_str=day,
                start_time=pair[0],
                end_time=pair[1],
                location=_text(row, "location") or None,
                campus=(campus_names or {}).get(code) if code else None,
                seat=_text(row, "seat") or None,
                credits=float(credits_raw) if credits_raw.replace(".", "", 1).isdigit() else None,
                teacher=_text(row, "teacher") or None,
                row_id=_text(row, "row_id") or None,
                task_id=_text(row, "task_id") or None,
                exam_code=_text(row, "exam_code") or None,
            )
        )
        rep.parsed += 1
    return out


#: `KSSJMS` 括号里的星期后缀，实测形态：`(星期二)`。
_WEEKDAY_IN_TEXT = re.compile(r"星期([一二三四五六日天])")
_WEEKDAY_CHARS = "一二三四五六日"  # 下标 0 = 星期一，与 date.isoweekday() 对齐


def _check_weekday_consistency(rep: ParseReport, name: str, day: str, time_text: str) -> None:
    """`KSRQ` 推出来的星期 vs `KSSJMS(星期X)`：不一致只 warning，**不丢行**。

    §4.1 的 22 行真样本逐行核对过，两处**全部一致**；这条是防御性的（两个独立字段，
    谁先腐化不可知）。取舍方向：以 `KSRQ` 为准（客户端显示的就是它推出来的那天），
    丢掉整行等于把一次真实考试信息抹掉，比一条噪音严重得多。
    """
    match = _WEEKDAY_IN_TEXT.search(time_text)
    if match is None:
        return
    try:
        actual = date.fromisoformat(day)
    except ValueError:
        return  # 日期本身有问题，交给下游 build_exam_events 的跳过分支
    claimed = _WEEKDAY_CHARS.index(match.group(1).replace("天", "日"))
    if claimed != actual.isoweekday() - 1:
        rep.warn(
            f"考试「{name}」的时间文本写的是星期{match.group(1)}，"
            f"但考试日期 {day} 是星期{actual.isoweekday()}；按日期为准"
        )


def _uid_token(exam: ExamSchedule, *, announce: bool = True) -> str:
    """考试的身份令牌：``WID`` → ``KSRWID`` → 内容组合。

    它同时是 UID 的原料**和** :func:`_exam_key` 的配对键（两处必须同源，见该函数的说明）：
    改这里等于同时改「客户端认不认得出是同一条事件」与「diff 把两行算不算同一场考试」。

    ``announce`` 决定降级信号的级别：导出路径（:func:`make_exam_uid`）用默认 ``True``，
    缺 ``WID`` 记 ``INFO``、再缺 ``KSRWID`` 记 ``WARNING``，让用户必须知道 UID 不稳；
    diff 路径（:func:`_exam_key`）传 ``False``，把同一句话降到 ``DEBUG``——降级行在 diff
    里每行两侧都会命中，而它的可见信号本就是「取消 + 新增」那两行，无须再逐行吼一次。
    """
    if exam.row_id:
        return f"WID={exam.row_id}"
    if exam.task_id:
        logger.log(
            logging.INFO if announce else logging.DEBUG,
            "考试「%s」缺少 WID，UID 降级到 KSRWID",
            exam.course_name,
        )
        return f"KSRWID={exam.task_id}"
    logger.log(
        logging.WARNING if announce else logging.DEBUG,
        "考试「%s」缺少 WID 与 KSRWID，UID 降级到内容组合（改期会被视为新事件）",
        exam.course_name,
    )
    # 必须含 KSSJMS：实测同一门课同一天有两场（上午 + 晚场），只到日期粒度会撞车
    return "|".join(
        [exam.course_id or "", exam.exam_code or "", exam.date_str, exam.start_time, exam.end_time]
    )


def make_exam_uid(semester_key: str, exam: ExamSchedule) -> str:
    """``sha256("<学期>|<考试身份>")[:32]@xjtu-timetable-calendar``。

    与课程 UID 同一命名空间但不同配方：**刻意不含时间与教室**（换考场应当原地更新），
    也**绝不含年级/学号**。
    """
    payload = f"{semester_key.strip()}|{_uid_token(exam)}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{digest}@{UID_DOMAIN}"


def _exam_type_suffix(exam_name: str | None) -> str:
    """从 ``KSMC`` 取类型词。实测见过 期中/结课/期末考试；**不做枚举白名单**，
    将来出现「补考」「缓考」时原样带出（设计文档 §11）。"""
    if not exam_name:
        return "考试"
    tail = exam_name.rsplit("学期", 1)[-1].strip()
    return tail or "考试"


# --------------------------------------------------------------------------- #
# 变更比对（设计文档 §6.6）：`diff` 子命令的「考试变更」小节
# --------------------------------------------------------------------------- #
#: 五类变更，同时也是输出顺序（新增 → 取消 → 时间 → 教室 → 座位）。
EXAM_KIND_ADDED = "新增"
EXAM_KIND_CANCELLED = "取消"
EXAM_KIND_TIME = "时间变更"
EXAM_KIND_ROOM = "教室变更"
EXAM_KIND_SEAT = "座位变更"
EXAM_CHANGE_KINDS: tuple[str, ...] = (
    EXAM_KIND_ADDED,
    EXAM_KIND_CANCELLED,
    EXAM_KIND_TIME,
    EXAM_KIND_ROOM,
    EXAM_KIND_SEAT,
)


@dataclass(frozen=True)
class ExamChange:
    """一场考试的一次变更。

    **不复用** :class:`xjtu_calendar.diff.SlotChange`（§6.6:338-343）：那个形状强制
    ``weekday: int`` 与 ``periods: tuple[int, ...]``，打印侧还硬编码「星期X」「第N节」。
    考试既没有周次也没有节次 —— 塞 ``weekday=0`` 会让
    ``'一二三四五六日'[change.weekday - 1]`` **静默印成「日」**，塞越界值直接
    ``IndexError`` 把 ``diff`` 崩掉。这里只放考试真实拥有的事实，五类各自可断言。

    Attributes
    ----------
    kind:
        :data:`EXAM_CHANGE_KINDS` 之一。
    course_name / exam_name:
        变更归属的考试；``exam_name`` 是 ``KSMC`` 原文（可能为 ``None``）。
    date_str:
        变更后（新增／取消时即那一场本身）的考试日期，用于排序与"是哪一场"的定位。
    old / new:
        - 新增：``old`` 为空串，``new`` 是那场考试的摘要（日期 起止，教室，座位）；
        - 取消：``old`` 同上，``new`` 为空串；
        - 时间变更：两侧都是 ``"YYYY-MM-DD HH:MM-HH:MM"``（改期与改时刻同一类，
          日期都在文本里，用户看得见挪到了哪天）；
        - 教室变更：两侧都是"校区 教室"或「地点未知」；
        - 座位变更：两侧都是座位号或「无座位号」。
    """

    kind: str
    course_name: str
    exam_name: str | None
    date_str: str
    old: str
    new: str


@dataclass(frozen=True)
class ExamDiff:
    """``diff_exams`` 的结果：按 :data:`EXAM_CHANGE_KINDS` 顺序稳定排好的变更。"""

    changes: tuple[ExamChange, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.changes


def _exam_when(exam: ExamSchedule) -> str:
    return f"{exam.date_str} {exam.start_time}-{exam.end_time}"


def _exam_place(exam: ExamSchedule) -> str:
    """教室展示文本：与 :func:`build_exam_events` 的 ``LOCATION`` 同一拼法（校区 + 教室）。"""
    return " ".join(part for part in (exam.campus, exam.location) if part) or "地点未知"


def _exam_seat(exam: ExamSchedule) -> str:
    # 空串与缺失都归「无座位号」：打出来什么都没有的话，用户会以为 diff 漏了内容。
    return exam.seat or "无座位号"


def _exam_detail(exam: ExamSchedule) -> str:
    """新增／取消行的一句话摘要。"""
    seat = f"，座位 {exam.seat}" if exam.seat else ""
    return f"{_exam_when(exam)}，{_exam_place(exam)}{seat}"


def _exam_key(exam: ExamSchedule) -> str:
    """配对键**就是** UID 的身份令牌（评审 R-G7）。

    这里刻意不调用"看起来一样"的第二份配方，而是直接复用 :func:`_uid_token`
    （``WID`` → ``KSRWID`` → ``课程号|KSDM|日期|开始|结束`` 组合）：两条配方一旦分叉
    （例如键里不写 ``KSDM`` 与结束时刻），缺 ``WID`` 的行会被报成「时间变更」，
    而它在导出时带着的是**新 UID** —— v1 没有 ``STATUS:CANCELLED`` / ``METHOD:CANCEL``
    通路（§7.1），旧事件会永久留在每个订阅者的日历里。同源之后这类行只会报成
    取消 + 新增：diff 不承诺它做不到的原地更新。

    调用时刻意传 ``announce=False``：这里用 ``_uid_token`` 只是为了**取配对键**，不是要
    走导出口径的告警。降级行在 diff 里每行两侧各命中一次，若沿用 INFO/WARNING 就会把
    同一句话吼两遍；它的可见信号本就是「取消 + 新增」那两行，降级线索降到 ``DEBUG`` 即可
    （见 :func:`_uid_token`）。
    """
    return _uid_token(exam, announce=False)


def _exam_group(exams_in: Sequence[ExamSchedule]) -> dict[str, list[ExamSchedule]]:
    """按 :func:`_exam_key` 分组；同键多行（服务端给了重复 ``WID``，病态但必须可预期）保序留全。"""
    groups: dict[str, list[ExamSchedule]] = {}
    for exam in exams_in:
        groups.setdefault(_exam_key(exam), []).append(exam)
    for rows in groups.values():
        # 排序键必须**包含被比较的字段**（座位／教室），否则同键多行只是换了行序时，
        # 逐位配对会把 ``[12, 30]`` 与 ``[30, 12]`` 配成两条幻影「座位变更」；纳入比较项
        # 后两侧都规整成同一顺序，纯置换自我抵消（评审 F4）。
        rows.sort(
            key=lambda exam: (
                exam.date_str,
                exam.start_time,
                exam.course_name,
                _exam_seat(exam),
                _exam_place(exam),
            )
        )
    return groups


def _exam_field_changes(before: ExamSchedule, after: ExamSchedule) -> list[ExamChange]:
    pairs = (
        (EXAM_KIND_TIME, _exam_when(before), _exam_when(after)),
        (EXAM_KIND_ROOM, _exam_place(before), _exam_place(after)),
        (EXAM_KIND_SEAT, _exam_seat(before), _exam_seat(after)),
    )
    return [
        ExamChange(
            kind=label,
            course_name=after.course_name,
            exam_name=after.exam_name,
            date_str=after.date_str,
            old=before_text,
            new=after_text,
        )
        for label, before_text, after_text in pairs
        if before_text != after_text
    ]


def _exam_side_change(kind: str, exam: ExamSchedule) -> ExamChange:
    detail = _exam_detail(exam)
    return ExamChange(
        kind=kind,
        course_name=exam.course_name,
        exam_name=exam.exam_name,
        date_str=exam.date_str,
        old=detail if kind == EXAM_KIND_CANCELLED else "",
        new="" if kind == EXAM_KIND_CANCELLED else detail,
    )


def _exam_sort_key(change: ExamChange) -> tuple[int, str, str]:
    return (EXAM_CHANGE_KINDS.index(change.kind), change.date_str, change.course_name)


def diff_exams(old_exams: Sequence[ExamSchedule], new_exams: Sequence[ExamSchedule]) -> ExamDiff:
    """比对新旧两份考试快照（先用 :func:`parse_exam_rows` 解析成 :class:`ExamSchedule`）。

    按 :func:`_exam_key` 配对：只在新侧 → 新增，只在旧侧 → 取消，两侧都有 → 逐项比较
    时间／教室／座位。

    **首次拿到考试快照**（旧侧为空）时不需要特殊分支：按同一套算法自然得到「全部新增」。
    这不是省事 —— §6.6:336-337 明确禁止"没有基线就静默跳过"，否则学生第一次拿到考试时
    ``diff`` 打出「无变化」，日历里却多出一整批考试事件。
    """
    old_groups = _exam_group(old_exams)
    new_groups = _exam_group(new_exams)
    changes: list[ExamChange] = []
    for key in sorted(set(old_groups) | set(new_groups)):
        before = old_groups.get(key, [])
        after = new_groups.get(key, [])
        for old_exam, new_exam in zip(before, after, strict=False):
            changes.extend(_exam_field_changes(old_exam, new_exam))
        # 同键多行时逐位配对；多出来的部分绝不静默丢弃（导出侧的 UID 断言也丢后来者，
        # 但 diff 的职责是把"少了/多了哪一场"说出来，而不是跟着装看不见）。
        changes.extend(_exam_side_change(EXAM_KIND_ADDED, exam) for exam in after[len(before) :])
        changes.extend(
            _exam_side_change(EXAM_KIND_CANCELLED, exam) for exam in before[len(after) :]
        )
    changes.sort(key=_exam_sort_key)
    return ExamDiff(tuple(changes))


def describe_exam_change(change: ExamChange) -> str:
    """一行人类可读的考试变更（``diff`` 的「考试变更」小节用；风格同 ``diff.describe_slot``）。

    行首的 ``+`` / ``-`` / ``~`` 记号由 CLI 决定，与课程小节保持一致 —— 展示归 CLI，
    本模块只产出正文。
    """
    label = f"{change.course_name}（{_exam_type_suffix(change.exam_name)}）"
    if change.kind in (EXAM_KIND_ADDED, EXAM_KIND_CANCELLED):
        return f"{change.kind}：{label} {change.new or change.old}"
    # 时间变更的两侧都带日期，不必重复；其余类别要把日期写进行里，否则同一门课的
    # 期中考试与期末考试的「座位变更」在输出里分不出是哪一场。
    when = "" if change.date_str in f"{change.old} {change.new}" else f"（{change.date_str}）"
    return f"{change.kind}：{label}{when}：{change.old} → {change.new}"


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


def render_exam_ledger(
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


def load_exam_ledger(text: str) -> dict[str, LedgerEntry]:
    """台账文本 → ``{UID: LedgerEntry}``。

    **不抛异常**（spec D15）：不可解析 ⇒ 一条 warning + 空 dict，调用方按"没有台账"继续。
    丢弃：无 UID、起止缺失或相等、**起止为 naive datetime**（spec D22：D9 要和 aware
    墙钟比较，naive 值会 ``TypeError``）、重复 UID（保留第一条）。
    """
    from icalendar import Calendar

    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:  # 截断、编码坏、结构走样都归这一类
        logger.warning("考试台账不可解析，本次按没有台账处理：%s", exc)
        return {}

    entries: dict[str, LedgerEntry] = {}
    for component in cal.walk("VEVENT"):
        uid = str(component.get("uid") or "").strip()
        if not uid:
            logger.warning("考试台账里有一条没有 UID，已丢弃")
            continue
        if uid in entries:
            logger.warning("考试台账里 UID 重复：%s，保留第一条", uid)
            continue
        start = getattr(component.get("dtstart"), "dt", None)
        end = getattr(component.get("dtend"), "dt", None)
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            logger.warning("考试台账条目 %s 的起止不是 DATE-TIME，已丢弃", uid)
            continue
        if start.tzinfo is None or end.tzinfo is None:
            logger.warning("考试台账条目 %s 的起止没有时区，已丢弃（否则保留期比较会失败）", uid)
            continue
        if end <= start:
            logger.warning("考试台账条目 %s 的结束不晚于开始，已丢弃", uid)
            continue
        entries[uid] = LedgerEntry(
            uid=uid,
            start=start,
            end=end,
            summary=str(component.get("summary") or ""),
            location=_optional_text(component.get("location")),
            description=_optional_text(component.get("description")),
        )
    return entries


def build_exam_events(exams: Sequence[ExamSchedule], semester_key: str) -> list[CalendarEvent]:
    """考试 → 单场、绝对时刻、无 RRULE / 无 VALARM 的 VEVENT。"""
    events: list[CalendarEvent] = []
    for exam in exams:
        try:
            day = date.fromisoformat(exam.date_str)
            start = combine(day, exam.start_time)
            end = combine(day, exam.end_time)
        except ValueError as exc:
            logger.warning("考试「%s」日期无法解析（%s），已跳过", exam.course_name, exc)
            continue
        location = " ".join(part for part in (exam.campus, exam.location) if part) or None
        description_lines = [
            exam.exam_name,
            f"课程号：{exam.course_id}" if exam.course_id else None,
            f"座位号：{exam.seat}" if exam.seat else None,
            f"学分：{exam.credits}" if exam.credits is not None else None,
            f"主考教师：{exam.teacher}" if exam.teacher else None,
        ]
        events.append(
            CalendarEvent(
                uid=make_exam_uid(semester_key, exam),
                # 与课程侧 ``meeting.course_name.strip()`` 一致：真实数据偶有尾随空格，
                # 不该被带进日历标题（`_text` 已在解析层 strip，这里兜住直接构造的行）。
                summary=f"{exam.course_name.strip()}（{_exam_type_suffix(exam.exam_name)}）",
                start=start,
                end=end,
                location=location,
                description="\n".join(line for line in description_lines if line),
                meeting=None,
            )
        )
    return events


def exam_snapshot_lag_days(cfg: Settings, semester: str) -> float | None:
    """考试快照落后于课表快照的天数（设计文档 §7:382-386 选定的陈旧口径）。

    用**相对**差而不是「距今几天」：课表与考试在同一次 fetch 里落盘，两者一起变老
    是正常的；只有考试没跟着更新时才说明缓存过期。考试比课表新（先抓到考试、之后
    才动课表）返回 ``0.0``，任一快照缺失返回 ``None``（此时没什么可提醒的）。

    mtime→天数 的算式唯一归属点是 :func:`xjtu_calendar.subscribe.snapshot_age_days`
    （本函数只作差，不再自己读 mtime），Task 7 的 `_exam_fallback_note` 走的是快照
    **日期**（另一条口径），两处不合并——见 Task 11 报告里的重复说明。
    """
    from .subscribe import snapshot_age_days

    # 函数体内 import：subscribe→exporter→exams，顶层再引会成环。
    exam_age = snapshot_age_days(cfg, semester, kind="exams")
    timetable_age = snapshot_age_days(cfg, semester, kind="timetable")
    if exam_age is None or timetable_age is None:
        return None
    return max(0.0, exam_age - timetable_age)
