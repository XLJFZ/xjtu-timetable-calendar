"""eHall 原始 JSON -> 标准化模型。

**这一层是接口变更的唯一适配点。**
内部模型绝不依赖 eHall 的原始字段名；学校改字段时只改本模块。

字段名通过**候选列表**匹配，而不是硬编码单一键名——教务系统的字段命名
在不同接口/版本间并不统一（``courseName`` / ``kcmc`` / ``name`` 都出现过）。

.. warning::
    本模块的字段候选来自 eHall 常见命名约定。**未经真实响应验证的映射
    不会静默猜测**：解析出的课程若缺少年级/周次等关键信息会被记录为
    警告并跳过，同时 :meth:`TimetableParser.report` 会列出所有跳过的项。
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .errors import ParseError
from .models import CAMPUS_UNKNOWN, Course, CourseMeeting
from .periods import PeriodParseError, parse_periods
from .weeks import WeekParseError, parse_week_mask, parse_weeks

__all__ = ["WEEKDAY_ALIASES", "ParseReport", "TimetableParser"]

#: 星期文本 -> 序号（``1`` = 星期一）
WEEKDAY_ALIASES: dict[str, int] = {
    "一": 1, "1": 1, "mon": 1, "monday": 1, "周一": 1, "星期一": 1, "礼拜一": 1,
    "二": 2, "2": 2, "tue": 2, "tuesday": 2, "周二": 2, "星期二": 2, "礼拜二": 2,
    "三": 3, "3": 3, "wed": 3, "wednesday": 3, "周三": 3, "星期三": 3, "礼拜三": 3,
    "四": 4, "4": 4, "thu": 4, "thursday": 4, "周四": 4, "星期四": 4, "礼拜四": 4,
    "五": 5, "5": 5, "fri": 5, "friday": 5, "周五": 5, "星期五": 5, "礼拜五": 5,
    "六": 6, "6": 6, "sat": 6, "saturday": 6, "周六": 6, "星期六": 6, "礼拜六": 6,
    "日": 7, "天": 7, "7": 7, "sun": 7, "sunday": 7, "周日": 7, "周天": 7,
    "星期日": 7, "星期天": 7, "礼拜日": 7, "礼拜天": 7,
}

#: 各字段的候选键名（按优先级排列）
#:
#: **首选键来自 2026-09-20 对真实接口的观测**（见
#: ``_notes/ehall-probe.md``，``POST /jwapp/sys/wdkb/modules/xskcb/xskcb.do``）：
#: ``KCM`` 课程名 / ``KCH`` 课程号 / ``SKJS`` 教师 / ``JASMC`` 教室 /
#: ``SKXQ`` 星期(1-7) / ``KSJC``+``JSJC`` 起止节次 / ``SKZC`` 周次位掩码 /
#: ``ZCMC`` 周次展示串 / ``XXXQDM_DISPLAY`` 校区。
#:
#: 每个字段只保留**少量有依据的英文兼容键**，用于旧版手工固件与
#: 其他教务系统的温和适配；纯猜测的键名一律不收录——
#: 候选列表越长，误匹配风险越高（例：``xm`` 在西交大语义是**学生姓名**，
#: 绝不能当教师字段）。
FIELD_CANDIDATES: dict[str, tuple[str, ...]] = {
    "course_id": ("KCH", "courseId", "courseCode", "kch"),
    "course_name": ("KCM", "courseName", "kcmc"),
    "teacher": ("SKJS", "teacher", "teacherName"),
    "location": ("JASMC", "location", "classroom", "cdmc"),
    "campus": ("XXXQDM_DISPLAY", "campus", "campusName"),
    "weekday": ("SKXQ", "weekday", "weekDay", "xqj"),
    "period_start": ("KSJC", "startPeriod"),
    "period_end": ("JSJC", "endPeriod"),
    "periods": ("periods", "jcs"),
    "weeks_mask": ("SKZC",),
    "weeks": ("ZCMC", "weeks", "zcd"),
    "credits": ("XF", "credits", "credit"),
    "course_list": ("datas", "kbList", "courses", "list"),
}

#: 星期字段里可能出现的**时段后缀**，需要先剥掉再匹配
#: 例如 ``"星期五下午"`` -> ``"星期五"``。
_WEEKDAY_SUFFIXES = (
    "上午", "下午", "晚上", "中午", "上半", "下半",
    "第1-2节", "第3-4节", "第5-6节", "第7-8节",
    "am", "pm",
)

_WEEKDAY_NUM_PATTERN = re.compile(r"^(\d)$")


@dataclass
class ParseReport:
    """解析过程的质量报告，供 CLI 展示与排障。"""

    total_candidates: int = 0
    parsed: int = 0
    skipped: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def skip(self, reason: str) -> None:
        self.skipped.append(reason)

    def warn(self, reason: str) -> None:
        self.warnings.append(reason)

    @property
    def ok(self) -> bool:
        return self.parsed > 0

    def summary(self) -> str:
        parts = [f"候选 {self.total_candidates} 条，成功解析 {self.parsed} 条"]
        if self.skipped:
            parts.append(f"跳过 {len(self.skipped)} 条")
        if self.warnings:
            parts.append(f"警告 {len(self.warnings)} 条")
        return "；".join(parts)


class TimetableParser:
    """把 eHall 课表响应解析成标准化模型。

    Parameters
    ----------
    max_week:
        学期总周数，用于裁剪周次与展开裸「单周/双周」。
    """

    def __init__(self, *, max_week: int = 30) -> None:
        self.max_week = max_week
        self.report = ParseReport()

    # ------------------------------------------------------------------ #
    # 入口
    # ------------------------------------------------------------------ #
    def parse(self, payload: Any) -> tuple[list[Course], list[CourseMeeting]]:
        """解析课表响应。

        Parameters
        ----------
        payload:
            eHall 接口返回的 JSON（已 ``json.loads``）。

        Returns
        -------
        tuple[list[Course], list[CourseMeeting]]
            ``(课程元信息, 课程安排)``。两者通过 ``course_id`` / ``course_name`` 关联。
        """
        records = list(self._iter_records(payload))
        self.report.total_candidates = len(records)

        courses: dict[str, Course] = {}
        meetings: list[CourseMeeting] = []

        for index, record in enumerate(records):
            if not isinstance(record, Mapping):
                self.report.skip(f"第 {index} 条不是对象：{type(record).__name__}")
                continue

            course = self._parse_course(record)
            if course is None:
                continue
            courses[course.course_id or course.name] = course

            for meeting in self._parse_meetings(record, course, index):
                meetings.append(meeting)

        return list(courses.values()), meetings

    # ------------------------------------------------------------------ #
    # 记录枚举
    # ------------------------------------------------------------------ #
    def _iter_records(self, payload: Any) -> Iterator[Any]:
        """从各种可能的响应包裹结构中找出课程记录列表。

        优先使用明确的容器键；找不到时退回「递归寻找最长的对象列表」。
        """
        if payload is None:
            raise ParseError("课表响应为空")

        if isinstance(payload, list):
            yield from payload
            return

        if not isinstance(payload, Mapping):
            raise ParseError(f"课表响应既不是对象也不是列表，而是 {type(payload).__name__}")

        # eHall wdkb 应用的真实信封：datas.<模块名>.rows（2026-09-20 观测）
        datas = payload.get("datas")
        if isinstance(datas, Mapping):
            for module in datas.values():
                if isinstance(module, Mapping) and isinstance(module.get("rows"), list):
                    yield from module["rows"]
                    return

        # 明确的容器键
        for key in FIELD_CANDIDATES["course_list"]:
            value = payload.get(key)
            if isinstance(value, list) and value:
                # 记录可能还嵌一层（如 data.rows）
                nested = self._unwrap(value)
                yield from nested
                return
            if isinstance(value, Mapping):
                nested = self._unwrap(value)
                if nested:
                    yield from nested
                    return

        # 兜底：递归找最长的对象列表
        best = self._longest_object_list(payload)
        if best:
            yield from best
            return

        raise ParseError(
            "无法在课表响应中定位课程列表。"
            f"顶层键为：{sorted(map(str, payload.keys()))}"
        )

    def _unwrap(self, value: Any, depth: int = 0) -> list[Any]:
        """剥掉 ``data`` / ``result`` 之类的一层包裹。"""
        if depth > 4:
            return value if isinstance(value, list) else []
        if isinstance(value, list):
            # 若列表只有一个元素且它是对象，且其中含课程字段，则下钻
            if len(value) == 1 and isinstance(value[0], Mapping):
                inner = self._longest_object_list(value[0])
                if inner:
                    return inner
            return value
        if isinstance(value, Mapping):
            for key in ("records", "rows", "list", "items", "data", "result"):
                if key in value:
                    return self._unwrap(value[key], depth + 1)
        return []

    def _longest_object_list(self, payload: Mapping[str, Any], depth: int = 0) -> list[Any]:
        """递归寻找「看起来像课程记录列表」的最长列表。"""
        if depth > 4:
            return []

        best: list[Any] = []

        def looks_like_record(item: Any) -> bool:
            if not isinstance(item, Mapping):
                return False
            keys = {str(k).lower() for k in item}
            has_name = any(
                c.lower() in keys for c in FIELD_CANDIDATES["course_name"]
            )
            has_time = any(
                c.lower() in keys for c in FIELD_CANDIDATES["weeks"]
            ) or any(c.lower() in keys for c in FIELD_CANDIDATES["weekday"])
            return has_name and has_time

        for value in payload.values():
            if isinstance(value, list) and value and looks_like_record(value[0]):
                if len(value) > len(best):
                    best = value
            elif isinstance(value, Mapping):
                nested = self._longest_object_list(value, depth + 1)
                if len(nested) > len(best):
                    best = nested

        return best

    # ------------------------------------------------------------------ #
    # 单条解析
    # ------------------------------------------------------------------ #
    def _get(self, record: Mapping[str, Any], field_name: str) -> Any:
        """按候选键名取值（大小写不敏感）。"""
        lowered = {str(k).lower(): v for k, v in record.items()}
        for candidate in FIELD_CANDIDATES[field_name]:
            value = lowered.get(candidate.lower())
            if value not in (None, "", [], {}):
                return value
        return None

    def _parse_course(self, record: Mapping[str, Any]) -> Course | None:
        name = self._get(record, "course_name")
        if not name:
            self.report.skip(f"课程缺少名称：{_preview(record)}")
            return None

        course_id = self._get(record, "course_id")
        teacher = self._get(record, "teacher")
        # 真实 xskcb 响应不含类别字段；credits 仅在选课接口（xsdkkc.XF）出现
        credits = self._get(record, "credits") if "credits" in FIELD_CANDIDATES else None

        return Course(
            course_id=str(course_id).strip() if course_id else None,
            name=str(name).strip(),
            teacher=_join_text(teacher),
            credits=_to_float(credits),
            category=_join_text(self._get_optional(record, "category")),
        )

    def _get_optional(self, record: Mapping[str, Any], field_name: str) -> Any:
        """同 :meth:`_get`，但字段没有候选键定义时返回 ``None`` 而非抛错。"""
        if field_name not in FIELD_CANDIDATES:
            return None
        return self._get(record, field_name)

    def _parse_meetings(
        self, record: Mapping[str, Any], course: Course, index: int
    ) -> list[CourseMeeting]:
        """一条记录可能包含多个时段（如 ``weeks`` 是多段）。"""
        label = f"第 {index} 条（{course.name}）"

        # --- 星期 ---
        raw_weekday = self._get(record, "weekday")
        weekday = parse_weekday(raw_weekday)
        if weekday is None:
            self.report.skip(f"{label}：无法识别星期 {raw_weekday!r}")
            return []

        # --- 节次：优先结构化 KSJC/JSJC（真实接口形态），文本解析仅作回退 ---
        raw_start = self._get(record, "period_start")
        raw_end = self._get(record, "period_end")
        raw_periods = self._get(record, "periods")
        if (
            raw_start is not None and raw_end is not None
            and str(raw_start).strip().isdigit() and str(raw_end).strip().isdigit()
        ):
            start, end = int(str(raw_start)), int(str(raw_end))
            if not (1 <= start <= end <= 30):
                self.report.skip(
                    f"{label}：节次区间非法（{start}-{end}）"
                )
                return []
            periods = list(range(start, end + 1))
        else:
            try:
                periods = parse_periods(_as_text(raw_periods))
            except PeriodParseError as exc:
                self.report.skip(f"{label}：{exc}")
                return []

        # --- 周次：优先结构化 SKZC 位掩码（真实接口形态），展示串仅作回退 ---
        raw_mask = self._get(record, "weeks_mask")
        raw_weeks = self._get(record, "weeks")
        weeks: list[int] | None = None
        mask_text = str(raw_mask or "").strip()
        if mask_text:
            try:
                weeks = parse_week_mask(mask_text, max_week=self.max_week)
            except WeekParseError:
                weeks = None  # 不是合法位掩码 → 回退展示串
        if weeks is None:
            try:
                weeks = parse_weeks(_as_text(raw_weeks), max_week=self.max_week)
            except WeekParseError as exc:
                self.report.skip(f"{label}：{exc}")
                return []
        if not weeks:
            self.report.skip(f"{label}：周次为空（掩码/展示串均无有效周）")
            return []

        # --- 地点 / 校区 ---
        location = _join_text(self._get(record, "location"))
        campus = _join_text(self._get(record, "campus")) or _guess_campus(location)

        meeting = CourseMeeting(
            course_id=course.course_id,
            course_name=course.name,
            teacher=course.teacher,
            location=location,
            campus=campus,
            weekday=weekday,
            periods=periods,
            weeks=weeks,
            raw_week_text=_as_text(raw_weeks),
            raw_period_text=_as_text(raw_periods),
        )

        self.report.parsed += 1
        return [meeting]


# --------------------------------------------------------------------------- #
# 字段清洗工具
# --------------------------------------------------------------------------- #
def parse_weekday(value: Any) -> int | None:
    """把各种星期写法解析为 1..7。

    支持：``5``、``"5"``、``"星期五"``、``"周五"``、``"Fri"``、``"Friday"``、
    ``"礼拜五"``、``"星期天"``。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 7 else None

    text = str(value).strip().lower()
    if not text:
        return None

    # 剥掉「下午」「am」等时段后缀，只留星期本身
    for suffix in _WEEKDAY_SUFFIXES:
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    if not text:
        return None

    # 纯数字
    match = _WEEKDAY_NUM_PATTERN.match(text)
    if match:
        num = int(match.group(1))
        return num if 1 <= num <= 7 else None

    if text in WEEKDAY_ALIASES:
        return WEEKDAY_ALIASES[text]

    # 去掉「星期/周/礼拜」前缀再查
    for prefix in ("星期", "周", "礼拜"):
        if text.startswith(prefix):
            rest = text[len(prefix):]
            if rest in WEEKDAY_ALIASES:
                return WEEKDAY_ALIASES[rest]

    # 英文长名（friday / friday afternoon）
    for alias, num in WEEKDAY_ALIASES.items():
        if len(alias) > 3 and alias in text:
            return num

    return None


