"""``--no-exams`` 是否真的透到构建函数。重点锁 rotate：

`_subscribe_rotate` 走同一条 build_ics_for_semester，但现状既不接 --input 也不接旗标，
用户在 push 上明确关掉考试后随手 rotate 一次就会把考试塞回订阅 URL
（设计文档 §6.7：「用户明确关掉的开关被后台重新打开」）。

这里用捕获式假实现**只锁接线**；`include_exams=False` 的真实产物形状在
T8 的 `tests/test_exporter_exams.py` 里已经断过，不重复。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from exam_support import SEMESTER, exam_payload, exam_row
from subscribe_support import make_home

from xjtu_calendar import subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import ExportResult

#: 假的 info 必须凑齐 cmd_export 打印用的键，否则它会 KeyError 而不是走到断言。
STUB_INFO = {
    "semester_name": "示例学期",
    "courses": 1,
    "meetings": 1,
    "events": 1,
    "date_range": "2030-02-25 ~ 2030-06-21",
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """校历/作息/课表快照 + 一份有考试的 raw/exams-<学期>.json。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    cfg = make_home(tmp_path, semester=SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.parent.mkdir(parents=True, exist_ok=True)
    exam_path.write_text(
        json.dumps(exam_payload([exam_row()]), ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """拦下 build_ics_for_semester 记录参数；publish 换成 no-op（真 publish 要动 git）。"""
    calls: list[dict[str, Any]] = []

    def fake(_cfg: Settings, semester: str, **kwargs: Any) -> ExportResult:
        calls.append({"semester": semester, **kwargs})
        return ExportResult(
            ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", info=dict(STUB_INFO), sequence_stats=None
        )

    monkeypatch.setattr("xjtu_calendar.cli.build_ics_for_semester", fake)
    monkeypatch.setattr(
        subscribe,
        "publish",
        lambda _cfg, _state, _ics: subscribe.PublishResult(
            outcome=subscribe.PublishOutcome.PUSHED,
            url="https://e.invalid/x.ics",
            content_sha256="0" * 64,
        ),
    )
    return calls


def _register(cfg: Settings) -> None:
    """跳过 `subscribe init`（它要真远端），直接落一份订阅状态。"""
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


def _export_argv(home: Path, extra: str | None = None) -> list[str]:
    argv = ["export", "--semester", SEMESTER, "-o", str(home / "out.ics")]
    return [*argv, *([extra] if extra else [])]


def _subscribe_argv(action: str, extra: str | None = None) -> list[str]:
    argv = ["subscribe", action, "--semester", SEMESTER]
    return [*argv, *([extra] if extra else [])]


def test_export_forwards_include_exams(home: Path, spy: list[dict[str, Any]]) -> None:
    cfg = Settings(home=home)

    assert main(_export_argv(home)) == 0
    assert spy[-1]["include_exams"] is True  # 默认包含（D2）
    assert main(_export_argv(home, "--no-exams")) == 0
    assert spy[-1]["include_exams"] is False
    assert cfg.raw_exams_path(SEMESTER).is_file()  # 关导出不删本地快照


@pytest.mark.parametrize("action", ["push", "rotate"])
def test_subscribe_actions_forward_the_flag(
    home: Path, spy: list[dict[str, Any]], action: str
) -> None:
    cfg = Settings(home=home)
    _register(cfg)
    if action == "rotate":
        # rotate 只在本地有留底时才重新发布（无留底分支直接 return，压根不调 build）
        last = subscribe.subscribe_dir(cfg) / f"last-{SEMESTER}.ics"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", encoding="utf-8")

    assert main(_subscribe_argv(action, "--no-exams")) == 0
    assert spy[-1]["include_exams"] is False, f"{action} 没把 --no-exams 传到位"
