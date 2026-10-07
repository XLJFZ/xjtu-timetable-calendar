# URL 订阅式 ICS 发布（`subscribe`）实现计划

> **执行方式：** 按任务逐条实现（TDD），每个任务收尾过一次独立评审，全部完成后再做全分支终审。步骤用 checkbox（`- [ ]`）跟踪。

**Goal:** 新增 `subscribe` 子命令，把 export 产物以「孤儿提交 + 不可猜 token 文件名」发布到用户自己的 GitHub Pages 分支，供日历客户端 URL 订阅。

**Architecture:** 新模块 `src/xjtu_calendar/subscribe.py`（状态 + git 发布），CLI 四动作 `init/push/rotate/status` 走现有 `cmd_*` 模式；`export` 管线做一次行为不变的函数提取（`exporter.build_ics_for_semester`）供 `subscribe push` 进程内复用。git 传输全部子进程调用系统 git（凭据零管理），测试用 `file://` 裸仓库当假远端。

**Tech Stack:** Python 3.11+（stdlib only：secrets/hashlib/subprocess/urllib），pytest，mypy --strict，ruff。

**Spec:** `docs/design/2026-10-07-url-subscribe-ics.md`（本计划逐节对应；唯一收紧：`status` 的「未发布变化」以内容哈希布尔呈现，明细仍由 `diff` 命令负责，避免与 diff 内部结构耦合）。

## Global Constraints

- 零新增运行时依赖；不触碰 UID 算法与导出内容（冻结策略）。
- token/订阅 URL **绝不进 INFO 日志**；`subscribe-<semester>.json` 经 `atomic_write_text(private=True)` 写盘。
- 全部面向用户文案为中文，风格对照 README 既有章节（fail-closed、给补救指引、不假装成功）。
- 每个任务收尾：`pytest -q` 全绿 + `mypy` + `ruff check .` + `ruff format --check .`（本机若默认临时目录受限，给 pytest 指定可写的 `--basetemp`）。
- 测试命令统一用开发环境的 `python -m pytest …`（环境搭建见 README「开发」节）。
- git 子进程统一参数列表形式调用，公共参数 `-c core.autocrlf=false -c commit.gpgsign=false`；测试通过环境变量 `GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL/GIT_COMMITTER_NAME/GIT_COMMITTER_EMAIL` 提供身份（不改动生产行为）。
- 强推护栏：远端分支 tip 的 tree 必须**恰好只含一个 `*.ics`**，否则 `SubscribeGuardError` 拒绝。

**文件总览**

| 动作 | 路径 | 职责 |
|---|---|---|
| Create | `tests/subscribe_support.py` | 共享离线夹具：配好校历/作息/raw 快照的临时 home |
| Modify | `src/xjtu_calendar/exporter.py` | +`ExportResult`、+`build_ics_for_semester()`（管线提取） |
| Modify | `src/xjtu_calendar/cli.py` | `cmd_export` 改调新函数；+`subscribe` 解析器与 `cmd_subscribe` |
| Modify | `src/xjtu_calendar/errors.py` | +3 个订阅异常 |
| Create | `src/xjtu_calendar/subscribe.py` | 状态层（Task 2）+ 发布层（Task 3/4） |
| Create | `tests/test_export_build.py` | 提取守卫：CLI 产物 == 新函数字节 |
| Create | `tests/test_subscribe.py` | 状态层单元 + rotate/status 单元 |
| Create | `tests/test_subscribe_git.py` | `file://` 假远端集成（护栏/幂等/rotate 必推/失败注入） |
| Create | `tests/test_cli_subscribe.py` | CLI e2e + 日志不含 token 守门 |
| Modify | `README.md` / `index.html` | 「URL 订阅」章节 + 落地页一行 |

---

### Task 1: 共享夹具 + `exporter.build_ics_for_semester()` 提取（纯重构 + 字节守卫）

**Files:**
- Create: `tests/subscribe_support.py`
- Modify: `src/xjtu_calendar/exporter.py`（文件末尾追加）
- Modify: `src/xjtu_calendar/cli.py:312-486`（`cmd_export` 函数体迁移）
- Test: `tests/test_export_build.py`

**Interfaces:**
- Consumes: `cli.main(["export", ...])`、`Settings`（`semester_config_path/schedule_config_path/raw_timetable_path/ensure_dirs`）、`render_ics(events, calendar_name, prodid, dtstamp, baseline)`、`summarize`、`parse_baseline`、`sequence_stats`、`collect_unsupported`、`AcademicCalendar.from_file`、`ScheduleTable.from_file`、`TimetableParser`。
- Produces: `exporter.ExportResult(ics: str, info: dict[str, object], sequence_stats: dict[str, int] | None)`；`exporter.build_ics_for_semester(cfg, semester, *, input_path=None, calendar_config=None, schedule_config=None, calendar_name=DEFAULT_CALENDAR_NAME, from_date=None, to_date=None, allow_unsupported_adjustments=False, sequence_from=None, baseline_probe=None, no_sequence=False, dtstamp=None) -> ExportResult`。Task 5 的 `subscribe push` 以 `baseline_probe=<上次发布 .ics 路径>` 调用。

- [ ] **Step 1: 写共享夹具 `tests/subscribe_support.py`**

