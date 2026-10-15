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
    return subscribe.read_journal(Settings(home=home), SEMESTER).journal


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
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """补齐之后，`push` 不许再报"留底"——否则用户分不清哪次真出过事。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    last_local, remote = _crash_between_state_and_baseline(home, origin, capsys)
    capsys.readouterr()

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    # 顺带把"幂等跳过也清了准备稿"这件事测成**非空洞**的： spy 只观察、不改行为，
    # 它看到的正是 discard 那一刻——准备稿确实在那儿（`write_journal` 排在 publish 之前），
    # 而 discard 之后它必须不在。少了这一层，下面那条断言可能只是在证明"文件从没被写过"。
    seen: dict[str, bool] = {}
    real_discard = subscribe.discard_pending

    def spy(cfg: Settings, semester: str) -> None:
        seen["pending_before_discard"] = subscribe.pending_path(cfg, semester).is_file()
        real_discard(cfg, semester)

    monkeypatch.setattr(subscribe, "discard_pending", spy)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    second = capsys.readouterr()
    assert "留底" not in second.err, "已经一致时不许再报不相容"
    assert "无变化" in second.out
    assert last_local.read_bytes() == remote
    # 审阅者 2026-10-15 要点名确认的那一格：NO_CHANGE 提前返回，**不许**把准备稿留在原地
    # （留着的下一次 push 会被 `pending_unresolved` 拦成 7，等于自己给自己造一个假故障）
    assert seen.get("pending_before_discard") is True, "NO_CHANGE 这一支本来确实写过准备稿"
    assert not subscribe.pending_path(Settings(home=home), SEMESTER).exists(), (
        "幂等跳过也清掉了准备稿：远端没收新东西，就没有未裁决的发布"
    )


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

    **`cwd=seed` 不是可有可无的**（2026-10-15 CI 才暴露）：这句 push 原先漏了 cwd，
    于是 git 跑在 pytest 的工作目录＝本工具仓库里，把**整个仓库的 tip**推进了临时裸仓库。
    Windows 上它"碰巧"是绿的——护栏照样拒绝（tip 上远不止一个 `.ics`），但拒绝的理由
    与被钉的契约无关；GitHub Actions 的 checkout 是 shallow 克隆，从 shallow 仓库 push
    直接被 `shallow update not allowed` 拒掉，这条用例才在 Linux 上红。
    """
    seed = origin.parent / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", str(seed))
    (seed / "stray.txt").write_text("not an ics\n", encoding="utf-8")
    _git("add", "-A", cwd=seed)
    _git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "stray", cwd=seed)
    _git("push", "-q", "--force", str(origin), f"HEAD:refs/heads/{branch}", cwd=seed)
    # 现场必须真是"tip 上只有一个非 .ics 文件"，否则这条用例钉的就不是护栏那一格
    assert _git("ls-tree", "-r", "--name-only", branch, cwd=origin).split() == ["stray.txt"]


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


