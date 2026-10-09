"""台账的 CLI 侧行为：写盘时序（D11）、目录缺失与退出码（D24①）、rotate 探针（D24②）。"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from exam_support import (
    SEMESTER,
    captured_logs,
    exam_home,
    exam_payload,
    exam_row,
    exam_uids,
    published_ledger,
    write_ledger,
)

from xjtu_calendar import cli, fileutil, subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import XjtuCalendarError
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
    """真 publish 要动 git；这里换成 no-op，并把调用记进 order 以便断言时序。

    R2 口径：台账这一侧**套壳并转调**——先记 ``"ledger-write"`` 再调真实的
    ``cli._write_exam_ledger`` 让它真的落盘。这样 ``order == ["publish", "ledger-write"]``
    描述的是一次真实发生过的写盘；把被测函数 stub 掉的话，两条时序/字节断言
    都会变成空话（bytes 无论写在哪都不会变）。
    """
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
    real_write_ledger = cli._write_exam_ledger

    def write_and_record(cfg, semester, text):
        order.append("ledger-write")
        real_write_ledger(cfg, semester, text)

    monkeypatch.setattr(cli, "_write_exam_ledger", write_and_record)
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
    ledger_warnings = [
        rec for rec in records if "台账" in rec.getMessage() and "没能写入" in rec.getMessage()
    ]
    assert ledger_warnings
    # spec §6.8「写失败只 **warning**」：只匹配消息的话，降级成 info/debug 照样绿。
    assert all(rec.levelno >= logging.WARNING for rec in ledger_warnings)
    # spec §8「日志不打印台账内容」：台账里有考试原名/考场/座位，异常与告警都不许带上它。
    assert not any("BEGIN:VCALENDAR" in rec.getMessage() for rec in records)
    assert (cfg.home / "out.ics").is_file()  # 产物照写，主功能不受牵连


def test_export_artifact_write_failure_leaves_ledger_untouched(tmp_path, monkeypatch):
    """D11 的反证（export 半边）：产物没落地 ⇒ 台账一个字节都不能动，且 export 非零。

    把 `cmd_export` 的 `_write_exam_ledger(...)` 挪到 `output.write_text(...)` **之前**，
    这条立刻红：本次的台账文本会先落盘，而 D20 会把"这次没能随产物发出去"的候选从台账里
    剪掉 ⇒ 撤销记录被永久抹掉，客户端那边的幽灵考试再没人管了。

    字节比对之所以有牙，是因为固件台账用 `exam_support.STAMP`（固定 2030-01-01）渲染，
    而本次渲染的 DTSTAMP 跟着课表快照 mtime 走 ⇒ 真写一次字节必变。若哪天有人给 CLI
    注入 `dtstamp=`，这条与两条 push 字节用例会一起变成空话。

    产物这一侧的 `OSError` 不靠打桩：把输出路径本身做成目录，`write_text` 必抛
    （Windows 是 `PermissionError`、POSIX 是 `IsADirectoryError`，同为 `OSError`）。
    """
    cfg = _env(tmp_path, monkeypatch)
    ledger = cfg.exam_ledger_path(SEMESTER)
    before = ledger.read_bytes()
    output = cfg.home / "out.ics"
    output.mkdir()

    assert _export(cfg) != 0  # 产物写失败就是真的导出失败，必须 fail-closed
    assert ledger.read_bytes() == before
    assert output.is_dir()  # 产物确实没落地


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


def test_push_writes_ledger_after_publish_succeeds(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    order = _stub_publish(monkeypatch)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert order == ["publish", "ledger-write"]  # D11：先发布成功，再动留底与台账

    # 写进去的必须是**台账文本**，不是产物文本：台账不是发布件，按 D10/D22 永远没有
    # SEQUENCE（`render_ics` 无条件写 SEQUENCE，产物里必现），而待撤销那条考试的 UID
    # 必须原样留在台账里排队。口径与 export 侧的 `test_export_writes_ledger_after_the_output`
    # 一致——少了这两句，把 `result_ics.ics` 误当台账写入的话时序照样绿。
    ledger_text = cfg.exam_ledger_path(SEMESTER).read_text(encoding="utf-8")
    assert "SEQUENCE" not in ledger_text
    assert exam_uids(cfg, exam_row(WID="WID-A"))[0] in ledger_text


def test_push_failure_leaves_ledger_untouched(tmp_path, monkeypatch):
    """D11 的反证：把台账写入挪到 publish 之前，这条立刻红。

    R2 裁决：不 stub `_write_exam_ledger`——让**真**函数在场。台账文本的 DTSTAMP
    跟着课表快照 mtime 走、与固件的固定 2030-01-01（`exam_support.STAMP`）不同，真写
    一次字节必变；若把函数打桩掉，bytes 无论写在哪都不会变，断言就成了空话。
    ⇒ 反过来说：哪天有人给 CLI 注入 `dtstamp=`，这条与另外两条字节比对用例
    （NO_CHANGE、export 产物失败）会一起悄悄变成永真，改固件时务必同步改这里。
    """
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    ledger = cfg.exam_ledger_path(SEMESTER)
    before = ledger.read_bytes()

    def blow_up(_cfg, _state, _ics):
        raise XjtuCalendarError("远端拒绝")

    monkeypatch.setattr(subscribe, "publish", blow_up)
    # §7 fail-closed：发布失败必须是非零退出，不许悄悄当没事发生。
    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    assert ledger.read_bytes() == before


def test_no_change_push_leaves_ledger_untouched(tmp_path, monkeypatch):
    """publish 的「NO_CHANGE 早退」块里台账同样不写：此时台账本来也未变。"""
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    ledger = cfg.exam_ledger_path(SEMESTER)
    _stub_publish(monkeypatch, outcome=subscribe.PublishOutcome.NO_CHANGE)
    before = ledger.read_bytes()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert ledger.read_bytes() == before


def test_push_summary_reports_pending_cancellations(tmp_path, monkeypatch, capsys):
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    _stub_publish(monkeypatch)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "撤销：1 条" in out
    assert "一次性导入" in out  # 不许把话说满：这类客户端仍不会自动删


def test_push_summary_omits_zero_cancellations(tmp_path, monkeypatch, capsys):
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    cfg.exam_ledger_path(SEMESTER).unlink()  # 没有台账 ⇒ 没什么可撤销
    _stub_publish(monkeypatch)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "撤销" not in capsys.readouterr().out
