"""P1-02：`subscribe push` 的崩溃窗口必须**可恢复**，不能永久失同步。

真实序列（2026-10-14 外部审查提出、对着 `cli.py` 复核成立）：
`publish()` 在 push 成功后先 `save_state`（写 `last_push`），CLI 再写本地留底与台账。
程序若正好死在这两句中间 ⇒ 远端是 v2、状态认为已发 v2、**留底还是 v1**。
而 `publish()` 的 NO_CHANGE 分支在旧代码里**早于**留底写盘就 `return 0`，
于是这份落后永不自愈：下一次真实变更会拿 v1 字节当撤销判定与迁移基线，
把整学期误读成"凭空消失"——正是门 2 要拦的那种形态，只不过这次是它自己造的。

本文件钉三点：
1. NO_CHANGE 时比对留底 sha 与 `last_push.content_sha256`，不一致就**用远端那份字节补写**；
2. 补写是幂等的（第二次不再报修复）；
3. `doctor` 能看见这个状态（不靠"数据恰好没变"才被发现）。

远端一律 `file://` 裸仓库，全程离线（与 `test_cli_subscribe.py` 同一套现场做法）。
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar import subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings

URL_BASE = "https://me.github.io/timetable/"

#: 本文件专用发布分支名（护栏用例要往它上面塞一个非 .ics 文件）。
BRANCH = "polluted"

#: 测试内所有 git 子进程（含 publish 内部调用）继承这份身份，免依赖全局 user.name。
GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


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
    """text=True 会把 CRLF 折成 LF，字节级比对必须走二进制 stdout。"""
    proc = subprocess.run(
        ["git", "-C", str(cwd), "show", rev],
        capture_output=True,
        check=True,
    )
    return proc.stdout


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """home==tmp_path（三件套已布置），origin 为 file:// 裸仓库。"""
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin))
    make_home(tmp_path)
    return tmp_path, origin


def _init(origin: Path, extra: Sequence[str] = ()) -> int:
    return main(
        [
            "subscribe",
            "init",
            "--repo",
            origin.resolve().as_uri(),
            "--url-base",
            URL_BASE,
            *extra,
            "--semester",
            SEMESTER,
        ]
    )


def _state_sha(home: Path) -> str:
    """状态文件里记录的"最后一次发布的字节 sha"——留底应当与它逐字节对应。"""
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None and state.last_push is not None
    return state.last_push.content_sha256


def _crash_between_state_and_baseline(
    home: Path, origin: Path, capsys: pytest.CaptureFixture[str]
) -> tuple[Path, bytes]:
    """把现场摆成"push 成功、`save_state` 已落、留底还是旧的"那一瞬。

    完全离线、零改代码：正常发一次 → 取远端 blob（= 状态里 `last_push` 对应的字节）
    → 把留底换成"上一版"的字节。旧留底用**删掉最后一个 VEVENT** 模拟：
    形状合法、sha 不同，正是 v1→v2 之间那种"内容变了但都是合法 ICS"的形态。
    """
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    cfg = Settings(home=home)
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.last_push is not None
    remote = _git_bytes(f"cal:{state.token}.ics", origin)
    stale = remote[: remote.rindex(b"BEGIN:VEVENT")] + b"END:VCALENDAR\r\n"
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    last_local.write_bytes(stale)
    return last_local, remote


