"""撤销注入：门槛（D18）、过滤前候选（D19）、可发性（D20）、关开关（D4/D21）。

这个文件是整条通路**最该被证伪**的地方：`live = ∅` 在这里等于"取消整学期"，
所以三条降级用例与撤销正例**同权重**，一条都不许省。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from exam_support import (
    NOW,
    PAST,
    SEMESTER,
    captured_logs,
    exam_home,
    exam_payload,
    exam_row,
    exam_uids,
    published_ledger,
    timetable_envelope,
    write_ledger,
)
from icalendar import Calendar

from xjtu_calendar.exams import LedgerEntry
from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.fetcher import save_raw
from xjtu_calendar.schedules import combine


def test_vanished_exam_is_cancelled_in_the_next_export(tmp_path: Path) -> None:
    gone_row = exam_row(WID="WID-GONE")
    stay_row = exam_row(WID="WID-STAYS", KCM="示例课程乙")
    cfg = exam_home(tmp_path, gone_row, stay_row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, gone_row, stay_row))

    # 下次 fetch 之后 GONE 不再出现：直接覆写考试快照
    save_raw(exam_payload([stay_row]), cfg, SEMESTER, kind="exams")
    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    cancelled_uid = exam_uids(cfg, gone_row)[0]
    vevent = next(
        v
        for v in Calendar.from_ical(result.ics).walk("VEVENT")
        if str(v.get("uid")) == cancelled_uid
    )
    assert str(vevent.get("status")) == "CANCELLED"
    assert vevent.get("location") is None, "spec D3：撤销条目不许继续公开考场"
    assert vevent.get("description") is None, "spec D3：座位号与主考教师不许再出现"
    assert result.info["exam_cancellations"] == 1


def test_missing_exam_snapshot_cancels_nothing(tmp_path: Path) -> None:
    """D18：没抓过考试 ≠ 没有考试。台账非空也必须一条不撤。"""
    cfg = exam_home(tmp_path)
    ledger = write_ledger(cfg, SEMESTER, published_ledger(cfg))
    before = ledger.read_bytes()
    cfg.raw_exams_path(SEMESTER).unlink()

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is None
    assert ledger.read_bytes() == before, "门槛没过时台账一个字节都不许动（spec §6.6）"
    assert any("考试" in rec.getMessage() for rec in records)


def test_corrupt_exam_snapshot_cancels_nothing(tmp_path: Path) -> None:
    """D18：GBK 坏字节这类 `except Exception` 降级同样收回撤销权。

    形状对应 ``tests/test_exporter_exams.py:204-215``（那条断言的是"产物等于无考试"）；
    这里断言的是**撤销侧**：不得出现 STATUS:CANCELLED，也不得改写台账。
    """
    cfg = exam_home(tmp_path)
    before = write_ledger(cfg, SEMESTER, published_ledger(cfg)).read_bytes()
    cfg.raw_exams_path(SEMESTER).write_bytes(b"\xff\xfe\x00not-utf-8-at-all")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert cfg.exam_ledger_path(SEMESTER).read_bytes() == before


def test_all_rows_unparseable_cancels_nothing(tmp_path: Path) -> None:
    """D18：快照有行、但一条都构建不出来 ⇒ 不可信（与"确认无考试"区分开）。"""
    cfg = exam_home(tmp_path, exam_row(KSSJMS="明天下午"))
    write_ledger(cfg, SEMESTER, published_ledger(cfg))

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is None


def test_confirmed_empty_snapshot_cancels_everything_published(tmp_path: Path) -> None:
    """NO_EXAMS（`rows` 为空的可信快照）**应当**撤销全部已发布考试。"""
    rows = [exam_row(WID="WID-A"), exam_row(WID="WID-B", KCM="示例课程乙")]
    cfg = exam_home(tmp_path, *rows)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, *rows))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert result.ics.count("STATUS:CANCELLED") == 2
    assert result.info["exam_cancellations"] == 2


def test_expired_entries_are_neither_cancelled_nor_kept(tmp_path: Path) -> None:
    """D2/D9：原定时刻已过 ⇒ 什么都不发，条目就此退出台账。"""
    cfg = exam_home(tmp_path)
    write_ledger(cfg, SEMESTER, published_ledger(cfg))

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=PAST)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is not None  # 读到过台账 ⇒ 必须回写（把过期条目剪掉）
    assert result.info["exam_cancellations"] == 0


def test_from_date_filter_does_not_cancel_out_of_window_exams(tmp_path: Path) -> None:
    """D19：窗口外的考试不算"消失"，既不撤销也不剪台账。"""
    inside = exam_row(WID="WID-IN")
    outside = exam_row(
        WID="WID-OUT",
        KCM="示例课程乙",
        KSRQ="2030-09-01 00:00:00",
        KSSJMS="2030-09-01 09:00-11:00(星期日)",
    )
    cfg = exam_home(tmp_path, inside, outside)
    ledger_entries = [
        *published_ledger(cfg, inside),
        LedgerEntry(
            uid=exam_uids(cfg, outside)[0],
            start=combine(date(2030, 9, 1), "09:00"),
            end=combine(date(2030, 9, 1), "11:00"),
            summary="示例课程乙（结课考试）",
        ),
    ]
    write_ledger(cfg, SEMESTER, ledger_entries)

    result = build_ics_for_semester(
        cfg, SEMESTER, from_date="2030-06-01", to_date="2030-06-30", cancel_expiry_at=NOW
    )

    assert "STATUS:CANCELLED" not in result.ics


def test_uid_absent_from_baseline_is_dropped_and_warned(tmp_path: Path) -> None:
    """D20：留底/基线里没有该 UID ⇒ 不发 `SEQUENCE:0` 的撤销，warning + 移出台账。"""
    cfg = exam_home(tmp_path)
    entries = published_ledger(cfg)
    write_ledger(cfg, SEMESTER, entries)
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    with captured_logs() as records:
        # baseline_probe 指向不存在的文件 ⇒ `baseline` 为空 ⇒ UID 不在基线里
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(tmp_path / "no-such.ics"), cancel_expiry_at=NOW
        )

    assert "STATUS:CANCELLED" not in result.ics
    assert any("无法安全下发撤销" in rec.getMessage() for rec in records)
    assert result.exam_ledger_text is not None
    assert entries[0].uid not in result.exam_ledger_text


def test_no_exams_emits_neither_live_nor_cancellations(tmp_path: Path) -> None:
    """D4/D21：关开关时撤销与 live 考试一同缺席，且**不产出**台账文本。"""
    cfg = exam_home(tmp_path)
    before = write_ledger(cfg, SEMESTER, published_ledger(cfg)).read_bytes()
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, include_exams=False)

    assert "STATUS:CANCELLED" not in result.ics
    assert "VEVENT" in result.ics  # 课程侧照常
    assert result.exam_ledger_text is None
    assert cfg.exam_ledger_path(SEMESTER).read_bytes() == before


def test_cancellations_do_not_mask_the_empty_calendar_warning(tmp_path: Path) -> None:
    """课程与 live 考试全空、台账非空 ⇒ 仍要打"没有生成任何事件"。

    判空看的是**撤销之前**的集合（`exporter.py:745-747` 的既有语义：课表才是主功能，
    那句告警的措辞本身就写着"可能全部落在停课日期或被日期过滤排除"）。撤销条目
    不该把一份其实没什么内容的日历撑成"正常"。
    """
    cfg = exam_home(tmp_path)
    write_ledger(cfg, SEMESTER, published_ledger(cfg))
    # 把课表置成**空但合法**的快照（而不是删文件）：删掉课表会让 `load_raw` 直接抛
    # `TimetableFetchError`、整个导出炸掉，也就走不到撤销注入；只有"课表解析出零门课"
    # 才对应本用例要证的场景——课程与 live 考试全空、台账非空。
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps(timetable_envelope([]), ensure_ascii=False), encoding="utf-8"
    )
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert any("没有生成任何事件" in rec.getMessage() for rec in records)
    assert "STATUS:CANCELLED" in result.ics  # 撤销照发，只是不许掩盖告警