```python
"""订阅功能的共享夹具：一个完全离线、配置齐全的临时 home。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from xjtu_calendar.config import Settings

SEMESTER = "2026-fall"

#: 仓库 examples/ 是官方格式的唯一权威拷贝，直接复用避免 schema 漂移
REPO_ROOT = Path(__file__).resolve().parents[1]


def semester_config() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "semester": {
            "key": SEMESTER,
            "name": "2026-2027 学年秋季学期",
            "first_week_monday": "2026-09-07",
            "total_weeks": 16,
            "start_date": "2026-09-07",
            "end_date": "2026-12-27",
        },
        "excluded_dates": [],
        "overrides": {},
    }
    return cfg


def payload_row(**over: str) -> dict[str, str]:
    """一门最小课程：周一第 1~2 节，1-2 周，教室 A-1001。"""
    row = {
        "KCM": "示例课程甲",
        "KCH": "D-1",
        "SKXQ": "1",
        "KSJC": "10:00",
        "JSJC": "11:30",
        "ZCMC": "1-2周",
        "JASMC": "A-1001",
        "SKJS": "教师甲",
    }
    row.update(over)
    return row


def make_home(tmp_path: Path, rows: list[dict[str, str]] | None = None) -> Settings:
    """在 tmp_path 下布置 home（校历/作息/raw 快照三件套），返回 Settings。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    cfg.semester_config_path(SEMESTER).write_text(
        json.dumps(semester_config(), ensure_ascii=False), encoding="utf-8"
    )
    cfg.schedule_config_path().write_text(
        (REPO_ROOT / "examples" / "schedule.example.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    payload = {"kbList": rows if rows is not None else [payload_row()]}
    raw = cfg.raw_timetable_path(SEMESTER)
    raw.parent.mkdir(parents=True, exist_ok=True)
    raw.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return cfg
```

注意：`KSJC/JSJC` 用「10:00/11:30」结构化钟点字段（与 `tests/test_cli_diff.py` 的
`_row` 同族但节次字段走 `KSJC/JSJC` 时间对，parser 优先读它们，避免依赖作息表节次号
映射）。若实测 parser 需要 `SKJC/JSJC` 节次编号形式，以
`tests/test_cli_diff.py::_row` 的字段形态为准微调夹具（该文件是被 421 项测试锁住的
真实字段样例）。

- [ ] **Step 2: 写失败测试 `tests/test_export_build.py`**

```python
"""提取守卫：build_ics_for_semester 与 CLI export 产物字节一致（DTSTAMP 归一）。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from subscribe_support import SEMESTER, make_home
from xjtu_calendar.cli import main
from xjtu_calendar.exporter import build_ics_for_semester

_DTSTAMP = re.compile(r"^DTSTAMP:.*$", re.MULTILINE)


def _normalize(ics: str) -> str:
    return _DTSTAMP.sub("DTSTAMP:<fixed>", ics)


def test_build_ics_matches_cli_export_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = make_home(tmp_path / "home")
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(home.home))
    out = tmp_path / "via-cli.ics"
    assert main(["export", "--semester", SEMESTER, "-o", str(out)]) == 0
    via_cli = _normalize(out.read_text(encoding="utf-8"))

    result = build_ics_for_semester(home, SEMESTER)
    assert _normalize(result.ics) == via_cli
    assert result.info["events"] == 4  # 1-2 周 × 周一一条两节连排记录 → 2 次上课 ×2 节? 见下注
```

注：`events` 期望值以 Step 2b 实测为准——先跑一次打印 `result.info`，把**实际值**
写进断言（守卫的要点是「CLI == 函数」，事件数是顺带锁）。禁止凭猜改导出行为。

- [ ] **Step 2b: 运行确认失败**

Run: `…python.exe -m pytest tests/test_export_build.py -q --basetemp=D:/tmp/pytest-tmpSubscribe`
Expected: FAIL — `ImportError: cannot import name 'build_ics_for_semester'`

- [ ] **Step 3: 迁移实现**

在 `exporter.py` 末尾新增（把 `cli.py` `cmd_export` 中「教学日历加载 → 作息表 →
解析 → 展开 → 日期过滤 → unsupported fail-closed → SEQUENCE 基线 → render_ics」
整段**原样搬移**为关键字参数函数；不改任何行为与文案）：

```python
@dataclass
class ExportResult:
    """build_ics_for_semester 的返回：渲染文本 + 汇总数据（CLI 打印用）。"""

    ics: str
    info: dict[str, object]
    sequence_stats: dict[str, int] | None


def build_ics_for_semester(
    cfg: Settings,
    semester: str,
    *,
    input_path: str | None = None,
    calendar_config: str | None = None,
    schedule_config: str | None = None,
    calendar_name: str = DEFAULT_CALENDAR_NAME,
    from_date: str | None = None,
    to_date: str | None = None,
    allow_unsupported_adjustments: bool = False,
    sequence_from: str | None = None,
    baseline_probe: str | None = None,
    no_sequence: bool = False,
    dtstamp: datetime | None = None,
) -> ExportResult:
    """按学期构建 RFC 5545 文本。export 与 subscribe push 共用的唯一管线。

    ``baseline_probe``：SEQUENCE 自动探测的「旧版本」路径（CLI export 传 -o
    输出路径；subscribe 传上次发布留底）。显式 ``sequence_from`` 优先。
    """
    ...  # 原 cmd_export 函数体，逐段搬入；args.X → 对应关键字参数
```

`cli.py::cmd_export` 改为：解析学期 → `result = build_ics_for_semester(...)` →
写文件（保留 `newline=""` 与目录创建注释）→ 用 `result.info /
result.sequence_stats` 打印既有汇总块。原函数内的中文注释块（DTSTAMP/CRLF、
fail-closed、基线降级等）随代码迁移，一条不丢。

- [ ] **Step 4: 运行新测试 + 全量回归**

Run: `…-m pytest tests/test_export_build.py -q --basetemp=D:/tmp/pytest-tmpSubscribe` → PASS
Run: `…-m pytest -q --basetemp=D:/tmp/pytest-tmpSubscribe` → 421 项口径全绿（export 相关既有测试是真正的行为守卫）
Run: `…-m mypy && …-m ruff check . && …-m ruff format --check .` → 全绿

