"""考试安排（`studentWdksapApp`）的解析与导出。设计文档：docs/design/2026-10-08-exam-schedule.md。"""

from __future__ import annotations

import re

#: 日期前缀：`-` / `.` / `/` 三种连接符都见过或可能见到，必须先剥掉，
#: 否则 `2030.06.17` 里的 `30.06` 会被下面那条时刻正则吃掉。
_DATE_PREFIX = re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")

#: 两组「时:分」；分隔符实测见过 ``:``／全角 ``：``／半角点 ``.``／全角点 ``．`` 四种。
_HHMM = re.compile(r"(\d{1,2})[:：.．](\d{2})")


def parse_exam_time_text(text: str | None) -> tuple[str, str] | None:
    """从 ``KSSJMS`` 抓出起止时刻，返回零补齐的 ``("HH:MM", "HH:MM")``。

    抓不出两组合法时刻、或结束不晚于开始时返回 ``None`` —— 调用方**跳过该条**，
    绝不退化成 00:00 的假事件（设计文档 §6.4）。输出交给
    :func:`xjtu_calendar.schedules.combine` 落地成带 ``Asia/Shanghai`` 的 datetime。
    """
    if not text:
        return None
    body = _DATE_PREFIX.sub("", text)
    found: list[str] = []
    for hour_text, minute_text in _HHMM.findall(body):
        hour, minute = int(hour_text), int(minute_text)
        if hour > 23 or minute > 59:
            return None
        found.append(f"{hour:02d}:{minute:02d}")
        if len(found) == 2:
            break
    if len(found) < 2 or found[1] <= found[0]:
        return None
    return found[0], found[1]
