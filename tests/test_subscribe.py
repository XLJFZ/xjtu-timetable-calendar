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


class _FakeResponse:
    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self, _n: int) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_verify_url_accepts_a_valid_ics_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """成功分支：200 + 开头是 `BEGIN:VCALENDAR` 才算通过。

    `status --verify` 的这条判据此前只有失败用例；这里把包装器换掉，不碰网络。
    """
    monkeypatch.setattr(
        subscribe.urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(200, b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"),
    )
    ok, msg = subscribe.verify_url("https://example.com/x.ics")
    assert ok and msg == "HTTP 200"


def test_verify_url_rejects_200_with_a_non_ics_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """200 不代表成功：正文不是日历时判失败，避免把 Cloudflare 错误页当订阅。"""
    monkeypatch.setattr(
        subscribe.urllib.request,
        "urlopen",
        lambda *_a, **_k: _FakeResponse(200, b"<!DOCTYPE html><html>not found</html>"),
    )
    ok, msg = subscribe.verify_url("https://example.com/x.ics")
    assert not ok and msg == "HTTP 200"


def _state_for(url_base: str) -> SubscriptionState:
    """只给 url_base 的最小状态：webcal 判定只看 scheme，其余字段与本用例无关。"""
    return SubscriptionState(
        semester=SEMESTER,
        repo_url="https://github.com/alice/cal.git",
        branch="cal",
        token=new_token(),
        url_base=url_base,
    )


def test_webcal_url_swaps_scheme_and_keeps_path_and_token() -> None:
    """https ⇒ 只换 scheme：host/路径/token 与 `subscription_url` 逐字符一致。

    `webcal://` 是 URL scheme 而不是文件格式（followups §3），所以这条链接指向的就是
    已经发布的那一份 `.ics`——不新增任何发布物。
    """
    state = _state_for("https://alice.github.io/cal")  # 尾斜杠缺失也要归一
    assert state.webcal_url == f"webcal://alice.github.io/cal/{state.token}.ics"
    assert state.subscription_url == f"https://alice.github.io/cal/{state.token}.ics"
    assert state.webcal_url == state.subscription_url.replace("https://", "webcal://", 1)


@pytest.mark.parametrize(
    "url_base",
    [
        "http://alice.github.io/cal/",  # 明文 http：不发 webcal（宁可不提示）
        "http://pages.example.com/cal/",  # 自建 Pages 走 http
        "file:///tmp/fake-origin/",  # `file://` 彩排远端，根本没有公开 https 地址
    ],
)
def test_webcal_url_is_none_for_non_https(url_base: str) -> None:
    """非 https 一律 None：不给一个「看起来能点其实无效」的链接（followups §3）。"""
    assert _state_for(url_base).webcal_url is None
