"""考试侧共享固件：真实字段形态 + **合成**取值。

固件里的日期是编出来的（2030-*），不是"打码后的真实日期"——设计文档 §4 的样例才用
`YYYY-MM-DD` 占位。原因：`build_exam_events` 要 `date.fromisoformat(...)`，
占位串会把构造层测试全卡死。形态（全角冒号 / em dash / 点分式）照 §4.1 保留。
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date

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
