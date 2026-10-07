"""官方「停课 / 调课通知」抓取与解析（学术日历自动获取，Phase 2）。

数据来源
--------
学校每逢节假日都会在教务处网站（due.xjtu.edu.cn）发布
《关于……放假调休及教学工作安排的通知》，正文含一张**结构化三列表格**：

    日期 | 周次、星期 | 调休及教学安排

这正是本项目学期配置里 ``excluded_dates`` 与 ``overrides[source_date]``
的官方数据源。本模块负责：下载 → 解析表格 → 逐行分类 → 与学期配置
交叉校验 → 产出可直接合并进 ``academic_calendar`` JSON 的条目。

设计红线
--------
- **不猜**：解析不出的行进入 ``unresolved``（需人工确认），绝不猜测归类；
- **交叉校验**：通知里的「第 N 周星期 X」必须与用户学期配置的
  ``first_week_monday`` 推出的日期自洽（月/日一致），否则该行不可信；
  这一步同时解决了「通知只写月日、不写年份」的年份推断问题；
- **不做静默合并**：``--apply`` 只**新增**条目，与现有配置冲突的一律
  保留现值并警告——用户手工核对过的配置优先级永远高于自动解析。
- **全链路 fail-closed**：① ``parse_teaching_notice`` 不因日期写法不熟悉
  就丢掉业务行，也绝不在「找到表头却 0 个业务行」时假装成功；
  ② ``apply_notice`` 把认不出的行记入 ``unresolved``；
  ③ CLI 在 ``--apply`` 且 ``unresolved`` 非空时**拒绝写盘**，
  ④ ``merge_into_config`` 写前 / 写后都用正式领域模型校验，并原子替换。
  任何一环都不允许「写进去一半」。

HTML 解析用标准库 :mod:`html.parser`（零新增依赖）；
学校页面把文字切碎在大量 ``<span>`` 里、并用 ``rowspan`` 合并
「连续多天同一种安排」的单元格，必须按网格展开后再逐行分类。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .academic_calendar import AcademicCalendar
from .errors import ParseError, XjtuCalendarError
from .fileutil import atomic_write_text
from .logging_setup import get_logger

__all__ = [
    "NoticeApplication",
    "NoticeParseError",
    "NoticeRow",
    "NoticeTable",
    "apply_notice",
    "fetch_notice_html",
    "merge_into_config",
    "parse_teaching_notice",
]

logger = get_logger()

_DATE_RE = re.compile(r"^(?P<month>\d{1,2})月(?P<day>\d{1,2})日$")
_WEEK_RE = re.compile(r"第(?P<week>\d+)周星期(?P<weekday>[一二三四五六日天])")
_MAKEUP_RE = re.compile(r"上\s*(?P<month>\d{1,2})月(?P<day>\d{1,2})日")
_TABLE_HEADER_MARK = "调休及教学安排"

_WEEKDAY_NUM: dict[str, int] = {
    "一": 1,
    "二": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "日": 7,
    "天": 7,
}


class NoticeParseError(XjtuCalendarError):
    """通知页面解析失败（页面结构变化 / 没有找到调课表格）。"""


# --------------------------------------------------------------------------- #
# 表格抓取（标准库 HTMLParser，支持 rowspan / colspan 网格展开）
# --------------------------------------------------------------------------- #
class _GridParser(HTMLParser):
    """把 HTML 表格收集为「行 -> 单元格文本」的结构（含跨行跨列原文标记）。

    每个单元格记录 ``(text, rowspan, colspan)``；文字碎片在
    ``handle_data`` 里按顺序拼接，天然解决学校页面把一句话
    切碎进几十个 ``<span>`` 的问题。嵌套表格只取最外层。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[tuple[str, int, int]]]] = []
        self._depth = 0
        self._table: list[list[tuple[str, int, int]]] | None = None
        self._row: list[tuple[str, int, int]] | None = None
        self._parts: list[str] | None = None
        self._span: tuple[int, int] = (1, 1)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._depth += 1
            if self._depth == 1:
                self._table = []
                self.tables.append(self._table)
            return
        if self._depth != 1 or self._table is None:
            return
        if tag == "tr":
            self._row = []
            self._table.append(self._row)
        elif tag in ("td", "th") and self._row is not None and self._parts is None:
            attrs_dict = dict(attrs)
            self._parts = []
            self._span = (
                _to_positive_int(attrs_dict.get("rowspan"), 1),
                _to_positive_int(attrs_dict.get("colspan"), 1),
            )

    def handle_endtag(self, tag: str) -> None:
        if tag == "table":
            self._depth -= 1
            if self._depth == 0:
                self._table = None
        elif tag in ("td", "th") and self._parts is not None and self._row is not None:
            self._row.append(("".join(self._parts).strip(), *self._span))
            self._parts = None
        elif tag == "tr":
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)


