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


def test_fresh_subscriber_still_gets_ledger_text(tmp_path: Path) -> None:
    """§6.6 空值口径：门槛过了且**有 live 考试**就产出文本，哪怕本地还没有台账。

    全新订阅者（live 考试在、还没有台账文件）**必须**拿到非 None 的台账文本：Task 9/10
    只在 `exam_ledger_text is not None` 时落盘建文件，若这里给 None 就永远不会有台账，
    撤销通路对首屏用户直接死掉。文本须含本次 live 考试的 UID（D19：用**过滤前**的 live 集）。
    """
    cfg = exam_home(tmp_path)
    assert not cfg.exam_ledger_path(SEMESTER).exists(), "全新用户：本地还没有台账文件"

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert result.exam_ledger_text is not None
    for uid in exam_uids(cfg):
        assert uid in result.exam_ledger_text


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
    # D19 的另一半：台账同样**不许**按窗口截断——窗口外考试的 UID 必须还在回写文本里，
    # 否则一次局部导出就永久剪掉全局订阅状态（把 `live=` 换成过滤后的 `exam_events` 即红）。
    text = result.exam_ledger_text
    assert text is not None
    assert exam_uids(cfg, outside)[0] in text, "窗口外考试被从台账里剪掉了（D19：应吃过滤前集）"


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


def test_binary_ledger_degrades_without_changing_the_exit(tmp_path: Path) -> None:
    """D15/§7 红线：台账是**二进制/坏编码字节** ⇒ 读它不许冒出 build_ics_for_semester。

    `ledger_path.read_text(encoding="utf-8")` 在注入块里、**不在**外层考试事件 try 的保护
    范围内；不就地兜住就会把 UnicodeDecodeError 抛穿导出，让一个考试侧问题改变退出码。
    期望：不抛异常 + warning（只报学期/失败种类，绝不带台账内容）+ 当作没台账（撤销数 0），
    但仍用本次 live 考试产出 `exam_ledger_text`（覆盖坏台账正是 §7 的修复动作）。
    """
    cfg = exam_home(tmp_path)  # 课表 + live 考试可信
    ledger = cfg.exam_ledger_path(SEMESTER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe\x00not-utf-8-at-all")  # 非 UTF-8 字节，read_text 会炸

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert result.info["exam_cancellations"] == 0, "读不出 ⇒ 当作没台账，一条都不撤"
    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is not None, "仍须产出文本以覆盖这份坏台账"
    assert exam_uids(cfg)[0] in result.exam_ledger_text
    assert any("读不出" in rec.getMessage() for rec in records)


def test_cancellation_colliding_with_course_uid_is_dropped(tmp_path: Path) -> None:
    """§9：伪造一条与**课程 UID 撞车**的撤销条目 ⇒ 走既有全局 UID 断言丢弃路径，不静默写进产物。

    撤销条目不豁免 UID 唯一性。为造出撞车，**从渲染产物里取一条课程 UID**（不在测试里
    重算课程 UID 配方，配方抄第二份看守就废了）；该 UID 在台账里、不在 live 考试集里、
    时刻未过 ⇒ 进入撤销候选，但并入 `render_events` 时撞上课程事件 ⇒ 丢弃 + warning。
    """
    cfg = exam_home(tmp_path)  # 真课表（有课程事件）+ live 考试
    # 只渲染课程侧拿一个真实课程 UID，再用它伪造一条"消失的考试"撤销候选
    courses_only = build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    course_events = list(Calendar.from_ical(courses_only.ics).walk("VEVENT"))
    assert course_events, "课程固件没产出任何 VEVENT ⇒ 撞车前提不成立，先修固件"
    course_uid = str(course_events[0].get("uid"))

    write_ledger(
        cfg,
        SEMESTER,
        [
            LedgerEntry(
                uid=course_uid,
                start=combine(date(2030, 6, 20), "09:00"),
                end=combine(date(2030, 6, 20), "11:00"),
                summary="示例课程甲（结课考试）",
            )
        ],
    )

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics, "撞车撤销条目不许被写进产物"
    assert result.info["exam_cancellations"] == 0
    assert any("撤销事件 UID 与已有事件冲突" in rec.getMessage() for rec in records)
    # 评审遗留 1：被 UID 断言丢掉的条目**不许**留在台账里当 pending——撞车下轮照撞，
    # 留在台账就是永久兑现不了的承诺 + 每轮重试；须与 D20 的 unresolvable 同形退场。
    # （把 `pending_entries` 改回 `deliverable` 即红。）
    text = result.exam_ledger_text
    assert text is not None  # 本次有 live 考试 ⇒ 有台账文本
    assert course_uid not in text, "被丢弃的撤销条目不得继续占用台账"


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


def test_stats_updated_not_polluted_by_cancellations(tmp_path: Path) -> None:
    """D23：撤销条目的 UID 在基线里是 live 形态，混进 stats 就凭空抬高 `updated`。

    这条必须可反证：把 stats 的入参改回 `render_events`（含撤销），
    `updated == 0` 立即变 `updated == 1`。
    """
    cfg = exam_home(tmp_path)
    published = build_ics_for_semester(cfg, SEMESTER)  # 此刻考试还是 live
    baseline = tmp_path / "published.ics"
    baseline.write_text(published.ics, encoding="utf-8", newline="")
    write_ledger(cfg, SEMESTER, published_ledger(cfg))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")  # 确认无考试 ⇒ 全部撤销

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(baseline), cancel_expiry_at=NOW
    )

    assert result.info["exam_cancellations"] == 1
    stats = result.sequence_stats
    assert stats is not None
    published_events = published.ics.count("BEGIN:VEVENT")
    assert stats["preserved"] == published_events - 1  # 少的那条正是被撤销的考试
    assert stats["updated"] == 0
    assert stats["added"] == 0


