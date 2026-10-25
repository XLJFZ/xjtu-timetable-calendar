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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from .cancellation import load_ledger
from .config import Settings
from .errors import (
    SubscribeGuardError,
    SubscribeNotConfigured,  # noqa: F401  (Task 4/5 的 CLI 层使用)
    SubscribePublishError,
)
from .fileutil import atomic_write_text
from .logging_setup import get_logger
from .timeutil import now_local

__all__ = [
    "BYPASSABLE_INCOMPATIBILITY_REASONS",
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
    "fetch_published_file",
    "has_unpublished_changes",
    "has_unresolved_pending",
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
    #: 误删恢复的"重新编号计数器"：``stable_course_key -> 第几次重新编号``。
    #: 空 dict（= 从没用过 `--revive`）时 UID 配方与 0.9.3 逐字节相同 —— 这个不变量由
    #: `test_revive_salt_is_absent_by_default_and_rekeys_when_given` 与
    #: `test_push_without_revive_is_byte_identical` 一起钉住。
    #: 机制与代价见 docs/design/2026-10-11-subscription-followups.md §16。
    revive_salts: dict[str, int] = field(default_factory=dict)

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
        data = asdict(self)
        if not self.revive_salts:
            # 从没用过 `--revive` 的人，状态文件与 0.9.3 逐字节相同（键整个省略，而不是写个空表）。
            # 产物字节的不变量另有 golden 用例钉；这条管的是"别给没这功能的人凭空多一行"。
            del data["revive_salts"]
        return data

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
            revive_salts=_parse_revive_salts(data.get("revive_salts")),
        )


def _parse_revive_salts(value: object) -> dict[str, int]:
    """状态文件里的重新编号计数器：只收「字符串键 → 正整数」，其余一律丢掉。

    与 `_parse_uid_recipe` 同一个口径：**读不懂的字段按缺失处理，绝不因为一个手改坏的
    JSON 拒绝加载状态**。丢掉脏键而不是崩，代价只是那一门课不重新编号 —— 比"整个订阅
    状态加载失败、用户以为订阅坏了"轻得多。非正数（0 / 负 / 布尔）同样丢：盐值 0 必须与
    "缺席"同义，否则旧状态里一个 0 就会悄悄改掉 UID。
    """
    if not isinstance(value, dict):
        return {}
    salts: dict[str, int] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.strip():
            continue
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            continue
        salts[key] = raw
    return salts


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


