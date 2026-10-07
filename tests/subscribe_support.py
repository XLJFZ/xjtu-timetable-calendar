"""订阅功能的共享夹具：一个完全离线、配置齐全的临时 home。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xjtu_calendar.config import Settings

SEMESTER = "2026-fall"

#: 仓库 examples/ 是官方格式的唯一权威拷贝，直接复用避免 schema 漂移
REPO_ROOT = Path(__file__).resolve().parents[1]


def semester_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "semester": {
            "key": SEMESTER,
            "name": "2026-2027 学年秋季学期",
            "first_week_monday": "2026-09-07",
            "total_weeks": 16,
            "start_date": "2026-09-07",
            "end_date": "2026-12-27",
        },
        "excluded_dates": [],
        "overrides": {},
    }
    return cfg


def payload_row(**over: str) -> dict[str, str]:
    """一门最小课程：周一第 1~2 节，1-2 周，教室 A-1001。

    字段形态与 ``tests/test_cli_diff.py::_row`` 完全一致：``KSJC/JSJC`` 是
    **节次编号**（真实接口形态，parser 优先读它们），不是钟点文本。
    """
    row = {
        "KCM": "示例课程甲",
        "KCH": "D-1",
        "SKXQ": "1",
        "KSJC": "1",
        "JSJC": "2",
        "ZCMC": "1-2周",
        "JASMC": "A-1001",
        "SKJS": "教师甲",
    }
    row.update(over)
    return row


def make_home(tmp_path: Path, rows: list[dict[str, str]] | None = None) -> Settings:
    """在 tmp_path 下布置 home（校历/作息/raw 快照三件套），返回 Settings。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    cfg.semester_config_path(SEMESTER).write_text(
        json.dumps(semester_config(), ensure_ascii=False), encoding="utf-8"
    )
    cfg.schedule_config_path().write_text(
        (REPO_ROOT / "examples" / "schedule.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    payload = {"kbList": rows if rows is not None else [payload_row()]}
    raw = cfg.raw_timetable_path(SEMESTER)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg
