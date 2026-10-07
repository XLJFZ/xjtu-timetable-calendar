"""提取守卫：build_ics_for_semester 与 CLI export 产物字节一致（DTSTAMP 归一）。"""

from __future__ import annotations

import re
from datetime import datetime
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
    # newline=""：关掉通用换行翻译，否则读回的 CRLF 被转成 LF，
    # 「字节一致」守卫就名存实亡了。
    via_cli = _normalize(out.read_text(encoding="utf-8", newline=""))

    result = build_ics_for_semester(home, SEMESTER)
    assert _normalize(result.ics) == via_cli
    # Step 2b 实测值：周一 1-2 周、第 1~2 节连排 → 每周一个事件，共 2 个。
    assert result.info["events"] == 2
