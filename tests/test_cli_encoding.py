"""CLI 输出流编码护栏：非 UTF-8 控制台/管道下含 emoji 的行不得崩溃。

回归场景（2026-10-07 本地端到端预演抓到的真 bug）：中文 Windows 上
``subscribe rotate > log.txt`` 这类重定向让 stdout 回落到 locale 编码（cp936），
print("⚠️ …") 抛 UnicodeEncodeError——rotate 因此**中断在「token 已换、尚未
补发」的中间态**（spec §7 最危险的状态），且用户只看到一条未预期异常。

锁定两条行为（对应 :func:`xjtu_calendar.cli._harden_output_streams` 的两个分支）：
1. 非 TTY（重定向/管道）：统一按 UTF-8 落盘，警示行完整可读；
2. GBK 控制台（TTY）：保持原编码，不可编码字符降级为 ``?``——命令照常跑完，
   绝不把 UnicodeEncodeError 抛给用户。
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar.cli import main

URL_BASE = "https://me.github.io/timetable/"


class _FakeStream(io.TextIOWrapper):
    """伪装宿主 stream：``isatty()`` 由构造参数定死，编码保持传入值。

    ``encoding`` 不覆写——TextIOWrapper 的属性本就可写，正是要让
    ``reconfigure(encoding=...)`` 自然生效；覆写 isatty 是为了绕开
    pytest capture 流对 isatty 的 ``ValueError`` 实现。
    """

    def __init__(self, buffer: io.BytesIO, *, encoding: str, tty: bool) -> None:
        super().__init__(buffer, encoding=encoding, errors="strict", newline="\n")
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    make_home(tmp_path)
    return tmp_path


def _run_init_through(encoding: str, buffer: io.BytesIO, *, tty: bool) -> tuple[int, bytes]:
    """把 sys.stdout 换成指定形态的流，跑一次 ``subscribe init``（末尾打印 ⚠️）。"""
    stream = _FakeStream(buffer, encoding=encoding, tty=tty)
    saved = sys.stdout
    sys.stdout = stream
    try:
        code = main(
            [
                "subscribe",
                "init",
                "--repo",
                "https://github.com/me/cal.git",
                "--url-base",
                URL_BASE,
                "--semester",
                SEMESTER,
            ]
        )
    finally:
        stream.flush()
        sys.stdout = saved
    return code, buffer.getvalue()


def test_redirected_output_becomes_utf8(home: Path) -> None:
    """locale 编码的重定向管道：改按 UTF-8 落盘，⚠️ 与中文完整保留。"""
    buffer = io.BytesIO()
    code, raw = _run_init_through("cp936", buffer, tty=False)
    assert code == 0, "init 在非 UTF-8 管道下崩溃（UnicodeEncodeError 逃逸）"
    text = raw.decode("utf-8")
    assert "Traceback" not in text
    assert "订阅 URL" in text
    assert "⚠" in text and "subscribe rotate" in text  # 警示行一字不少


def test_gbk_console_degrades_not_crashes(home: Path) -> None:
    """GBK 控制台：保持原编码，⚠️ 降级为 ?，但整行与退出码无恙。"""
    buffer = io.BytesIO()
    code, raw = _run_init_through("gbk", buffer, tty=True)
    assert code == 0, "rotate/init 类警示行在 GBK 控制台把命令炸成 exit 1"
    text = raw.decode("gbk")
    assert "Traceback" not in text
    assert "订阅 URL" in text
    # 不可编码的 ⚠️ 被替换，警示行后半段必须照常送达用户
    assert "subscribe rotate" in text and "课表" in text