def test_ledger_text_carries_live_plus_in_window_pending(tmp_path: Path) -> None:
    cfg = exam_home(tmp_path, exam_row(WID="WID-A"), exam_row(WID="WID-B", KCM="示例课程乙"))
    entries = published_ledger(cfg, exam_row(WID="WID-A"), exam_row(WID="WID-B"))
    write_ledger(cfg, SEMESTER, entries)
    save_raw(exam_payload([exam_row(WID="WID-A")]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    text = result.exam_ledger_text
    assert text is not None
    assert "STATUS" not in text and "SEQUENCE" not in text  # D10
    assert entries[0].uid in text  # 本次仍 live
    assert entries[1].uid in text  # 已撤销但仍在窗口内 ⇒ 留在台账里以便下次复述


def test_ledger_text_none_when_neither_ledger_nor_exams(tmp_path: Path) -> None:
    """`None` ⟺ 调用方不得创建也不得改写文件（spec §6.6 的空值口径）。"""
    cfg = exam_home(tmp_path)
    cfg.raw_exams_path(SEMESTER).unlink()
    assert not cfg.exam_ledger_path(SEMESTER).exists()

    assert build_ics_for_semester(cfg, SEMESTER).exam_ledger_text is None


def test_stale_ledger_is_pruned_into_text_when_everything_expired(tmp_path: Path) -> None:
    """读到过台账 ⇒ 必须回写，哪怕剪完什么都不剩（否则过期条目永不退场）。"""
    cfg = exam_home(tmp_path)
    entries = published_ledger(cfg)
    write_ledger(cfg, SEMESTER, entries)
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=PAST)

    assert result.exam_ledger_text is not None
    assert entries[0].uid not in result.exam_ledger_text


def test_corrupt_ledger_without_live_exams_still_emits_text(tmp_path: Path) -> None:
    """R9 裁定（钉死边界，不是事故）：台账存在但读不出、且本次 live 考试为零 ⇒ 仍是文本。

    §6.6 的严格读法或许想要 `None`，裁定的结论是**保持现状**：用当前（空）状态覆盖一份
    读不出的台账正是 §7 规定的修复动作；调用方"不得创建"的约束管的是文件不存在
    的场景，这里文件已存在、写下去的是**修复**；文本字节跨进程稳定（DTSTAMP 取快照
    mtime，不吃挂钟），不会破坏 subscribe 的内容哈希幂等跳过。
    """
    cfg = exam_home(tmp_path)
    ledger = cfg.exam_ledger_path(SEMESTER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe\x00not-utf-8-at-all")  # read_text 直接炸 ⇒ 当作无台账
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")  # 确认无考试 ⇒ live 集为空

    with captured_logs() as records:
        first = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)
        second = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    text = first.exam_ledger_text
    assert text is not None, "R9：读不出的台账 + live 考试为零 ⇒ 仍产出空台账文本用于覆盖"
    assert "STATUS" not in text and "SEQUENCE" not in text  # D10/D22
    assert len(Calendar.from_ical(text).walk("VEVENT")) == 0
    assert second.exam_ledger_text == text, "同一快照 ⇒ 文本字节一致（stamp 来自快照 mtime）"
    assert any("读不出" in rec.getMessage() for rec in records)
