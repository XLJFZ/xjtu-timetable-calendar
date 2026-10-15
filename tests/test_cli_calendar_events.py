"""0.9-P1-03 校历事件的 CLI 接入与两道发布门。

覆盖三件事，每条都对应设计文档里一句裁定：

- ``--include-calendar-events`` 贯通 ``export`` / ``subscribe push`` / ``subscribe rotate``
  （§6 第 2 条：**默认关**，且与 ``--no-exams`` 同形，不能只接到 export 上）；
- §12.2 **正式发布整体拒绝**（新增退出码 ``8``）：开了旗标而配置有错、或有未裁决冲突 ⇒
  构建之前就拦下，远端与本地状态一字不动；
- §12.4 **不许把"漏写一个旗标"解释成"用户主动删光了校历事件"**：上一版产物里确实有
  校历事件、这次没开旗标 ⇒ 同样拒绝，并把"要什么"说清楚。

最后一组用例守的是 v0.8 那次门 2 误拦的同型陷阱（AGENTS.md 收尾自检那条）：
**从没启用过这个特性的用户，不许被它的新门拦住。**
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar import subscribe
from xjtu_calendar.calendar_events import make_calendar_uid
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings

URL_BASE = "https://me.github.io/timetable/"
WINTER_UID = make_calendar_uid(SEMESTER, "2026-fall-winter")

GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


def event_item(**over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source_id": "2026-fall-winter",
        "category": "vacation",
        "title": "标记-寒假",
        "start": "2027-01-18",
        "end": "2027-02-28",
        "source_url": "https://due.xjtu.edu.cn/xxfw/xl.htm",
    }
    item.update(over)
    return item


def conflict_item(**over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "topic": "winter_start",
        "source_id": "2026-fall-winter",
        "source_urls": [
            "https://due.xjtu.edu.cn/xxfw/xl.htm",
            "https://jwc.xjtu.edu.cn/info/1095/10442.htm",
        ],
        "dates": ["2027-01-18", "2027-02-28"],
        "decided_on": "2026-10-15",
    }
    item.update(over)
    return item


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True,
        text=True,
        check=True,
        encoding="utf-8",
    )
    return proc.stdout


def _git_bytes(rev: str, cwd: Path) -> bytes:
    proc = subprocess.run(["git", "-C", str(cwd), "show", rev], capture_output=True, check=True)
    return proc.stdout


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """三件套齐全的 home + file:// 裸仓库；`events` 段默认不写（= 特性未启用）。"""
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin))
    make_home(tmp_path)
    return tmp_path, origin


