"""日志脱敏的按键打码测试。

README 承诺日志「绝不输出……姓名」，但教师姓名在真实 eHall 响应里
挂在 ``SKJS``（及英文兼容键 ``teacher``）下，早先的键表没有覆盖它们，
``--debug`` 下 ``redact(payload)`` 会把教师姓名原样打进日志。
"""

from __future__ import annotations

from xjtu_calendar.logging_setup import redact


def test_redact_masks_teacher_name_keys() -> None:
    assert redact({"SKJS": "教师甲"}) == {"SKJS": "***"}
    assert redact({"teacher": "教师甲"}) == {"teacher": "***"}
    assert redact({"teacherName": "教师甲"}) == {"teacherName": "***"}


def test_redact_keeps_non_sensitive_fields() -> None:
    """课程名/教室等展示字段不受影响，脱敏范围不无限扩大。"""
    payload = {"KCM": "示例课程甲", "JASMC": "A-1001", "SKXQ": "5"}
    assert redact(payload) == payload