def _to_positive_int(raw: str | None, default: int) -> int:
    try:
        value = int(raw or "")
    except ValueError:
        return default
    return value if value >= 1 else default


def _expand_grid(
    rows: list[list[tuple[str, int, int]]],
) -> list[list[str]]:
    """把 rowspan / colspan 展开成规整网格。

    返回的每行是「按列顺序的文本列表」。合并单元格的文本会出现在它
    覆盖的每一行——这正是通知表格「连续多天共享一个安排单元格」的表达方式。
    """
    grid: list[list[str]] = []
    carry: dict[int, tuple[str, int]] = {}  # 列号 -> (文本, 剩余行数)

    for row in rows:
        occupied: dict[int, str] = {}
        col = 0
        cell_index = 0
        while True:
            if col in carry:
                text, remaining = carry.pop(col)
                occupied[col] = text
                if remaining > 1:
                    carry[col] = (text, remaining - 1)
                col += 1
                continue
            if cell_index >= len(row):
                break
            text, rowspan, colspan = row[cell_index]
            cell_index += 1
            for offset in range(colspan):
                occupied[col + offset] = text
                if rowspan > 1:
                    carry[col + offset] = (text, rowspan - 1)
            col += colspan
        if occupied:
            width = max(occupied) + 1
            grid.append([occupied.get(c, "") for c in range(width)])
    return grid


# --------------------------------------------------------------------------- #
# 解析与分类
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class NoticeRow:
    """调课表格里的一行（已完成 rowspan 展开）。"""

    date_text: str
    week_text: str
    arrangement: str


