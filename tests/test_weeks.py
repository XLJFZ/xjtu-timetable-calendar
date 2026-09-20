"""周次解析测试。"""

from __future__ import annotations

import pytest

from xjtu_calendar.weeks import (
    WeekOutOfRangeError,
    WeekParseError,
    format_weeks,
    normalize_week_text,
    parse_week_mask,
    parse_weeks,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- 基本连续区间 ---
        ("1-16周", list(range(1, 17))),
        ("1-8周", [1, 2, 3, 4, 5, 6, 7, 8]),
        ("9-16周", list(range(9, 17))),
        ("1-1周", [1]),
        # --- 离散周次 ---
        ("1,3,5,7周", [1, 3, 5, 7]),
        ("2,4,6,8周", [2, 4, 6, 8]),
        ("1,2,3周", [1, 2, 3]),
        # --- 混合区间（用户明确点名的用例） ---
        ("1-4,7-12周", [1, 2, 3, 4, 7, 8, 9, 10, 11, 12]),
        ("2,5-8周", [2, 5, 6, 7, 8]),
        # --- 单双周 ---
        ("1-16周（单）", [1, 3, 5, 7, 9, 11, 13, 15]),
        ("1-16周（双）", [2, 4, 6, 8, 10, 12, 14, 16]),
        ("1-16周(单)", [1, 3, 5, 7, 9, 11, 13, 15]),
        ("1-16周(双)", [2, 4, 6, 8, 10, 12, 14, 16]),
        ("1-8周单周", [1, 3, 5, 7]),
        ("1-8周双周", [2, 4, 6, 8]),
        ("单周", [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23, 25, 27, 29]),
        ("双周", [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30]),
        # --- 「第」前缀 ---
        ("第1-8周", [1, 2, 3, 4, 5, 6, 7, 8]),
        ("第1,3,5周", [1, 3, 5]),
        # --- 无「周」字 ---
        ("1-8", [1, 2, 3, 4, 5, 6, 7, 8]),
        ("1,3,5", [1, 3, 5]),
        # --- 全角与波浪线 ---
        ("１－１６周", list(range(1, 17))),
        ("1～8周", [1, 2, 3, 4, 5, 6, 7, 8]),
        ("1—8周", [1, 2, 3, 4, 5, 6, 7, 8]),
        ("1至8周", [1, 2, 3, 4, 5, 6, 7, 8]),
        ("1，3，5周", [1, 3, 5]),
        ("1、3、5周", [1, 3, 5]),
        # --- 无空格与有多余空格 ---
        ("1 - 8 周", [1, 2, 3, 4, 5, 6, 7, 8]),
        (" 1-8周 ", [1, 2, 3, 4, 5, 6, 7, 8]),
        # --- 去重与乱序 ---
        ("5,1-3,2周", [1, 2, 3, 5]),
        ("3-1周", [1, 2, 3]),
    ],
)
def test_parse_weeks(text: str, expected: list[int]) -> None:
    assert parse_weeks(text) == expected


def test_parse_weeks_rejects_out_of_range_instead_of_clipping() -> None:
    """显式写出的周次越界 → 报错，**绝不裁剪**。

    旧行为是把 ``1-30周`` 悄悄砍成 ``1-16周``，用户拿到「解析成功但少了几周」
    的结果 —— 与 exporter 的 P0 是同一条原则：显式输入不得被静默篡改。
    """
    with pytest.raises(WeekOutOfRangeError, match="超出解析上限 16"):
        parse_weeks("1-30周", expansion_limit=16)


def test_parse_week_mask_rejects_bits_beyond_limit() -> None:
    """位掩码是显式声明：第 limit 位之后仍有置位 → 报错，不取低位了事。"""
    # 第 20 位为 1，但 limit = 16
    mask = "0" * 19 + "1" + "0" * 5
    with pytest.raises(WeekOutOfRangeError, match="超出解析上限 16"):
        parse_week_mask(mask, expansion_limit=16)

    # 置位全部落在范围内则正常返回
    ok_mask = "1" * 16 + "0" * 4
    assert parse_week_mask(ok_mask, expansion_limit=16) == list(range(1, 17))


def test_parse_week_mask_error_is_not_swallowed_as_parse_error() -> None:
    """越界异常必须是 WeekParseError 的子类，但调用方能区分二者。"""
    assert issubclass(WeekOutOfRangeError, WeekParseError)
    mask = "0" * 30 + "1"
    with pytest.raises(WeekOutOfRangeError):
        parse_week_mask(mask, expansion_limit=30)


def test_parse_weeks_parity_uses_expansion_limit_not_semester_fact() -> None:
    """裸「单周 / 双周」按解析边界展开 —— 它是安全上限，不是学期长度。

    因此这里**不报错**（没有显式声明），但调用方不应据此认为学期真有 9 周。
    """
    assert parse_weeks("单周", expansion_limit=9) == [1, 3, 5, 7, 9]
    assert parse_weeks("双周", expansion_limit=9) == [2, 4, 6, 8]


def test_parse_weeks_parity_with_explicit_range_still_validates() -> None:
    """原文自己声明了超界范围（``1-40周（单）``）→ 仍报错，因为那是显式输入。"""
    with pytest.raises(WeekOutOfRangeError, match="超出解析上限"):
        parse_weeks("1-40周（单）", expansion_limit=30)


def test_parse_weeks_rejects_week_zero() -> None:
    """``< 1`` 同样是显式越界，报错而不是悄悄丢掉。"""
    with pytest.raises(WeekOutOfRangeError, match="教学周必须 >= 1"):
        parse_weeks("0-5周")


def test_parse_weeks_ignores_adjacent_period_text() -> None:
    """周次字段里混入的「(1-2节)」不应被误读成周次。"""
    assert parse_weeks("1-8周(1-2节)") == [1, 2, 3, 4, 5, 6, 7, 8]
    assert parse_weeks("1-16周 3学分") == list(range(1, 17))


@pytest.mark.parametrize("bad", ["", "   ", None, "周", "无", "？？"])
def test_parse_weeks_rejects_garbage(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_weeks(bad)  # type: ignore[arg-type]


def test_parse_weeks_rejects_week_outside_range() -> None:
    with pytest.raises(WeekOutOfRangeError):
        parse_weeks("20-25周", expansion_limit=16)


def test_parse_weeks_result_is_sorted_unique() -> None:
    result = parse_weeks("8,3-5,3,1周")
    assert result == sorted(set(result))
    assert result == [1, 3, 4, 5, 8]


def test_normalize_week_text() -> None:
    assert normalize_week_text("１－１６周（单）") == "1-16周(单)"


@pytest.mark.parametrize(
    ("weeks", "expected"),
    [
        ([1, 2, 3, 4], "1-4"),
        ([1, 2, 3, 4, 7, 8, 10], "1-4,7-8,10"),
        ([5], "5"),
        ([], ""),
        ([3, 1, 2], "1-3"),
    ],
)
def test_format_weeks(weeks: list[int], expected: str) -> None:
    assert format_weeks(weeks) == expected


def test_format_weeks_round_trip() -> None:
    """格式化后再解析应还原同一集合（不要求顺序，且仅适用于全覆盖写法）。"""
    original = [1, 2, 3, 4, 7, 8, 10]
    assert parse_weeks(format_weeks(original)) == original
