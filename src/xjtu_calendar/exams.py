"""考试安排（`studentWdksapApp`）的解析与导出。设计文档：docs/design/2026-10-08-exam-schedule.md。"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from typing import Any

from .exporter import UID_DOMAIN  # noqa: F401  （T5 用；顶层导入，exporter 反向只在函数内 import）
from .models import ExamSchedule
from .parser import ParseReport

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


#: 考试行的字段候选表。**不复用 parser.FIELD_CANDIDATES**：那张表按课程写
#: （teacher 认 SKJS，考试行是 ZJJSXM），且报错文案写死「课程」。
EXAM_FIELDS: dict[str, tuple[str, ...]] = {
    "course_id": ("KCH", "courseId"),
    "course_name": ("KCM", "courseName"),
    "exam_name": ("KSMC",),
    "date": ("KSRQ",),
    "time_text": ("KSSJMS", "KSSJ"),
    "location": ("JASMC",),
    "campus_code": ("XXXQDM",),
    "seat": ("ZWH", "KSWZ"),
    "credits": ("XF",),
    "teacher": ("ZJJSXM",),
    "row_id": ("WID",),
    "task_id": ("KSRWID",),
    "exam_code": ("KSDM",),
}


def _text(record: Mapping[str, Any], key: str) -> str:
    for candidate in EXAM_FIELDS[key]:
        value = record.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def iter_exam_rows(payload: Any) -> list[dict[str, Any]]:
    """按结构找出考试行；**不硬编码模块名**，也不看 rows 是否为空（空 rows 交给三态判定）。"""
    datas = payload.get("datas") if isinstance(payload, Mapping) else None
    if not isinstance(datas, Mapping):
        return []
    for module in datas.values():
        if isinstance(module, Mapping) and isinstance(module.get("rows"), list):
            # `isinstance(row, dict)` 而非 `Mapping`：真实 JSON 行必是 dict，
            # 也让返回类型 `list[dict[str, Any]]` 在 mypy strict 下成立。
            return [row for row in module["rows"] if isinstance(row, dict)]
    return []


def campus_names_from_timetable(timetable_payload: Any) -> dict[str, str]:
    """同学期课表快照里的 ``XXXQDM -> XXXQDM_DISPLAY`` 对照（考试行没有 DISPLAY 字段）。"""
    out: dict[str, str] = {}
    for row in iter_exam_rows(timetable_payload):
        code, name = row.get("XXXQDM"), row.get("XXXQDM_DISPLAY")
        if isinstance(code, str) and isinstance(name, str) and code and name:
            out[code] = name
    return out


def parse_exam_rows(
    payload: Any,
    *,
    campus_names: Mapping[str, str] | None = None,
    report: ParseReport | None = None,
) -> list[ExamSchedule]:
    """把考试行转成 :class:`ExamSchedule`。不可用的行跳过并计入 ``report``，绝不造 00:00 假事件。"""
    rep = report or ParseReport()
    out: list[ExamSchedule] = []
    for row in iter_exam_rows(payload):
        rep.total_candidates += 1
        name = _text(row, "course_name")
        raw_date = _text(row, "date")
        pair = parse_exam_time_text(_text(row, "time_text"))
        if not name:
            rep.skip("考试行缺少课程名（KCM），已跳过")
            continue
        if pair is None:
            rep.skip(f"考试「{name}」的时间描述无法解析：{_text(row, 'time_text')!r}")
            continue
        if len(raw_date) < 10:
            rep.skip(f"考试「{name}」缺少考试日期（KSRQ），已跳过")
            continue
        day = raw_date[:10]
        _check_weekday_consistency(rep, name, day, _text(row, "time_text"))
        code = _text(row, "campus_code")
        credits_raw = _text(row, "credits")
        out.append(
            ExamSchedule(
                course_id=_text(row, "course_id") or None,
                course_name=name,
                exam_name=_text(row, "exam_name") or None,
                date_str=day,
                start_time=pair[0],
                end_time=pair[1],
                location=_text(row, "location") or None,
                campus=(campus_names or {}).get(code) if code else None,
                seat=_text(row, "seat") or None,
                credits=float(credits_raw) if credits_raw.replace(".", "", 1).isdigit() else None,
                teacher=_text(row, "teacher") or None,
                row_id=_text(row, "row_id") or None,
                task_id=_text(row, "task_id") or None,
                exam_code=_text(row, "exam_code") or None,
            )
        )
        rep.parsed += 1
    return out


#: `KSSJMS` 括号里的星期后缀，实测形态：`(星期二)`。
_WEEKDAY_IN_TEXT = re.compile(r"星期([一二三四五六日天])")
_WEEKDAY_CHARS = "一二三四五六日"  # 下标 0 = 星期一，与 date.isoweekday() 对齐


def _check_weekday_consistency(rep: ParseReport, name: str, day: str, time_text: str) -> None:
    """`KSRQ` 推出来的星期 vs `KSSJMS(星期X)`：不一致只 warning，**不丢行**。

    §4.1 的 22 行真样本逐行核对过，两处**全部一致**；这条是防御性的（两个独立字段，
    谁先腐化不可知）。取舍方向：以 `KSRQ` 为准（客户端显示的就是它推出来的那天），
    丢掉整行等于把一次真实考试信息抹掉，比一条噪音严重得多。
    """
    match = _WEEKDAY_IN_TEXT.search(time_text)
    if match is None:
        return
    try:
        actual = date.fromisoformat(day)
    except ValueError:
        return  # 日期本身有问题，交给下游 build_exam_events 的跳过分支
    claimed = _WEEKDAY_CHARS.index(match.group(1).replace("天", "日"))
    if claimed != actual.isoweekday() - 1:
        rep.warn(
            f"考试「{name}」的时间文本写的是星期{match.group(1)}，"
            f"但考试日期 {day} 是星期{actual.isoweekday()}；按日期为准"
        )
