from dataclasses import FrozenInstanceError, replace

import pytest
from exam_support import DEMO_DAY, exam_payload, exam_row

from xjtu_calendar.exams import parse_exam_time_text
from xjtu_calendar.models import ExamSchedule


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
