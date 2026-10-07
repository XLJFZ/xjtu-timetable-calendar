"""打包与版本守卫测试。

两件事必须被锁死，否则「看起来能跑」和「装到别人机器上能跑」是两回事：

1. **包必须自包含** —— 默认接口定义随 wheel 分发，安装后不需要手工复制
   ``config/`` 里的任何文件。只检查「pyproject 写了 package-data」是不够的，
   必须确认资源真的能被 :func:`importlib.resources` 读到。
2. **版本号只有一个真源** —— ``pyproject.toml``。``__version__`` 无论是从
   已安装的 distribution 元数据读到，还是从源码解析，都必须与它一致。

   注意一个环境陷阱：``pythonpath = ["src"]`` 会把源码树里 ``build`` 留下的
   ``src/<name>.egg-info`` 一并放上 ``sys.path``，让 ``importlib.metadata``
   先命中它而不是真正安装的 ``dist-info``。版本守卫因此失败时，
   :func:`_metadata_mismatch_hint` 会直接指出该目录与处置方式 ——
   **这是环境残留，不要去改测试迁就它**。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from xjtu_calendar import DISTRIBUTION_NAME, __version__
from xjtu_calendar.fetcher import ENDPOINTS_FILE, bundled_endpoints_text, load_endpoints

REPO_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _pyproject_version() -> str:
    payload = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _source_tree_metadata_dirs() -> list[Path]:
    """源码树内、会被 ``sys.path`` 命中的元数据目录（构建产物，不是安装记录）。

    背景：``python -m build`` 与部分 ``pip install -e .`` 会在源码树里生成
    ``<name>.egg-info``。本项目 pytest 配了 ``pythonpath = ["src"]``，
    于是 ``src/`` 会排在 ``sys.path`` 前面，
    :func:`importlib.metadata.version` 会**先命中它**，而不是 site-packages 里
    真正安装的那份 ``*.dist-info``。两者版本不一致时，
    ``__version__`` 与命令行 ``--version`` 会给出**不同的答案**。
    """
    found: list[Path] = []
    for base in (REPO_ROOT, REPO_ROOT / "src"):
        found.extend(sorted(base.glob("*.egg-info")))
        found.extend(sorted(base.glob("*.dist-info")))
    return found


def _metadata_mismatch_hint(actual: str, expected: str) -> str:
    """版本对不上时给出可操作提示 —— 把「两个答案」的排查压缩成一条命令。"""
    lines = [
        f"实际 {actual!r}，期望 {expected!r}（pyproject.toml 的 project.version）。",
        "版本号只允许有一个真源（pyproject.toml）；对不上说明环境里出现了第二份元数据。",
    ]
    dirs = _source_tree_metadata_dirs()
    if dirs:
        lines += [
            "",
            "疑似来源 —— 源码树内的构建产物（是构建残留，不是安装记录）：",
            *(f"  - {p}" for p in dirs),
            '它们经 pytest 的 pythonpath = ["src"] 进入 sys.path，会先于',
            "site-packages 里的 dist-info 被 importlib.metadata 命中。",
            "",
            "处置（任选其一，属环境操作，不要改测试）：",
            "  1) 重新安装：python -m pip install -e . --no-deps",
            "  2) 重建产物：python -m build（会把它刷新到当前 pyproject 版本）",
            "  3) 移走它：mv src/<name>.egg-info /tmp/（*.egg-info/ 已在 .gitignore 内，可安全重建）",
        ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 版本单一真源
# --------------------------------------------------------------------------- #


def test_version_matches_pyproject() -> None:
    """``__version__`` 必须与 pyproject 的 project.version 一致（防双维护漂移）。

    失败时给出可操作提示：开发环境里最容易踩的是「源码树内残留的
    ``src/*.egg-info`` 遮蔽了已安装的 dist-info」——那会让这里的
    ``__version__`` 与命令行 ``--version`` 给出不同答案。
    """
    expected = _pyproject_version()
    assert __version__ == expected, _metadata_mismatch_hint(__version__, expected)


def test_version_matches_installed_metadata_when_available() -> None:
    """已安装时优先取 distribution 元数据，且同样等于 pyproject 版本。"""
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python 3.11+ 恒有
        pytest.skip("importlib.metadata 不可用")

    try:
        installed = version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        pytest.skip("当前未安装 distribution，走 pyproject 回退分支")

    expected = _pyproject_version()
    assert installed == expected, _metadata_mismatch_hint(installed, expected)
    assert __version__ == installed, _metadata_mismatch_hint(__version__, installed)


def test_no_hardcoded_version_constant_in_package_init() -> None:
    """``__init__.py`` 里不许再出现手写版本号常量（否则又变成两个真源）。"""
    source = (REPO_ROOT / "src" / "xjtu_calendar" / "__init__.py").read_text(encoding="utf-8")
    assert f'__version__ = "{_pyproject_version()}"' not in source


# --------------------------------------------------------------------------- #
# 包内默认接口定义
# --------------------------------------------------------------------------- #


def test_bundled_endpoints_readable() -> None:
    text = bundled_endpoints_text()
    assert text is not None, "包内默认接口定义不可读 —— wheel 将不自包含"
    assert '"endpoints"' in text


def test_package_data_declared_in_pyproject() -> None:
    payload = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    package_data = payload.get("tool", {}).get("setuptools", {}).get("package-data", {})
    assert "data/*.json" in package_data.get("xjtu_calendar", [])


def test_bundled_endpoints_file_exists_in_source_tree() -> None:
    """源码树里必须存在这份文件（它是 wheel 的打包来源）。"""
    bundled = REPO_ROOT / "src" / "xjtu_calendar" / "data" / ENDPOINTS_FILE
    assert bundled.is_file()


def test_load_endpoints_falls_back_to_bundled_config(tmp_path: Path, monkeypatch) -> None:
    """用户目录没有覆盖文件时，必须能用包内默认配置。"""
    import xjtu_calendar.config as config_module

    monkeypatch.setattr(config_module.settings, "home", tmp_path)
    endpoints = load_endpoints(cfg=config_module.settings)
    assert "timetable" in endpoints
    assert endpoints["timetable"].path.startswith("/jwapp/")


def test_user_override_wins_over_bundled(tmp_path: Path, monkeypatch) -> None:
    """用户目录覆盖优先级高于包内默认（覆盖机制必须保持兼容）。"""
    import xjtu_calendar.config as config_module

    override = tmp_path / ENDPOINTS_FILE
    override.write_text(
        '{"endpoints": [{"name": "timetable", "method": "POST", "path": "/custom/override.do"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module.settings, "home", tmp_path)
    endpoints = load_endpoints(cfg=config_module.settings)
    assert endpoints["timetable"].path == "/custom/override.do"


def test_empty_user_override_falls_back_to_bundled(tmp_path: Path, monkeypatch) -> None:
    """用户目录文件里只有占位符时，不应把有效配置一起废掉。"""
    import xjtu_calendar.config as config_module

    override = tmp_path / ENDPOINTS_FILE
    override.write_text(
        '{"endpoints": [{"name": "timetable", "path": "REPLACE_WITH_OBSERVED_PATH"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module.settings, "home", tmp_path)
    endpoints = load_endpoints(cfg=config_module.settings)
    assert endpoints["timetable"].path.startswith("/jwapp/")


def test_explicit_path_still_wins(tmp_path: Path, monkeypatch) -> None:
    import xjtu_calendar.config as config_module

    explicit = tmp_path / "explicit.json"
    explicit.write_text(
        '{"endpoints": [{"name": "timetable", "method": "POST", "path": "/explicit/path.do"}]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(config_module.settings, "home", tmp_path)
    endpoints = load_endpoints(explicit, cfg=config_module.settings)
    assert endpoints["timetable"].path == "/explicit/path.do"
