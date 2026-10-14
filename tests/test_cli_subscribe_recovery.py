"""P1-02：`subscribe push` 的崩溃窗口必须**可恢复**，不能永久失同步。

真实序列（2026-10-14 外部审查提出、对着 `cli.py` 复核成立）：
`publish()` 在 push 成功后先 `save_state`（写 `last_push`），CLI 再写本地留底与台账。
程序若正好死在这两句中间 ⇒ 远端是 v2、状态认为已发 v2、**留底还是 v1**。
而 `publish()` 的 NO_CHANGE 分支在旧代码里**早于**留底写盘就 `return 0`，
于是这份落后永不自愈：下一次真实变更会拿 v1 字节当撤销判定与迁移基线，
把整学期误读成"凭空消失"——正是门 2 要拦的那种形态，只不过这次是它自己造的。

本文件钉的是 A+ 方案（2026-10-14 与审查者对齐后定稿）的四条契约：
1. **构建之前**先查"本地四样（留底 / 两份台账 / 迁移标记）是否与最后一次已发布版本相容"，
   不相容就拒绝（退出码 7）并指一条 `subscribe repair` 的路——不再靠 NO_CHANGE 顺手补写；
2. 每次发布在推送**之前**落一份「发布恢复快照」（journal：ICS 字节 + 两份台账的目标字节 +
   迁移标记），落在发布工作目录之外；发布失败要把它**退回上一版**；
3. `subscribe repair` 必须**先核线上字节**再恢复，可重复执行，且绝不产生新的发布；
4. 没有快照（旧版本升级上来的状态）就**不假装能从 ICS 反推台账**——默认 fail-closed，
   但给 `--allow-unverified-baseline` 这个一次性人工出口。

远端一律 `file://` 裸仓库，全程离线（与 `test_cli_subscribe.py` 同一套现场做法）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home, payload_row

from xjtu_calendar import cli, subscribe
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


def _token(home: Path) -> str:
    state = subscribe.load_state(Settings(home=home), SEMESTER)
    assert state is not None
    return state.token


def _journal(home: Path) -> subscribe.PublishJournal | None:
    return subscribe.load_journal(Settings(home=home), SEMESTER)


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


def test_a_stale_local_baseline_is_refused_before_anything_else(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """留底落后于已发布字节 ⇒ `push` **拒绝**（退出码 7），并指一条 `repair` 的路。

    A+ 方案落地前，这一格是靠 NO_CHANGE 分支顺手补写留底来自愈的；但那套自愈只在
    "数据恰好没变"时才触发（场景 1 的反例），所以判据整体上移到构建之前，一律 fail-closed：
    本地四样与"最后一次真正发出去的版本"对不上就先停下，由 `subscribe repair` 负责补回。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    assert hashlib.sha256(last_local.read_bytes()).hexdigest() != _state_sha(home)
    tip_before = _git("rev-parse", "cal", cwd=origin)
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    captured = capsys.readouterr()
    assert "留底" in captured.err and "repair" in captured.err
    assert last_local.read_bytes() != remote, "拒绝就不该顺手改写用户的文件"
    assert _git("rev-parse", "cal", cwd=origin) == tip_before, "拒绝就不该动远端"

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "已按发布恢复快照对齐" in capsys.readouterr().out
    assert last_local.read_bytes() == remote, "留底必须与远端产物**逐字节**相同"
    assert hashlib.sha256(last_local.read_bytes()).hexdigest() == _state_sha(home)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out, "拦门的不是数据，修完就恢复成一次普通发布"