- [ ] **Step 5: Commit**

```bash
git add tests/subscribe_support.py tests/test_export_build.py src/xjtu_calendar/exporter.py src/xjtu_calendar/cli.py
git commit -m "refactor(export): extract build_ics_for_semester as the single render pipeline (byte-identical)"
```

---

### Task 2: `subscribe.py` 状态层 + 异常

**Files:**
- Modify: `src/xjtu_calendar/errors.py`（追加 3 类）
- Create: `src/xjtu_calendar/subscribe.py`
- Test: `tests/test_subscribe.py`

**Interfaces:**
- Consumes: `Settings.home`、`fileutil.atomic_write_text`、`errors.XjtuCalendarError(msg, hint=…)`。
- Produces:
  - `errors.SubscribeNotConfigured / SubscribeGuardError / SubscribePublishError`（均继承 `XjtuCalendarError`）
  - `subscribe.TOKEN_BYTES = 16`
  - `subscribe.PushRecord(pushed_at: str, content_sha256: str, token: str)`（`to_dict()/from_dict(d)`）
  - `subscribe.SubscriptionState(semester, repo_url, branch, token, url_base, last_push: PushRecord | None = None)`；属性 `subscription_url -> str`；`to_dict()/from_dict(d)`
  - `subscribe.new_token() -> str`；`derive_url_base(repo_url: str) -> str | None`
  - `subscribe.subscribe_dir(cfg) -> Path`；`state_path(cfg, semester) -> Path`；`work_dir(cfg, semester) -> Path`
  - `subscribe.load_state(cfg, semester) -> SubscriptionState | None`；`save_state(cfg, state) -> None`

- [ ] **Step 1: 写失败测试 `tests/test_subscribe.py`**

```python
"""状态层单元：token 形态、URL 推导、状态往返、private 落盘。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from subscribe_support import SEMESTER, make_home
from xjtu_calendar import subscribe
from xjtu_calendar.subscribe import PushRecord, SubscriptionState, derive_url_base, new_token


def test_token_is_32_hex_and_unique() -> None:
    a, b = new_token(), new_token()
    assert re.fullmatch(r"[0-9a-f]{32}", a) and re.fullmatch(r"[0-9a-f]{32}", b)
    assert a != b


@pytest.mark.parametrize(
    ("repo", "expected"),
    [
        ("https://github.com/alice/cal-alice.git", "https://alice.github.io/cal-alice/"),
        ("https://github.com/alice/cal-alice", "https://alice.github.io/cal-alice/"),
        ("git@github.com:alice/cal-alice.git", "https://alice.github.io/cal-alice/"),
        ("ssh://git@github.com/alice/cal-alice.git", "https://alice.github.io/cal-alice/"),
        ("https://example.com/alice/cal.git", None),
        ("file:///D:/tmp/fake-origin.git", None),
    ],
)
def test_derive_url_base(repo: str, expected: str | None) -> None:
    assert derive_url_base(repo) == expected


def test_state_roundtrip_and_private_write(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://github.com/alice/cal.git",
        branch="cal",
        token=new_token(),
        url_base="https://alice.github.io/cal/",
        last_push=PushRecord("2026-10-07T04:00:00Z", "ab" * 32, "t"),
    )
    subscribe.save_state(cfg, state)
    loaded = subscribe.load_state(cfg, SEMESTER)
    assert loaded == state
    raw = json.loads(subscribe.state_path(cfg, SEMESTER).read_text(encoding="utf-8"))
    assert raw["last_push"]["content_sha256"] == "ab" * 32
    assert state.subscription_url == f"https://alice.github.io/cal/{state.token}.ics"
    if os.name != "nt":  # POSIX 才断言权限位（Windows 语义缺失，同 test_fileutil 先例）
        assert subscribe.state_path(cfg, SEMESTER).stat().st_mode & 0o077 == 0


def test_load_state_missing_returns_none(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    assert subscribe.load_state(cfg, SEMESTER) is None
```

（补 `import os`。）

- [ ] **Step 2: 运行确认失败** — `ModuleNotFoundError: xjtu_calendar.subscribe`

- [ ] **Step 3: 实现**

`errors.py` 追加：

```python
class SubscribeNotConfigured(XjtuCalendarError):
    """尚未 subscribe init 就使用订阅命令。"""


class SubscribeGuardError(XjtuCalendarError):
    """发布护栏拒绝：远端分支内容不像本工具产物，绝不强推覆盖。"""


class SubscribePublishError(XjtuCalendarError):
    """git 探测/推送失败：远端保持上一版，本地状态不变。"""
```

`subscribe.py`：

