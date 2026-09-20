"""教学周次文本解析。

原始课表里的周次字段是一段自由文本，学校导出脚本、不同学期的教务系统
写法并不统一。本模块只做一件事：把这段文本解析成升序去重的周次列表。

设计约束
--------
- 不依赖 ``eval``，全部手写词法扫描。
- 不假设文本一定规范：跳过无法识别的字符，而不是抛错，除非整段文本
  一个周次都识别不出来（此时才抛 :class:`WeekParseError`）。
- 中文全角括号、全角逗号、全角波浪线等常见变体都要能处理。

支持的写法
----------
==============================  ==================================
输入                             输出
==============================  ==================================
``1-16周``                       ``[1..16]``
``1-8周``                        ``[1..8]``
``9-16周``                       ``[9..16]``
``1,3,5,7周``                    ``[1,3,5,7]``
``2,4,6,8周``                    ``[2,4,6,8]``
``1-4,7-12周``                   ``[1,2,3,4,7..12]``
``2,5-8周``                      ``[2,5,6,7,8]``
``1-16周（单）``                  ``[1,3,5,...,15]``
``1-16周（双）``                  ``[2,4,6,...,16]``
``单周`` / ``双周``               全部奇数周 / 偶数周（相对 max_week）
``第1-8周``                      ``[1..8]``
``1-8``                          ``[1..8]``（无「周」字也接受）
==============================  ==================================
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["WeekParseError", "format_weeks", "parse_week_mask", "parse_weeks"]


class WeekParseError(ValueError):
    """周次文本无法解析成任何有效教学周。"""


#: 周次字段里可能出现、但应当直接忽略的噪声片段。
#: 例如 ``1-8周(1-2节)``、``1-8周 3学分`` 之类混拼的内容。
_IGNORED_PATTERN = re.compile(
    r"\d+\s*[-–—~至]\s*\d+\s*(?:节|课时)"
    r"|\d+\s*(?:节|课时)"
    r"|\d+\s*(?:学分)"
    r"|[0-9]+\s*:\s*[0-9]+",
)

#: 全角 -> 半角归一化表。只映射确实会出现的符号，避免误伤中文语义字符。
_FULLWIDTH_MAP = {
    "（": "(",
    "）": ")",
    "，": ",",
    "、": ",",
    "；": ";",
    "：": ":",
    "－": "-",
    "—": "-",
    "–": "-",
    "～": "~",
    "．": ".",
    "　": " ",
}

_ODD_KEYWORDS = ("单周", "单数周", "(单)", "单)", "单)")
_EVEN_KEYWORDS = ("双周", "双数周", "(双)", "双)", "双)")

#: 周次之间的分隔符。注意**不含空格**：``"1 - 8 周"`` 这类稀疏写法必须
#: 先单独收紧（见 :data:`_SPACED_RANGE_PATTERN`），否则按空格分词会把
#: ``1-8`` 拆成 ``["1","-","8"]``，区间语义就丢了。
_SEPARATOR_PATTERN = re.compile(r"[,;、]+")

#: 形如 ``"1 - 8"`` / ``"1- 8"`` / ``"1 -8"`` 的带空格区间，归一成 ``"1-8"``。
_SPACED_RANGE_PATTERN = re.compile(r"\d+\s*[-~至]\s*\d+")

#: 形如「第3周」「1-8周」的整体匹配（供需要精确匹配的场景使用）。
_ORDER_PATTERN = re.compile(r"^第?\s*(\d+)\s*(?:[-~至]\s*(\d+))?\s*周?$")


@dataclass(frozen=True)
class _Range:
    """一段连续的周次区间（含端点）。"""

    start: int
    end: int

    def expand(self, max_week: int) -> list[int]:
        stop = min(self.end, max_week)
        if stop < self.start:
            return []
        return list(range(self.start, stop + 1))


def normalize_week_text(text: str) -> str:
    """把周次文本归一化成半角、无空白的紧凑形式。

    >>> normalize_week_text("１－１６周（单）")
    '1-16周(单)'
    """
    if not isinstance(text, str):
        raise WeekParseError(f"周次文本必须是字符串，实际得到 {type(text).__name__}")

    out = []
    for ch in text:
        if ch in _FULLWIDTH_MAP:
            out.append(_FULLWIDTH_MAP[ch])
        elif "\uff10" <= ch <= "\uff19":  # 全角数字
            out.append(chr(ord(ch) - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out).strip()


def _detect_parity(normalized: str) -> str | None:
    """返回 ``"odd"`` / ``"even"`` / ``None``。"""
    compact = normalized.replace(" ", "")
    if "单" in compact and "双" not in compact:
        return "odd"
    if "双" in compact and "单" not in compact:
        return "even"
    return None


def _strip_parity_markers(normalized: str) -> str:
    """去掉「单/双」「周」「第」等修饰，只留下数字与区间符号。"""
    text = normalized
    for token in ("单周", "双周", "单数周", "双数周"):
        text = text.replace(token, "")
    # 残缺括号（(单) / (双) 已被上面覆盖，这里清掉落单的括号与虚词）
    text = re.sub(r"[()（）]", " ", text)
    text = text.replace("周", " ").replace("第", " ")
    text = text.replace("单", " ").replace("双", " ")
    return text


def _parse_tokens(text: str) -> list[_Range]:
    """把清理后的文本切成若干 :class:`_Range`。"""
    ranges: list[_Range] = []
    for token in _SEPARATOR_PATTERN.split(text):
        token = token.strip()
        if not token:
            continue

        # 兼容「1~4」「1-4」「1至4」「1—4」等区间写法
        parts = re.split(r"[-~至]", token)
        parts = [p.strip() for p in parts if p.strip()]
        if not parts:
            continue

        numbers: list[int] = []
        for part in parts:
            if part.isdigit():
                numbers.append(int(part))

        if len(numbers) == 1:
            ranges.append(_Range(numbers[0], numbers[0]))
        elif len(numbers) >= 2:
            # 「1-2-3」这类畸形写法取首尾；「3-1」这类逆序写法取 min/max
            ranges.append(_Range(min(numbers[0], numbers[-1]), max(numbers[0], numbers[-1])))
    return ranges


def parse_week_mask(mask: object, *, max_week: int = 30) -> list[int]:
    """解析 eHall ``SKZC`` 周次**位掩码**（如 ``"1111111100000000"``）。

    真实接口（``POST /jwapp/sys/wdkb/modules/xskcb/xskcb.do``，2026-09-20 观测）
    用一个 01 串表示整学期的上课周：第 i 位为 ``1`` 表示第 i+1 周上课。
    这是**结构化**数据，优先于 ``ZCMC`` 展示串（如 ``"1-8周"``）使用。

    Raises
    ------
    WeekParseError
        掩码为空或含有 0/1 之外的字符（调用方应回退到展示串解析）。
    """
    text = str(mask or "").strip()
    if not text:
        raise WeekParseError("周次掩码为空")
    if not set(text) <= {"0", "1"}:
        raise WeekParseError(f"周次掩码含有非法字符：{text[:4]!r}…")

    weeks = [index + 1 for index, bit in enumerate(text) if bit == "1"]
    return [week for week in weeks if week <= max_week]


def parse_weeks(text: str, *, max_week: int = 30) -> list[int]:
    """把周次文本解析为升序去重的教学周列表。

    Parameters
    ----------
    text:
        原始周次文本，例如 ``"2,5-8周"``、``"1-16周（单）"``。
    max_week:
        学期总周数的上界，用于 (a) 裁剪超界周次、(b) 展开裸「单周」/「双周」。
        默认 30，足以覆盖国内高校常见学期长度。

    Returns
    -------
    list[int]
        升序、去重、且全部位于 ``1..max_week`` 的周次列表。

    Raises
    ------
    WeekParseError
        文本为空或完全无法识别出周次时。

    Examples
    --------
    >>> parse_weeks("2,5-8周")
    [2, 5, 6, 7, 8]
    >>> parse_weeks("1-16周（单）")
    [1, 3, 5, 7, 9, 11, 13, 15]
    """
    if text is None:
        raise WeekParseError("周次文本为 None")

    normalized = normalize_week_text(text)
    if not normalized:
        raise WeekParseError("周次文本为空")

    # 先摘掉「(1-2节)」这类与周次无关的数字片段，防止被误读成周次
    cleaned = _IGNORED_PATTERN.sub(" ", normalized)

    parity = _detect_parity(normalized)
    # 先把「1 - 8」这类带空格的区间收紧成「1-8」，
    # 否则后续按空格分词会把区间拆散，``parse_week_range`` 的语义就丢了。
    body = _SPACED_RANGE_PATTERN.sub(
        lambda m: m.group(0).replace(" ", ""), cleaned
    )
    body = _strip_parity_markers(body)
    ranges = _parse_tokens(body)

    if not ranges:
        if parity in ("odd", "even"):
            # 裸「单周」/「双周」：以学期周数上界为范围展开全学期
            ranges = [_Range(1, max_week)]
        else:
            raise WeekParseError(f"无法从 {text!r} 中解析出任何教学周")

    weeks: set[int] = set()
    for rng in ranges:
        weeks.update(rng.expand(max_week))

    if parity == "odd":
        weeks = {w for w in weeks if w % 2 == 1}
    elif parity == "even":
        weeks = {w for w in weeks if w % 2 == 0}

    result = sorted(w for w in weeks if 1 <= w <= max_week)
    if not result:
        raise WeekParseError(f"从 {text!r} 解析出的教学周为空（可能被单双周过滤或超出范围）")
    return result


def format_weeks(weeks: list[int]) -> str:
    """把周次列表压回可读文本，连续区间折叠成 ``a-b``。

    >>> format_weeks([1,2,3,4,7,8,10])
    '1-4,7-8,10'
    """
    if not weeks:
        return ""

    ordered = sorted(set(weeks))
    chunks: list[str] = []
    start = prev = ordered[0]
    for week in ordered[1:]:
        if week == prev + 1:
            prev = week
            continue
        chunks.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = week
    chunks.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(chunks)
