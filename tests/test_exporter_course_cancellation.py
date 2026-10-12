"""课程侧撤销的构建层契约（docs/design/2026-10-12-course-cancellation-uid-v2.md §6.5、§9 第 9-18 条）。

形状照 `tests/test_exporter_cancellation.py`：布置 home → 全量 live 时渲染一次落成留底并
抄成课程台账 → 改写 raw 快照制造"消失" → 再渲染断言撤销。测试**不给生产函数加测试专用
参数**，一切场景都通过改写磁盘上的快照/台账/留底驱动；撤销候选全部由 `baseline_probe`
指向真实留底供给可发性凭据（终审 F2 的课程侧对应物）。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from exam_support import SEMESTER, STAMP, captured_logs
from icalendar import Calendar
from subscribe_support import make_home, payload_row

from xjtu_calendar.cancellation import CANCELLATION_SUMMARY, load_ledger, render_ledger
from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.schedules import combine

#: 课程事件的保留期墙钟：固件里第 1 教学周周一是 2026-09-07，取 9-01 ⇒ 全部课次"未过期"。
NOW = combine(date(2026, 9, 1), "00:00")

#: 9 门最小课程（每门周一第 1~2 节、1-2 周 ⇒ 各 2 个事件、共 18 个）。
COURSE_NAMES = ("示例课程甲", "示例课程乙", "示例课程丙", "示例课程丁", "示例课程戊")


def _row(name: str, key: str, **over: str) -> dict[str, str]:
    return payload_row(KCM=name, KCH=key, **over)


def _rows() -> list[dict[str, str]]:
    extra = ("示例课程己", "示例课程庚", "示例课程辛", "示例课程壬")
    return [_row(name, f"D-{i}") for i, name in enumerate(COURSE_NAMES + extra, start=1)]


def _rewrite_snapshot(cfg, rows: list[dict[str, str]]) -> None:
    """模拟"下次 fetch 之后课变了"：直接覆写 raw 课表快照。"""
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps({"kbList": rows}, ensure_ascii=False), encoding="utf-8"
    )


def _write_course_ledger(cfg, entries) -> Path:
    path = cfg.course_ledger_path(SEMESTER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_ledger([], list(entries), dtstamp=STAMP), encoding="utf-8", newline="")
    return path


def _publish_baseline(tmp_path: Path, cfg, *, rows: list[dict[str, str]] | None = None) -> Path:
    """全量 live 时渲染一次：留底（`baseline_probe` 用）+ 照抄成课程台账。

    台账条目从**渲染产物**里 load 出来，绝不在测试里重算 UID 配方（配方抄第二份，
    看守就废了——与 `exam_support.exam_uids` 同一个理由）。
    """
    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)
    retained = tmp_path / "retained.ics"
    retained.write_text(result.ics, encoding="utf-8", newline="")
    if rows is not None:
        _write_course_ledger(cfg, load_ledger(result.ics, label="留底").values())
    return retained


def _uids_by_course(ics: str, name: str) -> set[str]:
    return {
        str(vevent.get("uid"))
        for vevent in Calendar.from_ical(ics).walk("VEVENT")
        if str(vevent.get("summary") or "") == name
    }


def _cancelled_sequences(ics: str) -> dict[str, int]:
    return {
        str(vevent.get("uid")): int(str(vevent.get("sequence")))
        for vevent in Calendar.from_ical(ics).walk("VEVENT")
        if str(vevent.get("status") or "") == "CANCELLED"
    }


def _cancelled_uids(ics: str) -> set[str]:
    return set(_cancelled_sequences(ics))


def test_vanished_course_is_cancelled_in_next_export(tmp_path: Path) -> None:
    """§9-9 正向 + CD5：台账里有、本次没有 ⇒ 产物出现该 UID 的 CANCELLED 条目，标题固定。"""
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    _rewrite_snapshot(cfg, [r for r in rows if r["KCH"] != "D-4"])

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )

    assert CANCELLATION_SUMMARY in result.ics
    assert "示例课程丁" not in result.ics, "撤销条目不许带课程名（CD5）"
    assert _cancelled_uids(result.ics) == _uids_by_course(
        retained.read_text(encoding="utf-8"), "示例课程丁"
    )
    assert result.info["course_cancellations"] == 2
    for vevent in Calendar.from_ical(result.ics).walk("VEVENT"):
        if str(vevent.get("status") or "") == "CANCELLED":
            assert vevent.get("location") is None, "spec §8：撤销条目不新增暴露面"
            assert vevent.get("description") is None


def test_untrusted_snapshot_cancels_nothing(tmp_path: Path) -> None:
    """门 1（CD13）：有一行解析不了 ⇒ 不读台账、不撤销、course_ledger_text 为 None。

    坏行让 D-4 缺席 live 集——若信任这份数据就会撤销仍然存在的课（与考试侧 F1 同形的坑），
    判据必须先把整条撤销通路关掉。
    """
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    corrupt = [dict(r) for r in rows]
    corrupt[3]["ZCMC"] = "看不懂周次"  # 该行解析失败 ⇒ report.skipped 非空
    _rewrite_snapshot(cfg, corrupt)

    with captured_logs() as records:
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
        )

    assert "STATUS:CANCELLED" not in result.ics
    assert result.course_ledger_text is None
    assert result.info["course_cancellations"] == 0
    trust_off = [
        rec.getMessage() for rec in records if "课程撤销通路本次不启用" in rec.getMessage()
    ]
    assert trust_off, "门 1 收回撤销权必须留下一条日志"
    assert not any("示例课程" in msg for msg in trust_off), "告警只许报类别词（§8）"


def test_mass_cancellation_blocks_the_build(tmp_path: Path) -> None:
    """门 2（CD4/CD12）：9 门课掉到 2 门 ⇒ 抛 MassCancellationBlocked；旗标放行后能正常产出。"""
    from xjtu_calendar.errors import MassCancellationBlocked

    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    _rewrite_snapshot(cfg, rows[-2:])  # 18 条里只剩 4 条 live ⇒ 候选 14、占比 0.78

    with pytest.raises(MassCancellationBlocked):
        build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW)

    result = build_ics_for_semester(
        cfg,
        SEMESTER,
        baseline_probe=str(retained),
        cancel_expiry_at=NOW,
        allow_mass_cancellation=True,
    )
    assert result.info["course_cancellations"] == 14


def test_date_range_does_not_change_course_cancellations(tmp_path: Path) -> None:
    """CD14（§9-14）：裁剪是课程侧的常态操作，对撤销候选与台账回写必须零影响。

    `LATE`（15-16 周，12 月）在窗口 `to_date=2026-09-30` 之外但**仍然 live**：
    候选若吃过滤后的集合，它会被当成"消失"撤销并从台账剪掉——一次局部导出动了全局
    订阅状态。这条用例双向可证伪。
    """
    rows = [
        _row("示例课程甲", "D-1"),
        _row("示例课程乙", "D-2"),
        _row("示例课程迟", "D-3", SKXQ="3", ZCMC="15-16周"),
        _row("示例课程丁", "D-4", SKXQ="4", ZCMC="15-16周"),
    ]
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    _rewrite_snapshot(cfg, rows[:3])  # D-4 真的退了课；D-3 只是被窗口挡在外面

    full = build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW)
    windowed = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW, to_date="2026-09-30"
    )

    gone_uids = _uids_by_course(retained.read_text(encoding="utf-8"), "示例课程丁")
    late_uids = _uids_by_course(retained.read_text(encoding="utf-8"), "示例课程迟")
    assert _cancelled_uids(full.ics) == gone_uids
    assert _cancelled_uids(windowed.ics) == _cancelled_uids(full.ics), (
        "CD14：窗口外 live 课被撤销了"
    )
    for text in (full.course_ledger_text, windowed.course_ledger_text):
        assert text is not None
        assert late_uids <= set(_uids_by_course(text, "示例课程迟")), "CD14：台账被窗口剪掉"


def test_cancellation_is_idempotent_across_repeated_renders(tmp_path: Path) -> None:
    """D8/§9-16：固定标题 ⇒ 指纹恒定 ⇒ 同一输入重渲染，撤销条目的 SEQUENCE 不漂移。"""
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    _rewrite_snapshot(cfg, rows[:8])  # 只掉 1 门（2 条候选），不惊动门 2

    first = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )
    second = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )
    assert _cancelled_sequences(first.ics) == _cancelled_sequences(second.ics)
    assert _cancelled_sequences(first.ics), "前提：确实撤销出了条目"


def test_no_baseline_queued_unresolvable_without_emitting(tmp_path: Path) -> None:
    """F2/F3 的课程侧对应物：没配基线来源 ⇒ 可发性凭据为空集，一条不撤但留在台账排队。

    `info["course_cancellations"]` 只数**真正进了产物**的条目，这条同时钉住计数口径。
    """
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    _publish_baseline(tmp_path, cfg, rows=rows)  # 台账有、留底文件有，但本次不传 baseline_probe
    _rewrite_snapshot(cfg, rows[-2:])

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.info["course_cancellations"] == 0
    assert result.course_ledger_text is not None
    vanished = _uids_by_course(
        cfg.course_ledger_path(SEMESTER).read_text(encoding="utf-8"), "示例课程甲"
    )
    assert vanished and vanished <= {
        str(vevent.get("uid"))
        for vevent in Calendar.from_ical(result.course_ledger_text).walk("VEVENT")
    }, "F3：不可下发的候选必须留在课程台账里排队"
    assert any("无法安全下发撤销" in rec.getMessage() for rec in records)


def test_stats_updated_not_polluted_by_course_cancellations(tmp_path: Path) -> None:
    """D23/§9-17：撤销条目混进 `sequence_stats` 就凭空抬高 `updated`——课程侧同样钉住。"""
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    _rewrite_snapshot(cfg, rows[:8])

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )

    assert result.info["course_cancellations"] == 2
    stats = result.sequence_stats
    assert stats is not None
    assert stats["updated"] == 0, "撤销条目不许进 stats 口径（D23）"
    assert stats["preserved"] == 16  # 留下的 8 门 × 2 事件全部原样
    assert stats["added"] == 0


def test_migration_branch_is_exempt_from_gate_two_and_surfaces_size(tmp_path: Path) -> None:
    """CD11/§9-4 构建层半边：迁移豁免熔断，但规模必须经 migration_summary 冒出来给 CLI 预告。

    形状 = 升级后第一次 `subscribe push`：课程台账还不存在（常态分支自然零候选），
    留底文本由 CLI 读好传进来（exporter 不读文件）。留底 18 条里 14 条不在本次 live——
    若把迁移喂进门 2，这次构建会被自己的升级掐死；豁免则应正常产出全量撤销。
    同时 exporter **只算不写**：订阅目录不许因为一次构建而凭空出现。
    """
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg)  # rows=None ⇒ 不写台账：模拟升级后第一次
    _rewrite_snapshot(cfg, rows[-2:])

    result = build_ics_for_semester(
        cfg,
        SEMESTER,
        cancel_expiry_at=NOW,
        published_baseline_text=retained.read_text(encoding="utf-8"),
    )

    assert result.migration_summary is not None
    assert result.migration_summary.candidates == 14
    assert abs(result.migration_summary.ratio - 14 / 18) < 1e-9
    assert result.info["course_cancellations"] == 14
    text = retained.read_text(encoding="utf-8")
    published = {str(vevent.get("uid")) for vevent in Calendar.from_ical(text).walk("VEVENT")}
    live = _uids_by_course(text, "示例课程辛") | _uids_by_course(text, "示例课程壬")
    assert _cancelled_uids(result.ics) == published - live  # 留底 − live，7 门课全撤
    assert not cfg.subscribe_dir.exists(), "exporter 一个字都不写盘（状态标记回写是 CLI 的职责）"
    # 迁移条目也要回写进课程台账文本，自限到各自原定时刻（与考试侧 pending 同形）。
    assert result.course_ledger_text is not None
