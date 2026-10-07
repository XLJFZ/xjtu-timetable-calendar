"""``subscribe`` 子命令的 CLI 接线 e2e：init → push →（改数据）push → rotate → status。

远端是 file:// 裸仓库（全程离线）。锁住两条红线：

- ``--debug`` 时 token 绝不出现在 stderr（日志通道），stdout 主动打印 URL 属预期（裁定 7）；
- 空/垃圾发布分支必须先过 ``git check-ref-format``，绝不进入 ls-remote/强推路径（裁定 1）。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import urllib.error
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from subscribe_support import SEMESTER, make_home, payload_row

from xjtu_calendar import exporter, subscribe
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

#: 钉住 DTSTAMP：render_ics 经 ``now_local()`` 取时钟，不钉的话同一数据两次渲染
#: 字节几乎必不同，「再 push → 无变化」分支将退化为同秒巧合（flaky）。
FIXED_STAMP = datetime(2026, 10, 7, 12, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


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
    """home==tmp_path（XJTU_CALENDAR_HOME 指到这里，校历/作息/raw 三件套已布置），origin 为 file:// 裸仓库。"""
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    monkeypatch.setattr(exporter, "now_local", lambda: FIXED_STAMP)
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
