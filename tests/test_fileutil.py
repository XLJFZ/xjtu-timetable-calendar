"""共享原子写入工具的行为测试。

原来只有 ``notices._atomic_write_text``（校历配置写回）用到原子替换；
而 ``auth.ensure_login`` 写 ``storage_state.json`**（自注「等价于登录凭据」）
却是普通 ``write_text``：非原子、无属主权限。审阅后把该能力提取为
``fileutil.atomic_write_text``，本节锁住它的三项契约：

1. 正常写入：内容正确、同目录**不留临时文件**；
2. 写入中途崩溃：旧内容原样保留（原子性），临时文件被清理；
3. ``private=True``：显式收紧为 0600（POSIX 下可验证；跨平台以 chmod 调用为凭）。
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from xjtu_calendar.fileutil import atomic_write_text


def test_writes_content_and_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "cfg.json"
    atomic_write_text(target, '{"a": 1}\n')
    assert target.read_text(encoding="utf-8") == '{"a": 1}\n'
    assert [p.name for p in tmp_path.iterdir()] == ["cfg.json"]


def test_replaces_existing_content(tmp_path: Path) -> None:
    target = tmp_path / "cfg.json"
    atomic_write_text(target, "old")
    atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_crash_during_replace_keeps_old_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "cfg.json"
    atomic_write_text(target, "old")

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        atomic_write_text(target, "new")

    assert target.read_text(encoding="utf-8") == "old"
    assert [p.name for p in tmp_path.iterdir()] == ["cfg.json"]


def test_private_requests_owner_only_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "chmod", lambda path, mode: calls.append(mode))
    atomic_write_text(tmp_path / "secret.json", "x", private=True)
    assert calls == [0o600]


def test_public_write_does_not_chmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(os, "chmod", lambda path, mode: calls.append(mode))
    atomic_write_text(tmp_path / "cfg.json", "x")
    assert calls == []


@pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 POSIX 权限位语义")
def test_private_file_is_owner_only_on_posix(tmp_path: Path) -> None:
    target = tmp_path / "secret.json"
    atomic_write_text(target, "x", private=True)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
