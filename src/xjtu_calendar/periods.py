"""节次文本解析。

与 :mod:`xjtu_calendar.weeks` 同构：把 ``"1-2节"``、``"3-4节"`` 这类自由文本
解析成节次序号列表。

关键设计：**本模块只输出节次序号，不输出钟点。**
``"1-2节"`` 究竟对应 08:00-09:50 还是 08:30-10:20，取决于当天执行的是
冬季还是夏季作息，属于 :mod:`xjtu_calendar.schedules` 的职责。
"""

from __future__ import annotations

import re

from .weeks import WeekParseError, normalize_week_text

__all__ = ["PeriodParseError", "format_periods", "parse_periods"]


class PeriodParseError(ValueError):
    """节次文本无法解析成任何有效节次。"""


#: 一天中理论上限的节次序号（防止 ``1-100节`` 这类脏数据炸开）
MAX_PERIOD = 15

_SEPARATOR_PATTERN = re.compile(r"[,;、]+")
_IN_RANGE_PATTERN = re.compile(r"[-~至]")

#: 形如 ``"1 - 2"`` 的带空格区间，归一成 ``"1-2"``（与 weeks 同构）。
_SPACED_RANGE_PATTERN = re.compile(r"\d+\s*[-~至]\s*\d+")

#: 括号内容。节次字段常带 ``"(第3周)"``、``"(实验)"`` 之类的补充说明，
#: 里面的数字是**周次**不是节次，必须在分词前整块丢弃，否则会被误读成节次序号。
_PARENTHETICAL_PATTERN = re.compile(r"[（(][^）)]*[）)]")


def parse_periods(text: str, *, max_period: int = MAX_PERIOD) -> list[int]:
    """把节次文本解析为升序去重的节次列表。

    Parameters
    ----------
    text:
        原始节次文本，例如 ``"1-2节"``、``"1-2,5-6节"``、``"5-8节"``。
    max_period:
        节次上界，超过的部分会被裁剪。

    Returns
    -------
    list[int]
        升序、去重、位于 ``1..max_period`` 的节次列表。

    Raises
    ------
    PeriodParseError
        文本为空或无法识别出任何节次。

    Examples
    --------
    >>> parse_periods("1-2节")
    [1, 2]
    >>> parse_periods("1-2,5-6节")
    [1, 2, 5, 6]
    """
    if text is None:
        raise PeriodParseError("节次文本为 None")

    normalized = normalize_week_text(text)
    if not normalized:
        raise PeriodParseError("节次文本为空")

    # 去掉「周」「星期」「第」等修饰，只留数字与区间符号
    body = normalized
    # 先丢掉括号补充说明（里面常是周次，不是节次）
    body = _PARENTHETICAL_PATTERN.sub(" ", body)
    for token in ("星期", "周", "第", "上午", "下午", "晚上", "中午"):
        body = body.replace(token, " ")
    # 收紧带空格的区间写法，避免被空格分词拆散
    body = _SPACED_RANGE_PATTERN.sub(lambda m: m.group(0).replace(" ", ""), body)
    body = body.replace("节", " ")

    periods: set[int] = set()
    for token in _SEPARATOR_PATTERN.split(body):
        token = token.strip()
        if not token:
            continue

        parts = [p.strip() for p in _IN_RANGE_PATTERN.split(token) if p.strip()]
        numbers = [int(p) for p in parts if p.isdigit()]
        if not numbers:
            continue
        if len(numbers) == 1:
            periods.add(numbers[0])
        else:
            lo, hi = min(numbers[0], numbers[-1]), max(numbers[0], numbers[-1])
            periods.update(range(lo, hi + 1))

    result = sorted(p for p in periods if 1 <= p <= max_period)
    if not result:
        raise PeriodParseError(f"无法从 {text!r} 中解析出任何节次")
    return result


def format_periods(periods: list[int]) -> str:
    """把节次列表压回可读文本。

    >>> format_periods([1, 2, 3, 4, 7])
    '1-4,7节'
    """
    if not periods:
        return ""

    ordered = sorted(set(periods))
    chunks: list[str] = []
    start = prev = ordered[0]
    for period in ordered[1:]:
        if period == prev + 1:
            prev = period
            continue
        chunks.append(f"{start}-{prev}" if start != prev else str(start))
        start = prev = period
    chunks.append(f"{start}-{prev}" if start != prev else str(start))
    return ",".join(chunks) + "节"


# 复用 weeks 的异常基类语义：调用方可以统一捕获解析类错误
_PARSE_ERROR_BASE = WeekParseError