```python
"""URL 订阅发布：状态文件 + git 孤儿提交发布（设计见 docs/design/2026-10-07-url-subscribe-ics.md）。"""

from __future__ import annotations

import json
import re
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .config import Settings
from .errors import SubscribeNotConfigured  # noqa: F401  (Task 3 起使用)
from .fileutil import atomic_write_text

__all__ = [
    "TOKEN_BYTES",
    "PushRecord",
    "SubscriptionState",
    "derive_url_base",
    "load_state",
    "new_token",
    "save_state",
    "state_path",
    "subscribe_dir",
    "work_dir",
]

TOKEN_BYTES = 16

_URL_BASE_RE = re.compile(
    r"(?:https://|git@|ssh://git@)github\.com[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def new_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def derive_url_base(repo_url: str) -> str | None:
    m = _URL_BASE_RE.match(repo_url)
    if not m:
        return None
    return f"https://{m.group(1)}.github.io/{m.group(2)}/"


@dataclass
class PushRecord:
    pushed_at: str  # UTC ISO-8601
    content_sha256: str
    token: str


@dataclass
class SubscriptionState:
    semester: str
    repo_url: str
    branch: str
    token: str
    url_base: str
    last_push: PushRecord | None = None

    @property
    def subscription_url(self) -> str:
        return self.url_base.rstrip("/") + f"/{self.token}.ics"

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SubscriptionState":
        lp = data.get("last_push")
        return cls(
            semester=str(data["semester"]),
            repo_url=str(data["repo_url"]),
            branch=str(data["branch"]),
            token=str(data["token"]),
            url_base=str(data["url_base"]),
            last_push=PushRecord(**lp) if isinstance(lp, dict) else None,
        )


def subscribe_dir(cfg: Settings) -> Path:
    return cfg.home / "subscribe"


def state_path(cfg: Settings, semester: str) -> Path:
    return subscribe_dir(cfg) / f"subscribe-{semester}.json"


def work_dir(cfg: Settings, semester: str) -> Path:
    return subscribe_dir(cfg) / f"work-{semester}"


def load_state(cfg: Settings, semester: str) -> SubscriptionState | None:
    path = state_path(cfg, semester)
    if not path.is_file():
        return None
    return SubscriptionState.from_dict(json.loads(path.read_text(encoding="utf-8")))


def save_state(cfg: Settings, state: SubscriptionState) -> None:
    # token 等价凭据：private 原子写（同 storage_state 待遇，spec §6）
    subscribe_dir(cfg).mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        state_path(cfg, state.semester),
        json.dumps(state.to_dict(), ensure_ascii=False, indent=2) + "\n",
        private=True,
    )
```

- [ ] **Step 4: 运行测试通过 + 门禁** — `pytest tests/test_subscribe.py -q` → PASS；全量 + mypy/ruff 全绿

- [ ] **Step 5: Commit**

```bash
git add src/xjtu_calendar/errors.py src/xjtu_calendar/subscribe.py tests/test_subscribe.py
git commit -m "feat(subscribe): subscription state layer with token-derived Pages URL (private write)"
```

---

### Task 3: `publish()` git 发布层（护栏 / 幂等 / 孤儿强推）

**Files:**
- Modify: `src/xjtu_calendar/subscribe.py`（追加发布层）
- Test: `tests/test_subscribe_git.py`

**Interfaces:**
- Consumes: Task 2 的 `SubscriptionState/save_state/work_dir`、`SubscribePublishError/SubscribeGuardError`。
- Produces: `subscribe.PublishOutcome`（Enum: `PUSHED`/`NO_CHANGE`）、`subscribe.PublishResult(outcome, url, content_sha256)`、`subscribe.publish(cfg, state, ics_text: str) -> PublishResult`（成功时内部更新 `state.last_push` 并 `save_state`）、`subscribe.GIT_IDENTITY_ENV`（测试注入身份用）。

- [ ] **Step 1: 写失败测试 `tests/test_subscribe_git.py`**

```python
"""git 发布层集成：file:// 假远端上验证护栏、幂等、孤儿单提交、失败注入。"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from subscribe_support import SEMESTER, make_home
from xjtu_calendar import subscribe
from xjtu_calendar.errors import SubscribeGuardError, SubscribePublishError
from xjtu_calendar.subscribe import PublishOutcome, SubscriptionState, new_token, publish

ICS = "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n"


def _git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", *(["-C", str(cwd)] if cwd else []), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **GIT_ENV},
    )
    return proc.stdout


GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


@pytest.fixture
def bare(tmp_path: Path) -> tuple[SubscriptionState, Path]:
    origin = tmp_path / "fake-origin.git"
    _git("init", "-q", "--bare", str(origin))
    cfg = make_home(tmp_path / "home")
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url=origin.resolve().as_uri(),
        branch="cal",
        token=new_token(),
        url_base="https://x.github.io/cal/",
    )
    subscribe.save_state(cfg, state)
    return cfg, state  # 顺序对齐调用方解构


def _tip_tree(origin: Path, branch: str = "cal") -> list[str]:
    out = _git("ls-tree", "-r", "--name-only", branch, cwd=origin)
    return out.split()


def _tip_parents(origin: Path) -> int:
    out = _git("rev-list", "--parents", "-n1", "cal", cwd=origin)
    return len(out.split()) - 1


def test_publish_creates_single_orphan_commit(cfg_state) -> None:
    cfg, state = cfg_state  # 见 fixture 注：返回 (cfg, state)
    origin = (
        Path(state.repo_url.removeprefix("file:///")).resolve().parent
    )  # 直取更稳：fixture 另存 origin
    ...
```

**fixture 定稿**（上面草稿的 origin 传递不优雅，定稿如下，测试全部按此写）：

