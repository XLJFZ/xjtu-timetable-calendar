"""``schedule`` 子命令的 CLI 级端到端测试（临时 HOME + 官方页固件）。

单元测试（test_schedule_notice.py）之外，这一层锁住 CLI 契约：

1. 默认只预览，不写任何文件；
2. ``--apply`` 产出的 schedule.json 必须能被 ScheduleTable 直接读懂，
   且夏/冬季钟点在 10-01 切换点两侧各归各的；
3. 幂等：第二次 ``--apply`` 文件字节不变；
4. fail-closed：有 unresolved、或学期范围无从确定时拒绝写入。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from xjtu_calendar.cli import main
from xjtu_calendar.schedules import ScheduleTable

FIXTURE = Path(__file__).parent / "fixtures" / "schedule_zxsj.html"
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
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    semesters = tmp_path / "semesters"
    semesters.mkdir(parents=True, exist_ok=True)
    (semesters / f"{SEMESTER}.json").write_text(
        json.dumps(OFFICIAL_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return tmp_path


def schedule_path(home_: Path) -> Path:
    return home_ / "schedules" / "schedule.json"


def _apply(home: Path) -> int:
    return main(["schedule", "--from-file", str(FIXTURE), "--semester", SEMESTER, "--apply"])


def test_schedule_preview_writes_nothing(home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["schedule", "--from-file", str(FIXTURE)])
    assert code == 0
    out = capsys.readouterr().out
    assert "14:30" in out  # 夏秋季第 5 节
    assert "14:00" in out  # 冬春季第 5 节
    assert not schedule_path(home).exists()
    assert not (home / "schedules").exists()


def test_schedule_apply_writes_reloadable_config(home: Path) -> None:
    assert _apply(home) == 0

    table = ScheduleTable.from_file(schedule_path(home))
    # 切换点两侧：09-15（10月1日前）按夏秋季，10-15 按冬春季
    assert table.resolve_period_time(date(2026, 9, 15), [5])[0].strftime("%H:%M") == "14:30"
    assert table.resolve_period_time(date(2026, 10, 15), [5])[0].strftime("%H:%M") == "14:00"


def test_schedule_apply_is_idempotent(home: Path) -> None:
    assert _apply(home) == 0
    before = schedule_path(home).read_bytes()
    assert _apply(home) == 0
    assert schedule_path(home).read_bytes() == before


def test_schedule_apply_refuses_unrecognized_rows(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "unknown.html"
    bad.write_text(
        "<table>"
        "<tr><td>项目</td><td>夏、秋季时间(5月1日开始实行)</td>"
        "<td>冬、春季时间(10月1日开始实行)</td></tr>"
        "<tr><td>第一节课</td><td>8:00-8:50</td><td>8:00-8:50</td></tr>"
        "<tr><td>新增类别</td><td>1:00-2:00</td><td>1:00-2:00</td></tr>"
        "</table>",
        encoding="utf-8",
    )
    code = main(["schedule", "--from-file", str(bad), "--semester", SEMESTER, "--apply"])
    assert code != 0
    assert not schedule_path(home).exists()
    assert "拒绝" in capsys.readouterr().err


def test_schedule_apply_requires_semester_config(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["schedule", "--from-file", str(FIXTURE), "--semester", "2099-2100-1", "--apply"])
    assert code != 0
    assert "未找到学期" in capsys.readouterr().err
    assert not schedule_path(home).exists()


def test_schedule_apply_refuses_when_semester_end_unknown(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """校历没有 total_weeks / end_date 时覆盖范围不可知 -> 拒绝，不猜截止日。"""
    partial = json.loads(json.dumps(OFFICIAL_CONFIG))
    partial["semester"].pop("total_weeks")
    (home / "semesters" / f"{SEMESTER}.json").write_text(
        json.dumps(partial, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    code = _apply(home)
    assert code != 0
    assert "学期末" in capsys.readouterr().err
    assert not schedule_path(home).exists()
