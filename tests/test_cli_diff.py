"""``diff`` 子命令的 CLI 级 e2e：快照比对 + fetch 轮转的联动契约。

锁住三条：

1. 显式 ``--old/--new`` 两个 raw JSON -> 变化逐条可见（周次/教室）；
2. 全同 -> 明确「无变化」（而不是空输出让人怀疑没跑成功）；
3. 默认路径来自 ``fetch`` 的快照轮转：连续两次 fetch 后 ``diff --semester`` 直接可用；
   只有一次 fetch 时明确告知缺上一份快照以及怎么补救。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from xjtu_calendar.cli import main

SEMESTER = "2026-fall"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    return tmp_path


def _row(
    course: str,
    cid: str,
    weeks: str = "1-3周",
    room: str = "A-1001",
    teacher: str = "教师甲",
) -> dict[str, Any]:
    return {
        "KCM": course,
        "KCH": cid,
        "SKXQ": "5",
        "KSJC": "1",
        "JSJC": "2",
        "ZCMC": weeks,
        "JASMC": room,
        "SKJS": teacher,
    }


def _write_payload(path: Path, *rows: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"kbList": list(rows)}, ensure_ascii=False), encoding="utf-8")
    return path


def test_diff_explicit_paths_reports_changes(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _write_payload(
        tmp_path / "old.json", _row("示例课程甲", "D-1", weeks="1-4周", room="A-1001")
    )
    new = _write_payload(
        tmp_path / "new.json", _row("示例课程甲", "D-1", weeks="1-3周", room="A-2002")
    )

    code = main(["diff", "--old", str(old), "--new", str(new)])
    assert code == 0

    out = capsys.readouterr().out
    assert "周次" in out and "1-4" in out and "1-3" in out
    assert "教室" in out and "A-2002" in out


def test_diff_identical_snapsticks_says_no_change(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _row("示例课程甲", "D-1")
    old = _write_payload(tmp_path / "old.json", row)
    new = _write_payload(tmp_path / "new.json", row)

    code = main(["diff", "--old", str(old), "--new", str(new)])
    assert code == 0
    assert "无变化" in capsys.readouterr().out


def test_diff_defaults_after_two_fetches(home: Path, tmp_path: Path) -> None:
    a = _write_payload(tmp_path / "a.json", _row("示例课程甲", "D-1", teacher="教师甲"))
    b = _write_payload(tmp_path / "b.json", _row("示例课程甲", "D-1", teacher="教师乙"))

    assert main(["fetch", "--from-file", str(a), "--semester", SEMESTER]) == 0
    assert main(["fetch", "--from-file", str(b), "--semester", SEMESTER]) == 0

    prev = home / "raw" / f"timetable-{SEMESTER}.prev.json"
    assert prev.is_file()  # fetch 轮转出了上一份快照

    code = main(["diff", "--semester", SEMESTER])
    assert code == 0


def test_diff_without_previous_snapshot_explains(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _write_payload(tmp_path / "a.json", _row("示例课程甲", "D-1"))
    assert main(["fetch", "--from-file", str(a), "--semester", SEMESTER]) == 0

    code = main(["diff", "--semester", SEMESTER])
    assert code != 0
    err = capsys.readouterr().err
    assert "--old" in err  # 给出补救办法，而不是只说「文件不存在」