def write_calendar_config(
    home: Path,
    events: list[dict[str, Any]] | None,
    conflicts: list[dict[str, Any]] | None = None,
) -> None:
    payload = json.loads((home / "semesters" / f"{SEMESTER}.json").read_text(encoding="utf-8"))
    if events is None:
        payload.pop("events", None)
    else:
        payload["events"] = events
    if conflicts is None:
        payload.pop("conflicts", None)
    else:
        payload["conflicts"] = conflicts
    (home / "semesters" / f"{SEMESTER}.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _init(origin: Path) -> int:
    return main(
        [
            "subscribe",
            "init",
            "--repo",
            origin.resolve().as_uri(),
            "--url-base",
            URL_BASE,
            "--semester",
            SEMESTER,
        ]
    )


def push(*flags: str) -> int:
    return main(["subscribe", "push", "--semester", SEMESTER, *flags])


def remote_bytes(origin: Path, token: str) -> bytes:
    return _git_bytes(f"cal:{token}.ics", origin)


def with_session(home: Path) -> None:
    """给一份会话：`doctor` 的「会话」那格默认是阻塞项，不铺平就没法验退出码。"""
    from datetime import UTC, datetime

    cfg = Settings(home=home)
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


# --------------------------------------------------------------------------- #
# 1. 旗标贯通三条路径
# --------------------------------------------------------------------------- #


def test_the_flag_exists_on_export_push_and_rotate() -> None:
    """§6 第 2 条要求它"与 ``--no-exams`` 一样贯通"——只挂到 export 上就是空话。"""
    import argparse

    from xjtu_calendar.cli import build_parser

    parser = build_parser()

    def flags(*argv: str) -> argparse.Namespace:
        return parser.parse_args(list(argv))

    assert flags("export", "--include-calendar-events").include_calendar_events is True
    assert flags("export").include_calendar_events is False, "默认必须是关"
    assert flags("subscribe", "push", "--include-calendar-events").include_calendar_events is True
    assert flags("subscribe", "push").include_calendar_events is False
    assert flags("subscribe", "rotate", "--include-calendar-events").include_calendar_events is True
    assert flags("subscribe", "rotate").include_calendar_events is False


def test_export_writes_the_event_only_when_asked(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    home, _origin = env
    write_calendar_config(home, [event_item()])
    out = home / "off.ics"

    assert main(["export", "--semester", SEMESTER, "-o", str(out)]) == 0
    assert WINTER_UID not in out.read_text(encoding="utf-8")
    capsys.readouterr()

    on = home / "on.ics"
    assert main(["export", "--semester", SEMESTER, "-o", str(on), "--include-calendar-events"]) == 0
    text = on.read_text(encoding="utf-8")
    assert f"UID:{WINTER_UID}" in text
    assert "DTSTART;VALUE=DATE:20270118" in text
    printed = capsys.readouterr().out
    assert "校历事件" in printed, printed


# --------------------------------------------------------------------------- #
# 2. §12.2：开了旗标遇配置错误 / 未裁决冲突 ⇒ 整体拒绝，退出码 8，零写盘
# --------------------------------------------------------------------------- #


def test_push_refuses_a_config_error_with_a_new_exit_code_and_touches_nothing(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert push() == 0
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None
    before = remote_bytes(origin, state.token)
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    ledger = home / "subscribe" / f"course-ledger-{SEMESTER}.ics"
    last_before = last_local.read_bytes()
    ledger_before = ledger.read_bytes() if ledger.is_file() else None
    capsys.readouterr()

    # 同学期两条：一条日期抄反、一条来源不是官方域名 —— 都是**配置错误**。
    write_calendar_config(
        home,
        [
            event_item(start="2027-02-28", end="2027-01-18"),
            event_item(source_id="2026-fall-summer", source_url="https://example.com/x.htm"),
        ],
    )
    assert push("--include-calendar-events") == 8

    err = capsys.readouterr().err
    assert "校历" in err
    # 拒绝必须**真的没动现场**：远端、留底、台账三样逐字节相同。
    assert remote_bytes(origin, state.token) == before
    assert last_local.read_bytes() == last_before
    if ledger_before is not None:
        assert ledger.read_bytes() == ledger_before


def test_push_refuses_an_unresolved_conflict(env: tuple[Path, Path]) -> None:
    home, origin = env
    assert _init(origin) == 0
    assert push() == 0
    write_calendar_config(home, [event_item()], [conflict_item()])
    assert push("--include-calendar-events") == 8


def test_adjudicated_conflict_and_clean_config_publish(env: tuple[Path, Path]) -> None:
    """同一条门在配置干净时必须放行 —— 否则这条门只是"永远拒绝"。"""
    home, origin = env
    assert _init(origin) == 0
    assert push() == 0
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None
    write_calendar_config(
        home, [event_item()], [conflict_item(adjudicated_by="以教务部校历图片为准")]
    )

    assert push("--include-calendar-events") == 0
    blob = remote_bytes(origin, state.token)
    assert WINTER_UID.encode() in blob
    assert b"DTSTART;VALUE=DATE:20270118" in blob


def test_rotate_carries_the_flag_and_the_same_gate(env: tuple[Path, Path]) -> None:
    """rotate 会**重新构建并补发**整份产物，所以旗标与准入门都必须同口径接上。

    不带旗标去 rotate = §12.4 那一格（漏写旗标会被当成"删光校历事件"）⇒ 必须拒，
    而不是悄悄把事件从线上丢掉 —— 那批事件不接撤销通路，客户端连"已取消"都收不到。
    """
    home, origin = env
    write_calendar_config(home, [event_item()])
    assert _init(origin) == 0
    assert push("--include-calendar-events") == 0

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 8

    assert main(["subscribe", "rotate", "--semester", SEMESTER, "--include-calendar-events"]) == 0
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None
    assert WINTER_UID.encode() in remote_bytes(origin, state.token)


# --------------------------------------------------------------------------- #
# 3. §12.4：漏写旗标不许被当成"用户删光了校历事件"
# --------------------------------------------------------------------------- #


def test_forgetting_the_flag_after_publishing_events_is_refused(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    home, origin = env
    write_calendar_config(home, [event_item()])
    assert _init(origin) == 0
    assert push("--include-calendar-events") == 0
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None
    published = remote_bytes(origin, state.token)
    capsys.readouterr()

    # 下一次：配置没变，只是**忘了带旗标**。
    assert push() == 8
    err = capsys.readouterr().err
    assert "--include-calendar-events" in err, "拒绝必须指出要补的那一个旗标"
    assert "删" in err or "清空" in err, err
    assert remote_bytes(origin, state.token) == published, "拒绝之后线上必须还是那一版"


def test_removing_the_events_array_is_still_allowed(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**主动**清空配置是明确选择，不该被 §12.4 拦住。

    那条门防的是"漏写旗标"，判据来自配置与留底的对照：配置里已经没有这些事件了，
    就说明用户是删掉了它们，而不是忘了开旗标。
    """
    home, origin = env
    write_calendar_config(home, [event_item()])
    assert _init(origin) == 0
    assert push("--include-calendar-events") == 0
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None

    write_calendar_config(home, None)
    assert push() == 0
    assert WINTER_UID.encode() not in remote_bytes(origin, state.token)
    capsys.readouterr()


def test_a_broken_events_block_never_blocked_a_push_that_never_asked(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """v0.8 门 2 误拦的同型陷阱（AGENTS.md 收尾自检那条）：**新门不许拦住没用它的人**。

    从没启用过校历事件的用户，配置里躺着一段写坏的 `events` 也不该被拒绝发布 ——
    那会让他每次 push 都被自己没要的特性掐死。他**开了旗标**才受 §12.2 的约束。
    """
    home, origin = env
    assert _init(origin) == 0
    assert push() == 0
    capsys.readouterr()

    write_calendar_config(home, [event_item(category="nope")])
    assert push() == 0, "没开旗标的正常发布被校历事件的新门误拦"
    assert push("--include-calendar-events") == 8, "开了旗标就必须拦住"


# --------------------------------------------------------------------------- #
# 4. doctor：体检与门必须一致（§12.2），但不许比门更凶
# --------------------------------------------------------------------------- #


def test_doctor_carries_a_calendar_row_even_when_nothing_is_configured(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """没配 `events` 是常态：这一格必须**看得见**（"未配置"），且不影响退出码。"""
    home, origin = env
    with_session(home)
    assert _init(origin) == 0
    assert push() == 0
    capsys.readouterr()

    assert main(["doctor", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "校历" in out, out


def test_doctor_is_green_when_the_user_says_the_events_are_intended(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """体检也在模拟"这条发布路径"：带旗标的 doctor 对一份能发的配置必须给绿。"""
    home, origin = env
    with_session(home)
    write_calendar_config(home, [event_item()])
    assert _init(origin) == 0
    assert push("--include-calendar-events") == 0
    capsys.readouterr()

    assert main(["doctor", "--semester", SEMESTER, "--include-calendar-events"]) == 0
    out = capsys.readouterr().out
    assert "校历事件" in out, out


def test_doctor_blocks_a_broken_events_block(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """§12.2 明写的一致：配置里有错误或未裁决冲突 ⇒ 门会拒、体检必须阻塞（退出码 5）。"""
    home, origin = env
    with_session(home)
    assert _init(origin) == 0
    assert push() == 0
    capsys.readouterr()

    write_calendar_config(home, [event_item(category="nope")])
    assert main(["doctor", "--semester", SEMESTER]) == 5
    out = capsys.readouterr().out
    assert "[阻塞] 校历事件" in out, out


def test_doctor_blocks_the_missed_flag_state(env: tuple[Path, Path]) -> None:
    """上一版含校历事件、配置里事件还在 ⇒ 不带旗标的那次发布会被 §12.4 拦住，体检同步阻塞。"""
    home, origin = env
    with_session(home)
    write_calendar_config(home, [event_item()])
    assert _init(origin) == 0
    assert push("--include-calendar-events") == 0

    assert main(["doctor", "--semester", SEMESTER]) == 5


def test_doctor_is_not_stricter_than_the_gate(env: tuple[Path, Path]) -> None:
    """配置写对了、也从没发布过校历事件 ⇒ 门放行（没开旗标），体检也不许判阻塞。

    这条守的是另一头的失衡：体检比门凶会让用户去修一个并不存在的问题。
    """
    home, origin = env
    with_session(home)
    write_calendar_config(home, [event_item()])
    assert _init(origin) == 0
    assert push() == 0

    assert main(["doctor", "--semester", SEMESTER]) == 0
