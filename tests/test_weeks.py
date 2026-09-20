"""周次解析测试。"""

from __future__ import annotations

import pytest

from xjtu_calendar.weeks import format_weeks, normalize_week_text, parse_weeks


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


def test_parse_weeks_clips_to_max_week() -> None:
    """超出学期总周数的部分应被裁掉，而不是原样返回。"""
    assert parse_weeks("1-30周", max_week=16) == list(range(1, 17))


def test_parse_weeks_parity_respects_max_week() -> None:
    assert parse_weeks("单周", max_week=9) == [1, 3, 5, 7, 9]
    assert parse_weeks("双周", max_week=9) == [2, 4, 6, 8]


def test_parse_weeks_ignores_adjacent_period_text() -> None:
    """周次字段里混入的「(1-2节)」不应被误读成周次。"""
    assert parse_weeks("1-8周(1-2节)") == [1, 2, 3, 4, 5, 6, 7, 8]
    assert parse_weeks("1-16周 3学分") == list(range(1, 17))


@pytest.mark.parametrize("bad", ["", "   ", None, "周", "无", "？？"])
def test_parse_weeks_rejects_garbage(bad: object) -> None:
    with pytest.raises(ValueError):
        parse_weeks(bad)  # type: ignore[arg-type]


def test_parse_weeks_rejects_week_outside_range() -> None:
    with pytest.raises(ValueError):
        parse_weeks("20-25周", max_week=16)


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
