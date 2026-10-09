"""升级安全钉：新代码在**没有台账**时必须逐字节复现 v0.5.0 的考试侧产物（spec D14）。

基线来历：由 tag ``v0.5.0`` 的 ``src/`` 对 ``legacy_exam_inputs.json`` 渲染而成，
两次独立运行逐字节一致。本机的 venv 是 editable 安装、指向主检出的 ``src/``，
所以**必须在 detached worktree 里跑旧代码**，并靠 ``xjtu_calendar.__file__`` 与
``git rev-parse HEAD`` 的输出确认跑的是哪一份——不靠版本号（``__version__`` 读的是
已安装的 dist-info，两边都是旧号）。

重新生成（依赖升级等正当理由导致产物确实该变时）：
``git worktree add --detach <路径> v0.5.0``，在该目录以 ``PYTHONPATH=src`` 照
``lay_out_home()`` 写一次性脚本渲染，先打印 ``xjtu_calendar.__file__`` 与 HEAD 再动手；
逐行检查新产物不含真实个人信息（``LOCATION`` / ``DESCRIPTION`` / ``X-WR-CALNAME``
是真实数据会落进去的三个位置），再连同本用例一起提交。脚本绑定本机路径，故不进仓库。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import build_ics_for_semester

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INPUTS = FIXTURES / "legacy_exam_inputs.json"
GOLDEN = FIXTURES / "legacy_exam_export_v050.ics"


def lay_out_home(tmp_path: Path) -> tuple[Settings, str]:
    """按固件布置 home：三件套 + 课表快照 + **考试快照**，两份快照打同一个 mtime。"""
    bundle = json.loads(INPUTS.read_text(encoding="utf-8"))
    semester = str(bundle["semester_key"])
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    cfg.semester_config_path(semester).write_text(
        json.dumps(bundle["semester_config"], ensure_ascii=False), encoding="utf-8"
    )
    cfg.schedule_config_path().write_text(
        json.dumps(bundle["schedule_config"], ensure_ascii=False), encoding="utf-8"
    )
    stamp = float(bundle["snapshot_mtime"])
    timetable = cfg.raw_timetable_path(semester)
    timetable.write_text(
        json.dumps(bundle["timetable_payload"], ensure_ascii=False), encoding="utf-8"
    )
    os.utime(timetable, (stamp, stamp))
    exams = cfg.raw_exams_path(semester)
    exams.write_text(json.dumps(bundle["exam_payload"], ensure_ascii=False), encoding="utf-8")
    os.utime(exams, (stamp, stamp))
    return cfg, semester


def test_upgrade_first_render_matches_v050_bytes(tmp_path: Path) -> None:
    cfg, semester = lay_out_home(tmp_path)
    assert not cfg.exam_ledger_path(semester).exists()  # 前置：测的就是"升级后第一次渲染"

    assert build_ics_for_semester(cfg, semester).ics.encode("utf-8") == GOLDEN.read_bytes()


def test_render_is_reproducible_without_a_ledger(tmp_path: Path) -> None:
    """同一输入连跑两次字节必须相同——撤销通路不许把挂钟带进产物。

    这条是上面那条的前提：`cancel_expiry_at` 默认取墙钟，若实现时让它影响了
    保留期**之外**的东西（比如每次都重排事件），两次就会分叉，本用例先红，
    而不是让 golden 以"莫名其妙变了"的形式红。
    """
    cfg, semester = lay_out_home(tmp_path)
    first = build_ics_for_semester(cfg, semester).ics
    second = build_ics_for_semester(cfg, semester).ics
    assert first == second


def test_v050_golden_fixture_keeps_its_crlf_bytes() -> None:
    """行尾契约（同 ``tests/test_legacy_course_export_golden.py`` 的最后一条）。

    ``.gitattributes`` 的例外是 glob ``tests/fixtures/*.ics -text``，固件**必须留在这个
    目录**；挪进子目录会被全局 ``* text=auto eol=lf`` 压平，新克隆、CI、本机工作树
    同时红成一场几千字节的假"课程侧回归"。
    """
    raw = GOLDEN.read_bytes()
    assert b"\r\n" in raw, "golden 固件里一个 CRLF 都没有：行尾归一化把它改了"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "固件里混进了裸 LF"
    assert raw.startswith(b"BEGIN:VCALENDAR\r\n")