def _stage_only(work: Path, filename: str) -> None:
    """索引里**只**放这一个文件，并把索引核对一遍（P2-03，隐私护栏）。

    旧实现是不带 pathspec 的 `git add -A`：`work-<学期>/` 里任何残留（用户手工放进去的
    笔记、上次中断留下的临时文件、别的工具写的日志）都会被一并提交、强推进那个
    **公开**分支。订阅分支的读者是"知道 URL 的人"，多发布一个文件就是本地文件外泄。

    做法：先把继承自上一分支的索引清空，再只加这一个文件。rotate 需要的"旧 token
    文件从 tip 消失"照样成立，而且比原来更彻底——旧注释里"删除也必须入索引"的麻烦，
    根源就是那份继承来的索引，清空它等于一次性解决。
    """
    # 空索引时 `git rm --cached` 会以「pathspec 不匹配」报错 ⇒ 用不抛错的 _git
    _git(work, "rm", "-r", "--cached", "-q", "--", ".")
    _must_git(work, "add", "--", filename)
    staged = _must_git(work, "ls-files", "--cached").split()
    if staged != [filename]:
        extra = "、".join(sorted(set(staged) - {filename})) or "（索引里连该发的文件都没有）"
        raise SubscribeGuardError(
            f"发布索引除了 {filename} 还出现了：{extra}。拒绝把多余的内容推上公开分支。",
            hint=f"检查发布工作目录 {work}：除了 <token>.ics 不该有别的成员，删掉多余文件后重试。",
        )


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
    _stage_only(work, f"{state.token}.ics")
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
# 五条不变量（2026-10-14 审阅复核后由三条扩成五条）：
# 1. **两份凭据各司其职**：`publish-pending-*` 是"这次准备发"，`publish-journal-*` 是
#    "远端确认收下过哪一版"。同一份内容既当快照又当准备记录，在远端结果不确定时会分家。
# 2. 已发布凭据**只能由准备稿提升而来**，且提升发生在写本地文件**之前**——反过来一旦
#    死在中间，留底会比凭据新，`repair` 拿远端一比对就发现凭据落后、拒绝恢复。
# 3. 抛异常**不等于**远端没推进（`save_state` 排在 `git push` 之后，push 返回非零也可能
#    远端已更新）。只有没被标 `REMOTE_UNCERTAIN_ATTR` 的失败才允许丢弃准备稿。
# 4. 落在发布工作目录**之外**（`work-<学期>/` 里的任何文件都可能被发布出去：
#    2026-10-15 起 staging 改成 `_stage_only`，索引里只放那一个 `<token>.ics`，
#    但"别往那儿放东西"仍然是对的——公开分支多一个文件就是本地文件外泄）。
# 5. 台账的 `keep` 语义要**同时**记两件事：这次别动 + 它当时的字节指纹。
#    记成"写空文件"等于把整份考试账本清掉；只记"别动"则那份台账事后被删/被改无人发现。
#    指纹为 `None` 记录的是"当时这份文件不存在"，于是"事后凭空冒出来一份"同样会被发现
#    （2026-10-15 审阅 P1-04）——keep 核对的是内容**与存在状态**两件事。

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
    #: ``None`` 也是有意义的读数：那份文件**当时不存在**（P1-04），于是后来出现的一份
    #: 同样算状态改变，而不是"没有指纹 ⇒ 免检"。
    course_ledger_sha256: str | None = None
    exam_ledger_sha256: str | None = None
    uid_recipe: int | None = None
    #: 这次构建实际用的重新编号表（误删恢复，§16）。``None`` = 0.10.1 及更早写下的
    #: 旧快照，字段缺席 ⇒ **不知道**那次发布用了什么表，`repair` 不许据此覆盖状态里的现表；
    #: 空 dict 才是"确定没用过 `--revive`"。
    revive_salts: dict[str, int] | None = None
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
            revive_salts=(
                None
                if data.get("revive_salts") is None
                else _parse_revive_salts(data.get("revive_salts"))
            ),
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


def _b64_decode_strict(raw: str) -> str | None:
    """严格解码：非法字符、填充错误、非 UTF-8 一律返回 ``None``。

    `b64decode` 默认**丢弃**非法字符（`b64decode("!!!")` → `b""`，2026-10-15 实测），
    所以"能解"和"解出来的是当初写进去的东西"是两件事——见 :func:`_journal_problem`。
    """
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except ValueError:
        return None


_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _journal_problem(journal: PublishJournal) -> str | None:
    """快照**自不自洽**的判据；返回一句不含内容的说明，``None`` 表示各项都对得上。

    为什么要在"读"这一层就判死（2026-10-15 审阅 P1-03）：恢复写盘用的是 `ics_text`
    与解码后的台账文本，相容性检查比的却是 `ics_sha256` 与台账 sha。两份读数来自不同
    版本时，`_restore_from_journal` 会先把三样文件写下去、再由收尾复核发现不对——本地
    被推进一个"既不等于线上、也不等于任何一份凭据"的第四态，比原地不动糟得多。
    台账那一项还有更凶的一层：坏掉的 Base64 会"成功地解码成空台账"，照写等于把整份
    撤销历史静默清零（用户红线：不得静默重置订阅历史）。

    说明文本只含字段名与判据，绝不带正文——快照里是台账级个人信息（spec §8）。
    """
    for label, value in (
        ("semester", journal.semester),
        ("token", journal.token),
        ("branch", journal.branch),
        ("ics_text", journal.ics_text),
    ):
        if not value:
            return f"快照字段 {label} 为空"
    for label, digest in (
        ("ics_sha256", journal.ics_sha256),
        ("course_ledger_sha256", journal.course_ledger_sha256),
        ("exam_ledger_sha256", journal.exam_ledger_sha256),
    ):
        if digest is not None and not _SHA_PATTERN.match(digest):
            return f"快照字段 {label} 不是合法的 sha256 十六进制"
    if hashlib.sha256(journal.ics_text.encode("utf-8")).hexdigest() != journal.ics_sha256:
        return "快照正文与自身哈希不符（ics_text 与 ics_sha256 自相矛盾）"
    for label, raw in (
        ("课程台账", journal.course_ledger_b64),
        ("考试台账", journal.exam_ledger_b64),
    ):
        if raw is not None and _b64_decode_strict(raw) is None:
            return f"快照里的{label}不是合法的 Base64 文本"
    return None


