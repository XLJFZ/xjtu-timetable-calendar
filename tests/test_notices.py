"""官方停课/调课通知解析测试。

fixture 是教务处真实通知《关于2026年国庆、中秋节放假调休及教学工作安排的通知》
（https://due.xjtu.edu.cn/info/1093/10462.htm）的调课表格 HTML，仅去除样式属性。
该通知为公开信息，不含任何个人数据。

核心断言：解析 + 交叉校验的结果必须与用户手工维护过的学期配置一致
（中秋 09-25~27、国庆 10-01~07 停课；09-20 借 10-06、10-10 借 10-07）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from xjtu_calendar.errors import XjtuCalendarError
from xjtu_calendar.notices import (
    apply_notice,
    merge_into_config,
    parse_teaching_notice,
)

FIXTURE = Path(__file__).parent / "fixtures" / "notice_holiday_2026.html"
FIRST_MONDAY = date(2026, 9, 14)  # 2026-2027-1 第一教学周周一（用户已确认）


@pytest.fixture
def table() -> object:
    return parse_teaching_notice(FIXTURE.read_text(encoding="utf-8"))


def test_parse_finds_all_date_rows(table: object) -> None:
    rows = table.rows  # type: ignore[attr-defined]
    date_texts = [r.date_text for r in rows]
    assert date_texts == [
        "9月20日",
        "9月25日",
        "9月26日",
        "9月27日",
        "10月1日",
        "10月2日",
        "10月3日",
        "10月4日",
        "10月5日",
        "10月6日",
        "10月7日",
        "10月10日",
    ]


def test_parse_rowspan_cells_are_expanded(table: object) -> None:
    """9月26/27日 共享一个「停课，放假」合并单元格，两行都必须拿到该文本。"""
    rows = {r.date_text: r for r in table.rows}  # type: ignore[attr-defined]
    assert rows["9月26日"].arrangement == "停课，放假"
    assert rows["9月27日"].arrangement == "停课，放假"
    assert rows["10月2日"].arrangement == "法定节假日，停课，放假"
    assert rows["10月3日"].arrangement == "法定节假日，停课，放假"
    assert rows["10月5日"].arrangement == "停课，放假"


def test_parse_week_text(table: object) -> None:
    rows = {r.date_text: r for r in table.rows}  # type: ignore[attr-defined]
    assert rows["9月20日"].week_text == "第1周星期日"
    assert rows["10月10日"].week_text == "第4周星期六"


def test_parse_captures_note_row(table: object) -> None:
    """作息切换提示（colspan=3 整行）归入 notes，不混进日期行。"""
    assert table.notes == [  # type: ignore[attr-defined]
        "10月1日前仍执行夏秋季作息时间，10月1日始执行冬春季作息时间（见后表）"
    ]


def test_apply_matches_hand_maintained_config(table: object) -> None:
    """解析结果必须与用户手工维护的学期配置完全一致。"""
    result = apply_notice(table, FIRST_MONDAY)  # type: ignore[arg-type]
    assert result.excluded_dates == [
        date(2026, 9, 25),
        date(2026, 9, 26),
        date(2026, 9, 27),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
    ]
    assert result.makeups == [
        (date(2026, 9, 20), date(2026, 10, 6)),
        (date(2026, 10, 10), date(2026, 10, 7)),
    ]
    assert result.unresolved == []


def test_apply_flags_rows_from_another_semester() -> None:
    """把通知套到别的学期（第一周周一不同）→ 周次对不上，全部转人工。"""
    table = parse_teaching_notice(FIXTURE.read_text(encoding="utf-8"))
    result = apply_notice(table, date(2026, 3, 2))  # 2025-2026 春季学期
    assert result.excluded_dates == []
    assert result.makeups == []
    assert len(result.unresolved) == len(table.rows)
    assert all("对不上" in r.reason for r in result.unresolved)


def test_apply_classifies_unknown_wording_as_unresolved() -> None:
    html = "\n".join(
        [
            "<table>",
            "<tr><td>日期</td><td>周次、星期</td><td>调休及教学安排</td></tr>",
            "<tr><td>11月2日</td><td>第8周星期一</td><td>全校运动会</td></tr>",
            "</table>",
        ]
    )
    result = apply_notice(parse_teaching_notice(html), date(2026, 9, 14))
    assert result.excluded_dates == []
    assert result.makeups == []
    assert len(result.unresolved) == 1
    assert result.unresolved[0].reason == "措辞不在已知分类内"


def test_parse_raises_when_table_missing() -> None:
    with pytest.raises(XjtuCalendarError):
        parse_teaching_notice("<html><body>页面改版了，没有表格</body></html>")


# --------------------------------------------------------------------------- #
# 合并（只新增、不覆盖）
# --------------------------------------------------------------------------- #
def _write_config(tmp_path: Path, payload: dict[str, object]) -> Path:
    import json

    path = tmp_path / "semester.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _application() -> object:
    table = parse_teaching_notice(FIXTURE.read_text(encoding="utf-8"))
    return apply_notice(table, FIRST_MONDAY)


def test_merge_adds_entries_into_empty_config(tmp_path: Path) -> None:
    import json

    path = _write_config(tmp_path, {"first_week_monday": "2026-09-14"})
    summary = merge_into_config(path, _application(), source_url="https://example")  # type: ignore[arg-type]
    assert len(summary["added_excluded"]) == 10  # type: ignore[index]
    assert len(summary["added_makeups"]) == 2  # type: ignore[index]
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert "2026-09-25" in merged["excluded_dates"]
    assert merged["overrides"]["2026-09-20"] == {"source_date": "2026-10-06"}
    assert merged["overrides"]["2026-10-10"] == {"source_date": "2026-10-07"}


def test_merge_never_overwrites_existing_values(tmp_path: Path) -> None:
    import json

    path = _write_config(
        tmp_path,
        {
            "first_week_monday": "2026-09-14",
            "excluded_dates": ["2026-09-25"],
            "overrides": {"2026-09-20": {"source_date": "2026-10-06", "note": "人工核对过"}},
        },
    )
    summary = merge_into_config(path, _application(), source_url="https://example")  # type: ignore[arg-type]
    merged = json.loads(path.read_text(encoding="utf-8"))
    # 已有条目原样保留
    assert merged["overrides"]["2026-09-20"]["note"] == "人工核对过"
    # 只补缺失的
    assert "2026-09-26" in merged["excluded_dates"]
    assert "2026-09-25" in merged["excluded_dates"]
    assert summary["added_makeups"] == ["2026-10-10 <- 2026-10-07"]  # type: ignore[index]
    assert summary["warnings"] == []  # type: ignore[index]


def test_merge_warns_on_conflicting_override(tmp_path: Path) -> None:
    import json

    path = _write_config(
        tmp_path,
        {
            "first_week_monday": "2026-09-14",
            "overrides": {"2026-09-20": {"source_date": "2026-10-13"}},
        },
    )
    summary = merge_into_config(path, _application(), source_url="https://example")  # type: ignore[arg-type]
    merged = json.loads(path.read_text(encoding="utf-8"))
    assert merged["overrides"]["2026-09-20"]["source_date"] == "2026-10-13"
    assert any("2026-09-20" in w for w in summary["warnings"])  # type: ignore[index]
