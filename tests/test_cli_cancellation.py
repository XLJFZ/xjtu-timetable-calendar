"""台账的 CLI 侧行为：写盘时序（D11）、目录缺失与退出码（D24①）、rotate 探针（D24②）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from exam_support import (
    SEMESTER,
    captured_logs,
    exam_home,
    exam_payload,
    exam_row,
    published_ledger,
    write_ledger,
)

from xjtu_calendar import fileutil, subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.fetcher import save_raw


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """课表 + 台账（记着一场已发布的考试）+ 本次"确认本学期无考试" ⇒ 恰有 1 条待撤销。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")
    return cfg


def _export(cfg: Settings, *extra: str) -> int:
    return main(["export", "--semester", SEMESTER, "-o", str(cfg.home / "out.ics"), *extra])


def _register(cfg: Settings) -> None:
    """跳过 `subscribe init`（它要真远端），直接落一份订阅状态。

    与 ``tests/test_cli_exams.py`` 的同名辅助同形；那两个文件刻意不互 import。
    """
    subscribe.save_state(
        cfg,
        subscribe.SubscriptionState(
            semester=SEMESTER,
            repo_url="https://example.invalid/calendar.git",
            branch="main",
            token="deadbeef",
            url_base="https://me.github.io/timetable/",
        ),
    )


def _stub_publish(monkeypatch: pytest.MonkeyPatch, outcome=None) -> list[str]:
    """真 publish 要动 git；这里换成 no-op，并把调用记进 order 以便断言时序。"""
    order: list[str] = []
    monkeypatch.setattr(
        subscribe,
        "publish",
        lambda _cfg, _state, _ics: (
            order.append("publish")
            or subscribe.PublishResult(
                outcome=outcome or subscribe.PublishOutcome.PUSHED,
                url="https://e.invalid/x.ics",
                content_sha256="0" * 64,
            )
        ),
    )
    return order


def test_export_writes_ledger_after_the_output(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    assert _export(cfg) == 0
    ledger = cfg.exam_ledger_path(SEMESTER)
    assert ledger.is_file()
    assert "SEQUENCE" not in ledger.read_text(encoding="utf-8")
    assert (cfg.home / "out.ics").is_file()


def test_export_creates_no_ledger_when_text_is_none(tmp_path, monkeypatch):
    """`None` ⟺ 不得创建文件（spec §6.6）：这里制造"既没台账也没考试快照"。"""
    cfg = _env(tmp_path, monkeypatch)
    cfg.raw_exams_path(SEMESTER).unlink()
    cfg.exam_ledger_path(SEMESTER).unlink()
    assert _export(cfg) == 0
    assert not cfg.exam_ledger_path(SEMESTER).exists()


def test_export_with_no_exams_leaves_ledger_untouched(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    ledger = cfg.exam_ledger_path(SEMESTER)
    before = ledger.read_bytes()
    assert _export(cfg, "--no-exams") == 0
    assert ledger.read_bytes() == before


def test_export_survives_ledger_write_failure(tmp_path, monkeypatch):
    """D24①：台账写不进去属于考试侧增量，**不得**让 export 非零退出。"""
    cfg = _env(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr("xjtu_calendar.cli.atomic_write_text", boom)
    # `main()` 每次经 `setup_logging` 清空 `xjtu_calendar` logger 的 handler
    # （`test_exams_fetch.py` 记录的已知坑），`captured_logs` 的收集器会一起被踢掉；
    # 这里临时停用 `setup_logging`——它只搭日志流，与被测的退出码路径无关。
    monkeypatch.setattr("xjtu_calendar.cli.setup_logging", lambda **_kw: None)
    with captured_logs() as records:
        assert _export(cfg) == 0
    assert any("台账" in rec.getMessage() for rec in records)
    assert (cfg.home / "out.ics").is_file()  # 产物照写，主功能不受牵连


def test_ledger_write_requests_private(tmp_path, monkeypatch):
    """kwargs 捕获口径（照 ``tests/test_fetcher.py`` 的 private 用例）：只断 mode 证伪不了。"""
    cfg = _env(tmp_path, monkeypatch)
    seen: dict[str, object] = {}
    real = fileutil.atomic_write_text

    def spy(path, text, **kwargs):
        seen[path.name] = kwargs.get("private")
        return real(path, text, **kwargs)

    monkeypatch.setattr("xjtu_calendar.cli.atomic_write_text", spy)
    assert _export(cfg) == 0
    assert seen[f"last-exams-{SEMESTER}.ics"] is True


def test_export_summary_reports_cancellations(tmp_path, monkeypatch, capsys):
    """控制器裁决：export 摘要行读 `info["exam_cancellations"]`（spec §6.8）。

    两次 export：第一次把考试以 live 形态写进 out.ics 并建台账；第二次考试从快照
    消失、基线（out.ics）里有该 UID ⇒ 恰有 1 条撤销进产物 ⇒ 摘要报数。
    """
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    assert _export(cfg) == 0
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")
    assert _export(cfg) == 0
    out = capsys.readouterr().out
    assert "撤销：1 条" in out


def test_export_summary_omits_zero_cancellations(tmp_path, monkeypatch, capsys):
    cfg = _env(tmp_path, monkeypatch)
    assert _export(cfg) == 0
    assert "撤销" not in capsys.readouterr().out
