"""官方「作息时间」页解析与作息表配置自动获取（路线图收口项）。

数据来源
--------
教务处公开页 ``https://due.xjtu.edu.cn/xxfw/zxsj.htm`` 中的「学生作息时间表」：
四列网格（表题 | 项目 | 夏、秋季时间(5月1日开始实行) | 冬、春季时间(10月1日开始实行)），
第 1..10 节课给出两季各自的起止钟点，切换点（几月几日）写在列表头原文里。

这正是 ``schedules/schedule.json`` 的官方数据源。本模块：
下载（复用 :func:`xjtu_calendar.notices.fetch_notice_html` 的匿名 GET + scheme 白名单）
→ 网格展开（复用 :mod:`notices` 的 rowspan/colspan 解析器）→ 逐行分类
→ 按学期范围铺排生效区间 → 合并写回配置。

设计红线（与 notice 子命令同口径）
--------------------------------
- **不猜**：认不出的项目行进 ``unresolved``，``--apply`` 遇到任何一条即整体拒绝；
  新增的行类别（如未来出现「晚自习」独立行）必须先人工确认格式再扩白名单。
- **不覆盖**：已有 profile / 已有区间一律保留现值并警告；无任何新增时文件字节不变。
- **写前写后校验**：以 :class:`ScheduleTable` 正式领域模型为准；合并结果若存在
  区间重叠或引用未定义 profile，拒绝落盘。
- **原子替换**：与校历配置、会话文件共用 :func:`xjtu_calendar.fileutil.atomic_write_text`。

切换点只写「月-日」，年份覆盖范围来自学期配置（``first_week_monday`` 起、
校历声明的学期末止）；校历给不出学期末时拒绝 ``--apply``——
宁可让用户补校历，也不猜一个截止日期。
"""

from __future__ import annotations

import itertools
import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .errors import ParseError
from .fileutil import atomic_write_text
from .logging_setup import get_logger
from .models import _validate_hhmm
from .notices import NoticeParseError, _expand_grid, _GridParser
from .schedules import ScheduleTable

__all__ = [
    "DEFAULT_SCHEDULE_URL",
    "SUMMER_PROFILE_KEY",
    "WINTER_PROFILE_KEY",
    "ScheduleNotice",
    "SchedulePlan",
    "merge_schedule_config",
    "parse_schedule_page",
    "plan_schedule",
    "tile_periods",
]

logger = get_logger()

#: 教务处公开的「作息时间」服务页（2026-10-07 观测可达，公开信息、无需登录）
DEFAULT_SCHEDULE_URL = "https://due.xjtu.edu.cn/xxfw/zxsj.htm"

#: profile 键与 examples/schedule.example.json 保持一致，用户可无缝互换
SUMMER_PROFILE_KEY = "xjtu-summer-autumn"
WINTER_PROFILE_KEY = "xjtu-winter-spring"

