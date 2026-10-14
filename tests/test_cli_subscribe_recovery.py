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
    assert "留底" not in capsys.readouterr().err
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


@pytest.mark.xfail(
    reason="P1-02 补验场景 1：自愈目前只挂在 NO_CHANGE 分支上，构建前的基线相容性检查未实现",
    strict=True,
)
def test_stale_baseline_must_be_caught_before_the_build(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**场景 1**：留底落后 + 数据又变了 ⇒ 自愈**不能只挂在 NO_CHANGE 分支上**。

    版本 A 发布成功后崩在写留底前（远端与状态都是 A、留底还是上一版），随后课表变成 C。
    因为 C 与 A 的哈希不同，`publish()` 不走 NO_CHANGE，那处自愈根本不触发；
    而 `build_ics_for_semester` 早在 `publish()` **之前**就把过期留底当 SEQUENCE 基线
    用掉了（`baseline_probe` / `published_baseline_text` 两个入参都读它）。
    相容性检查必须搬到构建之前，判据现成：`sha256(留底) vs last_push.content_sha256`。

    **当前实测：缺陷成立**（push 返回 0，过期留底被当基线用掉了），所以这条先
    `xfail(strict)` 钉住，修复落地后它会变成 XPASS 并强制我们摘掉标记。
    """
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    _crash_between_state_and_baseline(home, origin, capsys)  # 远端/状态=已发布版，留底=旧版
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    stale_digest = hashlib.sha256(last_local.read_bytes()).hexdigest()
    assert stale_digest != _state_sha(home)

    _rewrite_rows(home, _rows_with_b())  # 数据再变一版 ⇒ 必然走真实推送分支

    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    err = capsys.readouterr().err
    assert "留底" in err, "构建前必须拦住「过期基线」，而不是拿它算完 SEQUENCE 再发出去"
    assert hashlib.sha256(last_local.read_bytes()).hexdigest() == stale_digest, (
        "被拦住时不许顺手改留底（拦住 = 什么都不动，让用户决定）"
    )


@pytest.mark.xfail(
    reason="P1-02 补验场景 2：台账写失败被降级成 warning，留底与台账失配无人发现 ⇒ 撤销漏发",
    strict=True,
)
def test_ledger_write_failure_does_not_lose_a_future_cancellation(
    env: tuple[Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """**场景 2**：留底是 B、课程台账仍是 A ⇒ 下一次撤销会**漏发**。

    `cancel_candidates` 的差集是「台账 − live」，台账里根本没有那条 UID 就成不了候选；
    而台账写失败今天只降级成 warning（D24①，`_write_course_ledger` 的措辞就是
    "下次仍按原台账判断撤销"）。这条用例把"错过一次撤销"钉成失败。
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
    ledger = (home / "subscribe" / f"last-courses-{SEMESTER}.ics").read_text(encoding="utf-8")
    assert "示例课程乙" not in ledger, "台账写失败后应当留在旧内容（本用例的前提）"
    assert "示例课程乙" in (home / "subscribe" / f"last-{SEMESTER}.ics").read_text("utf-8"), (
        "留底必须已经带上乙 ⇒ 留底与台账从这一刻起失配"
    )

    # C：把乙删掉 ⇒ 它应当以 STATUS:CANCELLED 下发（撤销保留期还没过）
    fuse["on"] = False
    _rewrite_rows(home, [payload_row()])
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    published = (home / "subscribe" / f"last-{SEMESTER}.ics").read_text(encoding="utf-8")
    assert "STATUS:CANCELLED" in published, "台账落后于留底，乙的消失被当成「从来不存在」——撤销漏发"


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