def journal_path(cfg: Settings, semester: str) -> Path:
    """已发布状态凭据（committed）的路径。"""
    return subscribe_dir(cfg) / JOURNAL_FILENAME.format(semester=semester)


def pending_path(cfg: Settings, semester: str) -> Path:
    """本次发布准备稿（pending）的路径。"""
    return subscribe_dir(cfg) / PENDING_FILENAME.format(semester=semester)


def read_journal(cfg: Settings, semester: str, *, pending: bool = False) -> JournalRead:
    """读一份快照，**区分**"没写过"与"读不懂"（审阅 P1-②），并校验其自洽性（P1-03）。

    只报异常种类或判据名，不报 `str(exc)`：快照里是台账级个人信息，
    解码失败的原文可能跟着进日志。
    """
    path = pending_path(cfg, semester) if pending else journal_path(cfg, semester)
    if not path.is_file():
        return JournalRead(JOURNAL_ABSENT)
    try:
        # 必须显式 utf-8：快照里的 ICS 原文带课程名，zh-Windows 上默认编码是 cp936，
        # 用 `read_text()` 会把"读得懂"变成 UnicodeDecodeError ⇒ 误判成损坏。
        raw = path.read_text(encoding="utf-8")
        journal = PublishJournal.from_dict(json.loads(raw))
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
        return JournalRead(JOURNAL_INVALID, detail=type(exc).__name__)
    problem = _journal_problem(journal)
    if problem is not None:
        return JournalRead(JOURNAL_INVALID, detail=problem)
    return JournalRead(JOURNAL_VALID, journal)


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
    revive_salts: dict[str, int] | None = None,
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
        revive_salts=None if revive_salts is None else dict(revive_salts),
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
    #: `consistent` / `no_snapshot_verified`（弱判据放行）/ `ledger_pruned_by_time`
    #: （台账差异只落在原定时刻已过去的行上，见 `_difference_is_only_time_pruning`）
    #: / `pending_unresolved`（上次发布的结果还没裁决）/ `no_journal` / `journal_invalid`
    #: / `journal_token` / `journal_sha` / `journal_branch`（后三格＝凭据与状态里的发布记录
    #: 交叉核对不上）/ `revive_salts`（重新编号计数器与快照不一致）/ `baseline` /
    #: `course_ledger` / `exam_ledger` / `*_keep` / `uid_recipe`
    reason: str
    detail: str

    @property
    def bypassable(self) -> bool:
        """这一格能不能被 `--allow-unverified-baseline` 人工覆盖（见下面的白名单）。"""
        return self.ok or self.reason in BYPASSABLE_INCOMPATIBILITY_REASONS


