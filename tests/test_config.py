"""``Settings`` 数据目录与文件定位测试。"""

from __future__ import annotations

from xjtu_calendar.config import Settings


def test_raw_exams_paths_live_beside_timetable_snapshots(tmp_path):
    cfg = Settings(home=tmp_path)
    assert cfg.raw_exams_path("2026-2027-1").name == "exams-2026-2027-1.json"
    assert cfg.raw_exams_prev_path("2026-2027-1").name == "exams-2026-2027-1.prev.json"
    assert cfg.raw_exams_path("2026-2027-1").parent == cfg.raw_dir


def test_exam_ledger_path_lives_under_subscribe_dir(tmp_path):
    cfg = Settings(home=tmp_path)
    assert cfg.subscribe_dir == tmp_path / "subscribe"
    assert (
        cfg.exam_ledger_path("2026-2027-1") == tmp_path / "subscribe" / "last-exams-2026-2027-1.ics"
    )


def test_subscribe_function_delegates_to_settings(tmp_path):
    """目录字面量只能有一处（spec §6.1）。"""
    from xjtu_calendar import subscribe

    cfg = Settings(home=tmp_path)
    assert subscribe.subscribe_dir(cfg) == cfg.subscribe_dir
