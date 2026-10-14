"""URL 订阅发布：状态文件 + git 孤儿提交发布（设计见 docs/design/2026-10-07-url-subscribe-ics.md）。

状态层负责 token 生成、Pages URL 推导、状态文件的私有原子落盘与往返；
发布层（:func:`publish`）把渲染好的 .ics 文本以**无父孤儿单提交**强推到
状态文件记录的分支，护栏拒绝覆盖非本工具产物（spec §5.1）。
此外是四个供 CLI 组合的小工具：轮换 token、量快照新鲜度、判断有无未发布
变更，以及对订阅 URL 做匿名 GET 自检。
"""

from __future__ import annotations

import base64
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
from .logging_setup import get_logger

__all__ = [
    "JOURNAL_ABSENT",
    "JOURNAL_INVALID",
    "JOURNAL_VALID",
    "LEDGER_KEEP",
    "REMOTE_UNCERTAIN_ATTR",
    "TOKEN_BYTES",
    "CompatibilityCheck",
    "JournalRead",
    "PublishJournal",
    "PublishOutcome",
    "PublishResult",
    "PushRecord",
    "SubscriptionState",
    "check_local_compatibility",
    "derive_url_base",
    "discard_pending",
    "fetch_published_bytes",
    "has_unpublished_changes",
    "journal_path",
    "load_state",
    "needs_uid_migration",
    "new_token",
    "pending_path",
    "promote_pending",
    "publish",
    "read_journal",
    "rotate_token",
    "save_state",
    "snapshot_age_days",
    "state_path",
    "subscribe_dir",
    "verify_url",
    "work_dir",
    "write_journal",
]

logger = get_logger()

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
    #: 课程 UID 的配方版本（`exporter.UID_RECIPE_VERSION`）。
    #: **None = 这个 URL 上最后一次发布出自 v1** ⇒ 下次 push 走一次性迁移（CD7）。
    #: 旧版本工具读不出也写不回这个字段（`from_dict` 忽略未知键、`to_dict` 走 `asdict`），
    #: 降级再升回来会重走一次迁移分支，而那时留底已是 v2 产物 ⇒ 候选空集、无副作用（CD17）。
    uid_recipe: int | None = None

    @property
    def subscription_url(self) -> str:
        return self.url_base.rstrip("/") + f"/{self.token}.ics"

    @property
    def webcal_url(self) -> str | None:
        """iOS/macOS 的订阅链接：把 https 换成 webcal scheme，其余一律不给。

        `webcal://` 是 URL scheme 而非文件格式，所以不需要在发布分支上多放一个 `.webcal`
        文件（那会撞 tree 护栏与 rotate 清理，见 followups §3）。非 https 的 url_base
        （自建 Pages、http、`file://` 彩排远端）返回 ``None``——宁可不提示，也不给一个
        看起来能点其实无效的链接。
        """
        url = self.subscription_url
        if url.startswith("https://"):
            return "webcal://" + url[len("https://") :]
        return None

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
            # 缺失 = v0.7.x 写的文件（CD7）；坏值（手改/半写）按缺失处理，绝不因为
            # 一个读不懂的整数拒绝加载状态——迁移分支的最坏后果只是「多算一次空集」。
            uid_recipe=_parse_uid_recipe(data.get("uid_recipe")),
        )