def test_the_refusal_is_not_sticky(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """补齐之后，`push` 不许再报"留底"——否则用户分不清哪次真出过事。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    capsys.readouterr()

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    second = capsys.readouterr()
    assert "留底" not in second.err, "已经一致时不许再报不相容"
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


def _pollute_remote(origin: Path, *, branch: str = BRANCH) -> None:
    """往发布分支的 tip 上多塞一个文件，让 publish 的 tree 护栏**必然**拒绝发布。

    这是刻意选的失败点：护栏跑在任何 git 变更与 `save_state` 之前，所以"失败不留痕"
    是确定的。（试过"删掉裸仓库"制造远端不可达：`git push` 会在原路径**重建**一个空
    仓库并返回 0，在 Windows 上时灵时不灵，不能当判据。）

    `branch` 默认是本文件专用的 `polluted`；已经发过一次、要在真正的 `cal` 上制造
    失败时传 `branch="cal"`（护栏看的就是订阅分支的 tip）。
    """
    seed = origin.parent / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", str(seed))
    (seed / "stray.txt").write_text("not an ics\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "stray", cwd=seed)
    _git("push", "-q", "--force", str(origin), f"HEAD:refs/heads/{branch}")


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
    """**验收 3**：`repair` 只是"把远端字节补回本地"，**不许**重新构建产物。

    重新构建会按当前数据算 SEQUENCE / DTSTAMP，那等于用一次崩溃当机会把
    整份产物的版本号重写一遍——客户端看到的就不是"同一份日历"了。
    判据：修复后的留底与远端 blob **逐字节相同**，SEQUENCE 序列一字不差，
    且发布分支的 tip 一动没动（恢复不是第二次发布）。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    tip_before = _git("rev-parse", "cal", cwd=origin)
    capsys.readouterr()

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    repaired = last_local.read_bytes()
    assert repaired == remote
    assert _sequence_lines(repaired.decode("utf-8")) == _sequence_lines(remote.decode("utf-8")), (
        "恢复改动了 SEQUENCE"
    )
    assert _git("rev-parse", "cal", cwd=origin) == tip_before, "恢复不该产生第二次发布"

    # 再走一次幂等：既不重复报恢复，也不动字节
    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "无需恢复" in capsys.readouterr().out
    assert last_local.read_bytes() == remote


# --------------------------------------------------------------------------- #
# 审查者追加的相容性场景（2026-10-14 深夜后段）
# --------------------------------------------------------------------------- #
#
# 共同原则：**下一次发布之前**，构建用的 SEQUENCE 基线、课程台账、考试台账、UID 迁移
# 标记，都必须与"最后一次已发布的版本"相容。只比 ICS 哈希证明不了台账同步。


def _rows_with_b() -> list[dict[str, str]]:
    """甲（固件默认，第 1-2 周）+ 乙（第 10-12 周）。

    乙刻意放到第 10 周：固件学期是 2026-09-07 起 16 周，第 1-2 周的课相对真实挂钟
    **已经过期**，而过期条目会被 D2 筛直接踢出台账（`cancel_candidates` 的
    `item.start < now`）——那样"能不能撤销"的断言就成了空话。
    """
    return [payload_row(), payload_row(KCM="示例课程乙", KCH="D-2", ZCMC="10-12周")]


def _rewrite_rows(home: Path, rows: list[dict[str, str]]) -> None:
    """模拟"下次 fetch 之后课变了"：直接覆写 raw 课表快照。"""
    (home / "raw" / f"timetable-{SEMESTER}.json").write_text(
        json.dumps({"kbList": rows}, ensure_ascii=False), encoding="utf-8"
    )


def _install_ledger_fuse(monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    """给两份台账的写盘装一根可拔除的保险丝，返回开关字典。

    台账文件名是 `last-exams-<学期>.ics` / `last-courses-<学期>.ics`（**也是 `.ics`**，
    与发布留底 `last-<学期>.ics` 同目录同后缀），所以只能按精确文件名分流。
    刻意打更底层的 `atomic_write_text` 而不是替换 `_write_course_ledger`：要测的正是
    「写失败被降级成 warning」这个真实出口（D24①）。

    返回开关而**不用** `monkeypatch.undo()`：undo 会把固件设的 `XJTU_CALENDAR_HOME`
    一起回退到 autouse 隔离值，后续命令就找不到订阅状态了（实测踩过）。
    """
    real = cli.atomic_write_text
    names = {f"last-exams-{SEMESTER}.ics", f"last-courses-{SEMESTER}.ics"}
    fuse = {"on": True}

    def spy(path: Path, text: str, *, private: bool = False) -> None:
        if fuse["on"] and Path(path).name in names:
            raise OSError("disk is full")
        real(path, text, private=private)

    monkeypatch.setattr(cli, "atomic_write_text", spy)
    return fuse


def test_stale_baseline_is_refused_before_the_build_and_repairable(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**场景 1（已修）**：留底落后 + 数据又变了 ⇒ **构建之前**拒绝，`repair` 能补回。

    版本 A 发布成功后崩在写留底前（远端与状态都是 A、留底还是上一版），随后课表变成 C。
    旧实现里 C 与 A 哈希不同 ⇒ 不走 NO_CHANGE ⇒ 挂在 NO_CHANGE 上的自愈根本不触发，
    而 `build_ics_for_semester` 早在 `publish()` **之前**就把过期留底当 SEQUENCE 基线
    用掉了（`baseline_probe` / `published_baseline_text` 两个入参都读它）。
    现在相容性检查搬到构建之前（退出码 7），`subscribe repair` 按发布恢复快照补回。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    _crash_between_state_and_baseline(home, origin, capsys)  # 远端/状态=已发布版，留底=旧版
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    stale_bytes = last_local.read_bytes()
    published = _git_bytes(f"cal:{_token(home)}.ics", origin)
    assert stale_bytes != published

    _rewrite_rows(home, _rows_with_b())  # 数据再变一版 ⇒ 必然走真实推送分支

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "留底" in err, "构建前必须拦住「过期基线」，而不是拿它算完 SEQUENCE 再发出去"
    assert "repair" in err, "拒绝时必须给出恢复入口"
    assert last_local.read_bytes() == stale_bytes, (
        "被拦住时不许顺手改留底（拦住 = 什么都不动，让用户决定）"
    )

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "已按发布恢复快照对齐" in capsys.readouterr().out
    assert last_local.read_bytes() == published, "repair 补回的必须就是线上那一版字节"

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()


def test_ledger_write_failure_is_caught_and_repair_restores_cancellation(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**场景 2（已修）**：留底是 B、课程台账仍是 A ⇒ 现在**拒绝发布**，`repair` 后撤销正确下发。

    `cancel_candidates` 的差集是「台账 − live」，台账里根本没有那条 UID 就成不了候选；
    而台账写失败历史上只降级成 warning（D24①）。修复前第三次 push 会**静默漏发**撤销
    （实测：产物里一条 `STATUS:CANCELLED` 都没有）；现在相容性检查在构建前拦住，
    `repair` 把台账补回 B 那一次的内容，之后删乙就能正常收到 CANCELLED。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    # B：新增一节未来的课，但两份台账写不进去（留底照写 ⇒ 留底与台账从这一刻失配）
    _rewrite_rows(home, _rows_with_b())
    fuse = _install_ledger_fuse(monkeypatch)
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    course_ledger = home / "subscribe" / f"last-courses-{SEMESTER}.ics"
    assert "示例课程乙" not in course_ledger.read_text(encoding="utf-8"), (
        "台账写失败后应当留在旧内容（本用例的前提）"
    )
    assert "示例课程乙" in (home / "subscribe" / f"last-{SEMESTER}.ics").read_text("utf-8"), (
        "留底必须已经带上乙 ⇒ 留底与台账从这一刻起失配"
    )

    # C：把乙删掉。旧行为是"静默漏发"，新行为是构建前就拒绝。
    fuse["on"] = False
    _rewrite_rows(home, [payload_row()])
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "台账" in err and "repair" in err

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    assert "示例课程乙" in course_ledger.read_text(encoding="utf-8"), (
        "repair 必须把台账补回 B 那一次的内容"
    )

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    published = (home / "subscribe" / f"last-{SEMESTER}.ics").read_text(encoding="utf-8")
    assert "STATUS:CANCELLED" in published, "补回台账后，乙的退课应当以撤销下发"


def test_repair_is_idempotent_and_never_touches_the_remote(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**场景 6**：`repair` 可重复执行；第二次什么都不动，也**绝不 push**。

    恢复中途再崩一次是常态，所以 repair 必须能续上；而它一旦被误用成"重发"，
    就会凭空抬一轮 SEQUENCE —— 那条路要堵死。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    _crash_between_state_and_baseline(home, origin, capsys)
    tip_before = _git("rev-parse", "cal", cwd=origin)
    capsys.readouterr()

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "已按发布恢复快照对齐" in capsys.readouterr().out

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "无需恢复" in capsys.readouterr().out, "第二次必须认出已经一致"
    assert _git("rev-parse", "cal", cwd=origin) == tip_before, "repair 不许动远端"


def test_an_upgrade_without_a_snapshot_glues_itself_back_on(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**验收 5（旧版本兼容）**：没有快照，但留底字节 == 最后一次发布记录 ⇒ **放行**。

    真实升级现场：2026-10-14 拿用户的 home 原样拷贝后跑判定，读数就是
    `reason=no_journal` —— 每个 0.8.5 之前的老用户第一次 push 都会被自己的新保险丝
    拦住（与 v0.8 那次"门 2 误拦老用户几周"同型）。所以退回一条**不依赖快照**的弱判据：
    `last_push` 只在 push 成功后写，`sha256(留底) == last_push.content_sha256` 就证明
    留底正是线上那一版。此时两份台账与迁移标记**无法**证明（没有依据可查），所以必须
    警告；而推送前本来就会落新快照，于是这一次同时把依据补回来。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    (home / "subscribe" / f"publish-journal-{SEMESTER}.json").unlink()

    _rewrite_rows(home, _rows_with_b())  # 数据也变了 ⇒ 一定走真实推送分支
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    err = capsys.readouterr().err
    assert "台账" in err and "恢复快照" in err, "放行必须说明台账未校验、依据已补回"
    assert subscribe.journal_path(Settings(home=home), SEMESTER).is_file(), "依据当场补落"

    # 依据已在 ⇒ 回到强判据：留底再落后就会被拦
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()


def test_no_snapshot_and_a_lagging_baseline_is_still_refused(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """弱判据的边界：留底**也**对不上最后一次发布记录 ⇒ 仍然拒绝，且给得出人工出口。

    这一格与上一格的差别只在"有没有依据可查"之外，还"留底自己就不对"——那正是场景 1
    要拦的形态，弱判据不许把它放过去。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    _crash_between_state_and_baseline(home, origin, capsys)  # 留底落后
    (home / "subscribe" / f"publish-journal-{SEMESTER}.json").unlink()  # 又没有快照

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "留底" in err and "--allow-unverified-baseline" in err

    assert main(["subscribe", "push", "--semester", SEMESTER, "--allow-unverified-baseline"]) == 0
    assert "强行继续" in capsys.readouterr().err


def test_a_snapshot_from_another_token_is_refused(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """rotate 之后残留的旧快照**不能**当依据——它描述的是另一份订阅的字节。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    path = subscribe.journal_path(Settings(home=home), SEMESTER)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["token"] = "0" * 32
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    assert "token" in capsys.readouterr().err


def test_a_failed_publish_winds_the_journal_back(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**验收 4**：远端没收下 ⇒ 快照必须退回上一版，不许留着"从未发出去过"的字节。

    快照写在 publish **之前**（这样"推送成功、本地没落盘"才有依据可恢复），代价就是
    这一条必须单独钉：发布被护栏拦下时若不退回去，下一次 push 会被自己的相容性检查
    拦住，而 `repair` 又拒绝恢复（线上字节 ≠ 快照）——一次普通的推送失败就此变成死局。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    journal_file = subscribe.journal_path(Settings(home=home), SEMESTER)
    digest_before = hashlib.sha256(journal_file.read_bytes()).hexdigest()
    capsys.readouterr()

    _pollute_remote(origin, branch="cal")
    _rewrite_rows(home, _rows_with_b())
    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    capsys.readouterr()
    assert hashlib.sha256(journal_file.read_bytes()).hexdigest() == digest_before, (
        "推送失败后快照不该指向一个从未发出去过的版本"
    )


def test_journal_keep_semantics_never_writes_an_empty_exam_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**keep 语义**：快照里 ``None`` 的那份台账表示"这次别动"，恢复时必须跳过。

    `--no-exams` 那次根本不产出考试台账文本。把它误记成"写空文件"等于把用户整份考试
    账本清掉，而清空后的台账会让下一次撤销判定少一整批候选——比漏发更糟。
    """
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    cfg = Settings(home=tmp_path)
    journal = subscribe.write_journal(
        cfg,
        semester=SEMESTER,
        token="t",
        branch="cal",
        ics_text="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        course_ledger_text="课程台账字节",
        exam_ledger_text=None,
        uid_recipe=2,
    )
    assert subscribe.load_journal(cfg, SEMESTER) is not None
    assert journal.exam_ledger_text is None, "keep 哨兵必须原样往返"

    restored = cli._restore_from_journal(cfg, journal, SEMESTER)
    assert "考试台账" not in restored
    assert not cfg.exam_ledger_path(SEMESTER).exists(), "keep 不能写成空台账文件"
    assert "课程台账" in restored


def test_migration_marker_write_failure_leaves_no_second_sweep(
    env: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**场景 3**：迁移已发布成功、`uid_recipe` 标记没写上 ⇒ 下次会不会再扫一遍？

    审查者担心「标记一直缺失 + NO_CHANGE 提前返回」。**实测结论：不构成缺陷**——
    迁移候选的唯一来源是留底文本（CD15 不重算 v1 UID），而留底此时已是 v2 产物
    ⇒ 候选空集 ⇒ 零重复撤销、零多余预告；标记会在下一次真实发布时补上。
    所以这条按"应当如此"写成绿用例长期钉住，而不是当 bug 修。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    # 把状态改回"从 v0.7.x 升级上来"的样子（没有 uid_recipe 字段）
    cfg = Settings(home=home)
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    subscribe.save_state(cfg, replace(state, uid_recipe=None))

    # 正常完成一次迁移发布，然后把状态**倒回**"标记没写上"的那一刻
    # （标记回写是整条链的最后一步，所以崩在那里的现场 = 远端/留底/台账都是新版、
    #  只有 `uid_recipe` 缺失；注入写失败反而会测到"错误没被接住"这个无关问题）
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    subscribe.save_state(cfg, replace(state, uid_recipe=None))
    assert subscribe.load_state(cfg, SEMESTER).uid_recipe is None, "前提：标记确实没写上"

    # 再来一次：数据没变 ⇒ 走 NO_CHANGE，标记仍缺失，但**不该**有第二次撤销
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    cancelled = (home / "subscribe" / f"last-{SEMESTER}.ics").read_text(encoding="utf-8")
    assert cancelled.count("STATUS:CANCELLED") == 0, "不该出现重复撤销"
    assert "迁移" not in out, "数据没变时不该再发一次迁移预告"
