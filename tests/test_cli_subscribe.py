"""``subscribe`` 子命令的 CLI 接线 e2e：init → push →（改数据）push → rotate → status。

远端是 file:// 裸仓库（全程离线）。锁住两条红线：

- ``--debug`` 时 token 绝不出现在 stderr（日志通道），stdout 主动打印 URL 属预期（裁定 7）；
- 空/垃圾发布分支必须先过 ``git check-ref-format``，绝不进入 ls-remote/强推路径（裁定 1）。
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import urllib.error
from collections.abc import Sequence
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home, payload_row

from xjtu_calendar import subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings

URL_BASE = "https://me.github.io/timetable/"

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
    """home==tmp_path（XJTU_CALENDAR_HOME 指到这里，校历/作息/raw 三件套已布置），origin 为 file:// 裸仓库。

    不再钉时钟：I3 之后 DTSTAMP 由快照 mtime 推导，同一快照两次构建字节一致，
    「再 push → 无变化」分支是确定性行为而不是同秒巧合。
    """
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    origin = tmp_path / "origin.git"
    _git("init", "-q", "--bare", str(origin))
    make_home(tmp_path)
    return tmp_path, origin


def _init(origin: Path, *, url_base: str = URL_BASE, extra: Sequence[str] = ()) -> int:
    return main(
        [
            "subscribe",
            "init",
            "--repo",
            origin.resolve().as_uri(),
            "--url-base",
            url_base,
            *extra,
            "--semester",
            SEMESTER,
        ]
    )


def test_full_flow(env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    home, origin = env
    cfg = Settings(home=home)

    assert _init(origin) == 0
    out = capsys.readouterr().out
    assert "订阅 URL" in out and URL_BASE in out and "Pages" in out

    # 重复 init：业务错误 + 建议，不给 traceback
    assert _init(origin) != 0
    err = capsys.readouterr().err
    assert "已登记" in err and "Traceback" not in err

    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.token
    token = state.token

    # 首次 push：tip 恰好一个 <token>.ics，留底与远端字节一致（CRLF 完整，裁定 5）
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "已发布" in out and state.subscription_url in out
    tip = _git("ls-tree", "-r", "--name-only", "cal", cwd=origin).split()
    assert tip == [f"{token}.ics"]
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    blob = _git_bytes(f"cal:{token}.ics", origin)
    assert last_local.read_bytes() == blob
    assert b"BEGIN:VCALENDAR\r\n" in blob and b"\r\r\n" not in blob
    assert b"SEQUENCE:0" in blob  # 首次发布没有基线，全部按新增

    # 内容未变再 push：publish 幂等跳过
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out

    # 改数据（换教室）：留底充当 SEQUENCE 基线 → 同 UID 递增
    raw = cfg.raw_timetable_path(SEMESTER)
    raw.write_text(
        json.dumps({"kbList": [payload_row(JASMC="B-2002")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "已发布" in capsys.readouterr().out
    blob2 = _git_bytes(f"cal:{token}.ics", origin)
    assert blob2 != blob
    assert b"SEQUENCE:1" in blob2
    assert last_local.read_bytes() == blob2

    # rotate：换 token 并立即以新文件名重新发布，旧文件从 tip 消失
    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "新订阅 URL" in out and "已用新文件名重新发布" in out
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.token != token
    tip = _git("ls-tree", "-r", "--name-only", "cal", cwd=origin).split()
    assert tip == [f"{reloaded.token}.ics"]

    # rotate 重新发布成功后，status 才可以说「无变更」
    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert reloaded.subscription_url in out
    assert "一致，无变更" in out


def test_debug_stderr_never_contains_token(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """裁定 7（定稿形态）：--debug 全链路 push 后，token 不得出现在 stderr。

    日志走 stderr、URL 走 stdout，二者分流正是该设计的验收点：stdout 里能看到
    token（主动打印属预期），stderr 里一个字符都不行。
    """
    home, origin = env
    cfg = Settings(home=home)
    assert _init(origin) == 0
    capsys.readouterr()

    assert main(["--debug", "subscribe", "push", "--semester", SEMESTER]) == 0
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    captured = capsys.readouterr()
    assert state.token not in captured.err
    assert state.token in captured.out


def test_invalid_branch_fails_before_touching_remote(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """裁定 1：空/垃圾分支必须在 check-ref-format 处失败，绝不进入 ls-remote/强推。"""
    home, origin = env
    cfg = Settings(home=home)

    assert _init(origin, extra=["--branch", "bad branch"]) != 0
    err = capsys.readouterr().err
    assert "分支" in err and "Traceback" not in err
    assert subscribe.load_state(cfg, SEMESTER) is None  # 未落盘

    assert _init(origin, extra=["--branch", ""]) != 0
    capsys.readouterr()
    assert subscribe.load_state(cfg, SEMESTER) is None

    assert _init(origin) == 0
    capsys.readouterr()
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None
    state.branch = ""  # 篡改状态文件模拟分支丢失
    subscribe.save_state(cfg, state)

    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    err = capsys.readouterr().err
    assert "分支" in err
    # push 拦下之后 rotate 同样拦（且 token 未被换掉）
    before = subscribe.load_state(cfg, SEMESTER)
    assert before is not None
    assert main(["subscribe", "rotate", "--semester", SEMESTER]) != 0
    assert "分支" in capsys.readouterr().err
    after = subscribe.load_state(cfg, SEMESTER)
    assert after is not None and after.token == before.token

    # 拦截发生在任何 git 远端操作之前：work 目录没建，远端连 cal ref 都不存在
    assert not (home / "subscribe" / f"work-{SEMESTER}").exists()
    assert _git("ls-remote", origin.resolve().as_uri(), "refs/heads/cal").strip() == ""


def test_status_verify_reports_failure_not_traceback(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """裁定 3：verify_url 抛出的 ValueError/URLError/TimeoutError 必须在 CLI 边界被包住。"""
    _, origin = env
    assert _init(origin, url_base="file:///D:/tmp/cal") == 0
    capsys.readouterr()

    # 自然路径：非 http/https 协议 → 未通过（verify_url 自己返回，不抛）
    assert main(["subscribe", "status", "--semester", SEMESTER, "--verify"]) == 0
    out = capsys.readouterr().out
    assert "未通过" in out and "file" in out

    # 异常路径：verify_url 从底下抛出 → CLI 打印未通过 + 原因，退出码仍是 0，无 traceback
    for exc in (
        ValueError("unknown url type"),
        urllib.error.URLError("dns down"),
        TimeoutError("timed out"),
    ):

        def _boom(url: str, timeout: float = 10.0, _e: Exception = exc) -> tuple[bool, str]:
            raise _e

        monkeypatch.setattr(subscribe, "verify_url", _boom)
        assert main(["subscribe", "status", "--semester", SEMESTER, "--verify"]) == 0
        captured = capsys.readouterr()
        assert "未通过" in captured.out
        assert "Traceback" not in captured.err


def test_status_after_token_change_not_misleading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """裁定 2：status 的「无变更」必须同时满足 last_push 存在 + sha 一致 + token 一致。

    rotate 落盘后的形态是「新 token、last_push 仍是旧 token」：此时
    ``has_unpublished_changes``（只比 sha）会说没有变化，但远端还挂在旧
    文件名上——status 绝不得跟着说「无变更」。
    """
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    cfg = make_home(tmp_path)
    ics = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
    state = subscribe.SubscriptionState(
        semester=SEMESTER,
        repo_url=(tmp_path / "gone.git").as_uri(),
        branch="cal",
        token="tok-a",
        url_base=URL_BASE,
    )
    state.last_push = subscribe.PushRecord(
        pushed_at="2026-10-07T00:00:00Z",
        content_sha256=hashlib.sha256(ics.encode("utf-8")).hexdigest(),
        token="tok-a",
    )
    subscribe.save_state(cfg, state)
    last_local = subscribe.subscribe_dir(cfg) / f"last-{SEMESTER}.ics"
    last_local.parent.mkdir(parents=True, exist_ok=True)
    last_local.write_text(ics, encoding="utf-8", newline="")

    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    assert "一致，无变更" in capsys.readouterr().out

    state.token = "tok-b"  # rotate_token 的磁盘形态
    subscribe.save_state(cfg, state)
    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "无变更" not in out
    assert "重新发布" in out


def test_rotate_publish_failure_advises_push(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """裁定 4：rotate 后重新发布失败 → 明说「token 已换但尚未重发布」，不得报成功。"""
    home, origin = env
    cfg = Settings(home=home)
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    old = subscribe.load_state(cfg, SEMESTER)
    assert old is not None

    # 把远端改坏：rotate 内部的重新发布必然失败
    broken = subscribe.load_state(cfg, SEMESTER)
    assert broken is not None
    broken.repo_url = (home / "gone.git").as_uri()
    subscribe.save_state(cfg, broken)

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) != 0
    out = capsys.readouterr().out
    assert "尚未重新发布" in out and "subscribe push" in out
    assert "已用新文件名重新发布" not in out

    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.token != old.token
    assert reloaded.last_push is not None and reloaded.last_push.token == old.token

    # 按指引修好远端后 push 成功：新 token 文件名上线，旧文件从 tip 消失
    fixed = subscribe.load_state(cfg, SEMESTER)
    assert fixed is not None
    fixed.repo_url = origin.resolve().as_uri()
    subscribe.save_state(cfg, fixed)
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "已发布" in capsys.readouterr().out
    tip = _git("ls-tree", "-r", "--name-only", "cal", cwd=origin).split()
    assert tip == [f"{reloaded.token}.ics"]


def test_requires_init_and_semester(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """未 init / 未指定学期的错误口径与 cmd_export 一致；缺动作由 parser 拦截。"""
    _home, origin = env
    assert origin.is_dir()

    code = main(["subscribe", "push", "--semester", SEMESTER])
    assert code != 0
    err = capsys.readouterr().err
    assert "尚未登记订阅" in err and "init" in err and "Traceback" not in err

    assert main(["subscribe", "status"]) != 0
    assert "未指定学期" in capsys.readouterr().err

    with pytest.raises(SystemExit) as wrapped:
        main(["subscribe"])
    assert wrapped.value.code == 2


def test_corrupt_state_file_surfaces_domain_error(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """Task 2 carryover：状态文件损坏时 CLI 给业务错误，不把 KeyError/JSONDecodeError 甩给用户。"""
    home, origin = env
    assert _init(origin) == 0
    capsys.readouterr()
    state_file = home / "subscribe" / f"subscribe-{SEMESTER}.json"
    state_file.write_text("{ not json", encoding="utf-8")

    assert main(["subscribe", "status", "--semester", SEMESTER]) != 0
    err = capsys.readouterr().err
    assert "损坏" in err and "Traceback" not in err

    # init 的重复登记检查走同一读路径，同样必须拦下（而不是假装没登记过）
    assert _init(origin) != 0
    assert "损坏" in capsys.readouterr().err


def test_missing_git_blocks_publish_actions_not_status(
    env: tuple[Path, Path],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """I2（spec §7「git 不在 PATH」）：init/push/rotate 前置探测拦成业务错误。

    只 monkeypatch ``shutil.which``（探测风格同 find_browser）；status 不触碰
    git，必须照常可用——这是「探测只加在用 git 的路径」口径的锁定断言。
    """
    home, origin = env
    monkeypatch.setattr(shutil, "which", lambda name: None)

    for argv in (
        ["subscribe", "init", "--repo", origin.resolve().as_uri(), "--semester", SEMESTER],
        ["subscribe", "push", "--semester", SEMESTER],
        ["subscribe", "rotate", "--semester", SEMESTER],
    ):
        assert main(argv) != 0
        err = capsys.readouterr().err
        assert "git" in err and "安装" in err and "Traceback" not in err

    # init 被拦下后没有落盘任何状态
    assert not (home / "subscribe" / f"subscribe-{SEMESTER}.json").exists()

    # 已 init 的情况下 status 不受探测影响
    monkeypatch.setattr(shutil, "which", lambda name: "git")
    assert _init(origin) == 0
    capsys.readouterr()
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    assert URL_BASE in capsys.readouterr().out


def test_push_refuses_when_last_push_exists_but_archive_missing(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """I4：有成功推送记录但本地留底丢失 → 拒绝重建（否则全部事件 SEQUENCE 归零，
    客户端历史被静默重置）。远端与状态必须分毫未动。"""
    home, origin = env
    cfg = Settings(home=home)
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    tip_before = _git("rev-parse", "cal", cwd=origin)
    last_local = home / "subscribe" / f"last-{SEMESTER}.ics"
    last_local.unlink()

    assert main(["subscribe", "push", "--semester", SEMESTER]) != 0
    err = capsys.readouterr().err
    assert "本地留底缺失" in err and "subscribe init" in err and "Traceback" not in err

    assert _git("rev-parse", "cal", cwd=origin) == tip_before  # 远端仍是上一版
    state = subscribe.load_state(cfg, SEMESTER)
    assert state is not None and state.last_push is not None  # 状态未被动过

    # 按指引找回留底（现实中从旧 URL 下载，这里用远端 blob 原样放回）后 push 恢复工作
    blob = _git_bytes(f"cal:{state.token}.ics", origin)
    last_local.write_bytes(blob)
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out


def _webcal_lines(out: str) -> list[str]:
    """stdout 里所有带 webcal scheme 的行（本仓库只有提示行会用到这个 scheme）。"""
    return [line for line in out.splitlines() if "webcal://" in line]


def test_webcal_hint_shown_once_per_publish_facing_action(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """init / push / NO_CHANGE / status 四处各补**一行** iOS/macOS 的 webcal 链接。

    钉三点：① 只在 https 的 url_base 下出现（本 fixture 就是 https）；② 每次调用
    恰好一行，不重复刷屏；③ 内容与 `SubscriptionState.webcal_url` 逐字符一致——
    CLI 不许自己再拼一遍 scheme（三处共用同一个函数）。
    """
    _home, origin = env
    assert _init(origin) == 0
    state = subscribe.load_state(Settings(home=_home), SEMESTER)
    assert state is not None and state.webcal_url is not None

    out = capsys.readouterr().out
    lines = _webcal_lines(out)
    assert len(lines) == 1 and state.webcal_url in lines[0]
    assert "通常" in lines[0] and "未验证" in lines[0]
    # v0.4 真机踩过的坑：重定向到 cp936 管道时非 GBK 字符会炸（见 _harden_output_streams）
    lines[0].encode("gbk")

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "已发布" in out
    assert len(_webcal_lines(out)) == 1 and state.webcal_url in _webcal_lines(out)[0]

    # NO_CHANGE 分支也要打：用户可能第一次没记下链接，幂等跳过不是「没有链接」
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "无变化" in out
    assert len(_webcal_lines(out)) == 1 and state.webcal_url in _webcal_lines(out)[0]

    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert len(_webcal_lines(out)) == 1 and state.webcal_url in _webcal_lines(out)[0]


def test_rotate_prints_the_new_webcal_link(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """rotate 换的就是 URL 本身 ⇒ 提示行必须给**新** token 的 webcal 链接，且只有一行。

    旧地址在补发成功后立刻失效，这里若漏打、或把旧值打出来，iOS 用户会拿着一个
    已经死掉的链接反复重试——换 token 恰恰是最需要新链接的时刻。
    """
    home, origin = env
    assert _init(origin) == 0
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    old = subscribe.load_state(Settings(home=home), SEMESTER)
    assert old is not None and old.webcal_url is not None
    capsys.readouterr()

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    new = subscribe.load_state(Settings(home=home), SEMESTER)
    assert new is not None and new.webcal_url is not None
    assert new.token != old.token

    lines = _webcal_lines(capsys.readouterr().out)
    assert len(lines) == 1
    assert new.webcal_url in lines[0]
    assert old.webcal_url not in lines[0]


def test_webcal_hint_absent_for_non_https_and_before_init(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """两条「不许提示」的边界：自建非 https 的 url_base、以及尚未 init 的 status。

    后者还钉住退出码口径未变（SubscribeNotConfigured 的既有 1），webcal 提示不是
    一个新的失败模式，status 也不许因为 state 是 None 而炸。
    """
    home, origin = env
    assert _init(origin, url_base="http://pages.example.com/cal/") == 0
    out = capsys.readouterr().out
    assert "订阅 URL" in out and not _webcal_lines(out)
    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    assert not _webcal_lines(capsys.readouterr().out)

    # 换一份干净的 home：未 init 时 status 的口径与之前一致（无 webcal 行、无 traceback）
    (home / "subscribe" / f"subscribe-{SEMESTER}.json").unlink()
    code = main(["subscribe", "status", "--semester", SEMESTER])
    assert code == 1
    captured = capsys.readouterr()
    assert not _webcal_lines(captured.out)
    assert "尚未登记订阅" in captured.err and "Traceback" not in captured.err


def test_publish_never_carries_a_stray_file_from_the_work_dir(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """**P2-03（隐私）**：发布工作目录里的残留文件**绝不**能上公开分支。

    旧实现用不带 pathspec 的 `git add -A`：`work-<学期>/` 里只要有任何不是 `.ics`
    的东西（手工放进去的笔记、上一次中断留下的临时文件、别的工具写的日志），
    都会被一并提交并强推上那个**公开**分支——订阅分支的读者是"知道 URL 的人"，
    这等于把本地文件发布出去。远端 tip 的护栏只查"进来之前长什么样"，管不了
    "这一次要发出去的东西长什么样"。

    期望：只发布 `<token>.ics` 这一个文件，多余的东西既不进 tip 也不必让用户打扫。
    """
    home, origin = env
    cfg = Settings(home=home)
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    token = subscribe.load_state(cfg, SEMESTER).token

    work = subscribe.work_dir(cfg, SEMESTER)
    (work / "私人笔记.txt").write_text("绝不该出现在公开分支上\n", encoding="utf-8")
    (work / "nested").mkdir(exist_ok=True)
    (work / "nested" / "scratch.log").write_text("中断残留\n", encoding="utf-8")

    # 改一点数据，逼它走真正的发布分支（不是 NO_CHANGE）
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps({"kbList": [payload_row(JASMC="B-2002")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    tip = _git("ls-tree", "-r", "--name-only", "cal", cwd=origin).split()
    assert tip == [f"{token}.ics"], "发布分支上只许有那一个 ICS：多一个文件就是隐私外泄"
    blob = _git_bytes(f"cal:{token}.ics", origin)
    assert b"B-2002" in blob, "护栏不是靠少发内容实现的：该发的照发"
    assert (home / "subscribe" / f"last-{SEMESTER}.ics").read_bytes() == blob


def test_publish_still_replaces_the_old_token_file(
    env: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """P2-03 的反面：收紧 staging **不许**把 rotate 语义弄坏。

    旧注释解释过 `add -A` 的存在理由：孤儿分支的索引带着上一份 token 文件，
    删除不入索引 ⇒ tip 出现两个 `.ics`，旧 URL 还能读到课表。现在改成
    「清空索引 + 只加这一个文件」，结果必须仍是**恰好一个文件**。
    """
    home, origin = env
    cfg = Settings(home=home)
    assert _init(origin) == 0
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    capsys.readouterr()

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    capsys.readouterr()
    new_token = subscribe.load_state(cfg, SEMESTER).token

    tip = _git("ls-tree", "-r", "--name-only", "cal", cwd=origin).split()
    assert tip == [f"{new_token}.ics"], "rotate 之后 tip 必须只剩新 token 那一份"