def test_a_failed_publish_discards_the_pending_snapshot(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**审阅 P1-①（准备侧）**：远端**肯定没动**时，准备稿必须被清掉。

    判据只有一条：`publish()` 里"还没执行到 push"才敢说远端没推进。护栏/探测都在 push
    之前，所以被它们拦下 ⇒ committed 不变、pending 清除，下一次 push 站在一致的地基上。
    """
    home, origin = env
    cfg = Settings(home=home)
    pending = subscribe.pending_path(cfg, SEMESTER)
    committed = subscribe.journal_path(cfg, SEMESTER)

    assert _init(origin) == 0
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    assert committed.is_file() and not pending.exists(), "发布成功 ⇒ 准备稿提升为凭据，不留底稿"
    digest = hashlib.sha256(committed.read_bytes()).hexdigest()

    # 往分支 tip 塞一个非 .ics ⇒ 护栏在 push **之前**拦下 ⇒ 准备稿必须被丢弃
    _pollute_remote(origin, branch="cal")
    _rewrite_rows(home, _rows_with_b())
    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    capsys.readouterr()
    assert not pending.exists(), "远端肯定没动，留着准备稿会让 repair 误判成'可能已发出'"
    assert hashlib.sha256(committed.read_bytes()).hexdigest() == digest, "凭据不该被准备稿污染"


def test_a_pushed_but_unsaved_publish_keeps_the_recovery_snapshot(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**审阅 P1-①（危险侧）**：push 成功、`save_state` 抛 ⇒ **不许**退回凭据。

    审阅者的表格：`save_state` 抛错时远端已经是 B，而唯一带着 B 的完整课程/考试台账的
    恢复凭据被回退成 A ⇒ `repair` 从此没有 B 可恢复，A+ 的"远端推进到哪里、本地就有凭据
    恢复到哪里"整个失效。更要命的是退回之后四样**自洽地错**（留底=committed=last_push=A），
    门与 `doctor` 都看不见。

    期望：保留 pending ⇒ `repair` 用远端字节裁决出"其实发出去了"，把 pending 提升为
    committed、恢复留底与台账，并**把 `last_push` 本身也修对**。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    # B：数据变一版，然后让 push 之后的 save_state 抛 OSError
    _rewrite_rows(home, _rows_with_b())
    cfg = Settings(home=home)
    real_save = subscribe.save_state
    fuse = {"on": False}

    def blow_up(_cfg: Settings, _state: object) -> None:
        if fuse["on"]:
            raise OSError("disk is full")
        real_save(_cfg, _state)

    monkeypatch.setattr(subscribe, "save_state", blow_up)
    fuse["on"] = True
    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    capsys.readouterr()
    # 故障点已经过去了：关掉保险丝，否则测的是"磁盘一直满"而不是"崩过一次之后能不能恢复"
    fuse["on"] = False

    remote = _git_bytes(f"cal:{_token(home)}.ics", origin)
    assert "示例课程乙".encode() in remote, "前提：远端确实已经是 B"
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.last_push is not None
    assert hashlib.sha256(remote).hexdigest() != state.last_push.content_sha256

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    assert last_local.read_bytes() == remote, "留底必须补到线上那一版"
    fixed = subscribe.load_state(cfg, SEMESTER)
    assert fixed is not None and fixed.last_push is not None
    assert fixed.last_push.content_sha256 == hashlib.sha256(remote).hexdigest(), (
        "repair 必须把已发布状态元数据本身也修对，否则 doctor/status 长期报错版本"
    )

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "无需恢复" in capsys.readouterr().out

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out


def test_a_corrupt_snapshot_is_not_downgraded_to_the_weak_predicate(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**审阅 P1-②**：文件存在但读不懂 ≠ 从没生成过 ⇒ 必须拒绝，不能放行。

    "旧版本从没写过快照"与"新版本写过、后来损坏"不是同一种风险：后者意味着**有一份我们
    本该信任的凭据失效了**，此时用一条更宽松的判据继续发布，等于让一份损坏的安全校验文件
    触发更宽松的校验。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    cfg = Settings(home=home)
    subscribe.journal_path(cfg, SEMESTER).write_text("{ not json", encoding="utf-8")
    _rewrite_rows(home, _rows_with_b())

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "读不懂" in err
    assert "弱判据" not in err, "读不懂不许降级成'没有依据'再放行"


def test_a_kept_ledger_that_disappears_is_not_silently_compatible(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**审阅 P1-③**："本次不动考试台账"不等于"以后也不检查它"。

    固件里没有考试快照 ⇒ 每次发布的 `exam_ledger_text` 都是 `None`（keep）。若 keep 只记
    "别动"、不记"该文件当时是什么字节"，那么这份台账事后被删/被改，下一次 push 照样判定相容
    ⇒ 考试撤销候选静默少一整批，和场景 2 是同一类失效。
    """
    home, origin = env
    assert _init(origin) == 0
    cfg = Settings(home=home)
    exam_ledger = cfg.exam_ledger_path(SEMESTER)
    exam_ledger.parent.mkdir(parents=True, exist_ok=True)
    # 发布**之前**就存在的一份考试台账：这次不动它 ⇒ 快照只该记下它的指纹
    # write_bytes 而非 write_text：后者在 Windows 上会把 \n 翻成 \r\n，
    # 让这份"保留"的台账字节与断言里的字面量不一致（用例自己咬自己）。
    exam_ledger.write_bytes(b"BEGIN:VCALENDAR\r\nKEEP-ME\r\nEND:VCALENDAR\r\n")
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    staged = subscribe.read_journal(cfg, SEMESTER).journal
    assert staged is not None
    assert staged.exam_ledger_text is None, "keep：不该动就别存内容"
    assert staged.exam_ledger_sha256, "keep 必须同时把当时的指纹记下来"

    exam_ledger.unlink()
    _rewrite_rows(home, _rows_with_b())
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "台账" in err
    assert not exam_ledger.exists(), "被拦住时不许顺手重建内容我们并不知道的东西"
    # repair 不许把"补不回来"说成"已与线上一致"：keep 只记指纹、不记内容。
    assert main(["subscribe", "repair", "--semester", SEMESTER]) != 0
    assert "无法恢复" in capsys.readouterr().err


def test_repair_can_discard_a_pending_snapshot_the_remote_never_took(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**T1 那一格必须可自动恢复**：准备稿是 B、远端仍是 A ⇒ 安全放弃 B、恢复 A。

    审阅者的裁决表：远端 A + 本地有"A 已提交 / B 待发"时，`repair` 应能识别 B 从未落地，
    丢掉准备稿并把本地对齐回 A，**不需要用户带 `--allow-unverified-baseline`**。
    判据是远端真实字节，不是本地谁新。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    cfg = Settings(home=home)
    tip_before = _git("rev-parse", "cal", cwd=origin)

    # 布置"死在写准备稿之后、推送之前"的现场：准备稿描述一个远端从没收到过的版本
    published = _git_bytes(f"cal:{_token(home)}.ics", origin).decode("utf-8")
    pending_text = published.replace("END:VCALENDAR", "SUMMARY:从未发出\r\nEND:VCALENDAR")
    subscribe.write_journal(
        cfg,
        semester=SEMESTER,
        token=_token(home),
        branch="cal",
        ics_text=pending_text,
        course_ledger_text=None,
        exam_ledger_text=None,
        uid_recipe=2,
    )
    assert subscribe.pending_path(cfg, SEMESTER).is_file()
    capsys.readouterr()

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "丢弃从未发出的准备稿" in out, "清掉过期准备稿这件事必须说，不能静默动本地文件"
    assert (home / "subscribe" / f"last-{SEMESTER}.ics").read_bytes() == published.encode("utf-8")
    assert not subscribe.pending_path(cfg, SEMESTER).exists(), "确认没落地的准备稿必须丢弃"
    assert _git("rev-parse", "cal", cwd=origin) == tip_before, "repair 不许动远端"

    _rewrite_rows(home, _rows_with_b())
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0, "恢复完就该正常发布，不用旗标"
    capsys.readouterr()


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
    assert subscribe.read_journal(cfg, SEMESTER, pending=True).journal is not None
    assert journal.exam_ledger_text is None, "keep 哨兵必须原样往返"

    restored = cli._restore_from_journal(cfg, journal, SEMESTER)
    assert "考试台账" not in restored
    assert not cfg.exam_ledger_path(SEMESTER).exists(), "keep 不能写成空台账文件"
    assert "课程台账" in restored


def test_doctor_reports_the_same_incompatibility_push_refuses(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**审阅复核第 6 条**：门会拦的事，`doctor` 不能看不见。

    只比"留底 vs 最后一次发布记录"证明不了台账同步 ⇒ 体检绿灯、真发布时才发现本地散架。
    现在两者共用 `check_local_compatibility`，所以判据天然一处。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    _rewrite_rows(home, _rows_with_b())
    fuse = _install_ledger_fuse(monkeypatch)
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    fuse["on"] = False

    from xjtu_calendar.doctor import STATUS_OK, run_checks

    sub = [c for c in run_checks(Settings(home=home), SEMESTER) if c.name == "subscription"]
    assert sub and sub[0].status != STATUS_OK, "台账与已发布凭据失配时 doctor 不许给绿灯"
    assert "台账" in sub[0].detail
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7, "两处判据必须同进同退"


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


# --------------------------------------------------------------------------- #
# 审阅裁决（2026-10-15）：`doctor` 对「已证实不相容的本地发布状态」判**阻塞**（退出码 5）
#
# 原则一句：门会拦的事，体检不许只报普通警告。但**不新增退出码**——复用 `doctor`
# 既有的 `STATUS_FAIL` ⇒ `DOCTOR_EXIT_BLOCKED = 5`；`push` 继续用业务错误 7。
# --------------------------------------------------------------------------- #


def _subscription_check(home: Path) -> tuple[object, bool]:
    """跑一次只读体检，返回（订阅那一条，整体是否阻塞）。

    退出码按「除会话外」的项算：合成 home 里没有会话文件，而「无会话」本来就是
    一条 `fail`——留着它会掩盖我们真正要断言的那一格（订阅判定的成色）。
    """
    from xjtu_calendar.doctor import DOCTOR_EXIT_BLOCKED, doctor_exit_code, run_checks

    checks = [c for c in run_checks(Settings(home=home), SEMESTER) if c.name != "session"]
    sub = [c for c in checks if c.name == "subscription"]
    assert sub, "doctor 必须仍有订阅这一项"
    return sub[0], doctor_exit_code(checks) == DOCTOR_EXIT_BLOCKED


def _crash_after_push_is_committed(
    home: Path,
    origin: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Path:
    """把现场摆成"远端可能已经是 B、本地凭据还是 A"那一瞬，返回未裁决的准备稿路径。

    做法与 `test_a_pushed_but_unsaved_publish_keeps_the_recovery_snapshot` 同一条：
    正常发一次（A）→ 数据改成 B → 让 push 成功之后的 `save_state` 抛 OSError。
    故障点过去就把保险丝关掉，否则测的是"磁盘一直满"而不是"崩过一次之后怎么办"。
    """
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    _rewrite_rows(home, _rows_with_b())
    cfg = Settings(home=home)
    real_save = subscribe.save_state
    fuse = {"on": False}

    def blow_up(_cfg: Settings, _state: object) -> None:
        if fuse["on"]:
            raise OSError("disk is full")
        real_save(_cfg, _state)

    monkeypatch.setattr(subscribe, "save_state", blow_up)
    fuse["on"] = True
    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    capsys.readouterr()
    fuse["on"] = False

    pending = subscribe.pending_path(cfg, SEMESTER)
    assert pending.is_file(), "前提：这次发布的结果本地还没裁决"
    staged = subscribe.read_journal(cfg, SEMESTER, pending=True).journal
    assert staged is not None
    remote_sha = hashlib.sha256(_git_bytes(f"cal:{_token(home)}.ics", origin)).hexdigest()
    assert remote_sha == staged.ics_sha256, "前提：远端确实已经是准备稿那一版"
    return pending


def _private_dir_bytes(home: Path) -> dict[str, bytes]:
    """`subscribe/` 目录的逐文件字节快照，用来证明 doctor 一个字节都没动。"""
    sub = home / "subscribe"
    return {p.name: p.read_bytes() for p in sorted(sub.glob("*")) if p.is_file()}


def test_push_refuses_while_a_publish_result_is_unresolved(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**审阅裁决表（10-15）第 7 行**：准备稿未裁决 ⇒ `push` 必须拒绝（7）。

    这一格旧代码是**放行**的：本地四样与已发布凭据（A）逐样自洽，门看不见问题，
    而线上可能已经是准备稿（B）那一版 ⇒ 下一次构建拿 A 当 SEQUENCE 与撤销基线。
    "自洽地错"正是 P1-02 的靶心，所以未裁决的准备稿本身就是不相容。
    裁决只能由 `subscribe repair` 做（它有远端真实字节），doctor 与 push 都没有。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()

    _crash_after_push_is_committed(home, origin, monkeypatch, capsys)
    tip_before = _git("rev-parse", "cal", cwd=origin)
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "未裁决" in err and "repair" in err
    assert _git("rev-parse", "cal", cwd=origin) == tip_before, "拒绝就不该再推进远端"

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out, "裁决完就恢复成一次普通发布"


def test_doctor_blocks_a_lagging_local_copy(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """留底与已发布凭据不一致 ⇒ `doctor` 判**阻塞**（5），而不是「注意到但不阻止」。

    RED 的根据：这一格 `subscribe push` 已经会返回 7（`check_local_compatibility`
    的 `baseline`），而 doctor 早前只给 WARN ⇒ 体检绿灯、真发布时才发现本地散架。
    """
    from xjtu_calendar.doctor import STATUS_FAIL

    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    _crash_between_state_and_baseline(home, origin, capsys)

    check, blocking = _subscription_check(home)
    assert check.status == STATUS_FAIL, "门会拦的状态不许停在警告档"
    assert "repair" in check.detail
    assert blocking, "已证实不相容 ⇒ doctor 退出码 5"


def test_doctor_blocks_an_unresolved_publish_result(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """「要远端验证才能定」⇒ 阻塞，并把人交给 `repair`；doctor 自己**不 fetch**。

    现场 = push 已成功、`save_state` 抛：远端可能已经是 B，本地凭据却还是 A，
    唯一带 B 的记录躺在未裁决的准备稿里。此时任何"本地看起来自洽"都不算证据，
    而 doctor 的契约是纯本地只读（设计文档 §3.1），所以它只能如实报"定不下来"。
    """
    from xjtu_calendar.doctor import STATUS_FAIL

    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()

    pending = _crash_after_push_is_committed(home, origin, monkeypatch, capsys)

    check, blocking = _subscription_check(home)
    assert check.status == STATUS_FAIL
    assert blocking
    assert "repair" in check.detail
    assert pending.is_file(), "doctor 只读：未裁决的准备稿要留给 repair 处理"


def test_doctor_does_not_write_anything_when_it_blocks(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """doctor 提 `repair` 但**不代跑修复**：报阻塞前后 `subscribe/` 逐文件字节相同。

    审阅者 2026-10-15 的边界条款：体检仪保持只读，否则它会与 repair 抢同一批文件，
    而"警告里顺手把状态改了"是最难复现的一类事故。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    _crash_between_state_and_baseline(home, origin, capsys)

    before = _private_dir_bytes(home)
    _subscription_check(home)
    assert _private_dir_bytes(home) == before, "doctor 判阻塞也不许动本地凭据与留底"


def test_doctor_keeps_the_upgrade_cell_at_a_warning(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """「没有凭据、但留底与最后一次发布逐字节相符」这一格 push 是**放行**的（弱判据），
    所以 doctor 也不许判阻塞——否则体检比门更凶，用户会去做一次根本不必的 repair。
    """
    from xjtu_calendar.doctor import STATUS_WARN

    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    cfg = Settings(home=home)
    subscribe.journal_path(cfg, SEMESTER).unlink()  # 摆成"0.8.5 之前发布过"的现场

    check, blocking = _subscription_check(home)
    assert not blocking, "弱判据放行的状态不许抬到阻塞档"
    assert check.status == STATUS_WARN
    assert "台账" in check.detail, "警告要说清这次没能校验的是哪两样"


# --------------------------------------------------------------------------- #
# 审阅 P0-01（2026-10-15 集中审查）：`rotate` 必须走与 `push` **同一条**发布事务
#
# 旧实现里 rotate 只调 `publish()` 再回写留底/台账，**完全不碰两份凭据**：换 token 并
# 补发成功之后，`last_push.token` 已经是新的（B），而已发布凭据还停在旧 token（A）。
# 于是 `check_local_compatibility` 命中 `journal_token` ⇒ 后续每次 `subscribe push`
# 都被自己的保险丝拦住（退出码 7），而 `repair` 又因 `journal.token != state.token`
# 拒绝恢复——一次完全正常的操作把订阅永久锁死。既有那条 rotate 用例走的是「发布失败」
# 分支（`last_push` 根本没推进），所以全绿套件抓不到这个形态。
# --------------------------------------------------------------------------- #


def test_a_successful_rotate_leaves_the_credentials_on_the_new_token(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """正常序列：push（A）→ rotate 成功（B）→ 改数据 push ⇒ 必须仍然发得出去。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    assert "已用新文件名重新发布" in capsys.readouterr().out

    cfg = Settings(home=home)
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.last_push is not None
    journal = _journal(home)
    assert journal is not None, "rotate 的补发同样要落下恢复凭据"
    assert journal.token == state.token, "凭据必须跟着换到新 token，否则下次发布自锁"
    assert journal.ics_sha256 == state.last_push.content_sha256
    assert not subscribe.pending_path(cfg, SEMESTER).is_file(), "成功收尾不留未裁决准备稿"

    # 这一格正是旧实现的死点：repair 拿不到可用凭据，push 直接被 `journal_token` 拦住
    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    assert "无需恢复" in capsys.readouterr().out

    _rewrite_rows(home, _rows_with_b())
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "已发布" in capsys.readouterr().out


def test_a_rotate_that_crashed_after_push_is_adjudicated_by_repair(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """rotate 补发**成功、状态没落盘**：远端已是新 token 那一版，凭据还是旧的。

    审阅原话：「仅在 rotate 结束时补写一份快照，无法解决推送成功但本地写入失败的崩溃
    窗口。」所以这里要的不是收尾补写，而是与 push 同形的两凭据——准备稿（新 token）
    留在原地等裁决，`repair` 拿远端字节把它提升为已发布凭据。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    cfg = Settings(home=home)
    old_token = _token(home)
    real_save = subscribe.save_state
    fuse = {"on": False, "calls": 0}

    def blow_up(_cfg: Settings, _state: object) -> None:
        # rotate 的落盘顺序是 rotate_token（第 1 次）→ publish 的 save_state（第 2 次）。
        # 只炸第二次，才摆出"远端已推进、状态没跟上"那一瞬。
        fuse["calls"] += 1
        if fuse["on"] and fuse["calls"] > 1:
            raise OSError("disk is full")
        real_save(_cfg, _state)

    monkeypatch.setattr(subscribe, "save_state", blow_up)
    fuse["on"] = True
    assert main(["subscribe", "rotate", "--semester", SEMESTER]) != 0
    capsys.readouterr()
    fuse["on"] = False

    new_token = _token(home)
    assert new_token != old_token, "前提：token 已经换掉了"
    assert f"{new_token}.ics" in _git("ls-tree", "-r", "--name-only", "cal", cwd=origin).split(), (
        "前提：远端已经挂上新文件名"
    )
    pending = subscribe.pending_path(cfg, SEMESTER)
    assert pending.is_file(), "推送结果不确定 ⇒ 准备稿必须留着等裁决"
    staged = subscribe.read_journal(cfg, SEMESTER, pending=True).journal
    assert staged is not None and staged.token == new_token
    committed = _journal(home)
    assert committed is not None and committed.token == old_token

    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "准备稿" in out and "已发布" in out

    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.last_push is not None
    assert state.last_push.token == new_token
    assert state.last_push.content_sha256 == staged.ics_sha256
    assert not pending.is_file(), "裁决完准备稿就该消失"
    local = home / "subscribe" / f"last-{SEMESTER}.ics"
    assert local.read_bytes() == _git_bytes(f"cal:{new_token}.ics", origin), "留底与线上逐字节一致"

    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out, "裁决完就恢复成一次普通发布"


# --------------------------------------------------------------------------- #
# 审阅 P1-04（2026-10-15）：`keep` 只记了"内容指纹"，没记"当时在不在"
# --------------------------------------------------------------------------- #


def test_a_kept_ledger_that_appears_after_the_publish_is_also_incompatible(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**P1-04 最小反例**：发布时考试台账不存在 → 事后冒出一份 → 下次 push 必须拒绝。

    旧代码的 keep 分支是 `if keep_sha is not None and …`，于是「当时不存在」这一格
    整段跳过：既没有指纹可比，也没有存在状态可比。而这份台账正是考试撤销判定的输入
    （`cancel_candidates` 拿它当"上次发过什么"的凭据），半路冒出来的内容会让差集算出
    一批**从没上线过**的撤销，或反过来把真该撤的当成不存在——两种都静默。
    所以 keep 语义的完整陈述是两件事：内容指纹 **+** 在不在。
    """
    home, origin = env
    assert _init(origin) == 0
    cfg = Settings(home=home)
    exam_ledger = cfg.exam_ledger_path(SEMESTER)
    assert not exam_ledger.is_file(), "前提：这次发布没有考试台账可保留"
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    staged = subscribe.read_journal(cfg, SEMESTER).journal
    assert staged is not None
    assert staged.exam_ledger_text is None and staged.exam_ledger_sha256 is None
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    assert subscribe.check_local_compatibility(cfg, state, SEMESTER).ok, "前提：当场仍相容"

    exam_ledger.parent.mkdir(parents=True, exist_ok=True)
    exam_ledger.write_bytes(b"BEGIN:VCALENDAR\r\nAPPEARED-LATER\r\nEND:VCALENDAR\r\n")
    _rewrite_rows(home, _rows_with_b())
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "考试台账" in err and "出现" in err
    assert exam_ledger.read_bytes().startswith(b"BEGIN:VCALENDAR\r\nAPPEARED-LATER"), (
        "拒绝时不许顺手改动用户放进来的那份文件"
    )


# --------------------------------------------------------------------------- #
# 审阅 P1-03（2026-10-15）：快照自洽性必须在**任何恢复写入之前**校验
# --------------------------------------------------------------------------- #


def _corrupt_committed_journal(home: Path, **fields: object) -> None:
    """改已发布凭据里的某几个字段（模拟截断、手工改坏），JSON 结构保持合法。"""
    path = subscribe.journal_path(Settings(home=home), SEMESTER)
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _make_local_copy_lag(home: Path, origin: Path) -> tuple[Path, bytes, bytes]:
    """发一次并把留底改回"上一版"，返回（留底路径, 线上字节, 落后字节）。

    repair 收到这种现场必然要动笔——正好用来验证"校验没过就不许写一个字"。
    """
    cfg = Settings(home=home)
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    remote = _git_bytes(f"cal:{state.token}.ics", origin)
    stale = remote[: remote.rindex(b"BEGIN:VEVENT")] + b"END:VCALENDAR\r\n"
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    last_local.write_bytes(stale)
    return last_local, remote, stale


def test_a_snapshot_whose_body_disagrees_with_its_own_hash_is_invalid(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """`ics_text` 与 `ics_sha256` 自相矛盾 ⇒ 判 `JOURNAL_INVALID`，且 repair 一个字都不写。

    旧实现只要求 JSON 读得懂、字段齐，正文与哈希从不互校。`_restore_from_journal`
    落的是 `ics_text`，`check_local_compatibility` 比的是 `ics_sha256`——两份凭据描述
    不同版本时，恢复写盘与收尾复核互相打脸，中间那几次写已经发生了：一次失败的恢复
    把本地推到一个"既不等于线上、也不等于任何凭据"的第四态。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    cfg = Settings(home=home)
    committed = subscribe.read_journal(cfg, SEMESTER).journal
    assert committed is not None
    last_local, _, stale = _make_local_copy_lag(home, origin)
    _corrupt_committed_journal(
        home, ics_text=committed.ics_text.replace("BEGIN:VCALENDAR", "BEGIN:XCALENDAR")
    )
    capsys.readouterr()

    read = subscribe.read_journal(cfg, SEMESTER)
    assert read.status == subscribe.JOURNAL_INVALID, "正文与自身哈希不符的快照不算「读得懂」"
    assert "哈希" in read.detail, "判据要说清是哪一项自相矛盾"

    assert main(["subscribe", "repair", "--semester", SEMESTER]) != 0
    err = capsys.readouterr().err
    assert "读不懂" in err
    assert last_local.read_bytes() == stale, "校验没过 ⇒ 不许发生任何部分恢复"


def test_a_snapshot_whose_ledger_is_not_valid_base64_is_invalid(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """台账字段坏成"能解码成空串"也算非法——这一格旧代码是**静默清空**。

    `base64.b64decode("!!!")` 不抛错，它丢弃非法字符返回 `b""`（2026-10-15 实测）。
    于是旧实现把这份快照读成"合法、且这次该落的台账是空的"，`_restore_from_journal`
    照着写 ⇒ 真台账被覆写成空文件：撤销历史静默清零，正是用户红线里
    "不得静默重置订阅历史"那条。校验必须用 `validate=True` 的严格解码，且在写盘之前。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    cfg = Settings(home=home)
    last_local, _, stale = _make_local_copy_lag(home, origin)
    _corrupt_committed_journal(home, course_ledger_b64="!!!")
    capsys.readouterr()

    read = subscribe.read_journal(cfg, SEMESTER)
    assert read.status == subscribe.JOURNAL_INVALID
    assert "台账" in read.detail

    assert main(["subscribe", "repair", "--semester", SEMESTER]) != 0
    assert "读不懂" in capsys.readouterr().err
    assert last_local.read_bytes() == stale
    ledger = cfg.course_ledger_path(SEMESTER)
    assert not ledger.is_file() or ledger.stat().st_size > 0, "不许把台账清成空文件"


# --------------------------------------------------------------------------- #
# 审阅 P1-02（2026-10-15）：`--allow-unverified-baseline` 的权限要按 reason 收窄
#
# 旧代码是 `not compat.ok and not flag`：任何一格不相容都能被同一个旗标抹平。问题不是
# "用户能不能强行发布"，而是**强行发布会顺手销毁裁决所需的证据**：门一过，构建照跑，
# `_publish_with_journal` 第一句就是 `write_journal`——那份未裁决的准备稿（B）被这次
# 的新准备稿原地覆盖，`repair` 此后再也答不出"线上到底是哪一版"。
# 所以放行权限必须是一份**显式白名单**，未列出的 reason 一律不放行（默认收紧）。
# --------------------------------------------------------------------------- #


def test_the_flag_cannot_bypass_an_unresolved_pending(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """未裁决的准备稿那一格：带旗标也必须是 7，而且**准备稿字节一字不改**。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()

    pending = _crash_after_push_is_committed(home, origin, monkeypatch, capsys)
    pending_bytes = pending.read_bytes()
    tip_before = _git("rev-parse", "cal", cwd=origin)
    capsys.readouterr()

    assert main(["subscribe", "push", "--semester", SEMESTER, "--allow-unverified-baseline"]) == 7
    err = capsys.readouterr().err
    assert "未裁决" in err and "旗标" in err
    assert pending.read_bytes() == pending_bytes, "旗标不许覆盖掉唯一能裁决这份发布的证据"
    assert _git("rev-parse", "cal", cwd=origin) == tip_before, "被拦住就不该动远端"

    # 出口仍然是 repair（它有远端真实字节），走完之后旗标也不再需要
    assert main(["subscribe", "repair", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out


def test_the_flag_cannot_bypass_an_unreadable_credential(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """已发布凭据读不懂那一格同样不在白名单里：旗标不能把"校验文件坏了"降级成"没有校验"。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    cfg = Settings(home=home)
    capsys.readouterr()

    path = subscribe.journal_path(cfg, SEMESTER)
    path.write_text(path.read_text(encoding="utf-8")[:200], encoding="utf-8")  # 截断
    tip_before = _git("rev-parse", "cal", cwd=origin)

    assert main(["subscribe", "push", "--semester", SEMESTER, "--allow-unverified-baseline"]) == 7
    err = capsys.readouterr().err
    assert "读不懂" in err and "旗标" in err
    assert _git("rev-parse", "cal", cwd=origin) == tip_before


# --------------------------------------------------------------------------- #
# 审阅 P1-05（2026-10-15）：状态里的「上次发布记录」要与凭据**完整**交叉核对
#
# 已有的那一格只比 token（`journal_token`）。缺的两格同样是"两份凭据描述不同版本"：
# 字节哈希（`journal_sha`）与发布分支（`journal_branch`）。都属低成本 fail-closed：
# 现有崩溃序列到不了这两格（前面的格先接住），能到这里的是手工改动与今后新增形态。
# --------------------------------------------------------------------------- #


def test_a_credential_that_disagrees_with_the_publish_record_on_bytes_is_refused(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """凭据自洽（正文==自身哈希），但与 `last_push` 记的哈希不同 ⇒ 拒绝。

    这是"自洽地错"的又一种形状：留底、正文、哈希三者互相咬合，只有状态里那条发布记录
    说"我发出去的是另一版"。旧代码不查这一格 ⇒ 放行后拿凭据那一版的字节当 SEQUENCE
    基线，而线上是 `last_push` 那一版。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    cfg = Settings(home=home)
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.last_push is not None
    capsys.readouterr()

    path = subscribe.journal_path(cfg, SEMESTER)
    data = json.loads(path.read_text(encoding="utf-8"))
    forged = data["ics_text"].replace("END:VCALENDAR", "SUMMARY:另一版\r\nEND:VCALENDAR")
    data["ics_text"] = forged
    data["ics_sha256"] = hashlib.sha256(forged.encode("utf-8")).hexdigest()
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # 留底跟着改成同一版，好让 `baseline` 那一格先通过——要暴露的只剩交叉核对
    (home / "subscribe" / f"last-{SEMESTER}.ics").write_text(forged, encoding="utf-8", newline="")

    check = subscribe.check_local_compatibility(cfg, state, SEMESTER)
    assert not check.ok and check.reason == "journal_sha", "旧实现在这里是 ok=True"
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    assert "哈希" in capsys.readouterr().err


def test_a_credential_from_another_publish_branch_is_refused(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """凭据记的分支与当前订阅不符 ⇒ 它描述的是另一条发布通道上的产物，不能当依据。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    cfg = Settings(home=home)
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    capsys.readouterr()

    path = subscribe.journal_path(cfg, SEMESTER)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["branch"] = "cal-someone-else"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    check = subscribe.check_local_compatibility(cfg, state, SEMESTER)
    assert not check.ok and check.reason == "journal_branch"
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 7
    assert "分支" in capsys.readouterr().err


def test_rotate_still_checks_the_unresolved_pending_when_the_local_copy_is_missing(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """留底缺失时 rotate 有一条"不构建、只换 token"的早退路，但**未裁决的准备稿不看例外**。

    门要防的是"拿过期留底当 SEQUENCE 基线"，留底不在场时那件事不发生（spec §6.8 的早退
    文案照旧）。可换 token 会把手里两份凭据统统变成旧 token 的——`repair` 此后再也答不出
    线上是哪一版，那一格就从"待裁决"变成"永久无出口"。所以这一格必须在换 token 之前就停。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    pending = _crash_after_push_is_committed(home, origin, monkeypatch, capsys)
    old_token = _token(home)
    (home / "subscribe" / f"last-{SEMESTER}.ics").unlink()
    capsys.readouterr()

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 7
    err = capsys.readouterr().err
    assert "未裁决" in err
    assert _token(home) == old_token, "被拦住就不该把 token 换掉"
    assert pending.is_file(), "准备稿是唯一的裁决线索，不许被顺手清理"
