# Agent Skill 交付（doctor 子命令 + 两个 skill）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 CLI 加一个只读的 `doctor` 子命令作为"部署是否到位"的唯一判据源，并据此交付两个 Agent skill（西交大部署向导、教务接口考古方法论）。

**Architecture:** 判据逻辑放新模块 `src/xjtu_calendar/doctor.py`（纯函数，返回结构化 `Check` 列表），CLI 只做接线与打印；skill 是仓库根 `skills/` 下的两个目录，`SKILL.md` 指挥 agent 调 CLI 与 `doctor`，**不重复实现任何判据**。

**Tech Stack:** Python 3.11+，argparse CLI，pytest；skill 用 `SKILL.md` + `references/`（YAML frontmatter）。

**Spec:** `docs/design/2026-10-11-agent-skills.md`（已批准）。计划与规格冲突时以规格为准，并停下来报告，不要静默改设计。

**本计划的范围**只有 `doctor` 子命令与两个 skill。规格 §8 的**站点更新是另案**（bounded，
改 `index.html`），不在这里；它不依赖 `doctor`，可以并行也可以推后。

## Global Constraints

- 门禁四条，缺一不可，且只在收尾全量跑一次（迭代期用 `-k` 或单文件）：
  `pytest -q --basetemp=_notes/pytest-tmp`、`mypy`（无参数）、`ruff check .`、`ruff format --check .`。
