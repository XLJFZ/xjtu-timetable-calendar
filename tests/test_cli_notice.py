"""``notice`` 子命令的 CLI 级端到端测试（正式 schema + 真实 fixture + 临时 HOME）。

为什么必须有这一层
------------------
单元测试只覆盖 ``parse_teaching_notice()`` / ``apply_notice()`` /
``merge_into_config()`` 各自的行为，**抓不到 CLI 自己重新实现一套配置解析**
这类缺陷：正式学期配置把 ``first_week_monday`` 放在 ``semester`` 里，
而 CLI 曾直接在顶层取 ``raw["first_week_monday"]`` —— 三个单元函数全都正常，
``notice`` 命令却对合法配置报「缺少 first_week_monday」。

本文件用 ``XJTU_CALENDAR_HOME`` 指向临时目录、走完整的 ``main()``，
锁住两条契约：

1. 正式 schema 能被读取（预览与 ``--apply`` 都成功）；
2. 存在无法解析的行时 ``--apply`` 拒绝写盘，配置文件字节级不变。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from xjtu_calendar.academic_calendar import AcademicCalendar
from xjtu_calendar.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "notice_holiday_2026.html"
SEMESTER = "2026-2027-1"

OFFICIAL_CONFIG: dict[str, object] = {
    "semester": {
        "key": SEMESTER,
        "name": "2026-2027 学年秋季学期",
        "first_week_monday": "2026-09-14",
        "total_weeks": 18,
    },
    "excluded_dates": [],
    "overrides": {},
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时 XJTU_CALENDAR_HOME，内含一份**正式 schema** 的学期配置。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    semesters = tmp_path / "semesters"
    semesters.mkdir(parents=True, exist_ok=True)
    config_path(tmp_path).write_text(
        json.dumps(OFFICIAL_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


def config_path(home: Path) -> Path:
    return home / "semesters" / f"{SEMESTER}.json"


def _notice_html(path: Path, *rows: str) -> Path:
    body = "".join(rows)
    path.write_text(
        f"<table><tr><td>日期</td><td>周次、星期</td><td>调休及教学安排</td></tr>{body}</table>",
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# F1：正式 schema 可以预览（本轮 P0 bug 的守门测试）
# --------------------------------------------------------------------------- #
def test_notice_preview_reads_official_semester_schema(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """合法正式配置 + --from-file + --semester -> 预览成功。

    旧实现会在顶层找 ``first_week_monday``，对这份配置直接报错退出（返回 1）。
    """
    code = main(["notice", "--from-file", str(FIXTURE), "--semester", SEMESTER])
    assert code == 0

    out = capsys.readouterr().out
    assert "2026-09-25" in out  # 中秋停课
    assert "2026-10-10 <- 按 2026-10-07" in out  # 国庆调课

    # 预览不写盘
    assert json.loads(config_path(home).read_text(encoding="utf-8")) == OFFICIAL_CONFIG


# --------------------------------------------------------------------------- #
# F2：--apply 成功，且结果能被正式领域模型重新加载
# --------------------------------------------------------------------------- #
def test_notice_apply_writes_and_result_is_reloadable(home: Path) -> None:
    code = main(["notice", "--from-file", str(FIXTURE), "--semester", SEMESTER, "--apply"])
    assert code == 0

    academic = AcademicCalendar.from_file(config_path(home))
    excluded = {d.isoformat() for d in academic.excluded_dates}
    assert {"2026-09-25", "2026-09-26", "2026-09-27"} <= excluded
    assert {"2026-10-01", "2026-10-05", "2026-10-06", "2026-10-07"} <= excluded
    assert academic.overrides[date(2026, 9, 20)].source_date == date(2026, 10, 6)
    assert academic.overrides[date(2026, 10, 10)].source_date == date(2026, 10, 7)

    # 正式结构本身不能被写坏（semester 嵌套仍在）
    payload = json.loads(config_path(home).read_text(encoding="utf-8"))
    assert payload["semester"]["key"] == SEMESTER


# --------------------------------------------------------------------------- #
# F3：unresolved + --apply -> 拒绝写盘，文件字节级不变
# --------------------------------------------------------------------------- #
def test_notice_apply_refuses_when_wording_is_unrecognised(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """措辞不在已知分类内的行 -> 拒绝写入。"""
    config = config_path(home)
    before = config.read_bytes()
    bad = _notice_html(
        home / "unknown.html",
        "<tr><td>11月2日</td><td>第8周星期一</td><td>全校运动会</td></tr>",
    )

    code = main(["notice", "--from-file", str(bad), "--semester", SEMESTER, "--apply"])

    assert code != 0
    assert config.read_bytes() == before
    assert "拒绝修改学期配置" in capsys.readouterr().err


def test_notice_apply_refuses_when_date_format_changes(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """日期格式变为 ``2026年11月2日`` -> 必须进入 unresolved 并拒绝写入。

    旧 parser 会因日期格式不认识而 **静默丢掉整行**，于是
    「0 个停课 + 0 个调课」看起来像一次成功解析，配置被改写成空变更。
    """
    config = config_path(home)
    before = config.read_bytes()
    bad = _notice_html(
        home / "datefmt.html",
        "<tr><td>2026年11月2日</td><td>第8周星期一</td><td>停课，放假</td></tr>",
    )

    code = main(["notice", "--from-file", str(bad), "--semester", SEMESTER, "--apply"])

    assert code != 0
    assert config.read_bytes() == before
    assert "无法可靠解析" in capsys.readouterr().err


def test_notice_preview_marks_unresolved_as_not_applicable(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """预览模式仍可运行，但必须明确「存在 unresolved，结果不可直接应用」。"""
    bad = _notice_html(
        home / "unknown.html",
        "<tr><td>11月2日</td><td>第8周星期一</td><td>全校运动会</td></tr>",
    )
    code = main(["notice", "--from-file", str(bad), "--semester", SEMESTER])

    assert code == 0
    assert "不可直接应用" in capsys.readouterr().out


def test_notice_requires_existing_semester_config(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """学期配置不存在 -> 明确报错，不猜、不失败于未知异常。"""
    code = main(["notice", "--from-file", str(FIXTURE), "--semester", "2099-2100-1"])
    assert code != 0
    assert "未找到学期" in capsys.readouterr().err
