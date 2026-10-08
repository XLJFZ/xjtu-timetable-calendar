"""``exams.diff_exams``：考试变更的五类形状（设计文档 §6.6）。

为什么**不**复用 ``diff.SlotChange``（§6.6:338-343）：那个形状强制 ``weekday: int`` 与
``periods: tuple[int, ...]``，而 CLI 打印侧硬编码「星期X」「第N节」——考试既没有周次也没有
节次，``weekday=0`` 会让 ``'一二三四五六日'[change.weekday - 1]`` **静默印成「日」**，
越界值直接 ``IndexError``（把 diff 崩给你看）。所以这里另立一条独立、冻结的变更形状，
五类（新增 / 取消 / 时间变更 / 教室变更 / 座位变更）各自可断言。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest
from exam_support import DEMO_DAY, captured_logs, exam_payload, exam_row

from xjtu_calendar.exams import (
    ExamChange,
    describe_exam_change,
    diff_exams,
    make_exam_uid,
    parse_exam_rows,
)
from xjtu_calendar.models import ExamSchedule
from xjtu_calendar.parser import ParseReport

SEMESTER = "2026-2027-1"


def _exams(*rows: dict[str, str]) -> list[ExamSchedule]:
    return parse_exam_rows(exam_payload(list(rows)), report=ParseReport())


def _kinds(old: list[ExamSchedule], new: list[ExamSchedule]) -> list[str]:
    return [change.kind for change in diff_exams(old, new).changes]


def test_exam_change_is_frozen_and_carries_no_course_only_fields():
    """冻结 + 没有 weekday/periods/weeks：``SlotChange`` 那几个字段在考试上是不可能的。"""
    change = ExamChange(
        kind="座位变更",
        course_name="示例课程甲",
        exam_name="2029-2030学年 第二学期 结课考试",
        date_str=DEMO_DAY,
        old="12",
        new="30",
    )
    with pytest.raises(FrozenInstanceError):
        change.old = "13"  # type: ignore[misc]
    for impossible in ("weekday", "periods", "weeks"):
        assert not hasattr(change, impossible)


def test_identical_exam_lists_produce_an_empty_diff():
    exams = _exams(exam_row(), exam_row(WID="WID-DEMO-2", KCM="示例课程乙"))
    result = diff_exams(exams, exams)
    assert result.is_empty
    assert result.changes == ()


def test_new_exam_is_reported_as_added_with_its_full_detail():
    changes = diff_exams([], _exams(exam_row(ZWH="12"))).changes
    assert [change.kind for change in changes] == ["新增"]
    assert changes[0].course_name == "示例课程甲"
    assert changes[0].date_str == DEMO_DAY
    # 新增行要能直接拿去打印：日期 / 起止 / 教室 / 座位都在里面
    assert DEMO_DAY in changes[0].new
    assert "15:00-17:30" in changes[0].new
    assert "A-1001" in changes[0].new
    assert "座位 12" in changes[0].new
    assert changes[0].old == ""


def test_vanished_exam_is_reported_as_cancelled():
    changes = diff_exams(_exams(exam_row(ZWH="12")), []).changes
    assert [change.kind for change in changes] == ["取消"]
    assert "座位 12" in changes[0].old
    assert changes[0].new == ""


def test_time_change_pairs_by_wid_and_keeps_the_uid():
    """D5：WID 没变时改期报「时间变更」，而且**同一个 UID**（客户端原地更新，不会多出事件）。

    §11 还没验证的是「改期后服务器换不换 WID」；若换，本用例描述的配对就退化成
    取消 + 新增 —— 那正是 §11 说的「由 diff_exams 的取消小节兜底」。
    """
    old = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 09:00-11:30(星期一)"))
    new = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 14:00-16:30(星期一)"))
    changes = diff_exams(old, new).changes
    assert [change.kind for change in changes] == ["时间变更"]
    assert changes[0].old == f"{DEMO_DAY} 09:00-11:30"
    assert changes[0].new == f"{DEMO_DAY} 14:00-16:30"
    assert make_exam_uid(SEMESTER, old[0]) == make_exam_uid(SEMESTER, new[0])


def test_date_only_change_is_also_a_time_change():
    """改期（日期变、钟点不变）归入「时间变更」：`old`/`new` 都带日期，用户看得见挪了哪天。"""
    old = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 09:00-11:30(星期一)"))
    new = _exams(exam_row(KSRQ="2030-06-20 00:00:00", KSSJMS="2030-06-20 09:00-11:30(星期四)"))
    changes = diff_exams(old, new).changes
    assert [change.kind for change in changes] == ["时间变更"]
    assert changes[0].date_str == "2030-06-20"
    assert changes[0].old == f"{DEMO_DAY} 09:00-11:30"
    assert changes[0].new == "2030-06-20 09:00-11:30"


def test_room_change_is_its_own_kind():
    old = _exams(exam_row())
    new = _exams(exam_row(JASMC="B-2002"))
    changes = diff_exams(old, new).changes
    assert [change.kind for change in changes] == ["教室变更"]
    assert (changes[0].old, changes[0].new) == ("A-1001", "B-2002")
    assert make_exam_uid(SEMESTER, old[0]) == make_exam_uid(SEMESTER, new[0])


def test_room_change_uses_the_campus_map_when_both_sides_share_it():
    """校区名来自同学期课表对照表；比对两侧**共用同一份对照**，所以不会凭空报出变更。"""
    names = {"5": "创新港校区"}
    old = parse_exam_rows(exam_payload([exam_row()]), campus_names=names, report=ParseReport())
    new = parse_exam_rows(
        exam_payload([exam_row(JASMC="B-2002")]), campus_names=names, report=ParseReport()
    )
    changes = diff_exams(old, new).changes
    assert [change.kind for change in changes] == ["教室变更"]
    assert changes[0].old == "创新港校区 A-1001"
    assert changes[0].new == "创新港校区 B-2002"


def test_seat_change_is_its_own_kind():
    old = _exams(exam_row(ZWH="12"))
    new = _exams(exam_row(ZWH="30"))
    changes = diff_exams(old, new).changes
    assert [change.kind for change in changes] == ["座位变更"]
    assert (changes[0].old, changes[0].new) == ("12", "30")


def test_missing_seat_is_rendered_as_unknown_rather_than_empty_string():
    """空 vs 缺失要能显示：`""` 打出来什么都看不见，用户会以为 diff 漏了内容。"""
    old = _exams(exam_row(ZWH="", JASMC=""))
    new = _exams(exam_row(ZWH="12"))
    changes = diff_exams(old, new).changes
    assert sorted(change.kind for change in changes) == ["座位变更", "教室变更"]
    seat = next(change for change in changes if change.kind == "座位变更")
    assert (seat.old, seat.new) == ("无座位号", "12")
    room = next(change for change in changes if change.kind == "教室变更")
    assert (room.old, room.new) == ("地点未知", "A-1001")


def test_every_changed_field_on_the_same_exam_reports_every_applicable_kind():
    """同一场考试同时改期 + 换考场 + 换座位：三类都要报出来，互不吞掉。"""
    old = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 09:00-11:30(星期一)", JASMC="A-1001", ZWH="12"))
    new = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 14:00-16:30(星期一)", JASMC="B-2002", ZWH="30"))
    assert _kinds(old, new) == ["时间变更", "教室变更", "座位变更"]


def test_changes_are_ordered_by_kind_then_date_then_course():
    """输出顺序稳定：新增 → 取消 → 时间 → 教室 → 座位（§6.6 的五类口径）。"""
    old = _exams(
        exam_row(WID="WID-A", KCM="示例课程乙"),
        exam_row(WID="WID-B", KCM="示例课程丙"),
        exam_row(WID="WID-C", KCM="示例课程丁", ZWH="12"),
    )
    new = _exams(
        exam_row(WID="WID-B", KCM="示例课程丙", ZWH="30"),  # 座位变更
        exam_row(WID="WID-D", KCM="示例课程戊"),  # 新增
        exam_row(WID="WID-C", KCM="示例课程丁", ZWH="12", JASMC="B-2002"),  # 教室变更
    )
    assert _kinds(old, new) == ["新增", "取消", "教室变更", "座位变更"]


def test_pairing_falls_back_to_ksrwid_when_wid_is_absent():
    old = _exams(exam_row(WID="", KSRWID="KSRWID-7", ZWH="12"))
    new = _exams(exam_row(WID="", KSRWID="KSRWID-7", ZWH="30"))
    assert _kinds(old, new) == ["座位变更"]


@pytest.mark.parametrize(
    "over",
    [
        {"KSDM": "KSDM-OTHER"},  # 只换批次代码
        {"KSSJMS": f"{DEMO_DAY} 15:00-18:00(星期一)"},  # 只换结束时刻
        {"KSSJMS": f"{DEMO_DAY} 16:00-17:30(星期一)"},  # 只换开始时刻
        {"KSRQ": "2030-06-20 00:00:00", "KSSJMS": f"{DEMO_DAY} 15:00-17:30(星期一)"},  # 只换日期
    ],
    ids=["exam_code", "end_time", "start_time", "date"],
)
def test_composite_fallback_key_carries_the_same_fields_as_the_uid_recipe(over):
    """R-G7：降级配对键的字段集必须与 `make_exam_uid` 的降级配方**完全一致**。

    两侧都没有 WID / KSRWID 时，UID 落到 `KCH|KSDM|KSRQ|开始|结束` 组合；键若少了
    `KSDM` 或结束时刻，diff 会报出一条现实中不存在的「时间变更」——那条变更对应的其实是
    **新 UID**，而 v1 没有 `STATUS:CANCELLED` / `METHOD:CANCEL` 通路（§7.1），旧事件会
    永久留在每个订阅者的日历里。键与 UID 同源之后，这种行只会报成 取消 + 新增。
    """
    old = _exams(exam_row(WID="", KSRWID=""))[0]
    new = _exams(exam_row(WID="", KSRWID="", **over))[0]
    assert make_exam_uid(SEMESTER, old) != make_exam_uid(SEMESTER, new)
    assert sorted(_kinds([old], [new])) == ["取消", "新增"]


def test_composite_fallback_row_without_any_change_pairs_as_unchanged():
    old = _exams(exam_row(WID="", KSRWID=""))
    new = _exams(exam_row(WID="", KSRWID=""))
    assert diff_exams(old, new).is_empty
    assert make_exam_uid(SEMESTER, old[0]) == make_exam_uid(SEMESTER, new[0])


def test_rows_sharing_a_key_are_not_silently_dropped():
    """同一个键出现多行（服务端给了重复 WID，病态但必须可预期）：逐位配对，多出来的报新增/取消。

    用 dict 直接覆盖的话，后面的行会从 diff 里**凭空消失**；而导出侧的全局 UID 断言
    （Task 8）同样会丢弃后来者 —— 这条测试钉住「不静默丢」：三行变一行必须报出两场取消。
    """
    one = _exams(exam_row(ZWH="12"))
    three = [
        exam_row(ZWH="12"),
        exam_row(ZWH="12", KCM="示例课程乙"),
        exam_row(ZWH="12", KCM="示例课程丙"),
    ]
    added = diff_exams(one, _exams(*three)).changes
    assert [change.kind for change in added] == ["新增", "新增"]
    cancelled = diff_exams(_exams(*three), one).changes
    assert [change.kind for change in cancelled] == ["取消", "取消"]


def test_seat_only_permutation_of_a_shared_key_fabricates_no_change():
    """同键两行只是**行序颠倒**（纯座位对调）：排序键含被比较字段 → 自我抵消。

    排序键若只到 ``(date_str, start_time, course_name)``，两侧的 ``[12, 30]`` 与
    ``[30, 12]`` 会被逐位配成两条「座位变更」——那是两条现实中没发生的变更（服务端只是
    换了行序），把 ``_exam_seat`` 加进排序键后两侧都规整成 ``[12, 30]``，配对即相等。
    """
    old = _exams(exam_row(WID="WID-PERM", ZWH="12"), exam_row(WID="WID-PERM", ZWH="30"))
    new = _exams(exam_row(WID="WID-PERM", ZWH="30"), exam_row(WID="WID-PERM", ZWH="12"))
    assert diff_exams(old, new).is_empty


def test_room_only_permutation_of_a_shared_key_fabricates_no_change():
    """同上，换的是**教室**：``_exam_place`` 也必须在排序键里，否则纯对调会造出幻影变更。"""
    old = _exams(
        exam_row(WID="WID-ROOM", JASMC="A-1001"),
        exam_row(WID="WID-ROOM", JASMC="B-2002"),
    )
    new = _exams(
        exam_row(WID="WID-ROOM", JASMC="B-2002"),
        exam_row(WID="WID-ROOM", JASMC="A-1001"),
    )
    assert diff_exams(old, new).is_empty


def test_describe_exam_change_renders_a_single_readable_line():
    added = diff_exams([], _exams(exam_row(ZWH="12"))).changes[0]
    text = describe_exam_change(added)
    assert text.startswith("新增：示例课程甲（结课考试）")
    assert DEMO_DAY in text and "15:00-17:30" in text and "A-1001" in text and "座位 12" in text

    moved = diff_exams(_exams(exam_row(ZWH="12")), _exams(exam_row(ZWH="30"))).changes[0]
    text = describe_exam_change(moved)
    assert text.startswith("座位变更：示例课程甲（结课考试）")
    assert "12" in text and "30" in text and "→" in text
    # 值里不含日期的类别要把日期补在行里，否则同一天多场考试分不清是哪一场
    assert DEMO_DAY in text


# --------------------------------------------------------------------------- #
# C4：降级行走 diff 与走导出，日志级别必须分开
# --------------------------------------------------------------------------- #


def test_diff_of_degraded_rows_logs_debug_not_warning():
    """``_exam_key`` 传 ``announce=False``：降级行在 diff 两侧各命中一次，只留 DEBUG。

    打破红：把 ``exams._exam_key`` 里的 ``announce=False`` 删掉 → diff 改走导出口径的
    ``WARNING``，``assert not warnings`` 立即失败。第二条 ``DEBUG`` 断言证明降级没有
    被完全静音——brief 要求的可诊断性仍在。
    """
    degraded = exam_row(WID="", KSRWID="")  # WID 与 KSRWID 皆缺 → 内容组合降级
    with captured_logs() as records:
        changes = diff_exams(_exams(degraded), _exams(degraded)).changes
    assert changes == ()  # 两侧同一行：纯配对，diff 结果不因日志级别而改变
    warnings = [rec for rec in records if rec.levelname == "WARNING"]
    assert not warnings, [rec.getMessage() for rec in records]
    assert any(rec.levelname == "DEBUG" and "UID 降级" in rec.getMessage() for rec in records)


def test_export_path_still_warns_on_composite_fallback_uid():
    """C4 的另一半：导出侧 ``make_exam_uid`` 仍按 ``WARNING`` 吼——用户必须知道 UID 不稳。

    与上一条用**同一条**降级数据，差别只在走哪条路径（导出 vs diff）。
    """
    exam = _exams(exam_row(WID="", KSRWID=""))[0]
    with captured_logs() as records:
        make_exam_uid(SEMESTER, exam)
    warnings = [rec for rec in records if rec.levelname == "WARNING"]
    assert any("UID 降级" in rec.getMessage() for rec in warnings), [
        rec.getMessage() for rec in records
    ]


def test_export_path_logs_info_on_ksrwid_fallback_uid():
    """C4 三档降级里此前没被钉住的那一档：缺 ``WID``、有 ``KSRWID`` 时导出口径记 ``INFO``。

    ``_uid_token`` 是三档降级（``WID`` → ``KSRWID`` → 内容组合）：第三档已被
    :func:`test_export_path_still_warns_on_composite_fallback_uid` 钉成 ``WARNING``，
    diff 侧那一档由 :func:`test_diff_of_degraded_rows_logs_debug_not_warning` 钉成
    ``DEBUG``，**唯独第二档此前没有任何用例**。这里补上，走的是导出口径
    （:func:`make_exam_uid`，默认 ``announce=True``）。

    级别与文案一起钉：把 ``exams._uid_token`` 那行的 ``logging.INFO`` 改成
    ``logging.DEBUG``（或改成 ``WARNING``），本用例立即红。
    「不许是 WARNING」也是有内容的断言：``KSRWID`` 按场唯一，这一档的 UID 仍然稳定，
    吼成 WARNING 会让用户以为改期会造出新事件——那是第三档才成立的事实。
    """
    exam = _exams(exam_row(WID=""))[0]  # 缺 WID、有 KSRWID → 降到第二档
    assert exam.row_id is None and exam.task_id == "KSRWID-1"
    with captured_logs() as records:
        uid = make_exam_uid(SEMESTER, exam)
    assert uid.endswith("@xjtu-timetable-calendar")
    degraded = [rec for rec in records if "UID 降级" in rec.getMessage()]
    assert len(degraded) == 1, [rec.getMessage() for rec in records]
    assert degraded[0].levelname == "INFO", degraded[0].levelname
    assert degraded[0].getMessage() == "考试「示例课程甲」缺少 WID，UID 降级到 KSRWID"
    assert not [rec for rec in records if rec.levelname in ("WARNING", "ERROR")]