- 解释器一律用 cv-project0 那个绝对路径；裸 `python` 是没装 pytest 的 3.14。
- `ruff format --check .` **会格式化 Markdown 里的 ```python 围栏**。本计划的代码块写完
  后要跑一次 `ruff format .` 再提交，否则门禁红。
- 行宽 100；`RUF001/002/003` 已关闭（中文全角标点），不要为过 lint 改标点。
- 跟踪内容**不得**出现真实姓名、学号、课程名、考场、座位、订阅 token、本机绝对路径、
  编码 Agent 产品的品牌名与安装路径。测试夹具一律用可识别为假的中文占位名
  （`示例课程甲`、`演练楼-101`）。
- `doctor` 必须**只读**：不写任何文件、不触发 `fetch`、不碰 git；HTTP 只在 `--verify` 时
  发一次匿名 GET（复用 `subscribe.verify_url`）。
- 提交 ≠ 推送。每个任务结尾只做本地 commit；`git push`、打 tag、发 Release、上 PyPI
  各需单独授权。
- 版本号含义已在规格 §3.1 写明：`doctor` 是 feature，下一个版本应是 **0.7.0**，
  与「课程侧撤销」撞号——**本计划不做发版决定**。

---

## 文件结构

| 动作 | 路径 | 职责 |
|---|---|---|
| Create | `src/xjtu_calendar/doctor.py` | 判据核心：`Check` 数据类、`run_checks()`、`worst_status()`、`doctor_exit_code()` |
| Create | `tests/test_doctor.py` | 每条判据的成功/警告/阻塞三态 + 只读断言 |
| Modify | `src/xjtu_calendar/cli.py` | 加 `doctor` 子命令解析、`cmd_doctor`、`_HANDLERS` 注册 |
| Create | `tests/test_cli_doctor.py` | CLI 层：输出形状、退出码、`--verify` 才联网 |
| Modify | `README.md` | 命令参考加一行；退出码表加 `5`；「快速开始」末尾指向 skill |
| Create | `skills/xjtu-timetable-setup/SKILL.md` | 部署向导（八步，每步带证据） |
| Create | `skills/xjtu-timetable-setup/references/troubleshooting.md` | 401 / 灰页 / 残条 / 导入 vs 订阅 |
| Create | `skills/xjtu-timetable-setup/references/client-matrix.md` | 矩阵的**决策版**副本 + 同步标记 |
| Create | `skills/timetable-api-archaeology/SKILL.md` | 接口考古方法论（高自由度 + 停止条件） |
| Create | `skills/timetable-api-archaeology/references/probe-checklist.md` | 观测四问的可执行清单 |
| Create | `skills/timetable-api-archaeology/references/red-lines.md` | 凭据 / 合规 / 隐私红线 |
| Modify | `CHANGELOG.md` | `[未发布]` 加 Added 条目 |

不建空的 `scripts/`、`assets/` 目录；skill 目录里不放 README/CHANGELOG 之类的附属文件。

---

### Task 1: `doctor` 判据核心

**Files:**
- Create: `src/xjtu_calendar/doctor.py`
- Test: `tests/test_doctor.py`

**Interfaces:**
- Consumes（均为既有实现，不改签名）：
  `auth.inspect_session(cfg) -> SessionInfo`（`.usable` / `.saved_at` / `.describe()`）、
  `config.Settings`（`.semester_key` / `.semester_config_path(k)` / `.schedule_config_path()` /
  `.raw_timetable_path(k)`）、`subscribe.load_state(cfg, k)`、
  `subscribe.snapshot_age_days(cfg, k, kind=...)`、`subscribe.has_unpublished_changes(cfg, state, text)`、
  `subscribe.verify_url(url) -> tuple[bool, str]`、`exporter.build_ics_for_semester(cfg, k) -> ExportResult`
  （`.info["events"]` / `["exam_events"]` / `["exam_cancellations"]`）、
  `errors.XjtuCalendarError`。
- Produces（Task 2 与 skill 依赖这些确切名字）：
  `Check(name: str, status: str, detail: str)`、`STATUS_OK|STATUS_WARN|STATUS_FAIL: str`、
  `MIN_DOC_VERSION: tuple[int, int, int]`、`run_checks(cfg, semester, *, verify=False) -> list[Check]`、
  `worst_status(checks) -> str`、`doctor_exit_code(checks) -> int`、`DOCTOR_EXIT_BLOCKED: int = 5`。

- [x] **Step 1: 写失败的测试**

创建 `tests/test_doctor.py`。夹具：临时 home（`XJTU_CALENDAR_HOME`）+ 一份会话文件 +
一份学期校历 + 一份作息表 + 一份 raw 快照，用"逐项拆掉"的方式驱动三态。

> **校历与作息的 JSON 形状以仓库现有夹具为准**，别照抄下面的骨架：先读
> `tests/fixtures/` 与 `tests/test_calendar.py`、`tests/test_periods.py` 里已在用的配置，
> 键名照抄再改值。若校验器拒绝夹具，**改夹具，不许放宽判据**。

```python
"""`doctor` 判据核心：每条检查都要有成功 / 警告 / 阻塞三态，且全程只读。"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from xjtu_calendar.config import Settings
from xjtu_calendar.doctor import (
    DOCTOR_EXIT_BLOCKED,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    Check,
    doctor_exit_code,
    run_checks,
    worst_status,
)

SEMESTER = "2026-2027-1"


def _write_session(cfg: Settings, *, age_days: int = 0) -> None:
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    saved = datetime.now(UTC) - timedelta(days=age_days)
    (cfg.session_dir / "storage_state.json").write_text(
        json.dumps(
            {
                "cookies": [{"name": "JSESSIONID", "value": "v", "domain": ".example.edu.cn"}],
                "_saved_at": saved.isoformat(),
            }
        ),
        encoding="utf-8",
    )


def _write_semester_config(cfg: Settings) -> None:
    cfg.semesters_dir.mkdir(parents=True, exist_ok=True)
    cfg.semester_config_path(SEMESTER).write_text(
        json.dumps(
            {
                "name": "示例学期",
                "start_date": "2026-09-07",
                "end_date": "2027-01-24",
                "total_weeks": 21,
            }
        ),
        encoding="utf-8",
    )


def _write_schedule(cfg: Settings) -> None:
    path = cfg.schedule_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "profiles": {"normal": {}, "summer": {}},
                "periods": [{"index": 1, "start": "08:00", "end": "08:45"}],
                "switch_dates": {},
            }
        ),
        encoding="utf-8",
    )


def _write_snapshot(cfg: Settings) -> None:
    path = cfg.raw_timetable_path(SEMESTER)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"datas": {"xskcb": {"rows": []}}}), encoding="utf-8")


@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.setenv("XJTU_SEMESTER", SEMESTER)
    return Settings.load()


def statuses(checks: list) -> dict[str, str]:
    return {c.name: c.status for c in checks}
```

然后是先写出来、暂时全部失败的用例（每条只断一个状态）：

```python
def test_missing_session_is_fail(cfg: Settings) -> None:
    _write_semester_config(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["session"] == STATUS_FAIL


def test_fresh_session_is_ok(cfg: Settings) -> None:
    _write_session(cfg)
    _write_semester_config(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["session"] == STATUS_OK


def test_stale_session_warns_without_claiming_expiry(cfg: Settings) -> None:
    """会话旧只警告，**不许**说它过期——旧 ≠ 失效，误报会把人推向错误的修复动作。"""
    _write_session(cfg, age_days=45)
    _write_semester_config(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    check = next(c for c in run_checks(cfg, SEMESTER) if c.name == "session")
    assert check.status == STATUS_WARN
    assert "过期" not in check.detail
    assert "login --force" in check.detail


def test_missing_semester_config_is_fail(cfg: Settings) -> None:
    _write_session(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["semester_config"] == STATUS_FAIL


def test_missing_schedule_config_is_fail(cfg: Settings) -> None:
    _write_session(cfg)
    _write_semester_config(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["schedule_config"] == STATUS_FAIL


def test_missing_snapshot_is_fail(cfg: Settings) -> None:
    _write_session(cfg)
    _write_semester_config(cfg)
    _write_schedule(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["snapshot"] == STATUS_FAIL


def test_no_exam_snapshot_is_warn_not_fail(cfg: Settings) -> None:
    """未排考时本来就没有考试快照——这是常态，不能判成阻塞项。"""
    _write_session(cfg)
    _write_semester_config(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["exam_snapshot"] == STATUS_WARN


def test_unconfigured_subscription_is_warn(cfg: Settings) -> None:
    _write_session(cfg)
    _write_semester_config(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["subscription"] == STATUS_WARN


def test_render_failure_is_fail_and_does_not_raise(cfg: Settings) -> None:
    """校历缺失时 build 会抛错；doctor 要把它折成一条 fail，而不是让命令炸掉。"""
    _write_session(cfg)
    _write_snapshot(cfg)
    assert statuses(run_checks(cfg, SEMESTER))["render"] == STATUS_FAIL


def test_doctor_is_read_only(cfg: Settings) -> None:
    """只读承诺：跑完 doctor 后 home 下不得多出一个文件。"""
    _write_session(cfg)
    _write_semester_config(cfg)
    _write_schedule(cfg)
    _write_snapshot(cfg)
    before = {p for p in cfg.home.rglob("*") if p.is_file()}
    run_checks(cfg, SEMESTER)
    after = {p for p in cfg.home.rglob("*") if p.is_file()}
    assert after == before


def test_exit_code_and_worst_status_are_pure() -> None:
    """退出码与最差状态是纯映射，别拿夹具的真实渲染结果去赌。"""
    ok_warn = [Check("a", STATUS_OK, ""), Check("b", STATUS_WARN, "")]
    with_fail = [Check("a", STATUS_OK, ""), Check("b", STATUS_FAIL, "")]
    assert worst_status(ok_warn) == STATUS_WARN
    assert worst_status(with_fail) == STATUS_FAIL
    assert doctor_exit_code(ok_warn) == 0
    assert doctor_exit_code(with_fail) == DOCTOR_EXIT_BLOCKED
    assert worst_status([]) == STATUS_OK  # 空清单不报错，按无阻塞处理
```

- [x] **Step 2: 跑测试，确认它因"模块不存在"而失败**

Run: `pytest tests/test_doctor.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL / ERROR，`ModuleNotFoundError: No module named 'xjtu_calendar.doctor'`

- [x] **Step 3: 写最小实现**

创建 `src/xjtu_calendar/doctor.py`。要点：每条判据一个函数、返回 `Check`；任何内部异常
都在 `run_checks` 里兜成一条 `fail`，**doctor 自己不抛异常**（它是最后一步的体检仪，
炸了就等于没有报告）。

```python
"""`xjtu-calendar doctor` 的判据核心：把"部署到位没有"变成结构化、只读的检查清单。

