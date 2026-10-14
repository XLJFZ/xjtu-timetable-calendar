"""节次解析测试。"""

from __future__ import annotations

import pytest

from xjtu_calendar.periods import (
    PeriodOutOfRangeError,
    format_periods,
    parse_periods,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # --- 用户明确点名的用例 ---
        ("1-2节", [1, 2]),
        ("5-8节", [5, 6, 7, 8]),
        # --- 常见连排 ---
        ("3-4节", [3, 4]),
        ("5-6节", [5, 6]),
        ("7-8节", [7, 8]),
        ("9-10节", [9, 10]),
        ("1-4节", [1, 2, 3, 4]),
        # --- 逗号分隔 ---
        ("1,2节", [1, 2]),
        ("1-2,5-6节", [1, 2, 5, 6]),
        ("1,3,5节", [1, 3, 5]),
        # --- 单节 ---
        ("1节", [1]),
        ("11节", [11]),
        # --- 无「节」字 ---
        ("1-2", [1, 2]),
        ("1,2", [1, 2]),
        # --- 全角与波浪线 ---
        ("１－２节", [1, 2]),
        ("1～2节", [1, 2]),
        ("1至2节", [1, 2]),
        ("1，2节", [1, 2]),
        ("1、2节", [1, 2]),
        # --- 含「第」「星期」等噪声 ---
        ("第1-2节", [1, 2]),
        ("1-2节 3-4节", [1, 2, 3, 4]),
        # --- 去重与乱序 ---
        ("2,1-2节", [1, 2]),
        ("5,1-3节", [1, 2, 3, 5]),
        ("4-1节", [1, 2, 3, 4]),
    ],
)
def test_parse_periods(text: str, expected: list[int]) -> None:
    assert parse_periods(text) == expected


def test_parse_periods_splits_space_separated_ranges() -> None:
    """**P2-01**：空格是分隔符，``"1-2节 5-6节"`` 是 1、2、5、6 四节。

    旧行为返回 1..6（凭空多上第 3、4 两节）。与周次侧同一条根因：分词只认 ``,;、``，
    中间那段 ``2 5`` 不是纯数字就被丢弃，剩下的首尾被连成一整段。
    """
    assert parse_periods("1-2节 5-6节") == [1, 2, 5, 6]
    assert parse_periods("3-4节 7-8节") == [3, 4, 7, 8]
    assert parse_periods("1节 5节") == [1, 5]
    # 区间内部的空格照旧收紧（顺序：先收区间，再按空格分词）
    assert parse_periods("1 - 2节 5 - 6节") == [1, 2, 5, 6]


@pytest.mark.parametrize(
    ("text", "limit"),
    [
        ("1-30节", 15),
        ("1-30节", 8),
        ("20节", 15),
        ("16-20节", 15),
    ],
)
def test_parse_periods_refuses_out_of_range_instead_of_clipping(text: str, limit: int) -> None:
    """**P2-02**：显式写出的节次越界 ⇒ 报错，不许裁到 ``1..max_period``。

    旧行为是 `test_parse_periods_clips_upper_bound` 那条被钉住的"裁剪"：
    ``1-30节`` 返回 1..15 ⇒ 一门其实上到第 30 节的课被排成前 15 节，
    产物合法、时间却全错，而且没有任何一处提示。与周次侧的
    :func:`xjtu_calendar.weeks._require_within` 统一成同一条契约。
    """
    with pytest.raises(PeriodOutOfRangeError, match="超出解析上限"):
        parse_periods(text, max_period=limit)


def test_parse_periods_refuses_non_positive_period() -> None:
    """``< 1`` 同样是显式越界（第 0 节不存在），报错而不是悄悄丢掉。"""
    with pytest.raises(PeriodOutOfRangeError, match="必须 >= 1"):
        parse_periods("0-2节")


def test_parse_periods_result_is_sorted_unique() -> None:
    result = parse_periods("4,1-2,2节")
    assert result == sorted(set(result))
    assert result == [1, 2, 4]


@pytest.mark.parametrize("bad", ["", "   ", None, "节", "上午", "??"])
def test_parse_periods_rejects_garbage(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_periods(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("periods", "expected"),
    [
        ([1, 2], "1-2节"),
        ([1, 2, 3, 4, 7], "1-4,7节"),
        ([5], "5节"),
        ([], ""),
        ([2, 1], "1-2节"),
    ],
)
def test_format_periods(periods: list[int], expected: str) -> None:
    assert format_periods(periods) == expected


def test_periods_round_trip() -> None:
    original = [1, 2, 5, 6, 9]
    assert parse_periods(format_periods(original)) == original


def test_parse_periods_does_not_leak_into_weeks() -> None:
    """确保节次解析器不会把周次语义误带进来。"""
    assert parse_periods("1-2节") == [1, 2]
    # 「周」在节次文本里是噪声，不影响结果
    assert parse_periods("1-2节(第3周)") == [1, 2]
