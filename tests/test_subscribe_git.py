"""git 发布层集成：file:// 假远端上验证护栏、幂等、孤儿单提交、失败注入。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar import subscribe
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import SubscribeGuardError, SubscribePublishError
from xjtu_calendar.subscribe import PublishOutcome, SubscriptionState, new_token, publish

ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"
ICS2 = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nSUMMARY:v2\r\nEND:VCALENDAR\r\n"

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
def env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, SubscriptionState, Path]:
    for key, value in GIT_IDENTITY.items():
        monkeypatch.setenv(key, value)
    origin = tmp_path / "fake-origin.git"
    _git("init", "-q", "--bare", str(origin))
    cfg = make_home(tmp_path / "home")
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url=origin.resolve().as_uri(),
        branch="cal",
        token=new_token(),
        url_base="https://x.github.io/cal/",
    )
    subscribe.save_state(cfg, state)
    return cfg, state, origin


def _tip_tree(origin: Path, branch: str = "cal") -> list[str]:
    return _git("ls-tree", "-r", "--name-only", branch, cwd=origin).split()


def _tip_parents(origin: Path) -> int:
    out = _git("rev-list", "--parents", "-n1", "cal", cwd=origin)
    return len(out.split()) - 1


def test_publish_creates_orphan_tip(env: tuple) -> None:
    cfg, state, origin = env
    result = publish(cfg, state, ICS)
    assert result.outcome is PublishOutcome.PUSHED
    assert result.url == state.subscription_url
    assert _tip_tree(origin) == [f"{state.token}.ics"]
    assert _tip_parents(origin) == 0
    assert state.last_push is not None and state.last_push.token == state.token
    # 远端 blob 与提交的字节完全一致（CRLF 不被 autocrlf 吃掉）
    assert _git_bytes(f"cal:{state.token}.ics", origin) == ICS.encode("utf-8")
    # 成功即落盘：重载与内存态一致
    assert subscribe.load_state(cfg, SEMESTER) == state


def test_publish_skips_when_unchanged(env: tuple) -> None:
    cfg, state, origin = env
    publish(cfg, state, ICS)
    before = _git("rev-parse", "cal", cwd=origin)
    again = publish(cfg, state, ICS)
    assert again.outcome is PublishOutcome.NO_CHANGE
    assert _git("rev-parse", "cal", cwd=origin) == before


def test_publish_pushes_when_content_same_but_token_changed(env: tuple) -> None:
    cfg, state, origin = env
    publish(cfg, state, ICS)
    old = state.token
    state.token = new_token()
    result = publish(cfg, state, ICS)
    assert result.outcome is PublishOutcome.PUSHED
    # 旧文件必须从 tip 消失：孤儿重建 + `add -A` 把删除一并入树（pathspec 形态会残留）
    assert _tip_tree(origin) == [f"{state.token}.ics"]
    assert _tip_parents(origin) == 0
    assert old not in " ".join(_tip_tree(origin))
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.last_push == state.last_push


def test_guard_refuses_dirty_branch(env: tuple) -> None:
    cfg, state, origin = env
    # 手工往 cal 分支推一个含普通提交的仓库
    seed = origin.parent / "seed"
    _git("clone", "-q", origin.resolve().as_uri(), str(seed))
    (seed / "README.md").write_text("human work", encoding="utf-8")
    _git("checkout", "-q", "-b", "cal", cwd=seed)
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "real content", cwd=seed)
    _git("push", "-q", "origin", "cal", cwd=seed)
    with pytest.raises(SubscribeGuardError):
        publish(cfg, state, ICS)
    assert _tip_tree(origin) == ["README.md"]  # 远端分毫未动
    # 护栏在任何改动之前：状态（内存与磁盘）都还没有 last_push
    assert state.last_push is None
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.last_push is None


def test_guard_refuses_nested_single_ics(env: tuple) -> None:
    cfg, state, origin = env
    # tip 含且仅含一个文件，但在子目录里（dir/x.ics）：递归 ls-tree 会误判为
    # 「单个 .ics」放行，随后 add -A 把嵌套文件一并带上新 tip → 强推成两文件。
    seed = origin.parent / "seed"
    _git("clone", "-q", origin.resolve().as_uri(), str(seed))
    (seed / "dir").mkdir()
    (seed / "dir" / "x.ics").write_text("nested", encoding="utf-8")
    _git("checkout", "-q", "-b", "cal", cwd=seed)
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "nested ics", cwd=seed)
    _git("push", "-q", "origin", "cal", cwd=seed)
    with pytest.raises(SubscribeGuardError):
        publish(cfg, state, ICS)
    assert _tip_tree(origin) == ["dir/x.ics"]  # 远端分毫未动
    assert state.last_push is None
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.last_push is None


def test_publish_failure_keeps_previous_state(env: tuple) -> None:
    cfg, state, origin = env
    publish(cfg, state, ICS)
    first = state.last_push
    assert first is not None
    # 跨平台失败注入：repo_url 指向不存在的 file:// 仓库 → ls-remote 退出码非 0 非 2
    state.repo_url = (origin.parent / "gone.git").as_uri()
    with pytest.raises(SubscribePublishError, match="无法探测远端分支"):
        publish(cfg, state, ICS2)
    assert state.last_push == first
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.last_push == first
    assert _tip_tree(origin) == [f"{state.token}.ics"]  # 旧远端仍是上一版
