"""订阅状态文件的 `uid_recipe` 字段（规格 §6.6、CD7、CD17）。

判据只存一处：这个字段**缺失** = 该 URL 最后一次发布出自 v1 配方 ⇒ 下次 `subscribe push`
走一次性迁移分支。UID 是 hash、反推不了配方，所以不许用产物内容猜（CD7）。
CD17 的降级自愈也在这里钉住：旧版本读不出也写不回这个字段，重走迁移分支时留底已是
v2 产物 ⇒ 候选空集、无副作用。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from pathlib import Path

from subscribe_support import SEMESTER, make_home, payload_row

from xjtu_calendar import subscribe
from xjtu_calendar.cancellation import load_ledger
from xjtu_calendar.config import Settings
from xjtu_calendar.courses import migration_candidates
from xjtu_calendar.exporter import UID_RECIPE_VERSION, build_ics_for_semester
from xjtu_calendar.schedules import combine
from xjtu_calendar.subscribe import (
    SubscriptionState,
    load_state,
    needs_uid_migration,
    save_state,
)

#: 课程固件的保留期墙钟：固件第 1 教学周周一是 2026-09-07 ⇒ 取 9-01 时全部课次「未过期」
#: （与 `tests/test_exporter_course_cancellation.py` 同一口径）。
NOW = combine(date(2026, 9, 1), "00:00")


def _make_state(tmp_path: Path) -> tuple[Settings, SubscriptionState]:
    """布置 home 并登记一份 v0.7.x 形状的状态（那个版本没有 `uid_recipe` 这个概念）。"""
    cfg = make_home(tmp_path)
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://github.com/alice/cal.git",
        branch="cal",
        token=subscribe.new_token(),
        url_base="https://alice.github.io/cal/",
    )
    save_state(cfg, state)
    return cfg, state


def _rewrite_like_old_version(cfg: Settings, state: SubscriptionState) -> None:
    """模拟「降级回 v0.7.x 再升回来」：旧版 `from_dict` 忽略未知键、`to_dict` 走 `asdict`
    ⇒ 它写回的文件里这个字段必然不存在（CD17 的依据，规格 §4:117-118）。"""
    path = subscribe.state_path(cfg, state.semester)
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw.pop("uid_recipe", None)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_uid_recipe_round_trips(tmp_path: Path) -> None:
    cfg, state = _make_state(tmp_path)
    save_state(cfg, replace(state, uid_recipe=UID_RECIPE_VERSION))
    assert load_state(cfg, SEMESTER).uid_recipe == UID_RECIPE_VERSION


def test_uid_recipe_defaults_to_missing(tmp_path: Path) -> None:
    """构造时不写这个字段 ⇒ 默认 None ⇒ 迁移判定为真（CD7）。"""
    _cfg, state = _make_state(tmp_path)
    assert state.uid_recipe is None
    assert needs_uid_migration(state) is True


def test_old_state_file_reads_as_missing_recipe(tmp_path: Path) -> None:
    """v0.7.x 写的状态文件没有这个字段 ⇒ 读出来是 None ⇒ 触发迁移（CD7）。"""
    cfg, state = _make_state(tmp_path)
    _rewrite_like_old_version(cfg, state)
    loaded = load_state(cfg, SEMESTER)
    assert loaded.uid_recipe is None
    assert needs_uid_migration(loaded) is True


def test_needs_uid_migration_is_false_once_marked(tmp_path: Path) -> None:
    cfg, state = _make_state(tmp_path)
    save_state(cfg, replace(state, uid_recipe=UID_RECIPE_VERSION))
    assert needs_uid_migration(load_state(cfg, SEMESTER)) is False


def test_downgrade_drops_the_field_and_migration_is_a_noop(tmp_path: Path) -> None:
    """CD17：字段被旧版抹掉 ⇒ 下次 push 重走迁移分支，而此刻留底已是 v2 产物 ⇒ 候选空集。"""
    cfg = make_home(tmp_path, [payload_row()])
    result = build_ics_for_semester(cfg, SEMESTER)
    published = load_ledger(result.ics, label="发布留底")
    live_uids = set(published)
    assert published, "前提：留底里确实有课程事件"

    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://github.com/alice/cal.git",
        branch="cal",
        token=subscribe.new_token(),
        url_base="https://alice.github.io/cal/",
        uid_recipe=UID_RECIPE_VERSION,
    )
    save_state(cfg, state)
    _rewrite_like_old_version(cfg, state)

    loaded = load_state(cfg, SEMESTER)
    assert loaded.uid_recipe is None
    assert (
        migration_candidates(
            published=published, live_uids=live_uids, normal_cancel_uids=set(), now=NOW
        )
        == []
    )