@dataclass(frozen=True)
class NoticeTable:
    """一张调课表格：日期行 + 说明行（如作息切换提示）。"""

    rows: list[NoticeRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def parse_teaching_notice(html: str) -> NoticeTable:
    """从通知页 HTML 中解析出调课表格。

    在页面所有表格里定位表头含「调休及教学安排」的那一张；
    找不到时抛 :class:`NoticeParseError`（页面结构变化，需要人工适配，
    而不是静默返回空结果）。

    职责边界（fail-closed 的第一层）
    --------------------------------
    本函数只做「HTML 表格 -> 网格 -> 候选业务行」，**不判断日期格式能不能解析**：

    - 表头行（含「调休及教学安排」）跳过；
    - 全宽说明行（跨列后三列同文）归入 :attr:`NoticeTable.notes`；
    - 其余非空行一律作为候选业务行原样交给 :func:`apply_notice`。

    这是关键设计：如果在这里用「日期必须长成 ``N月N日``」筛掉不认识的行，
    学校一旦把日期改成 ``10 月 1 日`` / ``2026年10月1日`` / ``10月1日（星期四）``，
    整行就会**凭空消失**，连 ``unresolved`` 都进不去 —— 那是「静默丢数据」，
    违反了「不确定时宁可不写，也不能部分成功」的红线。
    解析失败的判定统一交给 :func:`apply_notice` 记入 ``unresolved``。

    找到目标表但一条业务行都没有时同样抛 :class:`NoticeParseError`：
    「0 个停课 + 0 个调课」与「真的没有调课」在结果上无法区分，
    绝不能把前者伪装成一次成功的解析。
    """
    parser = _GridParser()
    parser.feed(html)

    target: list[list[tuple[str, int, int]]] | None = None
    for table in parser.tables:
        for row in table:
            if any(_TABLE_HEADER_MARK in cell[0] for cell in row):
                target = table
                break
        if target is not None:
            break
    if target is None:
        raise NoticeParseError(
            "通知页面中没有找到调课安排表格（表头需含「调休及教学安排」）。"
            "页面结构可能已变化，请人工查看通知原文。"
        )

    grid = _expand_grid(target)
    result = NoticeTable()
    for grid_row in grid:
        if not grid_row:
            continue
        cells = [cell.strip() for cell in grid_row]
        # 表头行：既不进业务行，也不进说明
        if any(_TABLE_HEADER_MARK in cell for cell in cells):
            continue
        # 全宽说明行（如作息切换提示）：colspan 让三列拿到同一段文本
        if len(cells) >= 3 and cells[0] and len(set(cells[:3])) == 1:
            result.notes.append(cells[0])
            continue

        date_text = cells[0]
        week_text = cells[1] if len(cells) > 1 else ""
        arrangement = cells[2] if len(cells) > 2 else ""
        if not (date_text or week_text or arrangement):
            continue  # 完全空行
        # 只要不是表头 / 说明 / 空行，就一律按候选业务行传递，
        # 日期格式认不出来时由 apply_notice 记入 unresolved —— 不在这里丢弃。
        result.rows.append(
            NoticeRow(date_text=date_text, week_text=week_text, arrangement=arrangement)
        )

    if not result.rows:
        raise NoticeParseError(
            "找到了调课安排表格，却没有任何可处理的安排行。"
            "页面结构可能已变化；为避免把「解析不到」误判成「本次没有调课」，"
            "请人工查看通知原文后再决定。"
        )
    return result


def fetch_notice_html(url: str, *, timeout: float = 30.0) -> str:
    """下载通知页 HTML（公开页面，无需任何凭据）。

    只做一次 GET，带常规浏览器 UA；不跟随登录、不提交任何数据。
    **只允许 http/https**：``urlopen`` 本身支持 ``file://`` 等 scheme，
    不加白名单的话「匿名 GET 公开页」就只是说法而不是保证。
    本地文件请走 ``--from-file`` 的显式入口。
    """
    scheme = urlparse(url).scheme.lower()
    if scheme not in ("http", "https"):
        raise NoticeParseError(
            f"--url 只接受 http/https 地址，实际的 scheme 是 {scheme!r}",
            hint="通知页是公开网页；若要解析本地保存的 HTML，请改用 --from-file。",
        )
    request = Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
    with urlopen(request, timeout=timeout) as response:
        charset = response.headers.get_content_charset() or "utf-8"
        raw: bytes = response.read()
        return raw.decode(charset, errors="replace")


# --------------------------------------------------------------------------- #
# 交叉校验与产出
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class UnresolvedRow:
    """无法自动归类的行：原样保留，等人工确认。"""

    date_text: str
    week_text: str
    arrangement: str
    reason: str


@dataclass(frozen=True)
class NoticeApplication:
    """一次通知解析的最终产出。

    Attributes
    ----------
    excluded_dates:
        应加入 ``excluded_dates`` 的日期（停课放假日）。
    makeups:
        调课日 ``(target_date, source_date)``，对应
        ``overrides[target] = {"source_date": source}``。
    unresolved:
        需人工确认的行。**绝不**为它们生成任何自动条目。
    """

    excluded_dates: list[date] = field(default_factory=list)
    makeups: list[tuple[date, date]] = field(default_factory=list)
    unresolved: list[UnresolvedRow] = field(default_factory=list)


def _resolve_notice_date(
    month: int, day: int, week: int, weekday: int, first_week_monday: date
) -> date | None:
    """用「第 N 周星期 X」与学期第一周周一交叉推断通知中的年份。

    先按周次推出理论日期；月/日一致即采信——这同时完成两项校验：
    通知的周次标注与日期自洽、且属于该学期。不一致时退回「逐候选年份
    构造日期再反向验证周次」，仍失败则返回 ``None``（交人工）。
    """
    expected = first_week_monday + timedelta(weeks=week - 1, days=weekday - 1)
    if (expected.month, expected.day) == (month, day):
        return expected

    for year in (first_week_monday.year, first_week_monday.year + 1, first_week_monday.year - 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        offset = (candidate - first_week_monday).days
        if offset >= 0 and candidate.isoweekday() == weekday and offset // 7 + 1 == week:
            return candidate
    return None


def apply_notice(table: NoticeTable, first_week_monday: date) -> NoticeApplication:
    """把解析出的表格行分类成交全配置条目。

    分类规则（按通知原文的固定措辞）：

    - 「停当日课程，上M月D日（第N周星期X）课程」→ 调课
      ``overrides[target] = {"source_date": source}``；
    - 其余含「停课」「放假」「法定节假日」「调休」的行 → 停课日；
    - 两者都不满足 → ``unresolved``，交人工确认。
    """
    result = NoticeApplication()
    for row in table.rows:
        date_match = _DATE_RE.match(row.date_text)
        week_match = _WEEK_RE.search(row.week_text)
        if date_match is None or week_match is None:
            result.unresolved.append(
                UnresolvedRow(
                    row.date_text, row.week_text, row.arrangement, "日期或周次格式无法解析"
                )
            )
            continue

        weekday = _WEEKDAY_NUM.get(week_match.group("weekday"))
        if weekday is None:  # pragma: no cover - 正则字符集已覆盖
            result.unresolved.append(
                UnresolvedRow(row.date_text, row.week_text, row.arrangement, "星期无法识别")
            )
            continue

        target_date = _resolve_notice_date(
            int(date_match.group("month")),
            int(date_match.group("day")),
            int(week_match.group("week")),
            weekday,
            first_week_monday,
        )
        if target_date is None:
            result.unresolved.append(
                UnresolvedRow(
                    row.date_text,
                    row.week_text,
                    row.arrangement,
                    "周次星期与日期对不上学期第一周周一（可能不属于该学期）",
                )
            )
            continue

        makeup_match = _MAKEUP_RE.search(row.arrangement)
        if makeup_match is not None:
            # 来源日的「第N周星期X」写在安排列的括号里；行自身的周次在第 2 列
            source_matches = _WEEK_RE.findall(row.arrangement)
            if not source_matches:
                result.unresolved.append(
                    UnresolvedRow(
                        row.date_text,
                        row.week_text,
                        row.arrangement,
                        "调课行缺少来源日的周次星期标注",
                    )
                )
                continue
            source_week, source_weekday_name = source_matches[-1]
            source_weekday = _WEEKDAY_NUM.get(source_weekday_name)
            if source_weekday is None:  # pragma: no cover
                result.unresolved.append(
                    UnresolvedRow(
                        row.date_text, row.week_text, row.arrangement, "来源日星期无法识别"
                    )
                )
                continue
            source_date = _resolve_notice_date(
                int(makeup_match.group("month")),
                int(makeup_match.group("day")),
                int(source_week),
                source_weekday,
                first_week_monday,
            )
            if source_date is None:
                result.unresolved.append(
                    UnresolvedRow(
                        row.date_text,
                        row.week_text,
                        row.arrangement,
                        "来源日周次星期与日期对不上学期第一周周一",
                    )
                )
                continue
            result.makeups.append((target_date, source_date))
            continue

        if "停课" in row.arrangement or "放假" in row.arrangement:
            result.excluded_dates.append(target_date)
            continue

        result.unresolved.append(
            UnresolvedRow(row.date_text, row.week_text, row.arrangement, "措辞不在已知分类内")
        )

    result.excluded_dates.sort()
    result.makeups.sort()
    return result


# --------------------------------------------------------------------------- #
# 合并进学期配置（只新增，不覆盖）
# --------------------------------------------------------------------------- #
def merge_into_config(
    config_path: Path,
    application: NoticeApplication,
    *,
    source_url: str,
) -> dict[str, list[str]]:
    """把解析结果合并进学期配置 JSON，**只新增、不覆盖**。

    现有 ``excluded_dates`` / ``overrides`` 条目优先级永远高于自动解析：
    出现同日冲突时保留现值并记录到返回的 ``warnings``。
    返回变更摘要（``added_excluded`` / ``added_makeups`` / ``warnings``）。

    写回安全（fail-closed 的第二层）
    --------------------------------
    1. **写前校验**：读出的原配置必须先能通过
       :meth:`AcademicCalendar.from_dict` —— 配置已经损坏时，notice
       不应该顺手把它写回去，而应停下让人检查；
    2. **写后校验**：合并结果再次过一遍同一套领域模型校验。
       自动写入绝不能产出 ``export`` 读不懂的配置；
    3. **原子替换**：先写同目录临时文件，``flush`` + ``fsync`` 后用
       :func:`os.replace` 覆盖。用户往往只有这一份校历配置，
       直接覆盖时若进程中途崩溃会留下截断的 JSON。

    Raises
    ------
    ParseError
        原配置或合并结果不满足正式 schema（此时配置文件**未被修改**）。
    """
    try:
        raw: dict[str, Any] = json.loads(config_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ParseError(f"学期配置不是合法 JSON：{config_path}（{exc}）") from exc

    # 1) 写前校验：正式领域模型是唯一 schema 真源。
    AcademicCalendar.from_dict(raw)

    existing_excluded = {
        item if isinstance(item, str) else str(item) for item in raw.get("excluded_dates", [])
    }
    overrides_raw: dict[str, Any] = raw.get("overrides") or {}

    added_excluded: list[str] = []
    added_makeups: list[str] = []
    warnings: list[str] = []

    for day in application.excluded_dates:
        iso = day.isoformat()
        if iso in existing_excluded:
            continue
        if iso in overrides_raw:
            warnings.append(f"{iso} 已存在于 overrides，跳过停课日新增（现配置优先）")
            continue
        existing_excluded.add(iso)
        added_excluded.append(iso)

    for target, source in application.makeups:
        target_iso = target.isoformat()
        source_iso = source.isoformat()
        if target_iso in existing_excluded:
            warnings.append(
                f"{target_iso} 已在 excluded_dates 中，与调课日冲突，跳过新增（现配置优先）"
            )
            continue
        existing_override = overrides_raw.get(target_iso)
        if existing_override is not None:
            existing_source = (existing_override or {}).get("source_date")
            if existing_source == source_iso:
                continue
            warnings.append(
                f"{target_iso} 已有调课规则（source_date={existing_source}），"
                f"与通知解析结果（{source_iso}）不一致，保留现值"
            )
            continue
        overrides_raw[target_iso] = {"source_date": source_iso}
        added_makeups.append(f"{target_iso} <- {source_iso}")

    raw["excluded_dates"] = sorted(existing_excluded)
    raw["overrides"] = dict(sorted(overrides_raw.items()))

    # 2) 写后校验：合并结果必须仍能被 export 读取。
    AcademicCalendar.from_dict(raw)

    # 3) 原子替换写入（实现见 xjtu_calendar.fileutil，与会话文件共用）。
    atomic_write_text(config_path, json.dumps(raw, ensure_ascii=False, indent=2) + "\n")
    logger.info(
        "学期配置已更新（来源：%s）：新增停课 %d 天、调课 %d 条",
        source_url,
        len(added_excluded),
        len(added_makeups),
    )
    return {
        "added_excluded": added_excluded,
        "added_makeups": added_makeups,
        "warnings": warnings,
    }