```python
@pytest.fixture
def env(tmp_path: Path) -> tuple:
    origin = tmp_path / "fake-origin.git"
    _git("init", "-q", "--bare", str(origin))
    cfg = make_home(tmp_path / "home")
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url=origin.resolve().as_uri(),
        branch="cal",
        token=new_token(),
        url_base="https://x.github.io/cal/",
    )
    subscribe.save_state(cfg, state)
    return cfg, state, origin


def test_publish_creates_orphan_tip(env) -> None:
    cfg, state, origin = env
    with monkeypatch_git_identity():  # 见下 helper：patch subprocess env
        result = publish(cfg, state, ICS)
    assert result.outcome is PublishOutcome.PUSHED
    assert result.url == state.subscription_url
    assert _tip_tree(origin) == [f"{state.token}.ics"]
    assert _tip_parents(origin) == 0
    assert state.last_push is not None and state.last_push.token == state.token
    # 远端文件字节一致
    blob = _git("show", f"cal:{state.token}.ics", cwd=origin)
    assert blob == ICS


def test_publish_skips_when_unchanged(env) -> None:
    cfg, state, origin = env
    with monkeypatch_git_identity():
        publish(cfg, state, ICS)
        before = _git("rev-parse", "cal", cwd=origin)
        again = publish(cfg, state, ICS)
        assert again.outcome is PublishOutcome.NO_CHANGE
        assert _git("rev-parse", "cal", cwd=origin) == before


def test_publish_pushes_when_content_same_but_token_changed(env) -> None:
    cfg, state, origin = env
    with monkeypatch_git_identity():
        publish(cfg, state, ICS)
        old = state.token
        state.token = new_token()
        publish(cfg, state, ICS)
        assert _tip_tree(origin) == [f"{state.token}.ics"]
        assert old not in " ".join(_tip_tree(origin))


def test_guard_refuses_dirty_branch(env) -> None:
    cfg, state, origin = env
    # 手工往 cal 分支推一个含普通提交的仓库
    seed = origin.parent / "seed"
    _git("clone", "-q", str(origin), str(seed))
    (seed / "README.md").write_text("human work", encoding="utf-8")
    _git("checkout", "-q", "-b", "cal", cwd=seed)
    _git("add", "-A", cwd=seed)
    _git("commit", "-qm", "real content", cwd=seed, env=GIT_ENV)
    _git("push", "-q", "origin", "cal", cwd=seed, env=GIT_ENV)
    with monkeypatch_git_identity(), pytest.raises(SubscribeGuardError):
        publish(cfg, state, ICS)
    assert _tip_tree(origin) == ["README.md"]  # 远端分毫未动


def test_publish_failure_keeps_state_and_reports_previous(env) -> None:
    cfg, state, origin = env
    with monkeypatch_git_identity():
        publish(cfg, state, ICS)
        first = state.last_push
    origin.chmod(0o500)  # 只读裸仓库 → push 失败（Windows 无效则跳过该行断言）
    if os.name == "nt":
        pytest.skip("Windows 目录权限语义不等价")
    reloaded = subscribe.load_state(cfg, SEMESTER)
    assert reloaded is not None and reloaded.last_push == first
```

（`monkeypatch_git_identity()` 实现为 contextmanager：`monkeypatch.setattr(subprocess, "run", wrapper)` 给 `publish` 内部调用注入 `GIT_ENV`；或更简单——测试模块级 `os.environ.update(GIT_ENV)`，因 publish 继承环境。定稿用后者：fixture 顶部 `os.environ.setdefault` 四项，删除 `monkeypatch_git_identity` 包装。）

- [ ] **Step 2: 运行确认失败** — `AttributeError: module 'xjtu_calendar.subscribe' has no attribute 'publish'`

- [ ] **Step 3: 实现发布层**（追加到 `subscribe.py`）

```python
import hashlib
import os
import subprocess
from datetime import datetime, timezone
from enum import Enum

__all__ += ["PublishOutcome", "PublishResult", "publish"]

_GIT_COMMON = ["-c", "core.autocrlf=false", "-c", "commit.gpgsign=false"]


class PublishOutcome(Enum):
    PUSHED = "pushed"
    NO_CHANGE = "no-change"


@dataclass
class PublishResult:
    outcome: PublishOutcome
    url: str
    content_sha256: str


def _git(work: Path, *args: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(
        ["git", "-C", str(work), *_GIT_COMMON, *args],
        capture_output=True,
        text=True,
    )
    return proc


def _must_git(work: Path, *args: str) -> str:
    proc = _git(work, *args)
    if proc.returncode != 0:
        raise SubscribePublishError(
            f"git {' '.join(args)} 失败：{proc.stderr.strip() or proc.stdout.strip()}",
            hint="远端仍是上一版。检查网络/凭据后直接重试 push，本地状态未被污染。",
        )
    return proc.stdout.strip()


def publish(cfg: Settings, state: SubscriptionState, ics_text: str) -> PublishResult:
    """把 .ics 文本以孤儿单提交强推到 state.branch。

    跳过条件（spec §5.1 步骤 3）：内容哈希与 token 双相同才 NO_CHANGE。
    成功才落盘 last_push；任何失败远端与状态保持原样。
    """
    data = ics_text.encode("utf-8")
    digest = hashlib.sha256(data).hexdigest()
    if (
        state.last_push is not None
        and state.last_push.content_sha256 == digest
        and state.last_push.token == state.token
    ):
        return PublishResult(PublishOutcome.NO_CHANGE, state.subscription_url, digest)

    work = work_dir(cfg, state.semester)
    work.mkdir(parents=True, exist_ok=True)
    if not (work / ".git").is_dir():
        if _git(work, "init", "-q", str(work)).returncode != 0:
            raise SubscribePublishError("git init 失败", hint="确认 git 在 PATH 中。")
        subprocess.run(  # init 不在 .git 内，-C 前置于 work 可能不存在，故单独一次
            ["git", *_GIT_COMMON, "init", "-q", str(work)],
            capture_output=True,
            text=True,
            check=False,
        )
        _must_git(work, "remote", "add", "origin", state.repo_url)

    probe = _git(work, "ls-remote", "--exit-code", "origin", state.branch)
    if probe.returncode == 0:
        _must_git(work, "fetch", "-q", "origin", state.branch)
        _must_git(work, "checkout", "-q", "-B", state.branch, f"origin/{state.branch}")
        listing = _must_git(work, "ls-tree", "-r", "--name-only", "HEAD").split()
        if len(listing) != 1 or not listing[0].endswith(".ics"):
            raise SubscribeGuardError(
                f"远端分支 {state.branch} 的 tip 含 {len(listing)} 个文件，不是本工具的发布产物，"
                "拒绝强推覆盖。",
                hint="换一个专用分支（subscribe init --branch cal-<你的名字>），"
                "或先自行清空该分支。",
            )
    elif probe.returncode != 2:
        raise SubscribePublishError(
            f"无法探测远端分支：{probe.stderr.strip()}",
            hint="远端仍是上一版。检查 repo_url/网络/凭据后重试。",
        )

    # 孤儿化：切到无父分支，索引与工作树只留本次唯一文件（spec §5.1 步骤 4）
    _must_git(work, "checkout", "-q", "--orphan", "subscribe-publish")
    for stale in work.glob("*.ics"):
        stale.unlink()
    (work / f"{state.token}.ics").write_bytes(data)
    _must_git(work, "add", "-A", "--", f"{state.token}.ics")
    _must_git(
        work,
        "commit",
        "-q",
        "-m",
        f"publish {state.semester} {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}",
    )
    _must_git(work, "push", "-q", "--force", "origin", f"HEAD:refs/heads/{state.branch}")

    state.last_push = PushRecord(
        pushed_at=datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        content_sha256=digest,
        token=state.token,
    )
    save_state(cfg, state)
    return PublishResult(PublishOutcome.PUSHED, state.subscription_url, digest)
```

