from dataclasses import FrozenInstanceError, replace
from datetime import timedelta

import pytest
from exam_support import DEMO_DAY, captured_logs, exam_payload, exam_row

from xjtu_calendar.exams import (
    ExamState,
    build_exam_events,
    campus_names_from_timetable,
    classify_exam_payload,
    iter_exam_rows,
    make_exam_uid,
    parse_exam_rows,
    parse_exam_time_text,
)
from xjtu_calendar.exporter import render_ics
from xjtu_calendar.models import ExamSchedule
from xjtu_calendar.parser import ParseReport


def test_exam_row_has_the_observed_dirty_shapes():
    """固件必须保留实测形态，不许"洗干净"。"""
    rows = [
        exam_row(KSRQ="2030-06-18 00:00:00", KSSJMS="2030-06-18 15:00-17:30(星期二)"),
        exam_row(
            KSRQ="2030-06-20 00:00:00", KSSJMS="2030-06-20 9：00-11：30(星期四)"
        ),  # 全角+个位小时
        exam_row(KSRQ="2030-06-23 00:00:00", KSSJMS="2030-06-23 15:00—17:30(星期日)"),  # em dash
        exam_row(
            KSRQ="2030-06-23 00:00:00", KSSJMS="2030-06-23 考试时间为：9.30-12.00(星期日)"
        ),  # 点分式
    ]
    payload = exam_payload(rows)
    module = payload["datas"]["wdksap"]
    assert module["rows"] == rows
    assert module["extParams"] == {"msg": "查询成功", "code": 1, "logId": "DEMO"}
    assert payload["code"] == "0"  # 外层恒为字符串 "0"，与成败无关


def test_exam_schedule_is_frozen_and_rejects_end_before_start():
    exam = ExamSchedule(
        course_id="ARCH000000",
        course_name="示例课程甲",
        exam_name="结课考试",
        date_str=DEMO_DAY,
        start_time="09:00",
        end_time="11:30",
        location="A-1001",
        campus=None,
        seat="NN",
        credits=2.0,
        teacher="教师甲",
        row_id="WID-DEMO-1",
        task_id=None,
        exam_code="KSDM-1",
    )
    assert exam.course_name == "示例课程甲"
    with pytest.raises(FrozenInstanceError):
        exam.course_name = "改名"  # type: ignore[misc]
    with pytest.raises(ValueError):
        replace(exam, end_time="08:00")
    with pytest.raises(ValueError):
        replace(exam, end_time="9:00")  # 非零补齐但仍是合法时刻，须能参与比较
    assert replace(exam, start_time="9:00").start_time == "09:00"  # 归一化后再比较


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2030-06-17 15:00-17:30(星期一)", ("15:00", "17:30")),  # 基准：半角 + '-'
        ("2030-01-08 9：00-11：30(星期二)", ("09:00", "11:30")),  # 全角 + 个位小时
        ("2030-06-16 15:00—17:30(星期日)", ("15:00", "17:30")),  # em dash 连接
        ("2030-12-15 16:00—18:00(星期日)", ("16:00", "18:00")),
        ("2030-06-20 考试时间为：9.30-12.00(星期四)", ("09:30", "12:00")),  # 自由文本 + 点分
        ("15:00-17:00", ("15:00", "17:00")),  # 无日期前缀
    ],
)
def test_parse_exam_time_text_accepts_observed_shapes(text, expected):
    assert parse_exam_time_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "待定",  # 没有任何时刻
        "15:00",  # 只有一个时刻：起止无法确定
        "2030-01-08 25:00-26:30",  # 非法小时
        "2030-01-08 09:99-11:00",  # 非法分钟
        "2030-01-08 11:30-09:00",  # 结束早于开始
        "2030-01-08 09:00-09:00",  # 相等：CalendarEvent 会硬拒
    ],
)
def test_parse_exam_time_text_returns_none_on_unusable_text(text):
    assert parse_exam_time_text(text) is None


def test_date_prefix_is_stripped_before_matching():
    """日期里的 `.` / `-` 不能被当成时刻分隔符（`2030.06.17` 会被误读成 `30.06`）。"""
    assert parse_exam_time_text("2030.06.17 09:00-11:00(星期一)") == ("09:00", "11:00")
    assert parse_exam_time_text("2030-06-17 09:00-11:00") == ("09:00", "11:00")