设计出处见 `docs/design/2026-10-11-agent-skills.md` §3.1。三条硬约束：

1. **只读**——不写文件、不 fetch、不碰 git；HTTP 仅在 ``verify=True`` 时发一次匿名 GET。
2. **警告不等于失败**——"会话很旧"、"没有考试快照"都是正常状态，判成失败会把用户
   推向错误的修复动作（尤其"重登"这种有成本的操作）。
3. **不抛异常**——任何一条检查内部炸了，折成一条 ``fail`` 继续跑完其余项。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from . import subscribe
from .auth import inspect_session
from .errors import XjtuCalendarError

if TYPE_CHECKING:  # pragma: no cover
    from .config import Settings

__all__ = [
    "DOCTOR_EXIT_BLOCKED",
    "MIN_DOC_VERSION",
    "STATUS_FAIL",
    "STATUS_OK",
    "STATUS_WARN",
    "Check",
    "doctor_exit_code",
    "run_checks",
    "worst_status",
]

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

#: 有阻塞项时的退出码。1 留给未预期异常，2/3/4 已被认证与权限类占用。
DOCTOR_EXIT_BLOCKED = 5

#: 本文件与 skill 的判据按哪个版本的行为编写；低于它就警告"文档可能不适用"。
MIN_DOC_VERSION: tuple[int, int, int] = (0, 6, 1)

