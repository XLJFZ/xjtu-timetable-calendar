"""提取守卫：build_ics_for_semester 与 CLI export 产物字节一致（DTSTAMP 归一）；
外加发布幂等性的核心回归——同一快照两次独立构建必须字节一致（不钉时钟）。"""

from __future__ import annotations

import re
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar.cli import main
from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.timeutil import TZ_XIAN

_DTSTAMP = re.compile(r"^DTSTAMP:.*$", re.MULTILINE)

#: 固定导出时刻：CLI 与函数两次渲染的 DTSTAMP/LAST-MODIFIED 必须同源，
#: 否则「字节一致」断言会因秒级时钟抖动随机失败。
_FIXED_STAMP = datetime(2026, 10, 7, 12, 0, 0, tzinfo=TZ_XIAN)


def _normalize(ics: str) -> str:
    return _DTSTAMP.sub("DTSTAMP:<fixed>", ics)


def test_build_ics_matches_cli_export_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("xjtu_calendar.exporter.now_local", lambda: _FIXED_STAMP)
    home = make_home(tmp_path / "home")
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(home.home))
    out = tmp_path / "via-cli.ics"
    assert main(["export", "--semester", SEMESTER, "-o", str(out)]) == 0
    # 读回走 bytes→decode：关掉通用换行翻译保住 CRLF 完整性（同 cmd_export 的
    # write_text(newline="")），且兼容 CI 矩阵的 3.11/3.12——read_text 的
    # newline 参数是 3.13 才有的。
    via_cli = _normalize(out.read_bytes().decode("utf-8"))

    result = build_ics_for_semester(home, SEMESTER)
    assert _normalize(result.ics) == via_cli
    # Step 2b 实测值：周一 1-2 周、第 1~2 节连排 → 每周一个事件，共 2 个。
    assert result.info["events"] == 2


def test_build_ics_is_reproducible_without_clock_pinning(tmp_path: Path) -> None:
    """I3 回归：同一快照两次独立构建必须字节一致——不 monkeypatch 时钟。

    DTSTAMP 若退回 ``now_local()``，中间隔一秒的重建就会让 subscribe publish
    的内容哈希永远对不上（「内容无变化→跳过」形同虚设）。这里故意让挂钟走过
    一秒再构建第二次：修复后仍字节一致（stamp=快照 mtime），回退则必 RED。
    """
    home = make_home(tmp_path / "home")
    first = build_ics_for_semester(home, SEMESTER)
    time.sleep(1.05)
    second = build_ics_for_semester(home, SEMESTER)
    assert first.ics == second.ics

    # stamp 来源钉死为快照 mtime（UTC），而不是任何「恰好同秒」的挂钟巧合。
    mtime = datetime.fromtimestamp(home.raw_timetable_path(SEMESTER).stat().st_mtime, tz=UTC)
    assert f"DTSTAMP:{mtime:%Y%m%dT%H%M%SZ}" in first.ics
