"""作息表官方页解析与合并测试（复用 notice 的网格展开 + fail-closed 管道）。

真实依据：教务处「作息时间」公开页 due.xjtu.edu.cn/xxfw/zxsj.htm 的
「学生作息时间表」四列网格（固件 ``schedule_zxsj.html`` 为 2026-10-07 抓取）。
断言的钟点与页面逐格一致；作息切换点（5月1日 / 10月1日）取自列表头原文。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from xjtu_calendar.errors import ParseError
from xjtu_calendar.notices import NoticeParseError
from xjtu_calendar.schedule_notice import (
    SUMMER_PROFILE_KEY,
    WINTER_PROFILE_KEY,
    merge_schedule_config,
    parse_schedule_page,
    plan_schedule,
    tile_periods,
)
from xjtu_calendar.schedules import ScheduleTable

FIXTURE = Path(__file__).parent / "fixtures" / "schedule_zxsj.html"


def _notice():
    return parse_schedule_page(FIXTURE.read_text(encoding="utf-8"))


def _page(rows: str) -> str:
    return (
        "<table><tr><td>学生作息时间表</td><td>项目</td>"
        "<td>夏、秋季时间(5月1日开始实行)</td>"
        "<td>冬、春季时间(10月1日开始实行)</td></tr>" + rows + "</table>"
    )


# --------------------------------------------------------------------------- #
# 解析：教学节次、切换点、非教学行、fail-closed
# --------------------------------------------------------------------------- #
def test_parse_extracts_all_ten_periods() -> None:
    notice = _notice()
    assert sorted(notice.summer) == list(range(1, 11))
    assert sorted(notice.winter) == list(range(1, 11))
    # 与官方页面逐格一致（夏秋季第 5 节 14:30、冬春季第 5 节 14:00 是两季差异所在）
    assert notice.summer[1] == notice.winter[1] == ("8:00", "8:50")
    assert notice.summer[5] == ("14:30", "15:20")
    assert notice.winter[5] == ("14:00", "14:50")
    assert notice.summer[10] == ("20:40", "21:30")
    assert notice.winter[10] == ("20:10", "21:00")


def test_parse_extracts_switch_dates_from_headers() -> None:
    notice = _notice()
    assert notice.summer_switch == (5, 1)
    assert notice.winter_switch == (10, 1)


def test_parse_non_teaching_rows_become_notes() -> None:
    notice = _notice()
    assert notice.unresolved == []
    items = [item for item, _, _ in notice.notes]
    assert "午餐" in items
    assert "预备铃" in items


def test_parse_rejects_page_without_schedule_table() -> None:
    with pytest.raises(NoticeParseError):
        parse_schedule_page("<html><p>页面改版，没有表格了</p></html>")


def test_parse_rejects_page_missing_winter_column() -> None:
    html = (
        "<table><tr><td>项目</td><td>夏季时间</td></tr>"
        "<tr><td>第一节课</td><td>8:00-8:50</td></tr></table>"
    )
    with pytest.raises(NoticeParseError):
        parse_schedule_page(html)


def test_unknown_item_row_goes_to_unresolved_not_silently_dropped() -> None:
    """新增的行类别绝不被静默丢弃——宁可 unresolved 挡住 --apply。"""
    notice = parse_schedule_page(
        _page("<tr><td>新增项目</td><td>1:00-2:00</td><td>1:00-2:00</td></tr>")
    )
    assert len(notice.unresolved) == 1
    assert notice.unresolved[0].item_text == "新增项目"


def test_period_row_with_unparseable_time_is_unresolved() -> None:
    notice = parse_schedule_page(
        _page("<tr><td>第一节课</td><td>8点到8点半</td><td>8:00-8:50</td></tr>")
    )
    assert notice.unresolved
    assert "第一节课" in notice.unresolved[0].item_text


def test_fullwidth_time_digits_are_normalised() -> None:
    notice = parse_schedule_page(
        _page("<tr><td>第一节课</td><td>８:００－８:５０</td><td>8:00-8:50</td></tr>")
    )
    assert notice.summer[1] == ("8:00", "8:50")
    assert notice.unresolved == []


# --------------------------------------------------------------------------- #
# 区间铺排：切换点来自页面，年份覆盖来自学期范围
# --------------------------------------------------------------------------- #
def test_tile_fall_semester_splits_at_october_first() -> None:
    spans = tile_periods(date(2026, 9, 7), date(2026, 12, 28), (5, 1), (10, 1))
    assert [(s, e, p) for s, e, p in spans] == [
        (date(2026, 9, 7), date(2026, 9, 30), SUMMER_PROFILE_KEY),
        (date(2026, 10, 1), date(2026, 12, 28), WINTER_PROFILE_KEY),
    ]


def test_tile_spring_semester_crosses_into_summer() -> None:
    spans = tile_periods(date(2027, 3, 1), date(2027, 6, 30), (5, 1), (10, 1))
    assert [(s, e, p) for s, e, p in spans] == [
        (date(2027, 3, 1), date(2027, 4, 30), WINTER_PROFILE_KEY),
        (date(2027, 5, 1), date(2027, 6, 30), SUMMER_PROFILE_KEY),
    ]


def test_tile_single_regime_when_no_switch_inside() -> None:
    spans = tile_periods(date(2026, 11, 2), date(2027, 1, 15), (5, 1), (10, 1))
    assert spans == [(date(2026, 11, 2), date(2027, 1, 15), WINTER_PROFILE_KEY)]


def test_plan_schedule_from_notice_and_range() -> None:
    plan = plan_schedule(_notice(), lower=date(2026, 9, 7), upper=date(2026, 12, 28))
    assert set(plan.profiles) == {SUMMER_PROFILE_KEY, WINTER_PROFILE_KEY}
    assert plan.profiles[SUMMER_PROFILE_KEY]["periods"]["1"] == ["8:00", "8:50"]
    assert [p["profile"] for p in plan.periods] == [SUMMER_PROFILE_KEY, WINTER_PROFILE_KEY]
    assert plan.unresolved == []


def test_plan_schedule_refuses_unresolved() -> None:
    """存在认不出的行时，plan 直接为空方案并保留 unresolved 供 CLI 拒绝。"""
    notice = parse_schedule_page(
        _page("<tr><td>新增项目</td><td>1:00-2:00</td><td>1:00-2:00</td></tr>")
    )
    plan = plan_schedule(notice, lower=date(2026, 9, 7), upper=date(2026, 12, 28))
    assert plan.unresolved
    assert plan.profiles == {}


# --------------------------------------------------------------------------- #
# 合并写回：只新增、写前写后校验、原子替换
# --------------------------------------------------------------------------- #
def _plan():
    return plan_schedule(_notice(), lower=date(2026, 9, 7), upper=date(2026, 12, 28))


def test_merge_creates_reloadable_config(tmp_path: Path) -> None:
    target = tmp_path / "schedule.json"
    summary = merge_schedule_config(target, _plan())
    assert summary["added_profiles"] == [SUMMER_PROFILE_KEY, WINTER_PROFILE_KEY]
    table = ScheduleTable.from_file(target)
    assert table.resolve_period_time(date(2026, 9, 8), [1, 2])[0].strftime("%H:%M") == "08:00"
    assert table.resolve_period_time(date(2026, 10, 12), [5])[0].strftime("%H:%M") == "14:00"


def test_merge_never_overwrites_existing_profile(tmp_path: Path) -> None:
    target = tmp_path / "schedule.json"
    existing = {
        "profiles": {
            SUMMER_PROFILE_KEY: {"name": "手工核对过的夏季", "periods": {"1": ["08:00", "08:50"]}}
        },
        "periods": [{"start": "2026-09-07", "end": "2026-09-30", "profile": SUMMER_PROFILE_KEY}],
    }
    target.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    summary = merge_schedule_config(target, _plan())

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["profiles"][SUMMER_PROFILE_KEY]["name"] == "手工核对过的夏季"
    assert any("已存在" in w for w in summary["warnings"])
    assert SUMMER_PROFILE_KEY not in summary["added_profiles"]
    # 冬季 profile 与其区间是纯新增
    assert WINTER_PROFILE_KEY in summary["added_profiles"]


def test_merge_skips_overlapping_periods(tmp_path: Path) -> None:
    target = tmp_path / "schedule.json"
    existing = {
        "profiles": {
            SUMMER_PROFILE_KEY: {"name": "夏", "periods": {"1": ["08:00", "08:50"]}},
            WINTER_PROFILE_KEY: {"name": "冬", "periods": {"1": ["08:00", "08:50"]}},
        },
        "periods": [{"start": "2026-09-07", "end": "2026-12-31", "profile": SUMMER_PROFILE_KEY}],
    }
    target.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")

    summary = merge_schedule_config(target, _plan())

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert len(payload["periods"]) == 1  # 与现有区间重叠的新增一律跳过
    assert any("重叠" in w for w in summary["warnings"])


def test_merge_refuses_corrupt_original(tmp_path: Path) -> None:
    target = tmp_path / "schedule.json"
    target.write_text("{ not json", encoding="utf-8")
    before = target.read_bytes()

    with pytest.raises(ParseError):
        merge_schedule_config(target, _plan())

    assert target.read_bytes() == before
