"""URL 订阅发布：状态文件 + git 孤儿提交发布（设计见 docs/design/2026-10-07-url-subscribe-ics.md）。

状态层负责 token 生成、Pages URL 推导、状态文件的私有原子落盘与往返；
发布层（:func:`publish`）把渲染好的 .ics 文本以**无父孤儿单提交**强推到
状态文件记录的分支，护栏拒绝覆盖非本工具产物（spec §5.1）。
此外是四个供 CLI 组合的小工具：轮换 token、量快照新鲜度、判断有无未发布
变更，以及对订阅 URL 做匿名 GET 自检。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from .config import Settings
from .errors import (
    SubscribeGuardError,
    SubscribeNotConfigured,  # noqa: F401  (Task 4/5 的 CLI 层使用)
    SubscribePublishError,
)
from .fileutil import atomic_write_text

__all__ = [
    "TOKEN_BYTES",
    "PublishOutcome",
    "PublishResult",
    "PushRecord",
    "SubscriptionState",
    "derive_url_base",
    "has_unpublished_changes",
    "load_state",
    "new_token",
    "publish",
    "rotate_token",
    "save_state",
    "snapshot_age_days",
    "state_path",
    "subscribe_dir",
    "verify_url",
    "work_dir",
]

TOKEN_BYTES = 16

_URL_BASE_RE = re.compile(
    r"(?:https://|git@|ssh://git@)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def new_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def derive_url_base(repo_url: str) -> str | None:
    m = _URL_BASE_RE.match(repo_url)
    if not m:
        return None
    return f"https://{m.group(1)}.github.io/{m.group(2)}/"


@dataclass
class PushRecord:
    pushed_at: str  # UTC ISO-8601
    content_sha256: str
    token: str


@dataclass
class SubscriptionState:
    semester: str
    repo_url: str
    branch: str
    token: str
    url_base: str
    last_push: PushRecord | None = None

    @property
    def subscription_url(self) -> str:
        return self.url_base.rstrip("/") + f"/{self.token}.ics"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> SubscriptionState:
        raw_push = data.get("last_push")
        last_push: PushRecord | None = None
        if isinstance(raw_push, dict):
            last_push = PushRecord(
                pushed_at=str(raw_push["pushed_at"]),
                content_sha256=str(raw_push["content_sha256"]),
                token=str(raw_push["token"]),
            )
        return cls(
            semester=str(data["semester"]),
            repo_url=str(data["repo_url"]),
            branch=str(data["branch"]),
            token=str(data["token"]),
            url_base=str(data["url_base"]),
            last_push=last_push,
        )


def subscribe_dir(cfg: Settings) -> Path:
    return cfg.home / "subscribe"


def state_path(cfg: Settings, semester: str) -> Path:
    return subscribe_dir(cfg) / f"subscribe-{semester}.json"


def work_dir(cfg: Settings, semester: str) -> Path:
    return subscribe_dir(cfg) / f"work-{semester}"


def load_state(cfg: Settings, semester: str) -> SubscriptionState | None:
    path = state_path(cfg, semester)
    if not path.is_file():
        return None
    return SubscriptionState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_state(cfg: Settings, state: SubscriptionState) -> None:
    # token 等价凭据：private 原子写（同 storage_state 待遇，spec §6）
    subscribe_dir(cfg).mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_path(cfg, state.semester),
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        private=True,
    )


# --------------------------------------------------------------------------- #
# git 发布层（spec §5.1）
# --------------------------------------------------------------------------- #

_GIT_COMMON = ["-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"]

#: 内部中转分支名：每次发布先孤儿化到该分支再强推 refs/heads/<state.branch>。
_ORPHAN_BRANCH = "subscribe-publish"


class PublishOutcome(Enum):
    PUSHED = "pushed"
    NO_CHANGE = "no-change"


@dataclass
class PublishResult:
    outcome: PublishOutcome
    url: str
    content_sha256: str


def _git(work: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(work), *_GIT_COMMON, *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _must_git(work: Path, *args: str) -> str:
    proc = _git(work, *args)
    if proc.returncode != 0:
        raise SubscribePublishError(
            f"git {' '.join(args)} 失败：{proc.stderr.strip() or proc.stdout.strip()}",
            hint="远端仍是上一版，本地状态未被污染。修复原因后重新运行 subscribe push。",
        )
    return (proc.stdout or "").strip()


def _current_branch(work: Path) -> str | None:
    """HEAD 指向的本地分支名；分离 HEAD 时返回 None。"""
    proc = _git(work, "symbolic-ref", "--quiet", "--short", "HEAD")
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _ensure_work_repo(work: Path, repo_url: str) -> None:
    """工作目录必须是 git 仓库且 origin 与 state.repo_url 一致（URL 是唯一真相）。"""
    work.mkdir(parents=True, exist_ok=True)
    if not (work / ".git").is_dir():
        # init 不能用 -C（目录可能尚不存在于 git 视角），一次带路径的形式完成。
        init = subprocess.run(
            ["git", *_GIT_COMMON, "init", "-q", str(work)],
            capture_output=True,
            text=True,
            check=False,
        )
        if init.returncode != 0:
            raise SubscribePublishError(
                f"git init 失败：{(init.stderr or init.stdout).strip()}",
                hint="确认 git 在 PATH 中。",
            )
        _must_git(work, "remote", "add", "origin", repo_url)
    elif _git(work, "remote", "set-url", "origin", repo_url).returncode != 0:
        # 缓存目录里 origin 缺失（手工动过 work dir）：补注册，仍失败则照常抛错。
        _must_git(work, "remote", "add", "origin", repo_url)


def _enter_orphan(work: Path) -> None:
    """切到无父孤儿分支，索引与工作树只留本次唯一文件（spec §5.1 步骤 4）。

    ``checkout --orphan`` 对已存在的同名分支直接报错（第二次发布必踩），且
    当前分支不可删除——若 HEAD 正停在残留的孤儿分支上，先 detach 再清理。
    """
    if _current_branch(work) == _ORPHAN_BRANCH:
        _must_git(work, "checkout", "-q", "--detach")
    _git(work, "branch", "-D", _ORPHAN_BRANCH)  # 首次发布时通常不存在，忽略失败
    _must_git(work, "checkout", "-q", "--orphan", _ORPHAN_BRANCH)


def publish(cfg: Settings, state: SubscriptionState, ics_text: str) -> PublishResult:
    """把 .ics 文本以孤儿单提交强推到 state.branch。

    跳过条件（spec §5.1 步骤 3）：内容哈希与 token 双相同才 NO_CHANGE。
    成功才落盘 last_push；任何失败（护栏/探测/推送）远端与状态保持原样。
    """
    data = ics_text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if (
        state.last_push is not None
        and state.last_push.content_sha256 == digest
        and state.last_push.token == state.token
    ):
        return PublishResult(PublishOutcome.NO_CHANGE, state.subscription_url, digest)

    work = work_dir(cfg, state.semester)
    _ensure_work_repo(work, state.repo_url)

    # ls-remote --exit-code：0=分支存在，2=无匹配 ref（视为不存在），其余=远端不可达。
    probe = _git(work, "ls-remote", "--exit-code", "origin", f"refs/heads/{state.branch}")
    if probe.returncode == 0:
        _must_git(
            work,
            "fetch",
            "-q",
            "origin",
            f"+refs/heads/{state.branch}:refs/remotes/origin/{state.branch}",
        )
        _must_git(work, "checkout", "-q", "-B", state.branch, f"origin/{state.branch}")
        listing = _must_git(work, "ls-tree", "-r", "--name-only", "HEAD").split()
        # -r 递归列出：单条 `dir/x.ics` 也能过 len==1/endswith(".ics")，随后
        # add -A 会把嵌套文件带上新 tip，强推成两文件。故额外拒绝任何含 "/" 的路径，
        # 护栏要求的是「根目录下唯一一个 *.ics」。
        if len(listing) != 1 or "/" in listing[0] or not listing[0].endswith(".ics"):
            raise SubscribeGuardError(
                f"远端分支 {state.branch} 的 tip 含 {len(listing)} 个文件，不是本工具的发布产物，"
                "拒绝强推覆盖。",
                hint="换一个专用分支（subscribe init --branch cal-<你的名字>），"
                "或先自行清空该分支。",
            )
    elif probe.returncode != 2:
        raise SubscribePublishError(
            f"无法探测远端分支：{(probe.stderr or probe.stdout).strip()}",
            hint="远端仍是上一版。检查 repo_url/网络/凭据后重试。",
        )

    _enter_orphan(work)
    for stale in work.glob("*.ics"):
        stale.unlink()
    (work / f"{state.token}.ics").write_bytes(data)
    # 不带 pathspec 的 add -A：孤儿分支的索引仍带着旧 token 文件，
    # 必须让删除也入索引，否则 tip 会出现两个 .ics（rotate 语义失效）。
    _must_git(work, "add", "-A")
    now = datetime.now(UTC)
    _must_git(work, "commit", "-q", "-m", f"publish {state.semester} {now:%Y-%m-%dT%H:%MZ}")
    _must_git(work, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{state.branch}")

    state.last_push = PushRecord(
        pushed_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        content_sha256=digest,
        token=state.token,
    )
    save_state(cfg, state)
    return PublishResult(PublishOutcome.PUSHED, state.subscription_url, digest)


# --------------------------------------------------------------------------- #
# 轮换 / 新鲜度 / 自检
# --------------------------------------------------------------------------- #


def rotate_token(cfg: Settings, state: SubscriptionState) -> SubscriptionState:
    """换新 token 并落盘；last_push 保留（下次 publish 因 token 不同必推）。"""
    state.token = new_token()
    save_state(cfg, state)
    return state


def snapshot_age_days(cfg: Settings, semester: str) -> float | None:
    """raw 课表快照距今的天数；快照不存在返回 ``None``。"""
    path = cfg.raw_timetable_path(semester)
    if not path.is_file():
        return None
    import time

    return (time.time() - path.stat().st_mtime) / 86400.0


def has_unpublished_changes(cfg: Settings, state: SubscriptionState, ics_text: str) -> bool:
    """渲染结果与上次成功推送的内容是否不同（从未推送视为不同）。

    .. warning::
        本函数**只比内容 sha**；``publish`` 的跳过条件还要求 ``last_push.token``
        与当前 token 一致——rotate 之后数据未变时这里可能返回 ``False``，
        但 publish 仍会重推（旧 URL 已死，必须让远端挂上新文件名）。

    ``cfg`` 目前未使用，留着是为了与同族只读辅助一致的调用签名（CLI 无需区分）。
    """
    if state.last_push is None:
        return True
    return hashlib.sha256(ics_text.encode("utf-8")).hexdigest() != state.last_push.content_sha256


def verify_url(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """匿名 GET 自检（status --verify 用）。只接受 http/https，同 notice --url 白名单口径。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议：{parsed.scheme}（仅 http/https）"
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "xjtu-calendar-subscribe-check"}
    )
    try:
        # scheme 已在上面白名单里收敛到 http/https，不存在 file:// 之类的本地读取。
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            head = resp.read(64).decode("utf-8", "replace")
            ok = resp.status == 200 and head.lstrip().startswith("BEGIN:VCALENDAR")
            return ok, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"请求失败：{exc}"