def test_iter_exam_rows_finds_module_without_hardcoding_name():
    payload = exam_payload([exam_row()], module="someOtherModule")
    payload["datas"] = {"whatever": payload["datas"].pop("someOtherModule")}
    assert len(iter_exam_rows(payload)) == 1


def test_iter_exam_rows_keeps_empty_list_but_rejects_shape():
    assert iter_exam_rows(exam_payload([])) == []  # 空 rows 是合法结构，交给三态判定
    assert iter_exam_rows({"code": "0"}) == []


def test_parse_exam_rows_maps_fields_and_skips_unparsable():
    rows = [
        exam_row(),
        exam_row(WID="WID-DEMO-2", KSSJMS="待定"),  # 解析不出 → 跳过
        exam_row(WID="WID-DEMO-3", KCM=""),  # 缺课程名 → 跳过
    ]
    report = ParseReport()
    exams = parse_exam_rows(exam_payload(rows), report=report)
    assert [e.row_id for e in exams] == ["WID-DEMO-1"]
    assert len(report.skipped) == 2


def test_campus_names_from_timetable_builds_code_to_display_map():
    timetable = {
        "datas": {
            "xskcb": {
                "rows": [
                    {"XXXQDM": "5", "XXXQDM_DISPLAY": "创新港校区"},
                    {"XXXQDM": "1", "XXXQDM_DISPLAY": "兴庆校区"},
                    {"XXXQDM": "5", "XXXQDM_DISPLAY": "创新港校区"},
                ]
            }
        }
    }
    assert campus_names_from_timetable(timetable) == {"5": "创新港校区", "1": "兴庆校区"}


def test_parse_exam_rows_uses_campus_map_and_falls_back_silently():
    exam = parse_exam_rows(exam_payload([exam_row(XXXQDM="5")]), campus_names={"5": "创新港校区"})[
        0
    ]
    assert exam.campus == "创新港校区"
    unknown = parse_exam_rows(exam_payload([exam_row(XXXQDM="9")]), campus_names={})[0]
    assert unknown.campus is None  # 对照不到就留空，绝不硬编码代码表


def test_weekday_in_time_text_conflicts_with_date_warns_but_keeps_row():
    """§6.4 的**防御性**检查：22 行实测样本里两处星期全一致，没见过反例。

    仍然要检查，因为 `KSRQ` 与 `KSSJMS(星期X)` 是两个独立字段，谁先腐化不可知，
    而客户端显示的是 `KSRQ` 推出来的那个 —— 所以**以 KSRQ 为准**，只记 warning，
    不丢行（丢了才是真的把考试信息弄没了）。
    """
    report = ParseReport()
    # 2030-06-17 是星期一，文本却写(星期二)
    rows = [exam_row(KSSJMS="2030-06-17 15:00-17:30(星期二)")]
    exams = parse_exam_rows(exam_payload(rows), report=report)
    assert len(exams) == 1  # 保留
    assert exams[0].date_str == "2030-06-17"  # 以 KSRQ 为准
    assert any("星期" in w for w in report.warnings)


def test_weekday_consistent_produces_no_warning():
    report = ParseReport()
    parse_exam_rows(exam_payload([exam_row()]), report=report)  # DEMO_DAY=星期一，固件自洽
    assert report.warnings == []


def test_state_has_exams():
    out = classify_exam_payload(exam_payload([exam_row()]))
    assert out.state is ExamState.HAS_EXAMS
    assert len(out.rows) == 1


def test_state_no_exams_is_distinct_from_unknown():
    """code==1 + 空 rows：真实存在但主接口尚未观测到，判据得留得住。"""
    out = classify_exam_payload(exam_payload([], code=1, msg="操作成功"))
    assert out.state is ExamState.NO_EXAMS
    assert out.rows == ()  # rows 是 tuple，不是 list


def test_state_unknown_when_query_failed():
    """实测：本学期未排考 → code 0 / 查询失败。归入未知，**不许**覆盖快照。"""
    out = classify_exam_payload(exam_payload([], code=0, msg="查询失败"))
    assert out.state is ExamState.UNKNOWN
    assert out.msg == "查询失败"


def test_state_unknown_when_envelope_missing_or_string_code():
    assert classify_exam_payload({}).state is ExamState.UNKNOWN
    assert classify_exam_payload({"code": "0"}).state is ExamState.UNKNOWN
    assert classify_exam_payload(exam_payload([exam_row()], code="1")).state is ExamState.UNKNOWN


