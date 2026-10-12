"""课程侧撤销的构建层契约（docs/design/2026-10-12-course-cancellation-uid-v2.md §6.5、§9 第 9-18 条）。

形状照 `tests/test_exporter_cancellation.py`：布置 home → 全量 live 时渲染一次落成留底并
抄成课程台账 → 改写 raw 快照制造"消失" → 再渲染断言撤销。测试**不给生产函数加测试专用
参数**，一切场景都通过改写磁盘上的快照/台账/留底驱动；撤销候选全部由 `baseline_probe`
指向真实留底供给可发性凭据（终审 F2 的课程侧对应物）。
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from exam_support import SEMESTER, STAMP, captured_logs
from icalendar import Calendar
from subscribe_support import make_home, payload_row

from xjtu_calendar.cancellation import (
    CANCELLATION_SUMMARY,
    LedgerEntry,
    load_ledger,
    render_ledger,
)
from xjtu_calendar.exporter import build_ics_for_semester, render_ics
from xjtu_calendar.models import CalendarEvent
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


def _synth_entries(prefix: str, count: int, *, offset_days: int) -> list[LedgerEntry]:
    """批量合成台账条目：`offset_days` 相对 NOW（负数 ⇒ 原定时刻已过去）。

    UID/课程名都是占位值；合成条目**不在** live 集里（live UID 走真实配方），
    于是可发性完全由"哪些 UID 出现在 baseline_probe 留底里"控制。
    """
    entries = []
    for i in range(count):
        start = NOW + timedelta(days=offset_days, hours=i // 10, minutes=5 * (i % 10))
        entries.append(
            LedgerEntry(
                uid=f"{prefix}-{i}@xjtu-timetable-calendar",
                start=start,
                end=start + timedelta(hours=2),
                summary="示例课程甲",
            )
        )
    return entries


def _write_synth_baseline(tmp_path: Path, entries: list[LedgerEntry]) -> Path:
    """把合成条目渲染成"上次发布留底"（台账文本本身就是合法 ICS，供 parse_baseline 读 UID）。"""
    retained = tmp_path / "synth-baseline.ics"
    retained.write_text(render_ledger([], entries, dtstamp=STAMP), encoding="utf-8", newline="")
    return retained


def test_recarried_tombstones_do_not_refuse_later_pushes(tmp_path: Path) -> None:
    """发布阻断级回归（2026-10-12 真数据彩排复现）：迁移后重复携带的墓碑不算新撤销。

    序列照彩排走：①留底 UID 与本次 live 完全不同配方 ⇒ 迁移分支产出墓碑；
    ②把①的产物当基线、①的课程台账当台账、**同一份课表**再渲染一次 ⇒ 第二次不得抛
    `MassCancellationBlocked`（判据旧口径下 12/30 = 40% 会拦下，用户升级后每一次普通
    `push` 都被自己的保险丝误杀），且那批墓碑**仍须原样携带**（产物形态不变，README 承诺
    的「直到原定时刻过去由下一次发布收掉」不许改）。
    """
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    v1 = _synth_entries("v1", 12, offset_days=7)  # 冒充 v0.7 发布出去的 12 条旧事件
    retained_v1 = _write_synth_baseline(tmp_path, v1)

    first = build_ics_for_semester(
        cfg,
        SEMESTER,
        baseline_probe=str(retained_v1),
        cancel_expiry_at=NOW,
        published_baseline_text=retained_v1.read_text(encoding="utf-8"),
    )
    assert first.migration_summary is not None
    assert first.migration_summary.candidates == 12, "前提：迁移分支确实产出了一批墓碑"
    assert _cancelled_uids(first.ics) == {e.uid for e in v1}
    assert first.course_ledger_text is not None

    ledger_path = cfg.course_ledger_path(SEMESTER)
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger_path.write_text(first.course_ledger_text, encoding="utf-8", newline="")
    retained_after = tmp_path / "retained-after-migration.ics"
    retained_after.write_text(first.ics, encoding="utf-8", newline="")

    second = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained_after), cancel_expiry_at=NOW
    )
    assert _cancelled_uids(second.ics) == {e.uid for e in v1}, "墓碑要继续随产物携带，一条都不许少"
    assert len(_cancelled_uids(second.ics)) == 12
    assert second.info["course_cancellations"] == 12
    # 固定标题 ⇒ 墓碑内容恒定：重复携带本身不 bump SEQUENCE（幂等仍然成立）。
    assert _cancelled_sequences(second.ics) == _cancelled_sequences(first.ics)


def test_tombstone_exclusion_leaves_the_fuse_armed(tmp_path: Path) -> None:
    """这条守的是「排除墓碑之后门 2 还是不是保险丝」。

    基线里 60 条已 CANCELLED 的墓碑 + 20 条以 live 形态发布过的旧事件，台账 80 条全部未过期，
    本次课表一条没抓到（这 80 条全成候选）：新撤销 = 20，分母 = 未过期且非墓碑 = 20 ⇒ 100% ≥ 30%
    ⇒ 必须照旧熔断。若把排除逻辑写成"墓碑一律不看"，真正的抓漏就不响了。
    """
    from xjtu_calendar.errors import MassCancellationBlocked

    cfg = make_home(tmp_path, _rows(), semester=SEMESTER)
    tombstones = _synth_entries("tomb", 60, offset_days=7)
    published_live = _synth_entries("fresh", 20, offset_days=9)

    events = [
        CalendarEvent(
            uid=e.uid,
            summary=CANCELLATION_SUMMARY,
            start=e.start,
            end=e.end,
            status="CANCELLED",
        )
        for e in tombstones
    ] + [
        CalendarEvent(uid=e.uid, summary=e.summary, start=e.start, end=e.end)
        for e in published_live
    ]
    retained = tmp_path / "mixed-baseline.ics"
    retained.write_text(render_ics(events, dtstamp=STAMP), encoding="utf-8", newline="")
    _write_course_ledger(cfg, [*tombstones, *published_live])

    with pytest.raises(MassCancellationBlocked) as excinfo:
        build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW)
    # 熔断文案里的分子必须是「本次新撤销」= 20，不是 deliverable 的 80。
    assert "20 条课程事件" in str(excinfo.value)


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


def test_no_exams_leaves_the_course_cancellation_path_working(tmp_path: Path) -> None:
    """§7 表「带 `--no-exams`」那一行：考试开关只关考试侧（D4/D21），课程撤销照常。

    构建层目前只靠代码结构保证这件事（课程撤销块在 `include_exams` 的 `if` 之外），
    这条把它钉成契约：把整块挪进 `if include_exams:` 立刻红。
    """
    rows = _rows()
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    retained = _publish_baseline(tmp_path, cfg, rows=rows)
    _rewrite_snapshot(cfg, [r for r in rows if r["KCH"] != "D-4"])

    result = build_ics_for_semester(
        cfg,
        SEMESTER,
        baseline_probe=str(retained),
        cancel_expiry_at=NOW,
        include_exams=False,
    )

    assert _cancelled_uids(result.ics) == _uids_by_course(
        retained.read_text(encoding="utf-8"), "示例课程丁"
    )
    assert result.info["course_cancellations"] == 2
    assert result.info["exam_events"] == 0, "前提：`--no-exams` 真的关掉了考试侧"
    assert result.exam_ledger_text is None
    assert result.course_ledger_text is not None, "课程台账的回写不许跟着考试开关走"


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


def test_mass_cancellation_fuse_counts_only_upcoming_ledger_entries(tmp_path: Path) -> None:
    """门 2 分母改为「未过期台账条目」（终审裁定）：这条用例就是旧分母那个洞的钉子。

    台账 50 条 = 40 条已过期 + 10 条未过期，10 条未过期全部是撤销候选。
    旧分母：10/50 = 20% < 30% ⇒ 熔断**漏响**，剩余学期 100% 被静默撤销；
    新分母：10/10 = 100% ⇒ 必须抛 MassCancellationBlocked。
    """
    from xjtu_calendar.errors import MassCancellationBlocked

    cfg = make_home(tmp_path, _rows(), semester=SEMESTER)
    expired = _synth_entries("exp", 40, offset_days=-7)
    upcoming = _synth_entries("upc", 10, offset_days=7)
    _write_course_ledger(cfg, [*expired, *upcoming])
    retained = _write_synth_baseline(tmp_path, [*expired, *upcoming])

    with pytest.raises(MassCancellationBlocked):
        build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW)


def test_gate2_narrower_denominator_does_not_flag_small_edits(tmp_path: Path) -> None:
    """反向对照：40 条过期 + 40 条未过期，未过期里只有 1 条具备发布凭据（⇒ 候选 1）⇒ 不抛。

    证明分母收窄没有把常态小调整变成误伤：候选不足 10 条时双条件的比例条件根本轮不到。
    """
    cfg = make_home(tmp_path, _rows(), semester=SEMESTER)
    expired = _synth_entries("exp", 40, offset_days=-7)
    upcoming = _synth_entries("upc", 40, offset_days=7)
    _write_course_ledger(cfg, [*expired, *upcoming])
    retained = _write_synth_baseline(tmp_path, upcoming[:1])

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
    )
    assert result.info["course_cancellations"] == 1
    assert _cancelled_uids(result.ics) == {upcoming[0].uid}


def test_uid_collision_disables_the_course_cancellation_path(tmp_path: Path) -> None:
    """v2 配方放宽了碰撞面（终审裁定）：异名同 UID ⇒ 整条课程撤销通路本次不工作。

    碰撞意味着"分不清哪一行是哪门课"，此时任何撤销判断都不可信：零撤销、不迁移、
    不产出课程台账文本，课表产物照常；只发一条不含课程名的安全 warning（§8）。
    台账里放一条本来会被撤销的旧课（有留底凭据），短路必须让它**不**被撤销。
    """
    rows = [
        _row("示例课程甲", "D-1"),
        _row("示例课程乙", "D-1"),  # 同课程号、同天同节次、异名 ⇒ 同 UID
    ]
    cfg = make_home(tmp_path, rows, semester=SEMESTER)
    stale = _synth_entries("stale", 1, offset_days=7)
    _write_course_ledger(cfg, stale)
    retained = _write_synth_baseline(tmp_path, stale)

    with captured_logs() as records:
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(retained), cancel_expiry_at=NOW
        )

    assert "STATUS:CANCELLED" not in result.ics, "碰撞时一条撤销都不许下发"
    assert result.info["course_cancellations"] == 0
    assert result.course_ledger_text is None, "碰撞时不许改写课程台账"
    assert result.migration_summary is None
    msgs = [
        rec.getMessage() for rec in records if "课程撤销通路本次降级为不撤销" in rec.getMessage()
    ]
    assert msgs, "碰撞短路必须留下一条 warning"
    assert any("2 个课程 UID" in m for m in msgs), "1-2 周两个上课日各撞一个 UID"
    assert not any("示例课程" in m for m in msgs), "warning 只许报数量（§8）"
    assert result.ics, "课表产物照常产出"


def test_no_baseline_makes_gate2_unreachable(tmp_path: Path) -> None:
    """钉住「无基线 ⇒ 熔断不可达」：这是 doctor/render 类调用把熔断说成 FAIL 的反证。

    `baseline=None`（没有上次发布产物）时全部候选进 `unresolvable`、`deliverable` 为空，
    门 2 数的是 deliverable ⇒ 永不触发。如果哪天有人给渲染路径传了基线，
    此用例的语义就该重新讨论。
    """
    cfg = make_home(tmp_path, _rows(), semester=SEMESTER)
    upcoming = _synth_entries("upc", 12, offset_days=7)
    _write_course_ledger(cfg, upcoming)
    # 不传 baseline_probe / published_baseline_text：doctor/render 类的调用形状。

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)
    assert "STATUS:CANCELLED" not in result.ics
    assert result.info["course_cancellations"] == 0
    assert result.migration_summary is None
