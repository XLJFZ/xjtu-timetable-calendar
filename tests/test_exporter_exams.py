"""考试事件并进同一份 .ics（设计文档 §6.5）。

合并点是整个特性的**收口处**：课程侧的 ``seen_uids`` 去重只管课程，考试事件是在
它外面并进来的；``summarize``/``date_range`` 只吃课程，``sequence_stats`` 吃的是
「课程 + live 考试」（**不含撤销条目**，``docs/design/2026-10-09-exam-cancellation.md``
D23；撤销数量单独走 ``info["exam_cancellations"]``）；考试侧任何失败都只准降级，
不准让导出非零退出（§7）。
下面每条用例都按这些约束逐个证伪。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from exam_support import (
    DEMO_DAY,
    SEMESTER,
    captured_logs,
    exam_payload,
    exam_row,
    timetable_envelope,
)
from icalendar import Calendar
from subscribe_support import make_home, payload_row

from xjtu_calendar.cli import main
from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.fetcher import save_raw


def _home_with_exams(tmp_path: Path, *rows: dict[str, str]):
    """一个配置齐全的 home + 一份真信封形态的考试快照。

    考试快照在这里**手工落盘**（设计文档 §9 点名的固件改造点）：
    ``subscribe_support.make_home`` 写的是 ``{"kbList": rows}`` 简写信封，
    经裁定不改造它，覆写 ``raw_exams_path`` 就是这个改造点的落地方式。
    """
    cfg = make_home(tmp_path, semester=SEMESTER)
    save_raw(exam_payload(list(rows) or [exam_row()]), cfg, SEMESTER, kind="exams")
    return cfg


def _vevents(ics: str) -> dict[str, tuple[str, int]]:
    """``UID -> (SUMMARY, SEQUENCE)``，从**渲染后的产物**里读，不碰内部对象。"""
    index: dict[str, tuple[str, int]] = {}
    for component in Calendar.from_ical(ics).walk("VEVENT"):
        uid = str(component.get("uid"))
        index[uid] = (str(component.get("summary")), int(component.get("sequence") or 0))
    return index


def _exam_uid(ics: str) -> str:
    """合并产物里那场考试的 UID（本文件的用例都只放一场考试）。"""
    uids = [uid for uid, (summary, _) in _vevents(ics).items() if summary.endswith("（结课考试）")]
    assert len(uids) == 1, f"期望恰好一场考试，实际 {uids}"
    return uids[0]


def _vevent_block(ics: str, uid: str) -> str:
    """按 UID 取出该 VEVENT 的**原文**，用于逐行断言 DTSTART 的参数写法。"""
    for block in ics.split("BEGIN:VEVENT")[1:]:
        body = block.split("END:VEVENT")[0]
        if f"UID:{uid}" in body:
            return body
    raise AssertionError(f"产物里没有 UID 为 {uid} 的事件")


def _calname(ics: str) -> str:
    """``X-WR-CALNAME``：日历标题（§6.5:298 考试**不得**影响它）。"""
    return str(Calendar.from_ical(ics).get("x-wr-calname") or "")


def test_exams_merge_into_the_same_calendar(tmp_path):
    """D1：并进同一份 .ics，课程口径一个字不变。"""
    cfg = _home_with_exams(tmp_path)
    without = build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    with_exams = build_ics_for_semester(cfg, SEMESTER)

    assert with_exams.info["exam_events"] == 1
    # 课程口径一个字不变：Events / date_range 都不许被考试拉长
    assert with_exams.info["events"] == without.info["events"]
    assert with_exams.info["date_range"] == without.info["date_range"]
    assert _calname(with_exams.ics) == _calname(without.ics)  # 标题只从课程推导
    assert "（结课考试）" in with_exams.ics
    assert "（结课考试）" not in without.ics


def test_exam_events_do_not_collide_with_course_uids(tmp_path):
    cfg = _home_with_exams(tmp_path)
    ics = build_ics_for_semester(cfg, SEMESTER).ics
    uids = re.findall(r"^UID:(.+)$", ics, re.MULTILINE)
    assert len(uids) == len(set(uids))  # 全局唯一，含课程与考试


def test_duplicate_exam_uid_is_dropped_with_warning(tmp_path):
    """同一 WID 出现两行（脏数据）时，后来者被丢弃而不是写出重复 UID。"""
    cfg = _home_with_exams(tmp_path, exam_row(WID="DUP"), exam_row(WID="DUP", KCM="示例课程乙"))
    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER)
    assert result.info["exam_events"] == 1
    assert any("UID" in rec.getMessage() for rec in records)
    uids = re.findall(r"^UID:(.+)$", result.ics, re.MULTILINE)
    assert len(uids) == len(set(uids))  # 丢弃真的落到了产物上，不只是计数


def test_missing_exam_snapshot_is_logged(tmp_path):
    """简报的 ``..._is_silent``，按裁定 R3 改写：§2 D2 的「静默跳过」是**只在日志说明**。

    「没抓过考试」不是错误，但也不能一个字都不说 —— 用户会以为这学期真的没排考。
    级别必须是 info（断言 ``levelname == "INFO"``，实现成 warning 就红）。
    """
    cfg = make_home(tmp_path, semester=SEMESTER)
    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER)
    assert result.info["exam_events"] == 0  # 没抓过考试不是错误
    assert any(rec.levelname == "INFO" and "考试" in rec.getMessage() for rec in records), (
        "无考试快照必须留下一条 info 说明"
    )


def test_empty_exam_snapshot_is_logged(tmp_path):
    """§7「确认无考试」：空快照同样只留一条 info，事件为零，绝不告警。"""
    cfg = make_home(tmp_path, semester=SEMESTER)
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")
    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER)
    assert result.info["exam_events"] == 0
    assert any(rec.levelname == "INFO" and "考试" in rec.getMessage() for rec in records), (
        "空考试快照必须留下一条 info 说明"
    )


def test_no_exams_flag_keeps_snapshot_on_disk(tmp_path):
    cfg = _home_with_exams(tmp_path)
    build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    assert cfg.raw_exams_path(SEMESTER).is_file()  # 开关只影响导出，不删数据


def test_from_date_filter_also_cuts_exams(tmp_path):
    """v1 决定：日期范围一并裁剪考试（README 要写清）。"""
    cfg = _home_with_exams(
        # 考试刻意放在课表学期之外（2099），窗口 2098 整年一场课都没有：
        # 课程与考试必须被同一组边界裁掉。2099-01-04 是**星期日**，
        # 括号里的星期与日期自洽，否则这条用例会顺带触发一条无关的星期不一致提醒。
        tmp_path,
        exam_row(KSRQ="2099-01-04 00:00:00", KSSJMS="2099-01-04 09:00-11:00(星期日)"),
    )
    result = build_ics_for_semester(cfg, SEMESTER, from_date="2098-01-01", to_date="2098-12-31")
    assert result.info["exam_events"] == 0


@pytest.mark.parametrize(
    "raw_text",
    [
        "{ 这是被截断的半截 JSON",
        json.dumps(
            {
                "datas": {
                    "wdksap": {
                        "totalSize": "很多",
                        "rows": ["不是字典的一行"],
                        "extParams": {"msg": "查询成功", "code": 1},
                    }
                }
            },
            ensure_ascii=False,
        ),
    ],
    ids=["truncated-json", "malformed-rows"],
)
def test_corrupt_exam_snapshot_never_breaks_the_export(tmp_path, raw_text):
    """R2 / §7:387：绝不因为考试让 export 非零退出。

    第一个参数是**能让用例亮起来**的那个：半截 JSON 让 ``load_raw`` 抛
    ``json.JSONDecodeError``（它是 ``ValueError`` 的子类，**不是**
    ``TimetableFetchError``），简报原稿只包 ``except TimetableFetchError`` → 整个
    ``build_ics_for_semester`` 直接炸；并入整段 ``except Exception`` 后才降级。
    第二个参数（``rows`` 里混入非 dict、``totalSize`` 类型错）考的是"结构走样但不抛"
    的形状：考试侧归零，课程产物照常。
    """
    cfg = make_home(tmp_path, semester=SEMESTER)
    courses = build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    cfg.raw_exams_path(SEMESTER).write_text(raw_text, encoding="utf-8")

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER)

    assert result.info["exam_events"] == 0
    # 降级 = 产物与「压根不并入考试」逐字节一致（DTSTAMP 取课表快照 mtime，两次相同）
    assert result.ics == courses.ics
    assert any("考试" in rec.getMessage() for rec in records)  # 跳过必须可见


def test_non_utf8_exam_snapshot_degrades_and_export_exits_zero(tmp_path, monkeypatch):
    """Task 8 说宽 `except Exception` 真正可达的是"文件读不出来"这一类（终审 F4）。

    上面那条用例只覆盖了 `JSONDecodeError`（坏文本仍是合法 UTF-8）。而
    `load_raw` 是 `json.loads(path.read_text(encoding="utf-8"))` ——
    非 UTF-8 字节（编辑器另存、下载截断、Windows 默认 GBK）先在 `read_text`
    里抛 `UnicodeDecodeError`，**连 JSON 解析都到不了**。这类失败今天没有用例。
    断言口径按 §7:387：产物**逐字等于**"没有考试"的课表产物，且 CLI 退出码 0。
    """
    cfg = make_home(tmp_path, semester=SEMESTER)
    courses = build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    cfg.raw_exams_path(SEMESTER).write_bytes(b"\xff\xfe\x00not-utf-8-at-all")

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER)
    assert result.info["exam_events"] == 0
    assert result.ics == courses.ics
    assert any("考试" in rec.getMessage() for rec in records)  # 降级必须可见

    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(cfg.home))
    assert main(["export", "--semester", SEMESTER, "-o", str(tmp_path / "out.ics")]) == 0


def test_sequence_stats_see_exam_events(tmp_path):
    """R1：``sequence_stats`` 吃「课程 + live 考试」（§6.5:311；D23 起撤销条目不进
    stats，撤销数量走 ``info["exam_cancellations"]``，见
    ``docs/design/2026-10-09-exam-cancellation.md``）。

    简报原写法证伪不了任何东西：无基线时 ``sequence_stats`` 恒为 ``None``
    （``exporter.py:685-700``），断言 ``is not None`` **必红**；放宽成 ``added >= 1``
    又**恒真**（``make_home`` 自带课程事件，考试被完全排除也照样绿）。
    这里改成可证伪的口径：先落一份**只含课程**的产物当基线，再带上考试重建 ——
    唯一的新增必须恰好是那场考试（``added == 1``，不是 ``>= 1``）。
    """
    cfg = _home_with_exams(tmp_path)
    course_only = build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    baseline = tmp_path / "course-only.ics"
    baseline.write_text(course_only.ics, encoding="utf-8")

    result = build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(baseline))

    stats = result.sequence_stats
    assert stats is not None
    assert stats["added"] == 1  # 一场考试 = 恰好一条新增
    assert stats["updated"] == 0

    before, after = _vevents(course_only.ics), _vevents(result.ics)
    assert before  # 基线确实含课程事件，否则 added==1 可能只是空基线噪音
    assert stats["preserved"] == len(before)
    added = set(after) - set(before)
    assert len(added) == 1
    assert after[added.pop()][0].endswith("（结课考试）")  # 新增的那条就是考试


def test_rescheduled_exam_keeps_its_uid_and_bumps_sequence(tmp_path):
    """R5（§9:450-452）：同 ``WID``、不同 ``KSSJMS`` → UID 不变、SEQUENCE +1。

    撤销通路上线后（`docs/design/2026-10-09-exam-cancellation.md`）改期**仍只能**靠同
    UID 原地更新：一旦 UID 变了，旧条目虽会以 ``STATUS:CANCELLED`` 下发，但一次性导入型
    客户端不会回源，仍留下一场从没发生过的旧考试时间 —— 依然是本特性最糟的用户可见故障。
    """
    cfg = make_home(tmp_path, semester=SEMESTER)
    save_raw(exam_payload([exam_row(WID="RESCH-1")]), cfg, SEMESTER, kind="exams")
    first = build_ics_for_semester(cfg, SEMESTER)
    baseline = tmp_path / "first.ics"
    baseline.write_text(first.ics, encoding="utf-8")

    # 同一场考试改时间：WID 不变，KSSJMS 变
    save_raw(
        exam_payload([exam_row(WID="RESCH-1", KSSJMS=f"{DEMO_DAY} 09:00-11:00(星期一)")]),
        cfg,
        SEMESTER,
        kind="exams",
    )
    second = build_ics_for_semester(cfg, SEMESTER, baseline_probe=str(baseline))

    before, after = _vevents(first.ics), _vevents(second.ics)
    exam_uid = _exam_uid(first.ics)
    assert exam_uid in after  # 改期不产生新 UID
    assert set(before) == set(after)  # 也不留下"孤儿"事件
    assert before[exam_uid][1] == 0
    assert after[exam_uid][1] == 1
    assert second.sequence_stats is not None
    assert second.sequence_stats["updated"] == 1
    assert second.sequence_stats["added"] == 0


def test_merged_exam_event_is_a_datetime_not_an_all_day(tmp_path):
    """§6.4 红线在**合并产物**上复核（Task 5 已在构造点锁死，这里防合并层改回去）。

    必须断言渲染出来的参数行：全天事件（``VALUE=DATE``）会被
    ``sequence.parse_baseline``（``sequence.py:139-141``）静默丢弃 → 每次重发布
    SEQUENCE 恒为 0 → 客户端永不更新这条考试。
    """
    cfg = _home_with_exams(tmp_path)
    ics = build_ics_for_semester(cfg, SEMESTER).ics
    block = _vevent_block(ics, _exam_uid(ics))

    assert "DTSTART;TZID=Asia/Shanghai:20300617T150000" in block
    assert "DTEND;TZID=Asia/Shanghai:20300617T173000" in block
    assert "VALUE=DATE" not in block
    assert "BEGIN:VALARM" not in block  # D3：不写提醒


def test_input_path_feeds_only_the_timetable(tmp_path):
    """§6.5 第 3 条：``--input`` 只喂课表 payload，考试仍从 ``raw/exams-*.json`` 读。

    若实现误把 ``payload``（课表）当考试信封解析，``exam_events`` 会是 0
    （课程行没有 ``KSSJMS``，全被跳过），本用例立刻红。
    """
    cfg = _home_with_exams(tmp_path)
    course_file = tmp_path / "timetable-snapshot.json"
    course_file.write_text(
        json.dumps(timetable_envelope([payload_row()]), ensure_ascii=False), encoding="utf-8"
    )
    result = build_ics_for_semester(cfg, SEMESTER, input_path=str(course_file))
    assert result.info["exam_events"] == 1


def test_exams_payload_argument_injects_without_a_snapshot(tmp_path):
    """``exams_payload=`` 是 §6.5 点名的注入接缝（当前仅供测试/后续任务使用）。"""
    cfg = make_home(tmp_path, semester=SEMESTER)
    assert not cfg.raw_exams_path(SEMESTER).exists()
    result = build_ics_for_semester(cfg, SEMESTER, exams_payload=exam_payload([exam_row()]))
    assert result.info["exam_events"] == 1
    assert "（结课考试）" in result.ics


def test_campus_name_comes_from_the_timetable_snapshot(tmp_path):
    """LOCATION 的校区名前缀来自**同学期课表快照**的 XXXQDM -> XXXQDM_DISPLAY 对照。

    考试行只有 ``XXXQDM='5'``，没有 DISPLAY 字段（§6.3）。``make_home`` 写的是
    ``{'kbList': rows}`` 简写信封，``campus_names_from_timetable`` 认不出来，所以这条
    用例必须把课表快照换成真信封（设计文档 §9 点名的固件坑）。
    """
    cfg = _home_with_exams(tmp_path, exam_row(XXXQDM="5"))
    row = {**payload_row(), "XXXQDM": "5", "XXXQDM_DISPLAY": "创新港校区"}
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps(timetable_envelope([row]), ensure_ascii=False), encoding="utf-8"
    )
    ics = build_ics_for_semester(cfg, SEMESTER).ics
    # 断言锁定在**考试那条 VEVENT** 上：课程事件本来就会从 XXXQDM_DISPLAY 拿到校区，
    # 只看整份 .ics 的话，考试侧压根没查对照表也照样绿。
    assert "LOCATION:创新港校区 A-1001" in _vevent_block(ics, _exam_uid(ics))


def test_exam_only_in_range_does_not_warn_about_no_events(tmp_path):
    """日期窗口只框住考试那天（课程全在 2026 秋季）：`events` 为空但产物不是空的。

    钉的是「原来那句 `if not events:` 必须改成 `if not render_events:`」——
    否则每次只导出考试时段都会甩一条"没有生成任何事件"的假警告。
    """
    cfg = _home_with_exams(tmp_path)
    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, from_date=DEMO_DAY, to_date=DEMO_DAY)
    assert result.info["events"] == 0  # 课程口径：窗口里确实一次课都没有
    assert result.info["exam_events"] == 1
    assert not any("没有生成任何事件" in rec.getMessage() for rec in records)
