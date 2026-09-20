"""时区常量与时间工具。

.. important:: **本项目不处理 DST。**

中国全境（含西安）自 1991 年起不再实行夏令时，全年恒为 UTC+8。
学校的冬/夏季作息调整 **不是** DST，而是「第 N 节课对应的钟点不同」，
属于作息规则问题，由 :mod:`xjtu_calendar.schedules` 处理。

因此：

- 时区永远、唯一地使用 ``Asia/Shanghai``；
- 不读系统时区（用户可能在东京或纽约运行本程序）；
- 不使用 ``pytz``，只用标准库 ``zoneinfo``。
"""

from __future__ import annotations

from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

__all__ = ["TZ_NAME", "TZ_XIAN", "ensure_tz", "now_local"]

TZ_NAME = "Asia/Shanghai"

#: 项目使用的唯一时区。校园位于西安，事件时间语义固定属于 Asia/Shanghai。
try:
    TZ_XIAN: tzinfo = ZoneInfo(TZ_NAME)
except ZoneInfoNotFoundError:  # pragma: no cover - 仅在缺少 tzdata 的 Windows 上触发
    from datetime import timedelta, timezone

    # 末路兜底：Asia/Shanghai 恒为 UTC+8 且无 DST，语义完全等价。
    # 优先仍建议安装 tzdata 以获得完整的 IANA 数据。
    TZ_XIAN = timezone(timedelta(hours=8), name=TZ_NAME)


def ensure_tz(moment: datetime) -> datetime:
    """把 naive datetime 视为 ``Asia/Shanghai``，已带时区的原样返回。"""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=TZ_XIAN)
    return moment


def now_local() -> datetime:
    """当前时刻（带 ``Asia/Shanghai`` 时区）。"""
    return datetime.now(tz=TZ_XIAN)