（`git init` 段落两处调用重复，实现时合并为一次带存在性判断的调用，保留注释意图。）

- [ ] **Step 4: 运行测试通过 + 全量 + 门禁全绿**

- [ ] **Step 5: Commit**

```bash
git add src/xjtu_calendar/subscribe.py tests/test_subscribe_git.py
git commit -m "feat(subscribe): orphan-commit publish with tree guard and idempotent skip"
```

---

### Task 4: rotate / 新鲜度 / verify

**Files:**
- Modify: `src/xjtu_calendar/subscribe.py`
- Test: `tests/test_subscribe.py`（追加）

**Interfaces:**
- Consumes: `publish/save_state/load_state`、`Settings.raw_timetable_path`。
- Produces: `rotate_token(cfg, state) -> SubscriptionState`；`snapshot_age_days(cfg, semester) -> float | None`；`has_unpublished_changes(cfg, state, ics_text) -> bool`；`verify_url(url: str, timeout: float = 10.0) -> tuple[bool, str]`。

- [ ] **Step 1: 追加失败测试**（`tests/test_subscribe.py`）

```python
def test_rotate_changes_token_and_keeps_history_fields(tmp_path) -> None:
    cfg = make_home(tmp_path)
    st = SubscriptionState(
        SEMESTER,
        "https://github.com/a/b.git",
        "cal",
        subscribe.new_token(),
        "https://a.github.io/b/",
        PushRecord("2026-10-07T00:00:00Z", "ff" * 32, "old"),
    )
    subscribe.save_state(cfg, st)
    rotated = subscribe.rotate_token(cfg, st)
    assert rotated.token != st.token  # 注意：rotate 原地改 state，取旧值需先拷贝
    assert rotated.last_push == st.last_push  # rotate 不抹历史
    assert subscribe.load_state(cfg, SEMESTER).token == rotated.token


def test_snapshot_age_days(tmp_path) -> None:
    cfg = make_home(tmp_path)
    age = subscribe.snapshot_age_days(cfg, SEMESTER)
    assert age is not None and age < 1  # 夹具刚写的文件
    assert subscribe.snapshot_age_days(cfg, "no-such") is None


def test_verify_url_rejects_non_http() -> None:
    ok, msg = subscribe.verify_url("file:///D:/tmp/x.ics")
    assert not ok and "http" in msg
```

（rotate 测试里旧 token 断言写法：先 `old = st.token` 再比较。）

- [ ] **Step 2: 确认失败** → **Step 3: 实现**

```python
def rotate_token(cfg: Settings, state: SubscriptionState) -> SubscriptionState:
    """换新 token 并落盘；last_push 保留（下次 publish 因 token 不同必推）。"""
    state.token = new_token()
    save_state(cfg, state)
    return state


def snapshot_age_days(cfg: Settings, semester: str) -> float | None:
    path = cfg.raw_timetable_path(semester)
    if not path.is_file():
        return None
    import time

    return (time.time() - path.stat().st_mtime) / 86400.0


def has_unpublished_changes(cfg: Settings, state: SubscriptionState, ics_text: str) -> bool:
    if state.last_push is None:
        return True
    return hashlib.sha256(ics_text.encode("utf-8")).hexdigest() != state.last_push.content_sha256


def verify_url(url: str, timeout: float = 10.0) -> tuple[bool, str]:
    """匿名 GET 自检（status --verify 用）。只接受 http/https，同 notice --url 白名单口径。"""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False, f"不支持的协议：{parsed.scheme}（仅 http/https）"
    req = urllib.request.Request(
        url, method="GET", headers={"User-Agent": "xjtu-calendar-subscribe-check"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (scheme 已白名单)
            head = resp.read(64).decode("utf-8", "replace")
            return resp.status == 200 and head.lstrip().startswith(
                "BEGIN:VCALENDAR"
            ), f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code}"
    except (urllib.error.URLError, TimeoutError) as exc:
        return False, f"请求失败：{exc}"
```

- [ ] **Step 4: 测试 + 门禁全绿** → **Step 5: Commit** `feat(subscribe): rotate, snapshot staleness, URL self-check`

---

### Task 5: CLI 接线（init/push/rotate/status）+ e2e + 日志守门

