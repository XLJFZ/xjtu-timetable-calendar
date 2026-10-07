"""状态层单元：token 形态、URL 推导、状态往返、private 落盘。"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar import subscribe
from xjtu_calendar.subscribe import PushRecord, SubscriptionState, derive_url_base, new_token


def test_token_is_32_hex_and_unique() -> None:
    a, b = new_token(), new_token()
    assert re.fullmatch(r"[0-9a-f]{32}", a) and re.fullmatch(r"[0-9a-f]{32}", b)
    assert a != b


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("https://github.com/alice/cal-alice.git", "https://alice.github.io/cal-alice/"),
        ("https://github.com/alice/cal-alice", "https://alice.github.io/cal-alice/"),
        ("git@github.com:alice/cal-alice.git", "https://alice.github.io/cal-alice/"),
        ("ssh://git@github.com/alice/cal-alice.git", "https://alice.github.io/cal-alice/"),
        ("https://example.com/alice/cal.git", None),
        ("file:///D:/tmp/fake-origin.git", None),
    ],
)
def test_derive_url_base(repo: str, expected: str | None) -> None:
    assert derive_url_base(repo) == expected


def test_state_roundtrip_and_private_write(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://github.com/alice/cal.git",
        branch="cal",
        token=new_token(),
        url_base="https://alice.github.io/cal/",
        last_push=PushRecord("2026-10-07T04:00:00Z", "ab" * 32, "t"),
    )
    subscribe.save_state(cfg, state)
    loaded = subscribe.load_state(cfg, SEMESTER)
    assert loaded == state
    raw = json.loads(subscribe.state_path(cfg, SEMESTER).read_text(encoding="utf-8"))
    assert raw["last_push"]["content_sha256"] == "ab" * 32
    assert state.subscription_url == f"https://alice.github.io/cal/{state.token}.ics"


@pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 POSIX 权限位语义")
def test_state_file_is_owner_only_on_posix(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://github.com/alice/cal.git",
        branch="cal",
        token=new_token(),
        url_base="https://alice.github.io/cal/",
    )
    subscribe.save_state(cfg, state)
    assert subscribe.state_path(cfg, SEMESTER).stat().st_mode & 0o077 == 0


def test_load_state_missing_returns_none(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    assert subscribe.load_state(cfg, SEMESTER) is None
