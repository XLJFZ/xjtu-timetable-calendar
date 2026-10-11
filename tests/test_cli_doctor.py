"""CLI 层：`doctor` 的输出形状与退出码。判据本身在 tests/test_doctor.py。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.doctor import DOCTOR_EXIT_BLOCKED


def _with_session(cfg: Settings) -> None:
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    (cfg.session_dir / "storage_state.json").write_text(
        json.dumps(
            {
                "cookies": [{"name": "JSESSIONID", "value": "v"}],
                "_saved_at": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="utf-8",
    )


@pytest.fixture
def bare_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """配置齐全但**没有会话**的 home：会话是唯一阻塞项。"""
    monkeypatch.setenv("XJTU_SEMESTER", SEMESTER)
    cfg = make_home(tmp_path)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(cfg.home))
    return cfg


def test_doctor_prints_checklist_and_exits_blocked(bare_home: Settings, capsys) -> None:
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == DOCTOR_EXIT_BLOCKED
    assert "体检" in out
    assert "[阻塞] 会话" in out
    assert "session" not in out  # 面向用户的是中文标签，不是内部 name


def test_doctor_lists_every_check_with_a_chinese_label(
    bare_home: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    main(["doctor"])
    out = capsys.readouterr().out
    for label in ("版本", "会话", "学期校历", "作息表", "课表快照", "考试快照", "渲染", "订阅"):
        assert label in out
    assert "结论：" in out


def test_configured_home_exits_zero(bare_home: Settings, capsys) -> None:
    _with_session(bare_home)
    capsys.readouterr()
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 0
    assert "[阻塞]" not in out
    assert "0 项阻塞" in out


def test_doctor_does_not_touch_the_network_without_verify(
    bare_home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xjtu_calendar.doctor as doctor_module

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("doctor 默认不许联网")

    monkeypatch.setattr(doctor_module, "verify_url", _boom)
    assert main(["doctor"]) in (0, DOCTOR_EXIT_BLOCKED)


def test_verify_flag_reaches_the_url_check(
    bare_home: Settings, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """带 --verify 且已配置订阅时才自检 URL；未配置订阅时不该凭空多出一行。"""
    import xjtu_calendar.doctor as doctor_module

    monkeypatch.setattr(doctor_module, "verify_url", lambda url: (True, "HTTP 200"))
    main(["doctor", "--verify"])
    assert "URL 自检" not in capsys.readouterr().out

    _subscribe_state(bare_home)
    code = main(["doctor", "--verify"])
    out = capsys.readouterr().out
    assert "[通过] URL 自检" in out
    assert code in (0, DOCTOR_EXIT_BLOCKED)


def _subscribe_state(cfg: Settings) -> None:
    """写一份最小订阅状态（含 last_push），让 subscription 与 url 两项都有内容。"""
    from xjtu_calendar.subscribe import PushRecord, SubscriptionState, save_state

    save_state(
        cfg,
        SubscriptionState(
            semester=SEMESTER,
            repo_url="https://example.invalid/repo.git",
            branch="cal",
            token="t" * 32,
            url_base="https://example.invalid/cal",
            last_push=PushRecord(pushed_at="2026-10-11T00:00:00Z", content_sha256="x", token="t" * 32),
        ),
    )
