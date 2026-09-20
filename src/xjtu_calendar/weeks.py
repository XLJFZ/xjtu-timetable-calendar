"""教学周次文本解析。

原始课表里的周次字段是一段自由文本，学校导出脚本、不同学期的教务系统
写法并不统一。本模块只做一件事：把这段文本解析成升序去重的周次列表。

设计约束
--------
- 不依赖 ``eval``，全部手写词法扫描。
- 不假设文本一定规范：跳过无法识别的字符，而不是抛错，除非整段文本
  一个周次都识别不出来（此时才抛 :class:`WeekParseError`）。
- 中文全角括号、全角逗号、全角波浪线等常见变体都要能处理。

与「静默错误」有关的硬契约
--------------------------
核心原则：**显式输入不得被静默篡改；内部安全上限不得伪装成业务事实。**

====================  =========================================================
输入类型              越界处理
====================  =========================================================
显式周次文本           ``< 1`` 或 ``> expansion_limit`` 一律抛
（``1-18周``、          :class:`WeekOutOfRangeError`。**绝不裁剪**——
``第20周``、          ``"1-18周"`` 被悄悄变成 ``1-16周`` 属于「生成成功但内容
``1,3,5,31``）         错误」，比直接失败危险得多。
位掩码                第 ``expansion_limit`` 位之后仍有置位 → 抛
（``SKZC``）            :class:`WeekOutOfRangeError`。**绝不只取低位然后假装
                       解析成功**。
裸「单周 / 双周」       这是 shorthand，展开范围只能由 ``expansion_limit``
（无显式数字）         给出。它是**解析安全上限，不是学期长度** ——
                       调用方拿到的是「上限之内的奇数周」，不代表学期真有
                       这么多周。
====================  =========================================================

注意第三种与前两种的性质不同：前两者是**用户/教务系统明确声明的**周次，
越界说明数据或配置有错，必须报错；第三种是我们自己生成的展开，上限只是
「在不知道学期长度时的安全边界」，因此**不报错**，但调用方不应把它当成
「学期有 30 周」这一业务事实。

如果原文自己声明了超界范围（例如 ``1-40周（单）``），仍按第一种处理：
**报错**，因为那是显式输入。

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

__all__ = [
    "DEFAULT_EXPANSION_LIMIT",
    "WeekOutOfRangeError",
    "WeekParseError",
    "format_weeks",
    "parse_week_mask",
    "parse_weeks",
]

#: 解析侧的安全上限。**它不是「学期有多少周」这个业务事实**，只是
#: 「在不知道学期长度时，展开裸单双周 / 判定显式输入是否离谱」的边界。
#: 校历给出真实周数时，调用方应传自己的值（见 `Semester.total_weeks`）。
DEFAULT_EXPANSION_LIMIT = 30


class WeekParseError(ValueError):
    """周次文本无法解析成任何有效教学周。"""


class WeekOutOfRangeError(WeekParseError):
    """显式声明的周次超出解析边界。

    与父类的区别在于**调用方应如何反应**：普通的 :class:`WeekParseError`
    表示「这段文本我看不懂」，可以回退到别的字段或记为跳过；
    而本异常表示「文本看得懂、但内容越界了」，属于数据或配置错误，
    **不应回退、不应裁剪，应当终止并让用户看到**。
    """


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

    def weeks(self) -> list[int]:
        """原样展开，**不做任何裁剪** —— 越界由 :func:`_require_within` 报错。"""
        return list(range(self.start, self.end + 1))


def _require_within(rng: _Range, expansion_limit: int, source: str) -> None:
    """确认区间完全落在 ``1..expansion_limit`` 内，否则抛错。

    这里是「显式输入不得被静默篡改」的落点：以前越界部分会被 ``min()``
    悄悄砍掉，用户拿到的是「解析成功、但少了几周」的结果。
    """
    if rng.start < 1:
        raise WeekOutOfRangeError(
            f"周次 {rng.start} 非法（教学周必须 >= 1），来源：{source!r}"
        )
    if rng.end > expansion_limit:
        raise WeekOutOfRangeError(
            f"周次 {rng.end} 超出解析上限 {expansion_limit}，来源：{source!r}。\n"
            f"  这不一定是数据错误 —— 也可能是解析上限设小了。\n"
            f"  请核对校历（Semester.total_weeks）；若无法确认，"
            f"把 total_weeks 留空，解析会改用默认安全上限 "
            f"{DEFAULT_EXPANSION_LIMIT}。"
        )


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


def parse_week_mask(
    mask: object, *, expansion_limit: int = DEFAULT_EXPANSION_LIMIT
) -> list[int]:
    """解析 eHall ``SKZC`` 周次**位掩码**（如 ``"1111111100000000"``）。

    真实接口（``POST /jwapp/sys/wdkb/modules/xskcb/xskcb.do``，2026-09-20 观测）
    用一个 01 串表示整学期的上课周：第 i 位为 ``1`` 表示第 i+1 周上课。
    这是**结构化**数据，优先于 ``ZCMC`` 展示串（如 ``"1-8周"``）使用。

    掩码是**显式声明**：第 ``expansion_limit`` 位之后仍有 ``1`` 时抛
    :class:`WeekOutOfRangeError`。**绝不只取低若干位然后假装解析成功** ——
    那会静默丢掉学期后段的课。

    Raises
    ------
    WeekParseError
        掩码为空或含有 0/1 之外的字符（调用方可以回退到展示串解析）。
    WeekOutOfRangeError
        置位位置超出 ``expansion_limit``（**调用方不应回退、不应裁剪**）。
    """
    text = str(mask or "").strip()
    if not text:
        raise WeekParseError("周次掩码为空")
    if not set(text) <= {"0", "1"}:
        raise WeekParseError(f"周次掩码含有非法字符：{text[:4]!r}…")

    weeks = [index + 1 for index, bit in enumerate(text) if bit == "1"]
    beyond = [week for week in weeks if week > expansion_limit]
    if beyond:
        raise WeekOutOfRangeError(
            f"周次掩码在第 {beyond[0]} 位（含之后共 {len(beyond)} 位）为 1，"
            f"超出解析上限 {expansion_limit}（掩码长度 {len(text)}）。\n"
            f"  掩码是显式声明，这里不做截断。请核对校历（Semester.total_weeks）；"
            f"若无法确认，把 total_weeks 留空以改用默认安全上限 "
            f"{DEFAULT_EXPANSION_LIMIT}。"
        )
    return weeks


def parse_weeks(
    text: str, *, expansion_limit: int = DEFAULT_EXPANSION_LIMIT
) -> list[int]:
    """把周次文本解析为升序去重的教学周列表。

    Parameters
    ----------
    text:
        原始周次文本，例如 ``"2,5-8周"``、``"1-16周（单）"``。
    expansion_limit:
        解析边界。**它是安全上限，不是「学期总周数」这一业务事实**：
        它只决定两件事 —— (a) 显式声明的周次超过它时是否报错、
        (b) 裸「单周」/「双周」展开到哪一周为止。
        默认 :data:`DEFAULT_EXPANSION_LIMIT`。

    Returns
    -------
    list[int]
        升序去重的周次列表。**所有元素都来自输入本身**（裸单双周除外，
        它由 ``expansion_limit`` 界定），不存在「输入里有、结果里没有了」
        的静默丢弃。

    Raises
    ------
    WeekParseError
        文本为空或完全无法识别出周次时。
    WeekOutOfRangeError
        显式声明的周次 ``< 1`` 或 ``> expansion_limit`` 时。

    Examples
    --------
    >>> parse_weeks("2,5-8周")
    [2, 5, 6, 7, 8]
    >>> parse_weeks("1-16周（单）")
    [1, 3, 5, 7, 9, 11, 13, 15]
    >>> parse_weeks("1-30周", expansion_limit=16)  # 越界报错，不裁剪
    Traceback (most recent call last):
        ...
    xjtu_calendar.weeks.WeekOutOfRangeError: 周次 30 超出解析上限 16，来源：'1-30周'。
    <BLANKLINE>
      这不一定是数据错误 —— 也可能是解析上限设小了。
    <BLANKLINE>
      请核对校历（Semester.total_weeks）；若无法确认，把 total_weeks 留空，解析会改用默认安全上限 30。
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
            # 裸「单周」/「双周」没有显式数字，展开范围只能由解析上限给出。
            # 这是 shorthand —— 上限在此是**展开边界**，不代表学期真的这么长，
            # 所以不报错；但调用方不应把它当作「学期有 expansion_limit 周」。
            ranges = [_Range(1, expansion_limit)]
        else:
            raise WeekParseError(f"无法从 {text!r} 中解析出任何教学周")
    else:
        # 显式书写的周次：越界即报错，绝不裁剪（见 _require_within）
        for rng in ranges:
            _require_within(rng, expansion_limit, _as_source(text, parity))

    weeks: set[int] = set()
    for rng in ranges:
        weeks.update(rng.weeks())

    if parity == "odd":
        weeks = {w for w in weeks if w % 2 == 1}
    elif parity == "even":
        weeks = {w for w in weeks if w % 2 == 0}

    if not weeks:
        raise WeekParseError(f"从 {text!r} 解析出的教学周为空（可能被单双周过滤或超出范围）")
    return sorted(weeks)


def _as_source(text: object, parity: str | None) -> str:
    """给报错用的来源描述：原文 + 单双周修饰。"""
    suffix = {"odd": "（单周）", "even": "（双周）"}.get(parity or "", "")
    return f"{text}{suffix}"


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