def test_no_change_publish_repairs_a_stale_local_baseline(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """数据没变 ⇒ NO_CHANGE，但留底必须被补成远端那份字节，并说明发生了什么。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    assert hashlib.sha256(last_local.read_bytes()).hexdigest() != _state_sha(home)
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    captured = capsys.readouterr()
    assert "无变化" in captured.out
    # 修复说明走 logger：`setup_logging` 的 StreamHandler 落 stderr。
    # 不用 captured_logs/caplog —— `main()` 会重挂 handler，事先挂上的收集器会被摘掉
    # （`test_login_command_stops_claiming_readiness` 的注释记过这个坑）。
    assert "留底" in captured.err, "必须明说这次顺手修了留底，不能静默改写用户的文件"
    assert last_local.read_bytes() == remote, "留底必须与远端产物**逐字节**相同"
    assert hashlib.sha256(last_local.read_bytes()).hexdigest() == _state_sha(home)


def test_the_repair_is_idempotent(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """留底已经一致时，NO_CHANGE 不许再报修复（否则用户分不清哪次真出过事）。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    first = capsys.readouterr()
    assert "留底" in first.err and "无变化" in first.out

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    second = capsys.readouterr()
    assert "留底" not in second.err, "已经一致时不许再报修复"
    assert "无变化" in second.out
    assert last_local.read_bytes() == remote


def test_doctor_sees_a_baseline_that_lags_the_published_bytes(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`doctor` 要能直接看见"留底 ≠ 最后一次发布的字节"，而不是靠重建产物碰巧对上。

    旧实现只比"留底 vs 当前重建结果"：数据一旦也变了，这个状态就完全隐身。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, _remote = _crash_between_state_and_baseline(home, origin, capsys)

    from xjtu_calendar.doctor import STATUS_OK, run_checks

    checks = run_checks(Settings(home=home), SEMESTER)
    sub = [c for c in checks if c.name == "subscription"]
    assert sub, "doctor 必须仍有订阅这一项"
    assert sub[0].status != STATUS_OK, "留底落后于已发布字节时不许给绿灯"
    assert last_local.is_file()


# --------------------------------------------------------------------------- #
# 验收标准 2 / 3（2026-10-14 审查对齐）：失败不得提前推进、恢复必须幂等
# --------------------------------------------------------------------------- #


def _sequence_lines(text: str) -> list[str]:
    return [ln.strip() for ln in text.splitlines() if ln.startswith("SEQUENCE")]


def _pollute_remote(origin: Path) -> None:
    """往发布分支的 tip 上多塞一个文件，让 publish 的 tree 护栏**必然**拒绝发布。

    这是刻意选的失败点：护栏跑在任何 git 变更与 `save_state` 之前，所以"失败不留痕"
    是确定的。（试过"删掉裸仓库"制造远端不可达：`git push` 会在原路径**重建**一个空
    仓库并返回 0，在 Windows 上时灵时不灵，不能当判据。）
    """
    seed = origin.parent / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", str(seed))
    (seed / "stray.txt").write_text("not an ics\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "stray", cwd=seed)
    _git("push", "-q", str(origin), f"HEAD:refs/heads/{BRANCH}")


def test_failed_push_does_not_advance_any_published_state(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**验收 2**：发布被护栏拦下 ⇒ 状态文件、留底、台账一律不被推进。

    今天这条大概率已经成立（`save_state` 在 push 之后），钉住它是为了：
    引入"待发布记录 / 恢复"之后，**不许**把任何已发布语义提前落到本地。
    """
    home, origin = env
    assert _init(origin, extra=["--branch", BRANCH]) == 0
    capsys.readouterr()
    _pollute_remote(origin)

    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    capsys.readouterr()

    sub_dir = home / "subscribe"
    leftovers = sorted(
        p.relative_to(sub_dir).as_posix()
        for p in sub_dir.rglob("*")
        if p.is_file() and not p.relative_to(sub_dir).as_posix().startswith("work-")
    )
    assert leftovers == [f"subscribe-{SEMESTER}.json"], f"被拦下的发布留下了已发布痕迹：{leftovers}"
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None and state.last_push is None, "失败却推进了 last_push"


def test_the_repair_never_rewrites_sequence_numbers(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**验收 3**：自愈只是"把远端字节补回本地"，**不许**重新构建产物。

    重新构建会按当前数据算 SEQUENCE / DTSTAMP，那等于用一次崩溃当机会把
    整份产物的版本号重写一遍——客户端看到的就不是"同一份日历"了。
    判据：修复后的留底与远端 blob **逐字节相同**，SEQUENCE 序列一字不差。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    repaired = last_local.read_bytes()
    assert repaired == remote
    assert _sequence_lines(repaired.decode("utf-8")) == _sequence_lines(remote.decode("utf-8")), (
        "自愈改动了 SEQUENCE"
    )

    # 再走一次幂等：既不重复报修复，也不动字节
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "已修复" not in capsys.readouterr().out
    assert last_local.read_bytes() == remote
