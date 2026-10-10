"""考试侧共享固件：真实字段形态 + **合成**取值。

固件里的日期是编出来的（2030-*），不是"打码后的真实日期"——设计文档 §4 的样例才用
`YYYY-MM-DD` 占位。原因：`build_exam_events` 要 `date.fromisoformat(...)`，
占位串会把构造层测试全卡死。形态（全角冒号 / em dash / 点分式）照 §4.1 保留。
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from subscribe_support import make_home, payload_row

from xjtu_calendar.config import Settings
from xjtu_calendar.exams import (
    LedgerEntry,
    make_exam_uid,
    parse_exam_rows,
    render_exam_ledger,
)
from xjtu_calendar.fetcher import save_raw
from xjtu_calendar.parser import ParseReport
from xjtu_calendar.schedules import combine

SEMESTER = "2026-2027-1"
#: 合成基准日：2030-06-17 确实是星期一，与 KSSJMS 括号里的星期自洽。
DEMO_DAY = "2030-06-17"
#: 合成 dtstamp：台账与渲染类固件共用（Task 7 之后所有台账固件都用它）。
STAMP = combine(date(2030, 1, 1), "00:00")


@contextlib.contextmanager
def captured_logs(logger_name: str = "xjtu_calendar"):
    """抓 `xjtu_calendar` logger 的记录，**不用 caplog**。

    为什么：`logging_setup.setup_logging` 会把该 logger 的 ``propagate`` 置 False
    （`:111`），而 caplog 的 handler 挂在 root 上——只要同一 pytest 会话里有别的
    用例跑过 `main()`，caplog 就再也抓不到本项目日志了（表现为随机失败）。
    直接给该 logger 挂 handler 与 propagate 无关，永远收得到。
    """
    logger = logging.getLogger(logger_name)
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(handler)


def exam_row(**over: str) -> dict[str, str]:
    """一行考试记录，字段名与真实响应一致，值全部为合成数据。"""
    row = {
        "KCM": "示例课程甲",
        "KCH": "ARCH000000",
        "KSMC": "2029-2030学年 第二学期 结课考试",
        "KSRQ": f"{DEMO_DAY} 00:00:00",
        "KSSJMS": f"{DEMO_DAY} 15:00-17:30(星期一)",
        "JASMC": "A-1001",
        "ZWH": "NN",
        "XXXQDM": "5",
        "XF": "2.0",
        "ZJJSXM": "教师甲",
        "WID": "WID-DEMO-1",
        "KSDM": "KSDM-1",
        "KSRWID": "KSRWID-1",
    }
    row.update(over)
    return row


def timetable_envelope(rows: list[dict[str, str]]) -> dict[str, object]:
    """课表快照的**真实信封**形态（`datas.xskcb.rows`），不是 `{"kbList": ...}` 那种简写。

    `campus_names_from_timetable` 只认真信封；`subscribe_support.make_home` 写的是
    `{"kbList": rows}`（设计文档 §9 点名的固件改造点），所以要测校区对照就得用这个。
    """
    return {"datas": {"xskcb": {"totalSize": len(rows), "rows": list(rows)}}}


def exam_payload(
    rows: list[dict[str, str]], *, code: int = 1, msg: str = "查询成功", module: str = "wdksap"
) -> dict[str, object]:
    """完整响应信封：外层 code 恒为字符串 "0"，成败标志在 extParams.code。"""
    return {
        "code": "0",
        "datas": {
            module: {
                "totalSize": len(rows),
                "pageSize": 999,
                "rows": list(rows),
                "extParams": {"msg": msg, "code": code, "logId": "DEMO"},
            }
        },
    }


def exam_home(tmp_path: Path, *rows: dict[str, str], semester: str = SEMESTER) -> Settings:
    """带考试快照的 home：课表走真信封，考试快照手工落盘。

    `subscribe_support.make_home` 写的是 ``{"kbList": rows}`` 简写信封，
    `campus_names_from_timetable` 不认（设计文档 §9 点名的固件改造点，经裁定不改那个
    文件）。撤销通路的用例需要「课表 + 考试」都在真信封里，故在此提供第二份固件。
    """
    cfg = make_home(tmp_path, semester=semester)
    cfg.raw_timetable_path(semester).write_text(
        json.dumps(timetable_envelope([payload_row()]), ensure_ascii=False), encoding="utf-8"
    )
    save_raw(exam_payload(list(rows) or [exam_row()]), cfg, semester, kind="exams")
    return cfg


def write_ledger(cfg: Settings, semester: str, entries: Sequence[LedgerEntry]) -> Path:
    """把台账条目直接落成文件（writer 由 Task 3 提供，这里不重造）。"""
    path = cfg.exam_ledger_path(semester)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_exam_ledger([], list(entries), dtstamp=STAMP), encoding="utf-8", newline=""
    )
    return path


#: 保留期比较用的墙钟。`exam_row()` 的默认考期是 2030-06-17，所以 `NOW` 之下"时刻未过"
#: 是默认态，`PAST` 用来测过期退场（spec D2/D9）。
NOW = combine(date(2030, 6, 1), "08:00")
PAST = combine(date(2030, 7, 1), "08:00")


def exam_uids(cfg: Settings, *rows: dict[str, str]) -> list[str]:
    """走**真实管线**拿本次会发布的考试 UID。

    不要在测试里重算 sha256：那是把 UID 配方抄第二份，配方一改测试跟着改，看守就废了
    （与 spec 里"diff 的配对键必须委托 `_uid_token`"同一个理由）。
    """
    parsed = parse_exam_rows(
        exam_payload(list(rows) or [exam_row()]),
        campus_names={},
        report=ParseReport(),
    )
    return [make_exam_uid(SEMESTER, exam) for exam in parsed]


def published_ledger(cfg: Settings, *rows: dict[str, str]) -> list[LedgerEntry]:
    """把"上次发布过的考试"抄成台账条目（UID 与起止都取真实管线口径）。"""
    parsed = parse_exam_rows(
        exam_payload(list(rows) or [exam_row()]), campus_names={}, report=ParseReport()
    )
    return [
        LedgerEntry(
            uid=make_exam_uid(SEMESTER, exam),
            start=combine(date.fromisoformat(exam.date_str), exam.start_time),
            end=combine(date.fromisoformat(exam.date_str), exam.end_time),
            summary=f"{exam.course_name}（结课考试）",
            location=f"兴庆 {exam.location}" if exam.location else None,
            description=f"座位号：{exam.seat}" if exam.seat else None,
        )
        for exam in parsed
    ]