#: 会话保存多久只触发警告。**旧 ≠ 失效**——真正的判据是 fetch 是否 401。
SESSION_AGE_WARN_DAYS = 14


@dataclass(frozen=True)
class Check:
    """一条检查结果。``detail`` 面向用户，不得含可识别个人信息。"""

    name: str
    status: str
    detail: str
```

各判据函数（同文件内，逐个写小）：

```python
def _check_session(cfg: "Settings") -> Check:
    info = inspect_session(cfg)
    if not info.usable:
        return Check("session", STATUS_FAIL, "本地无会话，请先运行 login")
    if info.saved_at is None:
        return Check("session", STATUS_WARN, "会话缺少保存时间，无法判断新鲜度")
    age_days = (datetime.now(UTC) - info.saved_at).days
    if age_days >= SESSION_AGE_WARN_DAYS:
        return Check(
            "session",
            STATUS_WARN,
            f"会话已保存 {age_days} 天（旧不等于失效）。若 fetch 返回 401："
            "python -m xjtu_calendar login --force",
        )
    return Check("session", STATUS_OK, info.describe())
```

其余判据按这张契约表实现——**状态与 detail 里的关键串是测试断言的目标，不要自己发挥**：

| 函数 | 判据 | 状态 | detail 必须包含 |
|---|---|---|---|
| `_check_version` | `__version__` 三元组 < `MIN_DOC_VERSION` | warn | `低于本判据编写时的版本` |
| `_check_version` | 否则 | ok | 版本号本身 |
| `_check_semester_config` | `cfg.semester_config_path(k)` 不存在 | fail | `学期校历缺失` |
| `_check_schedule_config` | `cfg.schedule_config_path()` 不存在 | fail | `作息表缺失` |
| `_check_snapshot` | `snapshot_age_days(cfg, k)` 为 `None` | fail | `还没有课表快照，先运行 fetch` |
| `_check_snapshot` | 年龄 > 7 天 | warn | `天前` |
| `_check_snapshot` | 否则 | ok | `天前` |
| `_check_exam_snapshot` | `kind="exams"` 为 `None` | **warn** | `未排考时本来就没有`（**不许**写失败：这是常态） |
| `_check_render` | `build_ics_for_semester` 抛 `XjtuCalendarError` | fail | 异常消息（不含个人信息） |
| `_check_render` | `info["events"] == 0` | warn | `0 门课程`、`核对学期与周次` |
| `_check_render` | 否则 | ok | `N 门课程`、`考试 M 场`（M 取自 `exam_events`） |
| `_check_subscription` | `load_state` 为 `None` | warn | `未配置订阅通道` |
| `_check_subscription` | 有状态但 `last_push is None` | warn | `尚未发布过` |
| `_check_subscription` | 留底一致且 `last_push.token == state.token` | ok | `留底一致` |
| `_check_subscription` | 否则 | warn | `运行 subscribe push 重新发布` |
| `_check_url` | `verify_url` 返回 `False` 或抛异常 | fail | 失败原因（异常类型名） |
| `_check_url` | 返回 `True` | ok | HTTP 状态说明 |

```python
def run_checks(cfg: "Settings", semester: str, *, verify: bool = False) -> list[Check]:
    """跑完全部判据。任何单项内部异常都折成 fail，本函数不抛。"""
    checks: list[Check] = [
        _safe("version", _check_version),
        _safe("session", _check_session, cfg),
        _safe("semester_config", _check_semester_config, cfg, semester),
        _safe("schedule_config", _check_schedule_config, cfg),
        _safe("snapshot", _check_snapshot, cfg, semester),
        _safe("exam_snapshot", _check_exam_snapshot, cfg, semester),
        _safe("render", _check_render, cfg, semester),
    ]
    state = subscribe.load_state(cfg, semester)
    checks.append(_safe("subscription", _check_subscription, cfg, semester, state))
    if verify and state is not None:
        checks.append(_safe("url", _check_url, state))
    return checks


def _safe(name: str, probe: Callable[..., Check], *args: object) -> Check:
    """检查项的名字由调用方给：函数改名不该悄悄改变 CLI 的标签映射。"""
    try:
        return probe(*args)
    except Exception as exc:  # noqa: BLE001 —— 体检仪本身不许炸
        return Check(name, STATUS_FAIL, f"检查内部异常：{type(exc).__name__}: {exc}")


def worst_status(checks: list[Check]) -> str:
    """空清单按 ok 处理：没有检查项就没有阻塞项。"""
    if any(c.status == STATUS_FAIL for c in checks):
        return STATUS_FAIL
    if any(c.status == STATUS_WARN for c in checks):
        return STATUS_WARN
    return STATUS_OK


