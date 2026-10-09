"""``Settings`` 数据目录与文件定位测试。"""

from __future__ import annotations

from xjtu_calendar.config import Settings


def test_raw_exams_paths_live_beside_timetable_snapshots(tmp_path):
    cfg = Settings(home=tmp_path)
    assert cfg.raw_exams_path("2026-2027-1").name == "exams-2026-2027-1.json"
    assert cfg.raw_exams_prev_path("2026-2027-1").name == "exams-2026-2027-1.prev.json"
    assert cfg.raw_exams_path("2026-2027-1").parent == cfg.raw_dir
