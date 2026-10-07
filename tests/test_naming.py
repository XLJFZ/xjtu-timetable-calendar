"""日历标题（年级 + 上/下）的推导规则。

规则来源（2026-10-08 与用户确认）：
- 学期代码必须形如 ``YYYY-YYYY-N``，``N=1`` 上、``N=2`` 下，其余不猜；
- 年级 = 学年起始年 − 入学年级(``NJDM``) + 1，只在 1..5 内显示为大一…大五；
- 跨年级取多数；并列或数据缺失一律回退基础名（宁可少信息，不要错信息）。
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

import pytest
from subscribe_support import make_home, payload_row

from xjtu_calendar.cli import main
from xjtu_calendar.exporter import DEFAULT_CALENDAR_NAME, build_ics_for_semester
from xjtu_calendar.naming import calendar_title
from xjtu_calendar.parser import TimetableParser

BASE = DEFAULT_CALENDAR_NAME


def test_majority_grade_and_first_term():
    assert calendar_title("2026-2027-1", ["2024"] * 18) == f"{BASE} · 大三-上"


def test_second_term_is_marked_xia():
    assert calendar_title("2026-2027-2", ["2025"] * 4) == f"{BASE} · 大二-下"


def test_five_year_program_reaches_da_wu():
    assert calendar_title("2028-2029-1", ["2024"] * 3) == f"{BASE} · 大五-上"


def test_custom_semester_key_falls_back_to_base():
    """学期 key 允许用户自定义（README 举例 2026-fall），那种情况不编造后缀。"""
    assert calendar_title("2026-fall", ["2024"] * 3) == BASE


def test_third_term_number_is_not_guessed():
    assert calendar_title("2026-2027-3", ["2024"] * 3) == BASE


def test_no_grade_information_falls_back_to_base():
    assert calendar_title("2026-2027-1", []) == BASE


def test_non_numeric_grade_falls_back_to_base():
    assert calendar_title("2026-2027-1", ["2024级"]) == BASE


def test_grade_out_of_undergraduate_range_falls_back():
    """2026-2019+1 = 大六，不存在这样的本科生年级，宁可不显示。"""
    assert calendar_title("2026-2027-1", ["2019"]) == BASE


def test_majority_grade_wins_over_single_outlier():
    assert calendar_title("2026-2027-1", ["2024"] * 17 + ["2025"]) == f"{BASE} · 大三-上"


def test_tie_between_grades_falls_back_to_base():
    assert calendar_title("2026-2027-1", ["2024", "2025"]) == BASE


def test_outlier_grade_is_reported_at_info_level(caplog):
    with caplog.at_level(logging.INFO, logger="xjtu_calendar.naming"):
        calendar_title("2026-2027-1", ["2024"] * 17 + ["2025"])
    assert any("年级" in rec.getMessage() for rec in caplog.records)


def test_uniform_grades_produce_no_record(caplog):
    with caplog.at_level(logging.INFO, logger="xjtu_calendar.naming"):
        calendar_title("2026-2027-1", ["2024"] * 5)
    assert not caplog.records


def test_base_name_is_overridable():
    assert calendar_title("2026-2027-1", ["2024"], base="XJTU") == "XJTU · 大三-上"


@pytest.mark.parametrize(
    ("semester", "expected"),
    [("2025-2026-1", "大二-上"), ("2025-2026-2", "大二-下")],
)
def test_both_terms_of_one_academic_year(semester, expected):
    assert calendar_title(semester, ["2024"]) == f"{BASE} · {expected}"


def test_parser_reads_enrollment_year_into_meeting():
    """NJDM（入学年级）要落到 CourseMeeting 上，标题推导才有依据。"""
    payload = {"kbList": [payload_row(NJDM="2024")]}
    _, meetings = TimetableParser(expansion_limit=16).parse(payload)

    assert meetings[0].grade_year == "2024"


def test_parser_tolerates_missing_enrollment_year():
    _, meetings = TimetableParser(expansion_limit=16).parse({"kbList": [payload_row()]})

    assert meetings[0].grade_year is None


# --------------------------------------------------------------------------- #
# 管线接线：build_ics_for_semester / CLI export
# --------------------------------------------------------------------------- #
_REAL_CODE = "2026-2027-1"
_CALNAME = re.compile(r"^X-WR-CALNAME:(.*)$", re.MULTILINE)
_EVENTS = re.compile(r"BEGIN:VEVENT.*?END:VEVENT", re.DOTALL)


def _calname(ics: str) -> str:
    match = _CALNAME.search(ics)
    assert match is not None
    return match.group(1).strip()


def test_build_ics_uses_derived_title_by_default(tmp_path):
    home = make_home(tmp_path / "home", [payload_row(NJDM="2024")], semester=_REAL_CODE)
    result = build_ics_for_semester(home, _REAL_CODE)

    assert _calname(result.ics) == f"{BASE} · 大三-上"


def test_build_ics_custom_semester_key_keeps_base_name(tmp_path):
    """既有用户用 2026-fall 这类自定义 key，标题必须和升级前一致。"""
    home = make_home(tmp_path / "home", [payload_row(NJDM="2024")])
    result = build_ics_for_semester(home, "2026-fall")

    assert _calname(result.ics) == BASE


def test_build_ics_explicit_name_is_verbatim(tmp_path):
    home = make_home(tmp_path / "home", [payload_row(NJDM="2024")], semester=_REAL_CODE)
    result = build_ics_for_semester(home, _REAL_CODE, calendar_name="我的课表")

    assert _calname(result.ics) == "我的课表"


def test_grade_year_never_leaks_into_events(tmp_path):
    """带 NJDM 与不带 NJDM 的两份快照，VEVENT 部分必须逐字节一致。

    这是「订阅端不会因为加了年级就全量重收课」的唯一守卫。
    """
    stamp = datetime(2026, 10, 8, 0, 0, 0, tzinfo=UTC)
    plain = make_home(tmp_path / "plain", [payload_row()], semester=_REAL_CODE)
    graded = make_home(tmp_path / "graded", [payload_row(NJDM="2024")], semester=_REAL_CODE)

    ics_plain = build_ics_for_semester(plain, _REAL_CODE, dtstamp=stamp).ics
    ics_graded = build_ics_for_semester(graded, _REAL_CODE, dtstamp=stamp).ics

    assert _EVENTS.findall(ics_plain) == _EVENTS.findall(ics_graded)
    assert _calname(ics_plain) == BASE
    assert _calname(ics_graded) == f"{BASE} · 大三-上"


def test_cli_export_default_title_and_name_override(tmp_path, monkeypatch):
    home = make_home(tmp_path / "home", [payload_row(NJDM="2024")], semester=_REAL_CODE)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(home.home))

    out = tmp_path / "auto.ics"
    assert main(["export", "--semester", _REAL_CODE, "-o", str(out)]) == 0
    assert _calname(out.read_bytes().decode("utf-8")) == f"{BASE} · 大三-上"

    named = tmp_path / "named.ics"
    assert main(["export", "--semester", _REAL_CODE, "-o", str(named), "--name", "甲班课表"]) == 0
    assert _calname(named.read_bytes().decode("utf-8")) == "甲班课表"