_SWITCH_RE = re.compile(r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日开始实行")
_PERIOD_RANGE_RE = re.compile(
    r"^(?P<sh>\d{1,2}):(?P<sm>\d{2})\s*[-~–—]\s*(?P<eh>\d{1,2}):(?P<em>\d{2})$"
)

#: 项目列的「第N节课」写法（N 为中文数字）
_Teaching_RE = re.compile(r"^第(?P<num>[一二三四五六七八九十]+)节课?$")

_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _cn_to_int(text: str) -> int | None:
    """中文数字 1..20 转 int（够覆盖本专科节次；再大按未知处理）。"""
    if text == "十":
        return 10
    if text.startswith("十"):
        return 10 + _CN_DIGITS[text[1]]
    if text.endswith("十"):
        return _CN_DIGITS[text[0]] * 10
    match = re.fullmatch(r"([一二三四五六七八九])十([一二三四五六七八九])", text)
    if match:
        return _CN_DIGITS[match.group(1)] * 10 + _CN_DIGITS[match.group(2)]
    return _CN_DIGITS.get(text)


#: 非教学行的已知类别（子串匹配）。不在名单里的行**不会**被当成噪声丢弃，
#: 而是进 unresolved 挡住 --apply —— 学校新增行类别时强制人工确认。
_NON_TEACHING_MARKERS = (
    "起床",
    "盥洗",
    "早操",
    "早餐",
    "午餐",
    "晚餐",
    "预备铃",
    "课间操",
    "午休",
    "就寝",
    "熄灯",
)


@dataclass(frozen=True)
class UnresolvedItem:
    """认不出/解析不了的行，原样保留交人工。"""

    item_text: str
    summer_text: str
    winter_text: str
    reason: str


@dataclass(frozen=True)
class ScheduleNotice:
    """作息页的一次解析结果。"""

    summer: dict[int, tuple[str, str]] = field(default_factory=dict)
    winter: dict[int, tuple[str, str]] = field(default_factory=dict)
    summer_switch: tuple[int, int] | None = None
    winter_switch: tuple[int, int] | None = None
    notes: list[tuple[str, str, str]] = field(default_factory=list)
    unresolved: list[UnresolvedItem] = field(default_factory=list)


@dataclass(frozen=True)
class SchedulePlan:
    """可直接合并进 schedule.json 的方案。"""

    profiles: dict[str, dict[str, Any]] = field(default_factory=dict)
    periods: list[dict[str, str]] = field(default_factory=list)
    unresolved: list[UnresolvedItem] = field(default_factory=list)


def _normalize_time(value: str) -> str:
    """全角数字/符号收紧成 ``H:MM``。"""
    out = []
    for ch in value:
        if "\uff10" <= ch <= "\uff19":
            out.append(chr(ord(ch) - 0xFEE0))
        elif ch == "\uff1a":
            out.append(":")
        elif ch in ("\uff0d", "\u2014", "\u2013", "\uff5e"):
            out.append("-")
        else:
            out.append(ch)
    return "".join(out).strip()


def _parse_range(value: str) -> tuple[str, str] | None:
    text = _normalize_time(value)
    match = _PERIOD_RANGE_RE.match(text)
    if match is None:
        return None
    start = f"{int(match.group('sh'))}:{match.group('sm')}"
    end = f"{int(match.group('eh'))}:{match.group('em')}"
    _validate_hhmm(start, "节次开始")
    _validate_hhmm(end, "节次结束")
    return start, end


def _find_columns(grid: list[list[str]]) -> tuple[int, int, int]:
    """从表头行定位 (项目列, 夏秋列, 冬春列)。"""
    for row in grid:
        summer = winter = item = None
        for index, cell in enumerate(row):
            if "项目" in cell and item is None:
                item = index
            if "夏" in cell and "秋" in cell and summer is None:
                summer = index
            if "冬" in cell and "春" in cell and winter is None:
                winter = index
        if item is not None and summer is not None and winter is not None:
            return item, summer, winter
    raise NoticeParseError(
        "作息页中没有找到含「项目 / 夏、秋季时间 / 冬、春季时间」的表头，"
        "页面结构可能已变化，请人工查看后适配，而不是按旧格式硬解析。"
    )


def parse_schedule_page(html: str) -> ScheduleNotice:
    """解析「学生作息时间表」页面（网格展开复用 notices 的实现）。"""
    parser = _GridParser()
    parser.feed(html)
    if not parser.tables:
        raise NoticeParseError("作息页中没有任何表格，页面结构可能已变化。")

    grids = [_expand_grid(table) for table in parser.tables]
    header_grid: list[list[str]] | None = None
    for grid in grids:
        try:
            _find_columns(grid)
        except NoticeParseError:
            continue
        header_grid = grid
        break
    if header_grid is None:
        raise NoticeParseError(
            "页面中的表格都不是「学生作息时间表」（缺少 项目/夏秋季/冬春季 表头）。"
            "学校页面可能改版，请人工确认后再解析。"
        )

    item_col, summer_col, winter_col = _find_columns(header_grid)
    # 表题列是 rowspan 时业务列右对齐；部分页面表题只占表头一行，
    # 表体整体左移一列。**从右侧锚定取列**两种形态都能对上。
    expected_width = winter_col + 1

    def cell_at(row: list[str], col: int) -> str:
        from_right = expected_width - col
        if len(row) < from_right:
            return ""
        return row[-from_right].strip()

    def header_cell(column: int) -> str:
        for row in header_grid or []:
            for index, cell in enumerate(row):
                if index == column and ("夏" in cell or "冬" in cell or "时间" in cell):
                    return cell
        return ""

    switch_summer = _SWITCH_RE.search(_normalize_time(header_cell(summer_col)))
    switch_winter = _SWITCH_RE.search(_normalize_time(header_cell(winter_col)))

    notice = ScheduleNotice(
        summer_switch=(int(switch_summer.group("month")), int(switch_summer.group("day")))
        if switch_summer
        else None,
        winter_switch=(int(switch_winter.group("month")), int(switch_winter.group("day")))
        if switch_winter
        else None,
    )

    rows = list(header_grid)
    header_seen = False
    for row in rows:
        if not header_seen and any(
            index in (summer_col, winter_col) and ("夏" in cell or "冬" in cell)
            for index, cell in enumerate(row)
        ):
            header_seen = True
            continue  # 表头行本身
        item = cell_at(row, item_col)
        summer_raw = cell_at(row, summer_col)
        winter_raw = cell_at(row, winter_col)
        if not (item or summer_raw or winter_raw):
            continue

        teaching = _Teaching_RE.match(item)
        if teaching is not None:
            number = _cn_to_int(teaching.group("num"))
            if number is None:
                notice.unresolved.append(
                    UnresolvedItem(item, summer_raw, winter_raw, "节次中文数字无法识别")
                )
                continue
            start_pair = _parse_range(summer_raw)
            end_pair = _parse_range(winter_raw)
            if start_pair is None or end_pair is None:
                notice.unresolved.append(
                    UnresolvedItem(item, summer_raw, winter_raw, "钟点不是 HH:MM-HH:MM 区间")
                )
                continue
            notice.summer[number] = start_pair
            notice.winter[number] = end_pair
            continue

        if any(marker in item for marker in _NON_TEACHING_MARKERS):
            notice.notes.append((item, summer_raw, winter_raw))
            continue

        notice.unresolved.append(
            UnresolvedItem(item, summer_raw, winter_raw, "项目不在已知分类内（教学节次/作息噪声）")
        )

    if not notice.summer and not notice.winter and not notice.unresolved:
        # 「一节课都没有且没有任何可呈报的行」才视为结构性失败；
        # 有 unresolved 明细时呈报明细（--apply 依旧整体拒绝）。
        raise NoticeParseError(
            "找到了作息表格，却没有解析出任何一节课的钟点，也没有可呈报的行——"
            "为避免把「解析不到」当成「页面没有节次」，这里直接报错。"
        )
    return notice


def _profile_at(day: date, switches: list[tuple[tuple[int, int], str]]) -> str:
    """给定日期落在哪套作息：取「月-日」不晚于当日的最近切换点，全部更晚则回绕上一年。"""
    md = (day.month, day.day)
    active = None
    for point, key in switches:
        if point <= md and (active is None or point >= active[0]):
            active = (point, key)
    if active is not None:
        return active[1]
    return max(switches, key=lambda item: item[0])[1]


def tile_periods(
    lower: date,
    upper: date,
    summer_switch: tuple[int, int],
    winter_switch: tuple[int, int],
) -> list[tuple[date, date, str]]:
    """把「月-日切换点」铺成 [lower, upper] 上的闭区间序列。

    切换点本身来自页面表头原文（5月1日 / 10月1日开始实行），年份覆盖
    来自学期范围；区间首尾恰好拼满 ``[lower, upper]``，无缝隙无重叠。
    """
    if lower > upper:
        raise ParseError(f"覆盖范围非法：lower={lower.isoformat()} > upper={upper.isoformat()}")
    if summer_switch == winter_switch:
        raise ParseError(
            f"夏秋季与冬春季切换点相同（{summer_switch}），页面写法可能不完整或有歧义。"
        )
    switches = [(summer_switch, SUMMER_PROFILE_KEY), (winter_switch, WINTER_PROFILE_KEY)]

    points: list[date] = [lower]
    for year in range(lower.year, upper.year + 1):
        for md, _ in switches:
            try:
                moment = date(year, md[0], md[1])
            except ValueError:
                continue
            if lower < moment <= upper:
                points.append(moment)
    points.append(upper + timedelta(days=1))
    points = sorted(set(points))

    spans: list[tuple[date, date, str]] = []
    for start, next_start in itertools.pairwise(points):
        end = next_start - timedelta(days=1)
        spans.append((start, end, _profile_at(start, switches)))
    return spans


def plan_schedule(notice: ScheduleNotice, *, lower: date, upper: date) -> SchedulePlan:
    """把解析结果整理成可合并方案；有 unresolved 时**只返回明细不返回方案**。"""
    if notice.unresolved:
        return SchedulePlan(unresolved=list(notice.unresolved))
    if notice.summer_switch is None or notice.winter_switch is None:
        raise ParseError(
            "页面表头没有解析出「X月X日开始实行」的切换点，无法铺排生效区间；请人工确认页面格式。"
        )
    if not notice.summer or not notice.winter:
        raise ParseError("两季教学节次不完整（夏秋季或冬春季一个都没有），拒绝生成方案。")

    def profile_payload(name: str, times: dict[int, tuple[str, str]]) -> dict[str, Any]:
        return {
            "name": name,
            "periods": {
                str(number): [start, end] for number, (start, end) in sorted(times.items())
            },
        }

    profiles = {
        SUMMER_PROFILE_KEY: profile_payload(
            f"夏、秋季作息（{notice.summer_switch[0]}月{notice.summer_switch[1]}日起）",
            notice.summer,
        ),
        WINTER_PROFILE_KEY: profile_payload(
            f"冬、春季作息（{notice.winter_switch[0]}月{notice.winter_switch[1]}日起）",
            notice.winter,
        ),
    }
    periods = [
        {"start": start.isoformat(), "end": end.isoformat(), "profile": profile}
        for start, end, profile in tile_periods(
            lower, upper, notice.summer_switch, notice.winter_switch
        )
    ]
    return SchedulePlan(profiles=profiles, periods=periods)


def _overlaps(a: dict[str, str], b: dict[str, str]) -> bool:
    return not (a["end"] < b["start"] or a["start"] > b["end"])


def merge_schedule_config(path: Path, plan: SchedulePlan) -> dict[str, list[str]]:
    """把方案合并进 ``schedule.json`**，只新增、不覆盖；无任何新增时不写文件。

    返回摘要（``added_profiles`` / ``added_periods`` / ``warnings``）。
    写前校验原配置、写后校验合并结果（正式领域模型 ScheduleTable 为唯一 schema 真源）。
    """
    if plan.unresolved:
        raise ParseError(
            f"存在 {len(plan.unresolved)} 行无法可靠归类的作息条目，拒绝修改配置："
            + "；".join(f"{row.item_text}（{row.reason}）" for row in plan.unresolved)
        )

    if path.is_file():
        try:
            raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ParseError(f"作息表配置不是合法 JSON：{path}（{exc}）") from exc
    else:
        raw = {}

    existing_profiles: dict[str, Any] = dict(raw.get("profiles") or {})
    existing_periods: list[dict[str, str]] = [
        item for item in (raw.get("periods") or []) if isinstance(item, dict)
    ]
    # 写前校验：原配置坏在这里挡住，别让合并顺手写回坏文件。
    ScheduleTable.from_dict({"profiles": existing_profiles, "periods": existing_periods})

    added_profiles: list[str] = []
    added_periods: list[str] = []
    warnings: list[str] = []

    for key, payload in plan.profiles.items():
        if key in existing_profiles:
            warnings.append(f"作息表 {key} 已存在，保留现值（现配置优先）")
            continue
        existing_profiles[key] = payload
        added_profiles.append(key)

    for interval in plan.periods:
        if any(_overlaps(interval, current) for current in existing_periods):
            warnings.append(
                f"区间 {interval['start']}~{interval['end']}（{interval['profile']}）"
                "与现有区间重叠，跳过新增（现配置优先）"
            )
            continue
        existing_periods.append(interval)
        added_periods.append(f"{interval['start']}~{interval['end']} -> {interval['profile']}")

    if not added_profiles and not added_periods:
        # 幂等：第二次 --apply 不重写文件，字节保持不变
        return {
            "added_profiles": added_profiles,
            "added_periods": added_periods,
            "warnings": warnings,
        }

    merged = {
        "profiles": existing_profiles,
        "periods": sorted(existing_periods, key=lambda item: item["start"]),
    }
    # 写后校验：合并结果必须能被 export 读懂，且没有重叠/悬空引用。
    table = ScheduleTable.from_dict(merged)
    problems = table.validate()
    blocking = [p for p in problems if "重叠" in p or "未定义" in p]
    if blocking:
        raise ParseError("合并后的作息表未通过校验（配置未被改动）：\n" + "\n".join(blocking))
    for problem in problems:
        logger.warning("作息表配置问题（合并时已知）：%s", problem)

    atomic_write_text(path, json.dumps(merged, ensure_ascii=False, indent=2) + "\n")
    return {
        "added_profiles": added_profiles,
        "added_periods": added_periods,
        "warnings": warnings,
    }