def _parse_uid_recipe(value: object) -> int | None:
    """状态文件里的配方版本：只接受整数，其它形状（缺失/null/字符串）一律按「未知」。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def needs_uid_migration(state: SubscriptionState) -> bool:
    """迁移判定的唯一归属地：看字段，不猜 UID（UID 是 hash，反推不了配方，CD7）。"""
    return state.uid_recipe is None


def subscribe_dir(cfg: Settings) -> Path:
    """订阅目录（字面量已收敛到 :attr:`Settings.subscribe_dir`）。"""
    return cfg.subscribe_dir


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

#: `publish` 失败时挂在异常上的标记：远端**可能**已经推进（push 已开始执行）。
#: 调用方只许在没有这个标记时才丢弃发布准备稿（2026-10-14 外部审阅 P1-①）。
REMOTE_UNCERTAIN_ATTR = "publish_remote_uncertain"


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


def _git_bytes(work: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """`_git` 的二进制版：**不许** `text=True`。

    读线上 .ics 字节时通用换行翻译会把 CRLF 折成 LF，sha 立刻对不上
    （与 `doctor`/`status` 里那条"留底必须按 bytes 读"同一个坑）。
    """
    return subprocess.run(
        ["git", "-C", str(work), *_GIT_COMMON, *args],
        capture_output=True,
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
    """推送远端，并在失败时把"远端到底动没动"标在异常上。

    .. warning:: 抛异常**不等于**远端没收下这一版（2026-10-14 外部审阅 P1-①）：
       `git push` 之后还有 `save_state`，而 `git push` 自己返回非零也可能是远端已更新、
       响应却没回到客户端。所以失败分两档：带 `REMOTE_UNCERTAIN_ATTR` 标记的**不能**当成
       "远端没动"，调用方只许在没有该标记时丢弃发布准备稿。

    签名保持三参不变：`publish` 有多处测试替身按位置接三个参数，加参数会把它们全部打挂；
    标记挂在异常上比改公共签名便宜，也让 rotate 等既有调用零改动。
    """
    push_started = [False]
    try:
        return _publish_inner(cfg, state, ics_text, push_started)
    except Exception as exc:
        if push_started[0]:
            # 挂在异常上而不是改签名：这里要标的是**任意**异常类型（护栏错、git 错、
            # save_state 的 OSError），直接属性赋值过不了 mypy。
            setattr(exc, REMOTE_UNCERTAIN_ATTR, True)
        raise


def _publish_inner(
    cfg: Settings, state: SubscriptionState, ics_text: str, push_started: list[bool]
) -> PublishResult:
    """把 .ics 文本以孤儿单提交强推到 state.branch。

    跳过条件（spec §5.1 步骤 3）：内容哈希与 token 双相同才 NO_CHANGE。
    成功才落盘 last_push；推送**之前**的任何失败（护栏/探测）远端与状态保持原样。

    .. warning:: 本函数只保证"推送前失败不留痕"。**推送成功之后**还有两句不在这里：
       这里的 ``save_state`` 与调用方（CLI）随后写本地留底/台账。死在两句中间会留下
       "远端与状态已推进、留底还是旧的"这一状态——恢复入口是 `subscribe repair`
       （它拿远端真实字节裁决），**不是**在这里补写本地文件。也不许把本函数改成
       "先写本地再推送"，那样推送失败反而会留下一个比远端更新的基线。
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
    push_started[0] = True
    _must_git(work, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{state.branch}")

    state.last_push = PushRecord(
        pushed_at=now.isoformat(timespec="seconds").replace("+00:00", "Z"),
        content_sha256=digest,
        token=state.token,
    )
    save_state(cfg, state)
    return PublishResult(PublishOutcome.PUSHED, state.subscription_url, digest)


# --------------------------------------------------------------------------- #
# 发布恢复快照（write-ahead journal）—— P1-02 补验 A+ 方案
# --------------------------------------------------------------------------- #
#
# 为什么需要它：`subscribe push` 要同时推进四样东西（远端 ICS、本地留底、两份台账、
# 迁移标记），而它们**不在一个原子域里**。审查者补验的两条场景都是这么炸的：
# ①留底落后而数据又变了 ⇒ 构建直接拿过期留底当 SEQUENCE 基线；②留底是新的、
# 课程台账写失败 ⇒ 下次撤销候选（「台账 − live」）里根本没有那条 UID，静默漏发。
#
# 为什么不能"从留底反推台账"：产物里课程事件与考试事件**长得一样**——没有
# `CATEGORIES`、没有 `X-` 标记、UID 同域同形状（CD8 当初正因如此才把台账拆成两份）。
# 所以要在不改 ICS 格式的前提下完整恢复，只能在**推送之前**把这次要落的东西记下来。
#
# 三条不变量：
# 1. journal 永远等于"最后一次真正发出去的版本"——**写完不删**（否则远端已推进、
#    本地未提交的窗口又失去依据），下一次 push 在 publish 之前把它覆盖掉。
# 2. 落在发布工作目录**之外**（`work-<学期>/` 里 `git add -A` 会吞掉任何文件）。
# 3. 台账的 `keep` 语义必须显式记录：`--no-exams` 那次**不该**动考试台账，
#    记成"写空文件"等于把整份考试账本清掉。

#: 台账在快照里的"这次别动"哨兵。
LEDGER_KEEP = "keep"

#: 已发布状态凭据（committed）：只有确认远端收下某一版之后才推进到它。
JOURNAL_FILENAME = "publish-journal-{semester}.json"
#: 本次发布的准备稿（pending）：推送**之前**写，成功之后提升为 committed。
PENDING_FILENAME = "publish-pending-{semester}.json"

#: `read_journal` 的三态：从没写过 / 写得懂 / 写懂了但读不懂（损坏）。
JOURNAL_ABSENT = "absent"
JOURNAL_VALID = "valid"
JOURNAL_INVALID = "invalid"


@dataclass
class PublishJournal:
    """一次发布的完整本地目标状态（ICS + 两份台账 + 迁移标记）。

    含课程名/教师/教室/座位（台账本身就有），**永不发布**、只留本地、`private=True`
    ——与两份台账同一隐私等级（spec §8）。日志里只许出现 sha，不许出现内容。

    .. note:: 2026-10-14 外部审阅把"已发布凭据"与"待发准备稿"分成两个逻辑版本
       （物理上是两个文件）。二者共用这个结构：同一份内容写在不同路径上，语义由路径决定。
    """

    semester: str
    token: str
    branch: str
    ics_sha256: str
    ics_text: str
    #: ``None`` = 这次不该动该台账（`--no-exams` / 门槛没过 ⇒ 不产出文本）。
    course_ledger_b64: str | None = None
    exam_ledger_b64: str | None = None
    #: keep 语义的另一半：那次发布**不该动的这份台账**当时的字节指纹。
    #: 相容性检查据此发现"保留下来的台账事后被删/被改"——不检查它 ≠ 它可以随便变。
    course_ledger_sha256: str | None = None
    exam_ledger_sha256: str | None = None
    uid_recipe: int | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> PublishJournal:
        return cls(
            semester=str(data["semester"]),
            token=str(data["token"]),
            branch=str(data["branch"]),
            ics_sha256=str(data["ics_sha256"]),
            ics_text=str(data["ics_text"]),
            course_ledger_b64=(
                None if data.get("course_ledger_b64") is None else str(data["course_ledger_b64"])
            ),
            exam_ledger_b64=(
                None if data.get("exam_ledger_b64") is None else str(data["exam_ledger_b64"])
            ),
            course_ledger_sha256=(
                None
                if data.get("course_ledger_sha256") is None
                else str(data["course_ledger_sha256"])
            ),
            exam_ledger_sha256=(
                None if data.get("exam_ledger_sha256") is None else str(data["exam_ledger_sha256"])
            ),
            uid_recipe=None if data.get("uid_recipe") is None else int(str(data["uid_recipe"])),
            created_at=str(data.get("created_at") or ""),
        )

    @property
    def course_ledger_text(self) -> str | None:
        """``None`` = keep（这次不该动）；有值 = 应当原样落盘的目标字节。"""
        return None if self.course_ledger_b64 is None else _b64_decode(self.course_ledger_b64)

    @property
    def exam_ledger_text(self) -> str | None:
        return None if self.exam_ledger_b64 is None else _b64_decode(self.exam_ledger_b64)


@dataclass(frozen=True)
class JournalRead:
    """读一份快照的结果，**刻意区分**"没写过"与"读不懂"。

    审阅者 P1-②：旧版本从没写过快照 ⇒ 可以退回弱判据（明确告警）；
    新版本写过、后来损坏 ⇒ 那是"本该信任的凭据失效"，用更宽松的判据继续发布等于
    让一份损坏的安全校验文件触发更宽松的校验。折叠成同一个 ``None`` 就丢掉了这个区别。
    """

    status: str
    journal: PublishJournal | None = None
    detail: str = ""

    @property
    def present(self) -> bool:
        return self.status != JOURNAL_ABSENT


def _b64_decode(raw: str) -> str:
    return base64.b64decode(raw).decode("utf-8")


def _b64_encode(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def journal_path(cfg: Settings, semester: str) -> Path:
    """已发布状态凭据（committed）的路径。"""
    return subscribe_dir(cfg) / JOURNAL_FILENAME.format(semester=semester)


def pending_path(cfg: Settings, semester: str) -> Path:
    """本次发布准备稿（pending）的路径。"""
    return subscribe_dir(cfg) / PENDING_FILENAME.format(semester=semester)


def read_journal(cfg: Settings, semester: str, *, pending: bool = False) -> JournalRead:
    """读一份快照，**区分**"没写过"与"读不懂"（审阅 P1-②）。

    只报异常种类，不报 `str(exc)`：快照里是台账级个人信息，解码失败的原文可能跟着进日志。
    """
    path = pending_path(cfg, semester) if pending else journal_path(cfg, semester)
    if not path.is_file():
        return JournalRead(JOURNAL_ABSENT)
    try:
        # 必须显式 utf-8：快照里的 ICS 原文带课程名，zh-Windows 上默认编码是 cp936，
        # 用 `read_text()` 会把"读得懂"变成 UnicodeDecodeError ⇒ 误判成损坏。
        raw = path.read_text(encoding="utf-8")
        return JournalRead(JOURNAL_VALID, PublishJournal.from_dict(json.loads(raw)))
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
        return JournalRead(JOURNAL_INVALID, detail=type(exc).__name__)


def write_journal(
    cfg: Settings,
    *,
    semester: str,
    token: str,
    branch: str,
    ics_text: str,
    course_ledger_text: str | None,
    exam_ledger_text: str | None,
    uid_recipe: int | None,
) -> PublishJournal:
    """在**推送远端之前**把这次的目标状态落成「准备稿」（pending）。

    写不进去就不该开始推送：宁可远端停在上一版，也不要发一次事后无从恢复的发布。
    `keep`（文本为 ``None``）那份台账在这里记下**当时的字节指纹**，于是"这次不动它"
    仍然是一个日后可核验的事实，而不是一句免检承诺（审阅 P1-③）。
    """
    journal = PublishJournal(
        semester=semester,
        token=token,
        branch=branch,
        ics_sha256=hashlib.sha256(ics_text.encode("utf-8")).hexdigest(),
        ics_text=ics_text,
        course_ledger_b64=(None if course_ledger_text is None else _b64_encode(course_ledger_text)),
        exam_ledger_b64=None if exam_ledger_text is None else _b64_encode(exam_ledger_text),
        course_ledger_sha256=(
            None if course_ledger_text is not None else _sha_of(cfg.course_ledger_path(semester))
        ),
        exam_ledger_sha256=(
            None if exam_ledger_text is not None else _sha_of(cfg.exam_ledger_path(semester))
        ),
        uid_recipe=uid_recipe,
        created_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    )
    _dump_journal(pending_path(cfg, semester), journal)
    return journal


def _dump_journal(path: Path, journal: PublishJournal) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path, json.dumps(journal.to_dict(), ensure_ascii=False, indent=2) + "\n", private=True
    )


def promote_pending(cfg: Settings, semester: str) -> PublishJournal | None:
    """确认远端收下之后，把准备稿提升为「已发布凭据」（committed）。

    顺序要紧：**先推进凭据，再写本地留底/台账**。反过来（先本地后凭据）一旦死在中间，
    留底会比凭据新 ⇒ `repair` 拿远端一比对就发现凭据落后、拒绝恢复，只剩人工出口。
    提升失败（磁盘满等）不推翻发布：远端已经是新版，而准备稿还在，下一次
    `subscribe repair` 能按远端字节把它认出来并补做这一步。
    """
    read = read_journal(cfg, semester, pending=True)
    if read.status != JOURNAL_VALID or read.journal is None:
        return None
    journal = read.journal
    _dump_journal(journal_path(cfg, semester), journal)
    pending_path(cfg, semester).unlink(missing_ok=True)
    return journal


def discard_pending(cfg: Settings, semester: str) -> None:
    """丢掉一份**已确认远端没收下**的准备稿。

    只允许在"还没执行到 push"这个前提下调用；push 之后失败留着它才有意义
    （到底是哪一版由 `subscribe repair` 拿远端字节裁决）。
    """
    pending_path(cfg, semester).unlink(missing_ok=True)


@dataclass(frozen=True)
class CompatibilityCheck:
    """构建前的相容性判定结果。"""

    ok: bool
    #: `consistent` / `no_snapshot_verified`（弱判据放行）/ `no_journal` / `journal_token`
    #: / `baseline` / `course_ledger` / `exam_ledger` / `uid_recipe`
    reason: str
    detail: str


def _sha_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_local_compatibility(
    cfg: Settings, state: SubscriptionState, semester: str
) -> CompatibilityCheck:
    """本地四样（留底 / 两份台账 / 迁移标记）是否与"最后一次已发布版本"相容。

    **不碰网络**：它回答的是"本地自不自洽、有没有依据"，远端到底是不是这一版由
    `subscribe repair` 去核。`last_push is None` 直接通过（全新订阅，没有旧版本可对）。
    """
    if state.last_push is None:
        return CompatibilityCheck(True, "consistent", "尚未发布过，没有需要相容的旧版本")
    baseline = subscribe_dir(cfg) / f"last-{semester}.ics"
    read = read_journal(cfg, semester)
    if read.status == JOURNAL_INVALID:
        # 与"从没写过"分开（审阅 P1-②）：这份凭据本来是在的、现在读不懂 ⇒ 不能退回更宽松的
        # 弱判据继续发布。人工出口仍然给（旗标），但默认停手。
        return CompatibilityCheck(
            False,
            "journal_invalid",
            f"本地发布恢复快照读不懂（{read.detail}），不能按没有依据降级放行",
        )
    journal = read.journal
    if journal is not None and journal.token != state.last_push.token:
        return CompatibilityCheck(
            False,
            "journal_token",
            f"恢复快照属于另一个 token（{journal.token[:8]}…），与当前订阅记录不符，不能拿它当依据",
        )
    if journal is None:
        # 没有依据可查（0.8.5 之前发布过的状态、或凭据被删）。退回一条**不依赖快照**的
        # 弱判据：`last_push` 只在 push 成功后写，所以留底字节 sha 与它相符就说明留底
        # 正是线上那一版。不放行的话，每个老用户升级后第一次 push 都会被自己的保险丝
        # 拦住（与 v0.8 那次"门 2 误拦老用户几周"同型，2026-10-14 拿真实 home 实测过）。
        # 弱判据**证明不了**两份台账与迁移标记，只能交给调用方警告；本次推送前会重新
        # 落快照，依据就此补回。
        if _sha_of(baseline) == state.last_push.content_sha256:
            return CompatibilityCheck(
                True,
                "no_snapshot_verified",
                "本地没有发布恢复快照，但留底字节与最后一次发布记录逐字节相符；"
                "两份台账与迁移标记未能校验",
            )
        return CompatibilityCheck(
            False,
            "no_journal",
            f"已有一次发布记录，但本地既没有恢复快照，留底也与最后一次发布的字节不一致"
            f"（{baseline.name}）",
        )
    if _sha_of(baseline) != journal.ics_sha256:
        return CompatibilityCheck(
            False,
            "baseline",
            f"本地留底与最后一次已发布的 ICS 不一致（{baseline.name}）",
        )
    for label, human, path_key, want, keep_sha in (
        (
            "course_ledger",
            "课程台账",
            cfg.course_ledger_path(semester),
            journal.course_ledger_text,
            journal.course_ledger_sha256,
        ),
        (
            "exam_ledger",
            "考试台账",
            cfg.exam_ledger_path(semester),
            journal.exam_ledger_text,
            journal.exam_ledger_sha256,
        ),
    ):
        if want is None:
            # keep 语义：那次发布没产出这份台账 ⇒ **不动它**，但它仍是撤销判定的输入。
            # "不动"记成一个指纹，事后被删/被改就会被发现（审阅 P1-③）；不记指纹的话，
            # 这一格与场景 2 是同一类静默失效。
            if keep_sha is not None and _sha_of(path_key) != keep_sha:
                return CompatibilityCheck(
                    False,
                    f"{label}_keep",
                    f"{human} {path_key.name} 被保留过，却与那次发布时的字节不符（被删除或改动）",
                )
            continue
        if _sha_of(path_key) != hashlib.sha256(want.encode("utf-8")).hexdigest():
            return CompatibilityCheck(
                False,
                label,
                f"{human} {path_key.name} 与最后一次发布时的内容不一致",
            )
    if journal.uid_recipe is not None and state.uid_recipe != journal.uid_recipe:
        return CompatibilityCheck(
            False,
            "uid_recipe",
            f"状态文件里的 UID 配方标记与已发布版本不一致（应为 {journal.uid_recipe}）",
        )
    return CompatibilityCheck(True, "consistent", "留底、两份台账与迁移标记均与已发布版本相容")


def fetch_published_bytes(cfg: Settings, state: SubscriptionState, semester: str) -> bytes:
    """从发布分支拉回**当前线上**那份 .ics 的字节（离线、零凭据外泄）。

    刻意走 work 仓库的 `git fetch` + `git show` 而不是 HTTP：`url_base` 可能是
    `file://`（彩排远端），也可能 CDN 还在吐旧文件；git 侧拿到的就是 tip 的真实字节。
    """
    work = work_dir(cfg, semester)
    _ensure_work_repo(work, state.repo_url)
    _must_git(
        work,
        "fetch",
        "-q",
        "origin",
        f"+refs/heads/{state.branch}:refs/remotes/origin/{state.branch}",
    )
    proc = _git_bytes(work, "show", f"refs/remotes/origin/{state.branch}:{state.token}.ics")
    if proc.returncode != 0:
        raise SubscribePublishError(
            f"无法从远端分支 {state.branch} 读取 {state.token}.ics",
            hint="确认订阅仓库与分支无误后重试；远端不可达时 repair 也无能为力。",
        )
    return bytes(proc.stdout or b"")


# --------------------------------------------------------------------------- #
# 轮换 / 新鲜度 / 自检
# --------------------------------------------------------------------------- #


def rotate_token(cfg: Settings, state: SubscriptionState) -> SubscriptionState:
    """换新 token 并落盘；last_push 保留（下次 publish 因 token 不同必推）。"""
    state.token = new_token()
    save_state(cfg, state)
    return state


def snapshot_age_days(cfg: Settings, semester: str, *, kind: str = "timetable") -> float | None:
    """raw 快照距今的天数；快照不存在返回 ``None``。

    ``kind`` 与 :func:`xjtu_calendar.fetcher.save_raw` 同一口径（``"timetable"`` /
    ``"exams"``）。默认值刻意保持不变——`cli.py` 现有的 publish/status 两处调用点
    不传 kind，行为必须与改造前逐字一致（§7:382-386 选定陈旧口径时需要读考试侧快照，
    这是 Task 11 传 ``kind="exams"`` 的入口）。未知 kind 直接 ``ValueError``，与
    :func:`xjtu_calendar.fetcher._raw_paths` 同口径：拼错要当场炸，不许静默按课表读。
    """
    if kind == "exams":
        path = cfg.raw_exams_path(semester)
    elif kind == "timetable":
        path = cfg.raw_timetable_path(semester)
    else:
        raise ValueError(f"未知的快照类型：{kind}")
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
