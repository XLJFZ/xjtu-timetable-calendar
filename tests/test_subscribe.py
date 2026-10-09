"""状态层单元：token 形态、URL 推导、状态往返、private 落盘、rotate/新鲜度/自检。"""

from __future__ import annotations

import hashlib
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


def test_rotate_changes_token_and_keeps_history_fields(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    st = SubscriptionState(
        SEMESTER,
        "https://github.com/a/b.git",
        "cal",
        subscribe.new_token(),
        "https://a.github.io/b/",
        PushRecord("2026-10-07T00:00:00Z", "ff" * 32, "old"),
    )
    subscribe.save_state(cfg, st)
    old = st.token  # rotate 原地改 state，取旧值需先拷贝
    rotated = subscribe.rotate_token(cfg, st)
    assert rotated.token != old
    assert rotated.last_push is not None
    assert rotated.last_push.token == "old"  # rotate 不抹历史
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.token == rotated.token
    assert reloaded.last_push == st.last_push
    assert reloaded.subscription_url.endswith(f"/{rotated.token}.ics")


def test_snapshot_age_days(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    age = subscribe.snapshot_age_days(cfg, SEMESTER)
    assert age is not None and age < 1  # 夹具刚写的文件
    assert subscribe.snapshot_age_days(cfg, "no-such") is None
    # kind 默认值不变：既有调用点（cli.py publish/status）不传 kind，行为逐字保持。
    assert subscribe.snapshot_age_days(cfg, SEMESTER, kind="exams") is None  # 无考试快照


def test_snapshot_age_days_rejects_unknown_kind(tmp_path: Path) -> None:
    """N4：与 fetcher._raw_paths 同口径——kind 拼错要当场 ValueError，不许静默按课表读出错数据。"""
    cfg = make_home(tmp_path)
    with pytest.raises(ValueError, match="未知的快照类型"):
        subscribe.snapshot_age_days(cfg, SEMESTER, kind="exam")


def test_has_unpublished_changes(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    st = SubscriptionState(
        SEMESTER,
        "https://github.com/a/b.git",
        "cal",
        subscribe.new_token(),
        "https://a.github.io/b/",
    )
    assert subscribe.has_unpublished_changes(cfg, st, "BEGIN:VCALENDAR\n") is True
    ics = "BEGIN:VCALENDAR\nEND:VCALENDAR\n"
    st.last_push = PushRecord(
        "2026-10-07T00:00:00Z",
        hashlib.sha256(ics.encode("utf-8")).hexdigest(),
        st.token,
    )
    assert subscribe.has_unpublished_changes(cfg, st, ics) is False
    assert subscribe.has_unpublished_changes(cfg, st, ics + "X\n") is True


def test_verify_url_rejects_non_http() -> None:
    ok, msg = subscribe.verify_url("file:///D:/tmp/x.ics")
    assert not ok and "http" in msg


def test_verify_url_reports_fetch_failure_without_network() -> None:
    # 空 host：scheme 是 https，但连接前即失败（URLError），测试因此不碰真实网络。
    ok, msg = subscribe.verify_url("https://")
    assert not ok and "请求失败" in msg
