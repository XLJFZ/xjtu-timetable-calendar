"""作息表解析与「节次 -> 实际时间」解析器。

这是本项目第二重要的设计（仅次于 :class:`~xjtu_calendar.models.CourseMeeting`）。

关键概念澄清
------------
西安交通大学的冬/夏季作息调整 **不是时区 DST**：

- 时区恒为 ``Asia/Shanghai``，全年不变；
- 变的是「第 N 节课对应的钟点」，例如夏季与冬季的第 1 节起始时间不同。

因此本项目把两者彻底解耦：

    时区        -> 恒为 Asia/Shanghai（见 exporter）
    节次钟点    -> 由 ScheduleProfile 提供
    用哪套作息  -> 由 date 落在哪个 SchedulePeriod 决定

配置来源
--------
本项目**不内置任何未经官方来源确认的作息时间**。作息表完全来自配置文件
（见 ``examples/schedule.example.json``）。若某日期无匹配区间，
抛出 :class:`~xjtu_calendar.errors.ScheduleNotConfigured`，绝不猜测。
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .errors import ParseError, ScheduleNotConfigured
from .models import (
    SchedulePeriod,
    ScheduleProfile,
    _validate_hhmm,
)

__all__ = ["ScheduleTable", "resolve_period_time"]


@dataclass
class ScheduleTable:
    """作息表 = 若干套 :class:`ScheduleProfile` + 各套生效的日期区间。

    Attributes
    ----------
    profiles:
        ``profile_key -> ScheduleProfile``。
    periods:
        生效区间列表。区间之间**不应重叠**；重叠时取先匹配到的那个，
        并在 :meth:`validate` 中报告。
    """

    profiles: dict[str, ScheduleProfile] = field(default_factory=dict)
    periods: list[SchedulePeriod] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # 查询
    # ------------------------------------------------------------------ #
    def resolve_schedule_profile(self, day: date) -> ScheduleProfile:
        """返回该日期生效的作息表。

        Raises
        ------
        ScheduleNotConfigured
            没有任何区间覆盖该日期，或区间指向的作息表未定义。
        """
        for period in self.periods:
            if period.contains(day):
                profile = self.profiles.get(period.profile)
                if profile is None:
                    raise ScheduleNotConfigured(
                        f"日期 {day.isoformat()} 落在作息区间 {period.start.isoformat()}"
                        f"~{period.end.isoformat()}（profile={period.profile!r}），"
                        f"但该作息表未在配置中定义"
                    )
                return profile

        raise ScheduleNotConfigured(
            f"日期 {day.isoformat()} 没有匹配到任何作息区间，"
            f"请在作息表配置中补充覆盖该日期的 periods 项"
        )

    def resolve_period_time(
        self,
        day: date,
        periods: Iterable[int],
    ) -> tuple[datetime, datetime]:
        """把「某个日期 + 若干节次」解析成具体的起止时刻。

        多节连排（如 ``[1, 2]``）时，开始时间取第一节课的开始，
        结束时间取最后一节课的结束，中间的空档（课间）包含在内。

        Parameters
        ----------
        day:
            上课日期。
        periods:
            节次序号，例如 ``[5, 6]``。

        Returns
        -------
        tuple[datetime, datetime]
            带 ``Asia/Shanghai`` 时区信息的起止时刻。

        Raises
        ------
        ScheduleNotConfigured
            该日期无作息区间，或所需节次在该作息表中未定义。
        """
        ordered = sorted(set(periods))
        if not ordered:
            raise ValueError("periods 不能为空")

        profile = self.resolve_schedule_profile(day)

        missing = profile.missing_periods(ordered)
        if missing:
            raise ScheduleNotConfigured(
                f"{profile.name}（profile={profile.key!r}）中未定义节次 "
                f"{missing}，无法解析 {day.isoformat()} 的上课时间"
            )

        start_hhmm = profile.time_of(ordered[0])[0]
        end_hhmm = profile.time_of(ordered[-1])[1]

        return (
            combine(day, start_hhmm),
            combine(day, end_hhmm),
        )

    # ------------------------------------------------------------------ #
    # 校验 / 序列化
    # ------------------------------------------------------------------ #
    def validate(self) -> list[str]:
        """检查配置自身的一致性，返回问题描述列表（空列表表示无问题）。"""
        problems: list[str] = []

        ordered = sorted(self.periods, key=lambda p: p.start)
        # pairwise 比 zip(ordered, ordered[1:]) 更直白：后者会额外复制一份列表切片，
        # 且需要读者自己确认「相邻两两」的语义。行为完全等价。
        for prev, curr in itertools.pairwise(ordered):
            if curr.start <= prev.end:
                problems.append(
                    f"作息区间重叠：{prev.start.isoformat()}~{prev.end.isoformat()}"
                    f"（{prev.profile}）与 {curr.start.isoformat()}~{curr.end.isoformat()}"
                    f"（{curr.profile}）"
                )

        for period in self.periods:
            if period.profile not in self.profiles:
                problems.append(
                    f"作息区间 {period.start.isoformat()}~{period.end.isoformat()} "
                    f"引用了未定义的作息表 {period.profile!r}"
                )

        for key, profile in self.profiles.items():
            if not profile.periods:
                problems.append(f"作息表 {key!r} 没有任何节次时间定义")

        return problems

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ScheduleTable:
        """从字典构造。

        期望结构（与 ``examples/schedule.example.json`` 一致）::

            {
              "profiles": {
                "summer": {
                  "name": "夏季作息",
                  "periods": {"1": ["08:00", "08:50"], "2": ["09:00", "09:50"]}
                },
                "winter": {"name": "冬季作息", "periods": {...}}
              },
              "periods": [
                {"start": "2026-09-07", "end": "2026-10-11", "profile": "summer"},
                {"start": "2026-10-12", "end": "2027-01-10", "profile": "winter"}
              ]
            }
        """
        raw_profiles = payload.get("profiles") or {}
        if not isinstance(raw_profiles, dict):
            raise ParseError("作息表配置的 'profiles' 必须是对象")

        profiles: dict[str, ScheduleProfile] = {}
        for key, raw_profile in raw_profiles.items():
            raw_profile = raw_profile or {}
            raw_periods = raw_profile.get("periods")
            if not isinstance(raw_periods, dict):
                raise ParseError(f"作息表 {key!r} 缺少 'periods' 对象")

            normalized: dict[int, tuple[str, str]] = {}
            for idx, value in raw_periods.items():
                start, end = _coerce_period_entry(idx, value, key)
                normalized[int(idx)] = (start, end)

            profiles[str(key)] = ScheduleProfile(
                key=str(key),
                name=str(raw_profile.get("name") or key),
                periods=normalized,
            )

        periods: list[SchedulePeriod] = []
        for item in payload.get("periods") or []:
            if not isinstance(item, dict):
                raise ParseError("作息表 'periods' 列表中的每项必须是对象")
            try:
                periods.append(
                    SchedulePeriod(
                        start=_coerce_date(item["start"], "periods[].start"),
                        end=_coerce_date(item["end"], "periods[].end"),
                        profile=str(item["profile"]),
                    )
                )
            except KeyError as exc:
                raise ParseError(f"作息区间缺少字段：{exc}") from exc

        return cls(profiles=profiles, periods=periods)

    @classmethod
    def from_file(cls, path: str | Path) -> ScheduleTable:
        """从 JSON 文件加载作息表。"""
        file_path = Path(path)
        if not file_path.is_file():
            raise ScheduleNotConfigured(f"作息表文件不存在：{file_path}")
        try:
            payload = json.loads(file_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ParseError(f"作息表不是合法 JSON：{file_path}（{exc}）") from exc
        return cls.from_dict(payload)


def resolve_period_time(
    day: date,
    periods: Iterable[int],
    table: ScheduleTable,
) -> tuple[datetime, datetime]:
    """便捷函数：等价于 ``table.resolve_period_time(day, periods)``。"""
    return table.resolve_period_time(day, periods)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def combine(day: date, hhmm: str) -> datetime:
    """把日期与 ``"HH:MM"`` 拼成**带 Asia/Shanghai 时区**的 datetime。

    .. important::
        这里刻意不接收 naive datetime，也不读系统时区。课程的物理发生地
        在西安，事件时间语义固定属于 ``Asia/Shanghai``。
    """
    from .timeutil import TZ_XIAN

    _validate_hhmm(hhmm, "hhmm")
    hour, minute = (int(x) for x in hhmm.strip().split(":"))
    return datetime.combine(day, time(hour=hour, minute=minute), tzinfo=TZ_XIAN)


def _coerce_period_entry(idx: Any, value: Any, profile_key: str) -> tuple[str, str]:
    """解析 ``{"1": ["08:00", "08:50"]}`` 或 ``{"1": {"start": ..., "end": ...}}``。"""
    if isinstance(value, dict):
        start, end = value.get("start"), value.get("end")
    elif isinstance(value, (list, tuple)) and len(value) == 2:
        start, end = value
    else:
        raise ParseError(
            f"作息表 {profile_key!r} 第 {idx} 节的时间格式无效，"
            f"期望 [\"HH:MM\", \"HH:MM\"] 或 {{\"start\": ..., \"end\": ...}}"
        )

    if not isinstance(start, str) or not isinstance(end, str):
        raise ParseError(f"作息表 {profile_key!r} 第 {idx} 节的起止时间必须是字符串")
    return start.strip(), end.strip()


def _coerce_date(value: Any, label: str) -> date:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError as exc:
            raise ParseError(f"{label} 不是合法 ISO 日期：{value!r}") from exc
    raise ParseError(f"{label} 期望 ISO 日期字符串，实际为 {type(value).__name__}")
