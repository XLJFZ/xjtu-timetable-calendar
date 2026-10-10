"""撤销注入：门槛（D18）、过滤前候选（D19）、可发性（D20）、关开关（D4/D21）。

这个文件是整条通路**最该被证伪**的地方：`live = ∅` 在这里等于"取消整学期"，
所以三条降级用例与撤销正例**同权重**，一条都不许省。
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path

import pytest
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


def _sequence_of_uid(ics: str, uid: str) -> int:
    """从产物文本里按 UID 取该 VEVENT 的 SEQUENCE（真实留底驱动的 D8 用例用）。"""
    for vevent in Calendar.from_ical(ics).walk("VEVENT"):
        if str(vevent.get("uid")) == uid:
            return int(str(vevent.get("sequence")))
    raise AssertionError(f"产物里没有 UID {uid}")


def _cancelled_uids(ics: str) -> set[str]:
    """产物里带 STATUS:CANCELLED 的事件 UID 集合。"""
    return {
        str(vevent.get("uid"))
        for vevent in Calendar.from_ical(ics).walk("VEVENT")
        if str(vevent.get("status") or "") == "CANCELLED"
    }


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
    """消失的考试在下次渲染里被撤销（真实留底作可发性凭据）。

    终审 F2 之后，"没有留底"不再等于"以台账为凭据全可下发"——撤销要有一份**发布过的
    证据**才发得出。所以这里先在两场考试都还是 live 形态时渲染一次落成留底，再让 GONE
    消失：可发性判定据这份留底认 GONE 曾发布 ⇒ 撤销它。这条测的正是生产里 push/export
    都带留底/输出基线的真实路径，比旧版靠"无基线兜底"更强。
    """
    gone_row = exam_row(WID="WID-GONE")
    stay_row = exam_row(WID="WID-STAYS", KCM="示例课程乙")
    cfg = exam_home(tmp_path, gone_row, stay_row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, gone_row, stay_row))
    # 真实留底：两场都 live 时渲染一次，作为 SEQUENCE 基线（GONE 的发布凭据）
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )

    # 下次 fetch 之后 GONE 不再出现：直接覆写考试快照
    save_raw(exam_payload([stay_row]), cfg, SEMESTER, kind="exams")
    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )

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
    """NO_EXAMS（`rows` 为空的可信快照）**应当**撤销全部已发布考试。

    终审 F2 之后仍要撤销全部，但可发性得有发布凭据 ⇒ 先渲染一份两场都 live 的留底作基线。
    NO_EXAMS 的"可信"（门槛不收回撤销权）本身由 `test_confirmed_no_exams_is_still_trusted_*`
    单独钉住，这条专注"确认无考试 ⇒ 撤销全部已发布"。
    """
    rows = [exam_row(WID="WID-A"), exam_row(WID="WID-B", KCM="示例课程乙")]
    cfg = exam_home(tmp_path, *rows)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, *rows))
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )

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


def test_uid_absent_from_baseline_is_not_emitted_but_stays_in_ledger(tmp_path: Path) -> None:
    """D20 + 终审 F3：留底/基线里没有该 UID ⇒ 不发 `SEQUENCE:0` 的撤销，warning + **留在台账**。

    F3 之前的实现会把这类候选**从台账剪掉**（"不留下次重来的假象"）——但"不可下发"是**这一次
    调用方**判的（`--no-sequence`、`export -o 新路径`、窗口内导出的留底），剪掉等于让一次没
    发布的渲染永久毁掉下次 push 撤销它的能力 ⇒ ghost 永存。修法：unresolvable 留在台账里排队，
    自限（到自己的 DTSTART 就被 D2 剪掉）。台账保留这半由 `test_stale_ledger_*` 反面对照
    （过期条目才真的退场）。
    """
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
    msgs = [rec.getMessage() for rec in records if "无法安全下发撤销" in rec.getMessage()]
    assert msgs
    # Important 3（Task 12 修复轮）：这条日志在 rotate 的**只读探针**与 publish 失败两条
    # 路径上说的"已移出台账"是假话——那一轮台账根本没有被剪。句子只能描述本次渲染的决定
    # （候选不在留底 ⇒ SEQUENCE:0 ⇒ 客户端忽略 ⇒ 本次不发），不许断言台账记账。
    assert not any("移出台账" in msg for msg in msgs)
    assert result.exam_ledger_text is not None
    # 终审 F3（与旧断言相反）：unresolvable 条目**留在**台账里，等下一次有基线可对照的渲染。
    # （把 `pending_entries` 改回不含 unresolvable 即红。）
    assert entries[0].uid in result.exam_ledger_text


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
    # 终审 F2：可发性要有真实留底为凭据。渲染一份含课程事件的完整产物作基线——课程 UID
    # 本就在产物里 ⇒ 这条伪撤销候选被判"可下发"、进而在并入产物时与课程事件撞车。
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )

    with captured_logs() as records:
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
        )

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
    # 终审 F2：撤销要有真实留底作凭据。在课程与考试都还 live 时先渲染一份留底（含这场
    # 已发布考试的 UID），随后再把课程/考试清空——判空告警看的是撤销前的集合。
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )
    # 把课表置成**空但合法**的快照（而不是删文件）：删掉课表会让 `load_raw` 直接抛
    # `TimetableFetchError`、整个导出炸掉，也就走不到撤销注入；只有"课表解析出零门课"
    # 才对应本用例要证的场景——课程与 live 考试全空、台账非空。
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps(timetable_envelope([]), ensure_ascii=False), encoding="utf-8"
    )
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    with captured_logs() as records:
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
        )

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


def test_partially_unparseable_snapshot_disables_cancellation(tmp_path: Path) -> None:
    """终审 F1（Critical）：快照**逐行**跳过时，仍存在的考试不许被撤销。

    `parse_exam_rows` 与 `build_exam_events` 都是逐行跳过（缺 KCM / 时间文本解析不出 /
    缺 KSRQ / `date.fromisoformat` 抛）。所以"某一场考试的行本次坏了、其它场正常"时，
    那一场缺席 `live_exam_uids` ⇒ 旧实现把它当成"消失"发一条撤销 ⇒ **从所有订阅者日历
    里删掉一条仍然存在的考试**，比这条特性要消除的 v0.5 ghost 更糟（正是 D18 白名单要拦的）。
    修法：撤销可信度要求"快照每一行都被产物代表"——`report.skipped` 非空或
    `len(exam_events) < report.total_candidates` ⇒ 已解析的事件照发、但本次关闭撤销通路，
    并记一条**只含计数**的 warning（§8：不带考试名/考场/座位）。NO_EXAMS（total==0）仍可信。

    基线用真实留底，确保测的是 F1 的"关闭判据"而不是 F2 的"没有基线 ⇒ 一条不撤"分支——
    否则去掉 F1 修法、只留 F2 也能让这条绿，就不是可证伪的了。
    """
    a_row = exam_row(WID="WID-A", KCM="示例课程甲")
    b_good = exam_row(WID="WID-B", KCM="示例课程乙")
    cfg = exam_home(tmp_path, a_row, b_good)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, a_row, b_good))
    # 真实留底：两场都还是 live 形态时渲染一次，作 SEQUENCE 基线（绕开 F2 的空基线）
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )
    # 下次 fetch：B 的时间文本腐化成解析不出的形态 ⇒ 被逐行跳过；A 照常解析。
    broken_b = exam_row(WID="WID-B", KCM="示例课程乙", KSSJMS="明天下午")
    save_raw(exam_payload([a_row, broken_b]), cfg, SEMESTER, kind="exams")

    with captured_logs() as records:
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
        )

    a_uid = exam_uids(cfg, a_row)[0]
    b_uid = exam_uids(cfg, b_good)[0]
    # B 的行本次坏掉、缺席 live 集 ⇒ 旧实现会撤销它；修法必须**不撤**。
    assert b_uid not in _cancelled_uids(result.ics), "F1：坏掉一行的快照撤销了仍然存在的考试"
    assert "STATUS:CANCELLED" not in result.ics
    assert result.info["exam_cancellations"] == 0
    # A 照常以 live 形态出现：关闭撤销通路不许把已解析的事件也一并扣掉。
    live_uids = {str(v.get("uid")) for v in Calendar.from_ical(result.ics).walk("VEVENT")}
    assert a_uid in live_uids
    # 只含计数的 warning：出现"不启用撤销通路"，且不泄漏任何考试个人信息（§8）。
    cancel_off = [rec.getMessage() for rec in records if "不启用撤销通路" in rec.getMessage()]
    assert cancel_off, "部分不可解析的快照必须记一条关闭撤销通路的 warning"
    assert not any("示例课程" in msg for msg in cancel_off), "warning 不许带考试名"


def test_no_baseline_configured_cancels_nothing(tmp_path: Path) -> None:
    """终审 F2：压根没配基线（无 `--sequence-from` / 无 `--baseline_probe`）时一条都不撤销。

    台账记着一场已发布、本次消失的考试，但直接调用 `build_ics_for_semester` 不传任何基线来源
    ⇒ 没有"发布过"的证据可对照 ⇒ 撤销可发性判定给空集 ⇒ 那条进 `unresolvable`、一条都不撤。
    F2 删掉了旧的"没配基线就以台账为凭据全可下发"兜底分支——正是它让一次 `subscribe init` +
    首屏 push 给从没收到过该考试的新订阅者发一条无标题 `STATUS:CANCELLED`。
    断 `exam_cancellations == 0` 就能反证：旧的兜底分支下这里是 1。F3 保证它同时留在台账里。
    """
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert result.info["exam_cancellations"] == 0
    assert "STATUS:CANCELLED" not in result.ics
    assert exam_uids(cfg, row)[0] in result.exam_ledger_text  # F3：留在台账里排队


def test_confirmed_no_exams_is_still_trusted_after_the_partial_guard(tmp_path: Path) -> None:
    """F1 不许顺带改掉 NO_EXAMS：`total_candidates == 0` 仍可信、仍撤销全部已发布考试。

    新增的"每行都被产物代表"判据在空快照上是 `0 < 0` = False、`report.skipped` 空 ⇒ 不触发，
    撤销通路照旧全开。这条与 `test_confirmed_empty_snapshot_*` 成对：一条证正向、一条防止
    把可信判据写宽到把空快照也误关（去掉 F1 里 `elif not exam_events: total==0` 的分支即红）。
    """
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )

    assert result.info["exam_cancellations"] == 1
    assert exam_uids(cfg, row)[0] in _cancelled_uids(result.ics)


def test_cancellation_and_ledger_render_degrade_without_changing_the_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """终审 F4（Important）：撤销计算 + 台账序列化整段都在降级保护里。

    读台账的 `OSError`/`UnicodeDecodeError` 早就就地兜住了，但候选数学与
    `render_exam_ledger`（把一份手改/外来台账交给 icalendar 序列化）没有保护：任何意外
    （发不出的 DTSTART 时区、折不了的属性）都会从 `build_ics_for_semester` 冒出去，把一个
    考试侧问题变成 export/push 的非零退出——违反红线（spec §1、§7：考试侧任何失败一律降级）。
    修法：撤销 + 台账文本整段包 try/except，异常 ⇒ 只记 `type(exc).__name__`、撤销与台账文本
    清空、`render_events` 退回撤销前，产物照常。

    这里给 `render_exam_ledger` 打一个抛异常的桩模拟 icalendar 序列化意外——真实固件喂不出
    "读得进来却发不出去"的台账，打桩是唯一稳定的触发手段（与探针异常用例包装
    `build_ics_for_semester` 同法，代价是耦合函数名）。桩在生成留底之后才挂上，
    保证留底本身是干净的真实产物。
    """
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    retained = tmp_path / "retained.ics"
    retained.write_text(
        build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW).ics,
        encoding="utf-8",
        newline="",
    )
    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    def boom(*_args: object, **_kwargs: object) -> str:
        raise RuntimeError("模拟 icalendar 序列化意外")

    monkeypatch.setattr("xjtu_calendar.exams.render_exam_ledger", boom)

    with captured_logs() as records:
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
        )

    assert "STATUS:CANCELLED" not in result.ics, "异常时撤销条目不许留在产物里"
    assert result.info["exam_cancellations"] == 0
    assert result.exam_ledger_text is None, "异常时不得产出台账文本（调用方因此不写文件）"
    assert "BEGIN:VCALENDAR" in result.ics, "主产物照常生成，考试侧降级不拖崩导出"
    degrade = [rec for rec in records if "撤销通路本次降级" in rec.getMessage()]
    assert degrade and all(rec.levelno >= logging.WARNING for rec in degrade)
    assert not any("BEGIN:VCALENDAR" in rec.getMessage() for rec in degrade)


def test_cancellation_sequence_is_previous_plus_one_from_a_real_baseline(
    tmp_path: Path,
) -> None:
    """终审 D8（承重决策，此前无测试）：首次转撤销的 SEQUENCE == 上次发布 + 1，用真实留底驱动。

    D8 断言"撤销不需要任何新序号机制"——靠 `resolve_sequence` 的既有指纹规则：撤销条目字段
    与上次发布的 live 形态不同 ⇒ 指纹变 ⇒ `previous + 1`。基线必须是**真实留底**（不手拼
    `EventBaseline`）：先把一场考试以 live 形态渲染落成留底，再让它消失，撤销条目的 SEQUENCE
    应恰为留底里那条 +1。（spec §9「首次转撤销的序号 = 上次发布 + 1」点名这条。）
    """
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    uid = exam_uids(cfg, row)[0]
    first = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)
    retained = tmp_path / "retained.ics"
    retained.write_text(first.ics, encoding="utf-8", newline="")
    published_sequence = _sequence_of_uid(first.ics, uid)

    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )
    assert uid in _cancelled_uids(result.ics)
    assert _sequence_of_uid(result.ics, uid) == published_sequence + 1


def test_cancellation_entry_is_idempotent_across_repeated_renders(tmp_path: Path) -> None:
    """终审 D8（幂等半边）：把上次发布的撤销条目当基线再渲一次，序号与字节都不变。

    §6.5 的幂等靠既有指纹规则——首次转撤销后，产物里的撤销条目就是最小字段形态；下次以
    **那份产物**为基线再渲，字段完全相同 ⇒ 指纹不变 ⇒ 序号保持、条目字节一致。这正是
    `subscribe publish` 的 NO_CHANGE 跳过能生效的前提（撤销条目不许每次发布都涨一个序号）。
    全程用真实留底/真实产物驱动基线，不手拼 `EventBaseline`。
    """
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    first = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)
    retained = tmp_path / "retained.ics"
    retained.write_text(first.ics, encoding="utf-8", newline="")
    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")
    uid = exam_uids(cfg, row)[0]

    # 第一次撤销：基线是 live 形态 ⇒ 首次转撤销（序号 +1，见同批 previous_plus_one 用例）。
    r1 = build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW)
    assert uid in _cancelled_uids(r1.ics)
    # 把 r1 当新基线再渲一次：撤销条目字段与 r1 里的最小字段形态完全相同 ⇒ 序号保持、字节一致。
    step2 = tmp_path / "as-baseline.ics"
    step2.write_text(r1.ics, encoding="utf-8", newline="")
    r2 = build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(step2), cancel_expiry_at=NOW)
    assert uid in _cancelled_uids(r2.ics)
    assert _sequence_of_uid(r2.ics, uid) == _sequence_of_uid(r1.ics, uid), (
        "撤销条目不该每次发布都涨一个序号"
    )
    assert r2.ics == r1.ics, "同一条撤销再渲一次必须逐字节相同（publish 的 NO_CHANGE 跳过依赖它）"