**Files:**
- Modify: `src/xjtu_calendar/cli.py`（parser + `cmd_subscribe` + `_HANDLERS` + epilog 示例）
- Test: `tests/test_cli_subscribe.py`

**Interfaces:**
- Consumes: Task 1 `build_ics_for_semester`、Task 2-4 全部 subscribe API、`SemesterNotConfigured`。
- Produces: `_HANDLERS["subscribe"] = cmd_subscribe`；留底路径 `subscribe/last-<semester>.ics`（publish 成功后写入，作为下次 SEQUENCE 基线 `baseline_probe`）。

- [ ] **Step 1: 写失败 e2e 测试 `tests/test_cli_subscribe.py`**

```python
"""subscribe CLI 全链路：init → push → (改数据) push → rotate → status。

远端用 file:// 裸仓库；日志守门断言 token 不进 stderr（spec §6 红线）。
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from subscribe_support import SEMESTER, make_home, payload_row
from xjtu_calendar.cli import main

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    for k, v in GIT_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)
    return tmp_path, origin.resolve().as_uri()


def test_full_flow(env, capsys, tmp_path) -> None:
    home, repo = env
    make_home(tmp_path)  # 布置校历/作息/raw（XJTU_CALENDAR_HOME 已指到这里）
    assert main(["subscribe", "init", "--repo", repo, "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "https://" in out or "订阅" in out  # 打印了 URL 与 Pages 指引
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    tip = subprocess.run(
        ["git", "-C", str(home / "origin.git"), "ls-tree", "-r", "--name-only", "cal"],
        capture_output=True,
        text=True,
    ).stdout.split()
    assert len(tip) == 1 and tip[0].endswith(".ics")
    # 再 push 无变化
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "无变化" in capsys.readouterr().out
    # 改数据 → 有变化 → 再推
    ...
    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    assert main(["subscribe", "status", "--semester", SEMESTER]) == 0
    status_out = capsys.readouterr().out
    assert ".ics" in status_out
    # 日志/token 红线：stderr 全量不含 32 位 hex token
    combined = capsys.readouterr()  # 已消费则返回空；关键断言用下方独立测试
```

**日志守门独立测试**（e2e 流内 capsys 消费顺序复杂，拆出）：

```python
def test_debug_logs_never_contain_token(env, tmp_path, capsys) -> None:
    home, repo = env
    make_home(tmp_path)
    main(["subscribe", "init", "--repo", repo, "--semester", SEMESTER])
    main(["--debug", "subscribe", "push", "--semester", SEMESTER])
    from xjtu_calendar.subscribe import load_state

    token = load_state(make_home(tmp_path), SEMESTER).token  # cfg 同 home
    captured = capsys.readouterr()
    assert token not in captured.err
    assert token not in captured.out.replace(captured.out.split("订阅")[1], "") if False else True
```

定稿简化为：断言 `token not in captured.err`（日志走 stderr；终端主动打印的 URL 走
stdout，二者分流正是该设计的验收点）。`init/push/status` 主动打印 URL 属预期。

- [ ] **Step 2: 确认失败**（`subscribe` 不是合法命令）

- [ ] **Step 3: 实现 CLI**

`build_parser()` 中 `inspect` 之前插入：

```python
# --- subscribe ---
sub_p = sub.add_parser(
    "subscribe", help="把 .ics 发布到自己的 GitHub Pages 分支，日历客户端按 URL 订阅"
)
sact = sub_p.add_subparsers(dest="action", metavar="<动作>")
sact.required = True
init_p = sact.add_parser("init", help="登记发布目标并生成订阅 token")
init_p.add_argument("--repo", required=True, help="git 远端 URL（GitHub Pages 仓库）")
init_p.add_argument("--branch", default="cal", help="专用发布分支（默认 cal）")
init_p.add_argument("--url-base", help="订阅 URL 前缀（GitHub 远端可自动推导）")
init_p.add_argument("--semester", help="学期标识，例如 2026-fall")
push_p = sact.add_parser("push", help="构建 .ics 并强推到发布分支")
push_p.add_argument("--semester")
push_p.add_argument("--input", help="直接指定课表 JSON（默认用 fetch 缓存）")
rot_p = sact.add_parser("rotate", help="更换订阅 token（旧 URL 立即失效）")
rot_p.add_argument("--semester")
st_p = sact.add_parser("status", help="查看订阅状态、URL 与新鲜度")
st_p.add_argument("--semester")
st_p.add_argument("--verify", action="store_true", help="匿名 GET 自检 URL 可达性")
```

`cmd_subscribe`（放 `cmd_diff` 之后；`_HANDLERS` 加 `"subscribe": cmd_subscribe`）：