def doctor_exit_code(checks: list[Check]) -> int:
    return DOCTOR_EXIT_BLOCKED if worst_status(checks) == STATUS_FAIL else 0
```

`from collections.abc import Callable` 记得加到文件头。

`_check_version` 用 `from . import __version__` 解析出三元组，低于
`MIN_DOC_VERSION` 判 `warn`，否则 `ok`。
`worst_status` 按 `fail > warn > ok` 取最差；`doctor_exit_code` 在
`worst_status == STATUS_FAIL` 时返回 `DOCTOR_EXIT_BLOCKED`，否则 0。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_doctor.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（若个别夹具与真实配置校验器不兼容，改**夹具**去满足校验器，不许放宽判据）

- [x] **Step 5: 提交**

```bash
git add src/xjtu_calendar/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): 新增只读判据核心，把部署到位与否变成结构化检查清单"
```

---

### Task 2: 把 `doctor` 接上 CLI

**Files:**
- Modify: `src/xjtu_calendar/cli.py`（`build_parser` 的子命令区、`cmd_doctor`、`_HANDLERS`）
- Modify: `README.md`（命令参考表加一行；退出码表加 `5`；「快速开始」末尾加一句指向 skill）
- Test: `tests/test_cli_doctor.py`

**Interfaces:**
- Consumes: Task 1 的 `run_checks` / `worst_status` / `doctor_exit_code` / `STATUS_*`。
- Produces: 命令行 `xjtu-calendar doctor [--semester K] [--verify]`；
  退出码 0（无阻塞）/ 5（有阻塞）。skill 的每一步都靠这个退出码判定，**签名不可改**。

- [x] **Step 1: 写失败的测试**

```python
"""CLI 层：doctor 的输出形状与退出码。判据本身在 tests/test_doctor.py。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.doctor import DOCTOR_EXIT_BLOCKED

SEMESTER = "2026-2027-1"


@pytest.fixture
def bare_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.setenv("XJTU_SEMESTER", SEMESTER)
    (tmp_path / "session").mkdir(parents=True)
    (tmp_path / "session" / "storage_state.json").write_text(
        json.dumps(
            {"cookies": [{"name": "J", "value": "v"}], "_saved_at": "2026-10-11T00:00:00+00:00"}
        ),
        encoding="utf-8",
    )
    return Settings.load()


def test_doctor_prints_a_checklist_and_exits_blocked(bare_home: Settings, capsys) -> None:
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == DOCTOR_EXIT_BLOCKED  # 校历/作息/快照都没有
    assert "体检" in out
    assert "[阻塞]" in out
    assert "session" not in out  # 面向用户的是中文标签，不是内部 name


def test_doctor_lists_every_check_once(bare_home: Settings, capsys) -> None:
    main(["doctor"])
    out = capsys.readouterr().out
    for label in ("版本", "会话", "学期校历", "作息表", "课表快照", "考试快照", "渲染", "订阅"):
        assert label in out


def test_doctor_does_not_touch_the_network_without_verify(
    bare_home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import xjtu_calendar.subscribe as sub

    def _boom(*args: object, **kwargs: object) -> object:
        raise AssertionError("doctor 默认不许联网")

    monkeypatch.setattr(sub, "verify_url", _boom)
    main(["doctor"])
```

- [x] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_cli_doctor.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL ——`argument <命令>: invalid choice: 'doctor'`（`main` 返回 argparse 退出码 2）

- [x] **Step 3: 实现**

在 `build_parser` 里 `inspect` 之前插入：

```python
    doctor = sub.add_parser(
        "doctor",
        help="只读体检：会话 / 配置 / 快照 / 渲染 / 订阅通道，一次看全（不写任何文件）",
    )
    doctor.add_argument("--semester", help="学期标识，例如 2026-2027-1")
    doctor.add_argument(
        "--verify", action="store_true", help="额外对订阅 URL 发一次匿名 GET（默认不联网）"
    )
```

`cmd_doctor` 放在 `cmd_status` 之后，标签映射是它的全部"UI"：

```python
_DOCTOR_LABELS = {
    "version": "版本",
    "session": "会话",
    "semester_config": "学期校历",
    "schedule_config": "作息表",
    "snapshot": "课表快照",
    "exam_snapshot": "考试快照",
    "render": "渲染",
    "subscription": "订阅",
    "url": "URL 自检",
}

_DOCTOR_MARKS = {"ok": "[通过]", "warn": "[警告]", "fail": "[阻塞]"}


def cmd_doctor(args: argparse.Namespace, cfg: Settings) -> int:
    from . import doctor as doctor_module

    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured("未指定学期", hint="用 --semester 指定，或设置 XJTU_SEMESTER。")

    checks = doctor_module.run_checks(cfg, semester, verify=args.verify)
    print(f"体检（学期 {semester}）")
    for check in checks:
        label = _DOCTOR_LABELS.get(check.name, check.name)
        print(f"  {_DOCTOR_MARKS[check.status]} {label}：{check.detail}")
    blocked = [c for c in checks if c.status == doctor_module.STATUS_FAIL]
    print(f"\n结论：{len(blocked)} 项阻塞 / {len(checks)} 项检查。")
    return doctor_module.doctor_exit_code(checks)
```

`_HANDLERS` 里加 `"doctor": cmd_doctor,`。

- [x] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_cli_doctor.py tests/test_doctor.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [x] **Step 5: 更新 README**

命令参考表加一行 `doctor`；退出码表补 `5 —— doctor 检出阻塞项`；「快速开始」末尾加一句：
"想让编码 Agent 代你走这套流程，见仓库根 `skills/`。"
**不要**在 README 里写任何 Agent 产品的名字或安装路径。

- [x] **Step 6: 提交**

```bash
git add src/xjtu_calendar/cli.py tests/test_cli_doctor.py README.md
git commit -m "feat(cli): 新增只读子命令 doctor，把部署判据收敛成一处"
```

---

### Task 3: `skills/xjtu-timetable-setup`（部署向导）

**Files:**
- Create: `skills/xjtu-timetable-setup/SKILL.md`
- Create: `skills/xjtu-timetable-setup/references/troubleshooting.md`
- Create: `skills/xjtu-timetable-setup/references/client-matrix.md`

**Interfaces:**
- Consumes: `xjtu-calendar` 的 `--version` / `login` / `fetch` / `export` / `diff` / `notice` /
  `schedule` / `subscribe …` / `doctor`（Task 2 的退出码 0 与 5）。
- Produces: 一个可被任意兼容 Agent 加载的 skill 目录；`name` 必须与目录名一致。

- [x] **Step 1: 写 `SKILL.md` 的 frontmatter（逐字用这段）**

```markdown
---
name: xjtu-timetable-setup
description: 把西安交通大学 eHall 课表（含考试安排）导出为 .ics 并部署成按 URL 订阅的手机/网页日历。当用户提到西交大课表、eHall、教务系统导出、.ics 订阅、课表同步到手机日历、xjtu-calendar 命令时使用。负责引导登录、校历与作息配置、发布与客户端选择。
---
```

- [x] **Step 2: 写正文的八步，每步固定三段：`动作` / `证据` / `不满足时`**

正文用祈使句，控制在 200 行内（上限 500，但八步 × 三段不该超）。必须包含的内容，
逐条对应规格 §3：

1. **环境** — 动作：`xjtu-calendar --version`。证据：版本 ≥ 0.6.1。不满足：先装再回来，
   并写明"本 skill 按 0.6.1 的判据编写，版本更低时某些检查不存在"。
2. **登录** — 动作：**把命令原样交给用户，让他在自己的前台终端里执行**
   `xjtu-calendar login --force`。证据：会话文件 mtime 变化 **且** `fetch` 不再 401。
   不满足时：说明这是独立浏览器实例、平时浏览器里的登录态无效、窗口出现 `?browser=no`
   时先手输入口 URL 再考虑换浏览器。**显式禁止**：后台执行器、管道、替用户按 Enter。
3. **校历** — 动作：向用户要三件事（学期起始日、总周次、学期标识）。
   **禁止从课表数据推断**——工具刻意不内置猜测值，推断错了整学期周次错位而产物看起来正常。
4. **作息表** — 动作：优先 `xjtu-calendar schedule`（切换点取自官方页原文），拿不到就问用户。
5. **`fetch`** — 证据：快照存在、事件数 > 0、`--debug` 报告里 skipped 条目已逐条读过。
6. **`export`** — 证据：VEVENT 数与事件数吻合；**重复导出 UID 不变**（订阅能原地更新的前提）。
7. **订阅发布** — 动作：`subscribe init` → `subscribe push`。必须如实告知两件事：
   token 等价凭据；发布仓库若为公开，分支列表能直接翻到 `<token>.ics` 的文件名。
   证据：`subscribe status` 显示留底一致，且 `doctor --verify` 的 URL 自检通过。
8. **选客户端** — 指向 `references/client-matrix.md`，并给出决策式建议（不是清单）：
   要自动更新就必须是订阅型入口；一次性导入收不到后续任何变更与撤销。

结尾一节 **`## 收尾：一条命令定成败`**：跑 `xjtu-calendar doctor`，
退出码 0 才算交付完成；5 就回到对应步骤，**不许在阻塞项未清时说"应该没问题了"**。

- [x] **Step 3: 写 `references/troubleshooting.md`**

按症状组织，每条给"判据 → 处置"：`fetch` 401；`login` 看起来成功其实没动（不带 `--force`）；
后台跑 `login` 卡住且 profile 被占；灰页 `?browser=no`；`JSESSIONID` 同名 cookie 警告
（不影响取数，只在伴随 401 时才要处理）；考试没进日历的三查；撤销残条不消失；
日历名不更新；"导入"与"订阅"怎么分辨（订阅型带上次同步时间）。

- [x] **Step 4: 写 `references/client-matrix.md`（决策版 + 同步标记）**

文件头第一行必须是：

```markdown
> 来源：README「客户端行为矩阵」，同步于 <填本次实际的 commit 短 sha>。README 是权威；
> 改动 README 矩阵必须同步这一份并更新上面的 sha。
```

正文不抄全表，只写"遇到这个诉求就选哪条路 + 为什么"，并保留实测日期与
"单账户单样本，别当厂商承诺"的限定语。

- [x] **Step 5: 自检并提交**

对照规格 §6 的清单逐项检查（frontmatter 合法、`name` 与目录名一致、description 单行
≤1024 字符、正文 <500 行、无 TODO、无多余文件、python 围栏只在必要处）。

```bash
git add skills/xjtu-timetable-setup
git commit -m "feat(skills): 新增西交大课表部署向导 skill"
```

---

### Task 4: `skills/timetable-api-archaeology`（接口考古方法论）

**Files:**
- Create: `skills/timetable-api-archaeology/SKILL.md`
- Create: `skills/timetable-api-archaeology/references/probe-checklist.md`
- Create: `skills/timetable-api-archaeology/references/red-lines.md`

**Interfaces:**
- Consumes: 无代码依赖（纯方法论）。
- Produces: 一个明确声明"不产出可用订阅"的 skill。

- [x] **Step 1: frontmatter（逐字）**

```markdown
---
name: timetable-api-archaeology
description: 为编码 Agent 提供接入陌生学校教务系统的接口考古方法与停止条件：只读观测、区分"没有数据"与"查询失败"、周次编码形态、事件身份字段、对照探针、凭据与合规红线。当用户想让课表导出工具支持新学校、探测教务接口、评估某校接口能否做增量更新或撤销时使用。本技能不承诺能接入任意学校，也不产出可用订阅。
---
```

- [x] **Step 2: 正文第一节就是停止条件**

`## 第 0 步：先停下来问清楚` — 目标学校条款是否允许程序化访问？用户是否本人账号？
任一不清楚就**停**，并输出"我停在哪、需要什么才继续"。写死一条：
凭据永远由用户本人输入，agent 不代填、不读取、不保存、不截图。

- [x] **Step 3: 正文第二节：观测四问（每问要 payload 证据）**

字段与语义键能否一一对上；周次是位掩码 / 文本 / 日期区间；学期如何发现、代码格式；
**"没有数据"与"查询失败"能否区分**。第四问写成 **go/no-go**，并给出理由：
区分不了则撤销通路**不可用**而非稍差——"空"既可能真没有也可能抓取失败，
任一侧误判都会撤销用户已发布的整学期。

- [x] **Step 4: 正文第三、四节：身份字段与对照探针**

身份字段决定"改期"是原地更新还是删了重建（选错 ⇒ 旧事件永远留在客户端）；
每个待判信号必须配一个必然可见的对照变化，否则"什么都没变"无法解释——
写明这条是 2026-10 一整晚演练换来的。

- [x] **Step 5: `references/probe-checklist.md` 与 `references/red-lines.md`**

前者是可勾选的观测清单（含"样本必须先脱敏才允许进仓库"）；后者是红线：
不并发、不重试轰炸、不枚举学号/课程、不绕过权限、不提交真实数据、
未验证的客户端表现一律写"通常会"并注明未验证。

- [x] **Step 6: 自检并提交**

```bash
git add skills/timetable-api-archaeology
git commit -m "feat(skills): 新增教务接口考古方法论 skill（含 go/no-go 与红线）"
```

---

### Task 5: 收尾验证与文档回写

**Files:**
- Modify: `CHANGELOG.md`
- Modify: `docs/design/2026-10-11-agent-skills.md`（把"计划见 docs/plans/…"补上；矩阵同步 sha 落地）

- [x] **Step 1: 真实环境跑一次 `doctor`**

Run: `"$PY" -m xjtu_calendar doctor --verify`
Expected: 本机应全绿或仅剩警告（会话今天刚重登、校历作息已配、快照 0 天、订阅留底一致、
URL 200）。**把实际输出贴进提交信息**——这是"能验的那部分"的证据，不是猜测。

- [x] **Step 2: 隐私 grep**

黑名单**不写进这份计划**（写了就等于把用户名与产品名放进跟踪内容，正是本步要防的事）。
把待搜的真实标识放在仓库外一份文件里（`_notes/` 已 gitignore），一行一个：

```bash
grep -rniF -f _notes/privacy-patterns.txt skills/ src/xjtu_calendar/doctor.py docs/ README.md
```

Expected: 无匹配。命中的判据是"该行属于跟踪内容且含真实标识"；
命中后改正**当前检出**即可，历史对象里的残留由发布前的整树清理另行处理。

- [x] **Step 3: 四条门禁全量跑一次**

```bash
"$PY" -m ruff format . && "$PY" -m ruff check . && "$PY" -m mypy && "$PY" -m pytest -q --basetemp=_notes/pytest-tmp
```

（先 `ruff format .` 是因为它会格式化 Markdown 里的 python 围栏；跑完若它改动了
本计划或 skill 文件，把改动一起提交。）

- [x] **Step 4: 回写 CHANGELOG 与规格指针，提交**

`[未发布] → Added`：`doctor` 子命令、两个 skill。版本含义提醒照旧：下一个版本应为 0.7.0。

- [x] **Step 5: 报告验收边界**

向用户明确：已验 = 判据单测、CLI 退出码、真实环境 `doctor`、格式自检清单；
**未验 = 新用户旅程**（本机环境是热的，从零安装的人会不会卡住测不到）。
这一句必须同时写进 `SKILL.md` 的已知边界，不能只在对话里说。

---

## 自检对照（写完后自己跑一遍）

- 规格 §3.1 的每条 `doctor` 约束 → Task 1/2 是否都有对应用例？（只读、退出码、不抛异常、
  旧不等于失效、不联网）
- 规格 §3 的八步 → Task 3 Step 2 是否逐条落地，且第 2 步的三条禁令都在？
- 规格 §4 的四项 → Task 4 的 Step 2–4 是否覆盖，go/no-go 是否写清理由？
- 规格 §5 的同步标记 → Task 3 Step 4 是否要求真实 sha？
- 规格 §7 的"未验旅程" → Task 3 与 Task 5 Step 5 是否都写了？

---

## 执行记录（2026-10-11，全部任务已完成）

| 任务 | 提交 |
|---|---|
| Task 1 `doctor` 判据核心 | `f1ada85` |
| Task 2 CLI 接线 | `2a7e3b4` |
| Task 3 部署向导 skill | `015a39e` |
| Task 4 接口考古 skill | `a78e883` |
| Task 5 门禁修正与文档回写 | `983a663`、`f16c9b1` |
| 收尾追加（见下 ③） | `82526a3` |

与计划的差异，都是**跑出来的**而不是选出来的：

1. **隐私 grep 的黑名单不能写在计划里**（Step 2）。原计划把待搜的真实标识（用户名、
   发布仓库属主、token 前缀、本机路径前缀、Agent 产品名）直接写进那条 `grep` 命令，
   于是那行本身就成了跟踪内容里的违规——正是这一步要防的事。改为黑名单放在仓库外的
   一份文件里（一行一个），命令用 `grep -rniF -f` 读它。
2. **`ruff format` 会改已提交的代码**（Task 5 Step 3）。真跑下来动了三处：
   `doctor.py` 里被折成两行的 f-string、`tests/test_cli_doctor.py` 的超长构造调用，
   以及两个 `# noqa: BLE001`——该规则在本仓库并未启用，RUF100 会把整行注释删掉。
   处置是保留注释内容、丢掉 `noqa` 指令本身（写成 except 分支内的普通注释）。
   计划里"迭代只跑定向测试、门禁收尾跑一次"的纪律没问题，但**门禁第一次全量跑之前
   不该认为任务已完成**。
3. **`doctor` 不指定学期时退出码是 1，不是 5**（Step 1 真机首跑暴露）。它抛
   `SemesterNotConfigured`，连一行检查项都不打印。照计划写下的收尾命令
   `doctor --verify` 会让执行者把这个 1 误读成"体检没过"而回头折腾登录。
   skill 的收尾命令与排查各补一条，明确 0/1/5 三种码分别是什么意思。

未验的仍然是同一件：**新用户旅程**（本机环境是热的）。要验得找干净环境或没装过的同学。
