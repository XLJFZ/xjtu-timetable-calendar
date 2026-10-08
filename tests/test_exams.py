from dataclasses import FrozenInstanceError, replace

import pytest
from exam_support import DEMO_DAY, exam_payload, exam_row

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
