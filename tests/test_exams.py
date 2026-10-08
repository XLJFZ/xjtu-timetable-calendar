from dataclasses import FrozenInstanceError, replace

import pytest
from exam_support import DEMO_DAY, exam_payload, exam_row

from xjtu_calendar.exams import (
    ExamState,
    campus_names_from_timetable,
    classify_exam_payload,
    iter_exam_rows,
    parse_exam_rows,
    parse_exam_time_text,
)
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
