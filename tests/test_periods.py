"""节次解析测试。"""

from __future__ import annotations

import pytest

from xjtu_calendar.periods import format_periods, parse_periods


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


def test_parse_periods_clips_upper_bound() -> None:
    assert parse_periods("1-30节") == list(range(1, 16))
    assert parse_periods("1-30节", max_period=8) == list(range(1, 9))


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