def test_state_unknown_when_module_missing():
    assert classify_exam_payload({"code": "0", "datas": {}}).state is ExamState.UNKNOWN


def _exams(*rows):
    return parse_exam_rows(exam_payload(list(rows)), report=ParseReport())


def test_uid_prefers_wid_and_survives_time_or_room_change():
    a = _exams(exam_row())[0]
    moved = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 09:00-11:00(星期一)", JASMC="B-2002"))[0]
    assert make_exam_uid("2026-2027-1", a) == make_exam_uid("2026-2027-1", moved)
    assert make_exam_uid("2026-2027-1", a) != make_exam_uid("2025-2026-2", a)


def test_uid_falls_back_to_task_id_then_composite():
    by_task = _exams(exam_row(WID="", KSRWID="KSRWID-9"))[0]
    assert make_exam_uid("2026-2027-1", by_task).endswith("@xjtu-timetable-calendar")

    # 实测过的形态：同一门课同一天两场（上午 + 晚场）
    same_day_two_sessions = [
        exam_row(
            WID="", KSRWID="", KSRQ="2030-01-05 00:00:00", KSSJMS="2030-01-05 09:00-11:30(星期六)"
        ),
        exam_row(
            WID="", KSRWID="", KSRQ="2030-01-05 00:00:00", KSSJMS="2030-01-05 19:00-21:30(星期六)"
        ),
    ]
    uids = {make_exam_uid("2026-2027-1", e) for e in _exams(*same_day_two_sessions)}
    assert len(uids) == 2  # 降级键含起止时刻，同日两场不撞车


def test_exam_events_are_datetime_never_value_date():
    """红线：VALUE=DATE 会被 sequence.parse_baseline 静默丢弃 → SEQUENCE 永远归零。"""
    event = build_exam_events(_exams(exam_row()), "2026-2027-1")[0]
    assert event.start.utcoffset() == timedelta(hours=8)
    assert event.start.tzinfo is not None
    assert event.meeting is None  # 考试不冒充课程会议


def test_rendered_ics_uses_datetime_for_exams():
    text = render_ics(build_exam_events(_exams(exam_row()), "2026-2027-1"))
    assert "DTSTART;TZID=Asia/Shanghai:" in text
    assert "DTSTART;VALUE=DATE:" not in text
    assert "BEGIN:VALARM" not in text  # D3：不写提醒
    assert "RRULE:" not in text  # 单场事件，绝不周期化


def test_exam_event_summary_location_description_and_fields():
    exam = _exams(exam_row(ZJJSXM="教师甲", ZWH="NN", XF="3.0"))[0]
    event = build_exam_events([exam], "2026-2027-1")[0]
    assert event.summary == "示例课程甲（结课考试）"
    assert event.location == "A-1001"  # 没给校区对照表 -> 只有 JASMC，不硬编码校区
    assert "座位号：NN" in event.description
    assert "教师甲" in event.description
    assert "3.0" in event.description


def test_unparsable_exam_never_becomes_a_zero_oclock_event():
    assert build_exam_events(_exams(exam_row(KSSJMS="待定")), "2026-2027-1") == []


def test_exam_event_survives_baseline_roundtrip():
    """parse_baseline 会静默丢弃非 datetime 事件；被丢弃 = 每次重发布 SEQUENCE 恒为 0。"""
    from xjtu_calendar.sequence import parse_baseline

    text = render_ics(build_exam_events(_exams(exam_row()), "2026-2027-1"))
    baselines = parse_baseline(text)
    exam_uid = make_exam_uid("2026-2027-1", _exams(exam_row())[0])
    assert exam_uid in baselines  # 进不了基线的事件，客户端永远不会收到更新


def test_broken_date_reaching_build_is_dropped_with_warning():
    """`KSRQ="2030-13-45 ..."` 超过 10 字符、熬过 Task 3 的长度检查直达 `date_str`；
    build_exam_events 必须**丢弃并 warning**，既不许静默跳过，也不许让
    `date.fromisoformat` 的 ValueError 炸穿到导出层。"""
    exam = _exams(exam_row(KSRQ="2030-13-45 00:00:00", KSSJMS="2030-13-45 09:00-11:30"))[0]
    assert exam.date_str == "2030-13-45"  # 前提确认：解析层确实放行了
    with captured_logs() as records:
        assert build_exam_events([exam], "2026-2027-1") == []
    assert any("无法解析" in r.getMessage() and r.levelname == "WARNING" for r in records)
