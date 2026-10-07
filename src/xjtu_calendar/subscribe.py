"""URL 订阅发布：状态文件 + git 孤儿提交发布（设计见 docs/superpowers/specs/…）。

本模块目前只承载**状态层**：token 生成、Pages URL 推导、状态文件的
私有原子落盘与往返。git 探测 / 发布（Task 3/4）后续加入同一模块。
"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import Settings
from .errors import SubscribeNotConfigured  # noqa: F401  (Task 3 起使用)
from .fileutil import atomic_write_text

__all__ = [
    "TOKEN_BYTES",
    "PushRecord",
    "SubscriptionState",
    "derive_url_base",
    "load_state",
    "new_token",
    "save_state",
    "state_path",
    "subscribe_dir",
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