def _as_text(value: Any) -> str:
    """把字段值统一转成文本，兼容列表/字典。

    .. important::
        列表用**逗号**而非空格拼接。节次与周次解析器以逗号作为分隔符，
        用空格拼接会把 ``[1, 2]`` 变成 ``"1 2"`` 而被误判为无法解析。
        （``"1 2"`` 在数学上是两个独立数字，但 ``"1,2"`` 才是解析器能识别的写法。）
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return ",".join(_as_text(item) for item in value)
    if isinstance(value, Mapping):
        # 常见：{"week": "1-8", "period": "1-2"}
        return ",".join(_as_text(item) for item in value.values())
    return str(value)


def _join_text(value: Any) -> str | None:
    """拼接并规整文本；空值返回 ``None``。"""
    text = " ".join(_as_text(value).split())
    return text or None


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _guess_campus(location: str | None) -> str | None:
    """从地点文本里粗判校区。

    .. note::
        这只是**提示性**推断，用于地点展示。若学校接口直接提供了校区字段，
        以接口字段为准（见 :meth:`TimetableParser._parse_meetings`）。
    """
    if not location:
        return None
    text = location.strip()
    for marker in ("创新港", "兴庆", "雁塔", "曲江", "中国西部科技创新港"):
        if marker in text:
            return "创新港校区" if "创新港" in marker else f"{marker}校区"
    return None


def _preview(record: Mapping[str, Any], limit: int = 6) -> str:
    """给日志用的对象键预览（不含具体值，避免泄露个人信息）。"""
    keys = [str(k) for k in record][:limit]
    more = "" if len(record) <= limit else f" …(+{len(record) - limit})"
    return "{" + ", ".join(keys) + more + "}"


def resolve_campus(explicit: str | None, location: str | None) -> str:
    """对外暴露的校区解析（显式字段优先，其次推断，最后占位）。"""
    return explicit or _guess_campus(location) or CAMPUS_UNKNOWN


def parse_date_range(value: Any) -> tuple[date, date] | None:
    """解析 ``"2026-09-07~2026-12-27"`` 这类区间文本。"""
    text = _as_text(value)
    if not text:
        return None
    parts = re.split(r"[~至\-–—]", text)
    if len(parts) < 2:
        return None
    try:
        return date.fromisoformat(parts[0].strip()), date.fromisoformat(parts[1].strip())
    except ValueError:
        return None


def iter_meeting_groups(records: Sequence[Mapping[str, Any]]) -> Iterator[tuple[str, list[Mapping[str, Any]]]]:
    """按课程分组记录（供未来「同课程多时段合并」使用）。"""
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        key = str(record.get("courseName") or record.get("kcmc") or len(groups))
        groups.setdefault(key, []).append(record)
    yield from groups.items()
