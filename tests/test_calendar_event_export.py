"""0.9-P1-03 校历事件进入产物（exporter 层）。

规格：`docs/design/2026-10-15-academic-calendar-source.md` §4–§7 + §12.1–12.4，
接线口径沿用考试侧那套已定死的规矩（``docs/design/2026-10-09-exam-cancellation.md`` §6.5）：

- **默认关**：不带 ``--include-calendar-events`` 时产物字节必须与今天逐字节相同
  （既有不变量「现有订阅者的产物不因新特性而变」，§6 第 2 条）；
- **口径不并轨**：``info["events"]`` 与 ``date_range`` 只算课程事件（§6.5 第 1 条），
  校历事件另走 ``info["calendar_events"]``；
- **不接撤销通路**（§5）：上一版有、这一版没有 ⇒ 就是**从 feed 消失**，
  不下发 ``STATUS:CANCELLED``、不进熔断分母，只在日志里如实报数量；
- **日志零原文**（§8）：序号 / 字段名 / 计数 / UID 可以出现，标题与来源 URL 的值不行。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from icalendar import Calendar
from subscribe_support import SEMESTER, make_home

from xjtu_calendar.calendar_events import make_calendar_uid
from xjtu_calendar.exporter import build_ics_for_semester

WINTER_UID = make_calendar_uid(SEMESTER, "2026-fall-winter")


def event_item(**over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source_id": "2026-fall-winter",
        "category": "vacation",
        "title": "标记-寒假",
        "start": "2027-01-18",
        "end": "2027-02-28",
        "source_url": "https://due.xjtu.edu.cn/xxfw/xl.htm",
    }
    item.update(over)
    return item


def conflict_item(**over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "topic": "winter_start",
        "source_id": "2026-fall-winter",
        "source_urls": [
            "https://due.xjtu.edu.cn/xxfw/xl.htm",
            "https://jwc.xjtu.edu.cn/info/1095/10442.htm",
        ],
        "dates": ["2027-01-18", "2027-01-22"],
        "decided_on": "2026-10-15",
    }
    item.update(over)
    return item


def home_with_calendar(
    tmp_path: Path,
    events: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
) -> Any:
    """配置齐全 + 学期配置里带 ``events`` / ``conflicts`` 两段（其余三件套照旧）。"""
    cfg = make_home(tmp_path, semester=SEMESTER)
    path = cfg.semester_config_path(SEMESTER)
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    if events is not None:
        payload["events"] = events
    if conflicts is not None:
        payload["conflicts"] = conflicts
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg


def build(cfg: Any, *, include: bool = False, **kwargs: Any) -> Any:
    return build_ics_for_semester(cfg, SEMESTER, include_calendar_events=include, **kwargs)


def summaries(ics: str) -> list[str]:
    return [str(v.get("summary")) for v in Calendar.from_ical(ics).walk("VEVENT")]


# --------------------------------------------------------------------------- #
# 1. 默认关：产物一字不变
# --------------------------------------------------------------------------- #


def test_off_by_default_and_byte_identical_to_a_config_without_events(tmp_path: Path) -> None:
    """带 `events` 配置但不开旗标 ⇒ 与根本没有 `events` 的配置**逐字节相同**。

    这条钉的是"现有订阅者产物不变"那半边地基；只断言"没有校历 UID"是不够的，
    万一实现顺手改了排序或标题，前者的读数照样是绿的。

    DTSTAMP 跟着 raw 快照的 mtime 走（I3），两个 home 必须钉成同一时刻才谈得上逐字节。
    """
    from datetime import datetime

    from xjtu_calendar.timeutil import TZ_XIAN

    pinned = datetime(2026, 10, 15, 12, 0, 0, tzinfo=TZ_XIAN).timestamp()
    cfg = home_with_calendar(tmp_path, [event_item()])
    control = make_home(tmp_path / "control", semester=SEMESTER)
    for path in (
        cfg.raw_timetable_path(SEMESTER),
        control.raw_timetable_path(SEMESTER),
    ):
        os.utime(path, (pinned, pinned))

    with_events = build(cfg)
    without_events = build(control)

    assert with_events.ics == without_events.ics
    assert WINTER_UID not in with_events.ics


def test_info_counts_calendar_events_on_a_separate_line(tmp_path: Path) -> None:
    """§6.5 第 1 条：``events`` / ``date_range`` 的口径**始终只算课程事件**。

    并入新类别会让 `doctor` 的既有读数变义（那个数字已经写进若干文档与用例），
    所以校历事件另走 ``calendar_events``。
    """
    cfg = home_with_calendar(tmp_path, [event_item()])
    off = build(cfg)
    on = build(cfg, include=True)

    assert off.info["calendar_events"] == 0
    assert on.info["calendar_events"] == 1
    assert on.info["events"] == off.info["events"]
    assert on.info["date_range"] == off.info["date_range"]


# --------------------------------------------------------------------------- #
# 2. 开了旗标：事件真的进产物
# --------------------------------------------------------------------------- #


def test_enabled_calendar_event_is_rendered_as_an_all_day_vevent(tmp_path: Path) -> None:
    ics = build(home_with_calendar(tmp_path, [event_item()]), include=True).ics

    assert "标记-寒假" in summaries(ics)
    assert f"UID:{WINTER_UID}" in ics
    # 配置写闭区间 1/18–2/28 ⇒ 产物是不含的 3/1（§12.1 那唯一一次换算）。
    assert "DTSTART;VALUE=DATE:20270118" in ics
    assert "DTEND;VALUE=DATE:20270301" in ics
    assert "STATUS:CANCELLED" not in ics
    # 校历事件不进 DESCRIPTION 之外的第二处口径：来源 URL 是给**人**在产物里核对用的。
    assert "https://due.xjtu.edu.cn/xxfw/xl.htm" in ics


def test_enabled_events_get_sequence_zero_without_baseline(tmp_path: Path) -> None:
    """无基线时校历事件与课程事件同起 SEQUENCE:0；有基线才能守住"改日期 ⇒ +1"。"""
    ics = build(home_with_calendar(tmp_path, [event_item()]), include=True).ics
    assert "SEQUENCE:0" in ics


def test_sequence_bumps_when_the_published_school_calendar_date_moves(tmp_path: Path) -> None:
    """§5 的身份承诺**端到端**走一遍：同一 source_id、日期改了 ⇒ 同 UID、SEQUENCE +1。

    只测 `parse_baseline` 认 DATE 形态（P1-01 那条）不等于管线接得住 ——
    `--sequence-from` 这条路径要真的把上一版读进来。
    """
    cfg = home_with_calendar(tmp_path, [event_item()])
    first = build(cfg, include=True)

    previous = tmp_path / "prev.ics"
    previous.write_text(first.ics, encoding="utf-8")

    moved = home_with_calendar(
        tmp_path, [event_item(start="2027-01-22", end="2027-03-05")], conflicts=[]
    )
    second = build(moved, include=True, sequence_from=str(previous))

    assert f"UID:{WINTER_UID}" in second.ics, "改日期换掉了 UID ⇒ 身份配方没有走通"
    assert "SEQUENCE:1" in second.ics


# --------------------------------------------------------------------------- #
# 3. 未裁决冲突：两边都不生成（§7）
# --------------------------------------------------------------------------- #


def test_unresolved_conflict_withholds_that_event_even_with_the_flag(tmp_path: Path) -> None:
    ics = build(
        home_with_calendar(tmp_path, [event_item()], [conflict_item()]),
        include=True,
    ).ics
    assert WINTER_UID not in ics
    assert "标记-寒假" not in summaries(ics)


def test_adjudicated_conflict_lets_the_event_through(tmp_path: Path) -> None:
    ics = build(
        home_with_calendar(
            tmp_path,
            [event_item()],
            [conflict_item(adjudicated_by="以教务部校历图片为准")],
        ),
        include=True,
    ).ics
    assert WINTER_UID in ics


# --------------------------------------------------------------------------- #
# 4. 不接撤销通路（§5）：消失就是从 feed 消失
# --------------------------------------------------------------------------- #


def test_dropping_a_calendar_event_emits_no_cancellation_and_does_not_trip_the_fuse(
    tmp_path: Path,
) -> None:
    """上一版含校历事件、这一版没开旗标 ⇒ **零撤销条目**、零熔断。

    代价要如实写（§5）：旧事件从 feed 消失，客户端删不删全看它的脾气。
    这里证的是"不新增机制去掩盖它"：撤销通路一行都没走。
    """
    cfg = home_with_calendar(tmp_path, [event_item()])
    first = build(cfg, include=True)
    previous = tmp_path / "prev.ics"
    previous.write_text(first.ics, encoding="utf-8")

    result = build(cfg, include=False, sequence_from=str(previous))

    assert "STATUS:CANCELLED" not in result.ics
    assert result.info["calendar_events"] == 0
    assert result.info["exam_cancellations"] == 0
    assert result.info["course_cancellations"] == 0


# --------------------------------------------------------------------------- #
# 5. §8 零原文：日志里只许有序号、字段名、计数、UID
# --------------------------------------------------------------------------- #


def test_calendar_logs_never_carry_the_title_or_the_url_value(tmp_path: Path) -> None:
    """坏配置 + 未裁决冲突 + 事件被 withheld 三条通路一起吃这条守卫。

    标题会进**产物**（那是给人看的），但日志会被贴进 issue 与截图 ⇒ 零原文。
    """
    from exam_support import captured_logs

    cfg = home_with_calendar(
        tmp_path,
        [event_item(), event_item(source_id="2026-fall-bad", category="nope")],
        [conflict_item()],
    )
    with captured_logs() as records:
        build(cfg, include=True)

    joined = "\n".join(record.getMessage() for record in records)
    assert "标记-寒假" not in joined
    assert "xxfw" not in joined
    assert "due.xjtu.edu.cn" not in joined
    assert "2026-fall-winter" not in joined, "source_id 是人起的名字，同样不进日志（§8）"
    # 有东西可报：一条配置错误、一条被冲突挡住。
    assert "校历" in joined, joined


def test_withheld_event_count_is_reported_in_info(tmp_path: Path) -> None:
    result = build(
        home_with_calendar(
            tmp_path,
            [event_item(), event_item(source_id="2026-fall-registered", category="registration")],
            [conflict_item()],
        ),
        include=True,
    )
    assert result.info["calendar_events"] == 1
    assert result.info["calendar_withheld"] == 1


def test_a_broken_events_block_stays_silent_when_the_flag_is_off(tmp_path: Path) -> None:
    """§16.1 的日志面：没启用这特性的人，每轮普通发布都不该被校历警告刷屏。

    配置坏着这件事由 `doctor` 喊（体检那一格照旧报），构建侧的 warn 与计数都跟着旗标走——
    与"坏配置不许拦住没用它的人"是同一条裁决，只是落在**噪声**而不是**退出码**上。
    """
    from exam_support import captured_logs

    cfg = home_with_calendar(tmp_path, [event_item(category="nope")])

    with captured_logs() as records:
        off = build(cfg, include=False)
    joined = "\n".join(record.getMessage() for record in records)
    assert "校历事件配置错误" not in joined, joined
    assert off.info["calendar_config_errors"] == 0, off.info

    with captured_logs() as records:
        on = build(cfg, include=True)
    joined = "\n".join(record.getMessage() for record in records)
    assert "校历事件配置错误" in joined, joined
    assert on.info["calendar_config_errors"] == 1, on.info