#: `--allow-unverified-baseline` 允许人工覆盖的不相容原因——**显式白名单**
#: （2026-10-15 审阅 P1-02）。白名单之外的任何 reason（包括今后新增的）一律不放行。
#: 判据不是"用户想不想强行发布"，而是"强行继续会不会销毁裁决所需的证据"：
#:
#: - `pending_unresolved`：门一过，构建照跑，`write_journal` 第一件事就把那份未裁决的
#:   准备稿原地覆盖掉 ⇒ `repair` 此后再也答不出线上究竟是哪一版。能裁决它的只有远端字节。
#: - `journal_invalid`：本该信任的校验文件坏了，降级成"当作没有校验"等于让损坏触发
#:   更宽松的校验（与 P1-② 同一个理由）。
BYPASSABLE_INCOMPATIBILITY_REASONS = frozenset(
    {
        "no_journal",
        "journal_token",
        "journal_sha",
        "journal_branch",
        "baseline",
        "course_ledger",
        "exam_ledger",
        "course_ledger_keep",
        "exam_ledger_keep",
        "uid_recipe",
    }
)


def _pending_read(cfg: Settings, semester: str) -> JournalRead:
    return read_journal(cfg, semester, pending=True)


def _unresolved_pending(cfg: Settings, semester: str) -> CompatibilityCheck | None:
    """残留的**准备稿**（pending）= 上一次发布的结果本地还没裁决 ⇒ 一律不相容。

    为什么这一格必须单独判（2026-10-15 审阅裁决）：准备稿存在，说明那次推送死在
    "远端可能已经收下"之后、`save_state` 之前（或被标了 remote-uncertain 之后）。
    此时"本地四样 vs 已发布凭据"**完全可能逐样自洽**——因为比的是上一版凭据，而线上
    也许已经是准备稿那一版。自洽地错，门与体检都会瞎，所以判据不能只看 committed。
    谁来裁决：**只有** `subscribe repair` 拿远端真实字节能定（==准备稿 ⇒ 提升；
    ==已发布凭据 ⇒ 丢弃），本地无从判断，故默认停手。
    """
    read = _pending_read(cfg, semester)
    if read.status == JOURNAL_ABSENT:
        return None
    path_name = pending_path(cfg, semester).name
    if read.status == JOURNAL_INVALID:
        return CompatibilityCheck(
            False,
            "pending_unresolved",
            f"残留的发布准备稿读不懂（{path_name}，{read.detail}）："
            "上一次发布的结果既无法确认也无法放弃",
        )
    return CompatibilityCheck(
        False,
        "pending_unresolved",
        f"上一次发布留下了未裁决的准备稿（{path_name}）："
        "本地凭据仍是上一版，而线上可能已经是准备稿那一版",
    )


def has_unresolved_pending(cfg: Settings, semester: str) -> bool:
    """有没有一份**尚未裁决的准备稿**（CLI 的 rotate 用它决定是否必须先过门）。

    与 `check_local_compatibility` 的第一格同源，单独暴露是因为 rotate 有一条例外的早退
    路径：本地留底缺失时它压根不构建、不发布，只换 token——那道门保护的"拿过期留底当
    SEQUENCE 基线"在这一支不发生。但**未裁决的准备稿**不一样：换 token 会让那两份凭据
    全都属于旧 token，`repair` 此后无从裁决，所以这一格不看留底在不在。
    """
    return _unresolved_pending(cfg, semester) is not None