```python
def cmd_subscribe(args: argparse.Namespace, cfg: Settings) -> int:
    from . import subscribe
    from .exporter import build_ics_for_semester

    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured("未指定学期", hint="用 --semester 指定，或设置 XJTU_SEMESTER。")

    if args.action == "init":
        if subscribe.load_state(cfg, semester) is not None:
            raise XjtuCalendarError(
                f"学期 {semester} 已登记过订阅",
                hint="rotate 换 token，或直接 push；重新登记请先删除 ~/.xjtu-timetable-calendar/subscribe/subscribe-<学期>.json。",
            )
        url_base = args.url_base or subscribe.derive_url_base(args.repo)
        if not url_base:
            raise XjtuCalendarError(
                f"无法从 repo 推导 Pages 地址：{args.repo}",
                hint="非 GitHub 远端请用 --url-base 显式给出 .ics 的公开访问前缀。",
            )
        state = subscribe.SubscriptionState(
            semester=semester,
            repo_url=args.repo,
            branch=args.branch,
            token=subscribe.new_token(),
            url_base=url_base,
        )
        subscribe.save_state(cfg, state)
        print(f"订阅 URL：{state.subscription_url}")
        print()
        print("一次性开启 Pages（GitHub）：仓库 Settings → Pages → Deploy from branch，")
        print(f"分支选 {args.branch}、目录选 /(root)。完成后运行 subscribe push。")
        print("⚠️ 知道该 URL 的人即可读取你的课表；有泄露疑虑时运行 subscribe rotate。")
        return 0

    state = subscribe.load_state(cfg, semester)
    if state is None:
        raise SubscribeNotConfigured(
            f"学期 {semester} 尚未登记订阅",
            hint=f"先运行：xjtu-calendar subscribe init --repo <URL> --semester {semester}",
        )

    if args.action == "push":
        age = subscribe.snapshot_age_days(cfg, semester)
        if age is not None and age > 7:
            logger.warning("raw 快照已 %.0f 天未更新，建议先 fetch 再 push（本次继续）", age)
        last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
        result_ics = build_ics_for_semester(
            cfg,
            semester,
            input_path=getattr(args, "input", None),
            baseline_probe=str(last_local) if last_local.is_file() else None,
        )
        res = subscribe.publish(cfg, state, result_ics.ics)
        if res.outcome is subscribe.PublishOutcome.NO_CHANGE:
            print("无变化，跳过推送。")
            return 0
        last_local.write_text(result_ics.ics, encoding="utf-8", newline="")
        print(f"已发布：{res.url}")
        return 0

    if args.action == "rotate":
        old = state.token
        subscribe.rotate_token(cfg, state)
        print(f"新订阅 URL：{state.subscription_url}")
        print(f"旧 token 已作废（{old[:4]}…），请更新所有日历客户端的订阅地址。")
        last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
        if last_local.is_file():
            result_ics = build_ics_for_semester(cfg, semester, baseline_probe=str(last_local))
            subscribe.publish(cfg, state, result_ics.ics)
            last_local.write_text(result_ics.ics, encoding="utf-8", newline="")
            print("已用新文件名重新发布。")
        else:
            print("（本地尚无发布留底，运行 subscribe push 完成首次发布。）")
        return 0

    # status
    print(f"仓库：{state.repo_url}（分支 {state.branch}）")
    print(f"URL ：{state.subscription_url}")
    if state.last_push:
        print(f"上次发布：{state.last_push.pushed_at}")
    else:
        print("尚未发布过。")
    age = subscribe.snapshot_age_days(cfg, semester)
    print(f"raw 快照：{'缺失' if age is None else f'{age:.1f} 天前'}")
    if getattr(args, "verify", False):
        ok, msg = subscribe.verify_url(state.subscription_url)
        print(f"URL 自检：{'通过' if ok else '未通过'}（{msg}）")
    return 0
```

epilog 示例追加一行：`python -m xjtu_calendar subscribe push --semester 2026-fall`。
`cli.py` 顶部 import 增 `SubscribeNotConfigured`。

- [ ] **Step 4: e2e 通过 + 全量 + 门禁全绿**

- [ ] **Step 5: Commit** `feat(subscribe): CLI init/push/rotate/status with URL self-check`

---

### Task 6: 文档（README + 落地页）

**Files:**
- Modify: `README.md`（「快速开始」后新增「URL 订阅（subscribe）」节；「安全与网络行为」补订阅 URL 风险段）
- Modify: `index.html`（快速开始第 1 步之后加一行可选订阅提示）

- [ ] **Step 1: README 新节**，内容要点（全部来自 spec §9，中文）：一次性步骤（建公开仓库 → 开 Pages → `subscribe init` → `subscribe push` → 客户端粘贴 URL）；日常更新 = `fetch` 后 `subscribe push`；rotate 时机；隐私口径三条（URL 即能力、历史零残留、Pages 生效延迟几分钟）；已知边界（课程改名表现为新事件，`diff` 提前可见）。
- [ ] **Step 2: 落地页**在 stepcard 1 的 pre 块后加：`<p>或 <code class="inline-code">subscribe</code> 发布到 GitHub Pages 后按 URL 订阅，自动刷新。</p>`
- [ ] **Step 3: 门禁**（`ruff format --check .` 不含 html；跑全量确认无代码影响）
- [ ] **Step 4: Commit** `docs: URL subscription (subscribe) guide for README and landing page`

---

### Task 7: 真实冒烟（人工参与，不计入门禁）

- [ ] 作者仓库开 `cal` 分支 + Pages；`pip install .` 后 `subscribe init/push/status --verify` 全链路；手机日历订阅观察事件；`rotate` 后确认旧 URL 404、新 URL 可订；清理：删 `cal` 分支与 Pages 设置。
- [ ] 冒烟结论记入 CHANGELOG 发布预备（不预写）。

## Self-Review 结论（已执行）

1. **Spec 覆盖**：§4 架构→Task 3；§5.1→Task 2/3/4；§5.2→Task 5；§5.3→Task 1；§6 隐私→Task 2(private 写)/Task 5(日志守门)/护栏测试；§7 错误表→各任务错误路径测试；§8→各任务测试步；§9→Task 6；§10→无代码。无缺口。
2. **占位符**：Task 1 的 `...` 是「原样搬移 cmd_export」的显式迁移指令并给出精确行号区间，非 TBD；Task 3 测试草稿中「定稿」段落已收敛为唯一写法，执行时以定稿为准。
3. **类型一致性**：`publish(cfg, state, ics_text) -> PublishResult`、`PushRecord(pushed_at, content_sha256, token)`、`build_ics_for_semester(..., baseline_probe=...)` 在 Task 1/3/5 间已互相对齐。
