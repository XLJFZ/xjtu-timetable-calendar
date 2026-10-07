"""CLI「接口定义缺失」指引文案的守门测试。

背景（2026-10-07 审阅发现 P1）：``load_endpoints`` 的查找链是
「显式路径 > ``<数据目录>/ehall_endpoints.json`` > 包内默认」，
**从不读取仓库里的 ``config/``**；而 ``status`` / ``fetch`` 的引导文案
曾把用户指向 ``config/ehall_endpoints.json`` —— 照做会写出一个
程序永远不会读的文件，恰好违反「错误配置比缺配置危险」的项目原则。

本文件锁住契约：**面向用户的端点指引只允许出现真实查找位置。**
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xjtu_calendar.cli import main

STALE_PATH = "config/ehall_endpoints.json"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """临时数据目录，内含一个可用会话（让 fetch 走到端点检查一步）。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    session = tmp_path / "session"
    session.mkdir(parents=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def no_endpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟「所有来源都没有可用端点」的极端情形。

    包内默认配置的存在使得该分支在真实安装里几乎不可达，
    因此必须显式构造，才能检验**文案本身**而不是碰巧不执行到。
    """
    import xjtu_calendar.fetcher as fetcher

    monkeypatch.setattr(fetcher, "load_endpoints", lambda *a, **k: {})


def test_status_points_at_real_lookup_location(
    home: Path, no_endpoints: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """status 的指引必须指向真正会被读取的用户覆盖文件。"""
    code = main(["status"])
    assert code == 0

    out = capsys.readouterr().out
    assert STALE_PATH not in out
    assert str(home / "ehall_endpoints.json") in out


def test_fetch_http_hint_points_at_real_lookup_location(
    home: Path, no_endpoints: None, capsys: pytest.CaptureFixture[str]
) -> None:
    """fetch --source http 的失败建议不得再让用户写 config/ 下的文件。"""
    code = main(["fetch", "--source", "http", "--semester", "2026-fall"])
    assert code == 1

    err = capsys.readouterr().err
    assert STALE_PATH not in err
    assert "ehall_endpoints.json" in err