def _sha_of(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _difference_is_only_time_pruning(
    published_text: str, local_path: Path, *, now: datetime, label: str
) -> int | None:
    """本地台账与发布凭据里那份的差异，是否**只落在"原定时刻已过去"的行上**。

    是 ⇒ 返回被放行的差异行数；否 ⇒ `None`，交回原来的 fail-closed 判定。

    为什么需要这一格：`export` 与发布**共用同一份台账**，而台账按 D2/D10 会剪掉原定时刻
    已过去的行 ⇒ 两次发布之间只要过了几节课、中间跑过一次 `export`，本地台账就比凭据里那份
    少几行，门把这条**它自己的规则造出来的差异**当成不相容、拒绝发布（2026-10-24 彩排抓到，
    与 v0.8「门 2 误拦老用户几周」同型）。放行的代价是零：已过时刻的行本来就进不了
    撤销候选（`cancel_candidates` 的 `start < now` 那道筛），也不是任何事件的凭据。

    判据刻意写成"未过期的行必须逐字段原样在"而不是"只许少不许多"：
    ① 本地凭空多出来的行不是退场，照旧拦；
    ② 台账文件被读坏时 `load_ledger` 会降级成空表（D15 不抛异常），若只数"少了多少行"，
       一份坏文件在学期末（凭据里全是已过时刻的行）会被当成"时间剪枝"放过去 ——
       而那种时刻所有未过期行都必须原样在场，坏文件当场就露馅。
    """
    try:
        local_text = local_path.read_text(encoding="utf-8")
    except OSError:
        return None
    published = load_ledger(published_text, label=f"发布凭据里的{label}")
    local = load_ledger(local_text, label=f"本地{label}")
    if not set(local) <= set(published):
        return None
    forgiven = 0
    for uid, entry in published.items():
        kept = local.get(uid)
        if entry.start < now:
            # 已过时刻：不在、或字段变了，都不影响任何判定 ⇒ 放行并计数
            if kept is None or kept != entry:
                forgiven += 1
        elif kept != entry:
            # 还没到点的行少了一条或字段对不上 ⇒ 正是"真退课被静默漏发撤销"的形态
            return None
    return forgiven


def check_local_compatibility(
    cfg: Settings,
    state: SubscriptionState,
    semester: str,
    *,
    now: datetime | None = None,
) -> CompatibilityCheck:
    """本地四样（留底 / 两份台账 / 迁移标记）是否与"最后一次已发布版本"相容。

    **不碰网络**：它回答的是"本地自不自洽、有没有依据"，远端到底是不是这一版由
    `subscribe repair` 去核。`last_push is None` 直接通过（全新订阅，没有旧版本可对）
    ——但**未裁决的准备稿除外**，那是"远端可能已经推进过一次"的信号，与有没有旧版本无关。

    `now`：判"时间剪枝"用的墙钟，`None` 取 `now_local()`。单测靠它把"那行到底过期了没有"
    钉成确定性的，不去赌挂钟。
    """
    wall_clock = now or now_local()
    unresolved = _unresolved_pending(cfg, semester)
    if unresolved is not None:
        return unresolved
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
    # 凭据与状态里那条发布记录的**完整**交叉核对（2026-10-15 审阅 P1-05）。上面那格只看
    # token，这里补上字节与分支：三者任意一项对不上，凭据描述的就不是"当前订阅最后一次
    # 发出去的那一版"，拿它当依据等于拿别的版本的字节做 SEQUENCE 基线。
    # 这两格现有崩溃序列走不到（`journal_token` 与 `pending_unresolved` 先接住），能到的
    # 是手工改动与今后新增的形态——成本各一行，按 fail-closed 收下。
    if journal is not None and journal.ics_sha256 != state.last_push.content_sha256:
        return CompatibilityCheck(
            False,
            "journal_sha",
            f"恢复快照记录的字节哈希（{journal.ics_sha256[:12]}…）与状态里最后一次发布记录"
            f"（{state.last_push.content_sha256[:12]}…）不是同一版",
        )
    if journal is not None and journal.branch != state.branch:
        return CompatibilityCheck(
            False,
            "journal_branch",
            f"恢复快照记录的发布分支是 {journal.branch}，与当前订阅的 {state.branch} 不符，"
            "它描述的是另一条发布通道上的产物",
        )
    # 收尾那次 `save_state` 崩掉时，`last_push` / 留底 / 台账都已是新版，只有盐值还停在
    # 旧值——上面几格全部相符。不在这里比对，下一次普通 push 会按旧盐值重建，把刚救回来
    # 的课重新算回旧 UID（手机上已被删过、删除记忆不补 ⇒ 等于又抹掉一次）。
    # 旧格式快照没有这一项（None）＝ 不知道，跳过；repair 会把知道的表落回状态。
    if (
        journal is not None
        and journal.revive_salts is not None
        and state.revive_salts != journal.revive_salts
    ):
        return CompatibilityCheck(
            False,
            "revive_salts",
            "状态里的重新编号计数器与恢复快照记录的不一致"
            f"（快照 {len(journal.revive_salts)} 门 / 状态 {len(state.revive_salts)} 门）："
            "下一次发布会把换过 UID 的课重新算回旧值",
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
            # 指纹为 `None` 不是"免检"，它记录的是**当时这份文件不存在**（审阅 P1-04）：
            # 半路冒出来的一份台账同样会改写撤销候选的差集——`cancel_candidates` 把它当
            # "上次发过什么"的凭据，凭空多出来的一批候选要么撤出从没上线过的事件，
            # 要么让真该撤的因为"内容对不上"被丢弃，两种都静默。所以 keep 的完整陈述是
            # 两件事：**内容指纹 + 在不在**。
            now_sha = _sha_of(path_key)
            if now_sha != keep_sha:
                if keep_sha is None:
                    detail = (
                        f"{human} {path_key.name} 在那次发布时并不存在，现在却出现了："
                        "这份台账是撤销判定的输入，内容无从核对"
                    )
                elif now_sha is None:
                    detail = f"{human} {path_key.name} 被保留过，事后却被删除（快照只记指纹、补不回内容）"
                else:
                    detail = f"{human} {path_key.name} 被保留过，却与那次发布时的字节不符（被改动）"
                return CompatibilityCheck(False, f"{label}_keep", detail)
            continue
        if _sha_of(path_key) != hashlib.sha256(want.encode("utf-8")).hexdigest():
            forgiven = _difference_is_only_time_pruning(want, path_key, now=wall_clock, label=human)
            if forgiven is not None:
                return CompatibilityCheck(
                    True,
                    "ledger_pruned_by_time",
                    f"{human}有 {forgiven} 行因原定时刻已过而退场"
                    "（`export` 与发布共用这份台账，台账按设计剪掉过期行），本次按相容放行",
                )
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


def fetch_published_file(
    cfg: Settings, state: SubscriptionState, semester: str
) -> tuple[str, bytes]:
    """拉回发布分支 tip 上那份 .ics 的**文件名与字节**（离线、零凭据外泄）。

    刻意走 work 仓库的 `git fetch` + `git show` 而不是 HTTP：`url_base` 可能是
    `file://`（彩排远端），也可能 CDN 还在吐旧文件；git 侧拿到的就是 tip 的真实字节。

    文件名**不假设等于 `state.token`**，而且要把它一并交回（P0-01 之后必须如此）：
    `rotate` 的崩溃窗口里「状态里的新 token」与「远端那个旧文件名」正好分家，按
    `state.token.ics` 读会报"读不到"，让 `repair` 在唯一需要它的场合失灵。反过来，
    只比内容哈希也不够——rotate 补发常常内容一字未改、**只有文件名换了**，此时
    「远端等于准备稿」还是「远端等于上一版」只能靠文件名区分。读取判据与 `publish`
    的护栏同一条：tip 上恰好一个根目录 `.ics`。
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
    ref = f"refs/remotes/origin/{state.branch}"
    proc = _git(work, "ls-tree", "-r", "--name-only", ref)
    if proc.returncode != 0:
        raise SubscribePublishError(
            f"无法列出远端分支 {state.branch} 的 tip：{(proc.stderr or proc.stdout).strip()}",
            hint="确认订阅仓库与分支无误后重试；远端不可达时 repair 也无能为力。",
        )
    listing = (proc.stdout or "").split()
    if len(listing) != 1 or "/" in listing[0] or not listing[0].endswith(".ics"):
        raise SubscribePublishError(
            f"远端分支 {state.branch} 的 tip 含 {len(listing)} 个文件，不是本工具的发布产物，"
            "无法据此恢复",
            hint="先看 subscribe status 核对仓库与分支；确认线上产物后重试。",
        )
    show = _git_bytes(work, "show", f"{ref}:{listing[0]}")
    if show.returncode != 0:
        raise SubscribePublishError(
            f"无法从远端分支 {state.branch} 读取 {listing[0]}",
            hint="确认订阅仓库与分支无误后重试；远端不可达时 repair 也无能为力。",
        )
    return listing[0], bytes(show.stdout or b"")


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
