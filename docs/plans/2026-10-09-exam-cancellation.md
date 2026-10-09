# 考试取消通路（`STATUS:CANCELLED`）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. 每个任务收尾过一次独立评审，全部完成后再做全分支终审。Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让已发布的考试事件在源数据里消失后，于下一份发布产物里带 `STATUS:CANCELLED` 出现，直到它的原定时刻自然过去，从而消掉 v0.5 spec §7.1 的 ghost-event 限制。

**Architecture:** 新增一份**本地考试台账**（`home/subscribe/last-exams-<学期>.ics`）记录"曾以 live 形态发布过、原定时刻未过"的考试；渲染时 `台账 UID − 过滤前 live UID` 得到撤销候选，剪掉时刻已过与"UID 不在 SEQUENCE 基线里"的两类，剩下的以**最小字段**并入同一份 `method:PUBLISH` 产物。幂等不新增机制：靠 `sequence.resolve_sequence` 的既有指纹规则（撤销条目字段每次相同 ⇒ 保号），`EventBaseline` 一个字段都不改。撤销只在"考试数据可信"时计算，任何降级路径都不得产生撤销。

**Tech Stack:** Python 3.11+（stdlib only：`dataclasses`/`datetime`/`pathlib`/`logging`），`icalendar>=6.1.0`（台账读写与产物共用同一个库），pytest，mypy --strict，ruff。

**Spec:** `docs/design/2026-10-09-exam-cancellation.md`（本计划逐条对应 D1-D24；§12 记着原稿三处错，实现时不要"顺手改回去"）

## Global Constraints

- 零新增运行时依赖；**不触碰课程 UID 算法与课程事件导出内容**（legacy UID contract，`exporter.make_uid` docstring）。
- **课程侧字节不变是硬验收**：`tests/test_legacy_course_export_golden.py` 四条用例在任何任务收尾时都必须原样通过，不许改断言、不许加 `exclude`。
- **撤销的准入门槛（spec D18）**：只有"考试快照读到了且解析没炸"才允许计算撤销。`TimetableFetchError`、任何 `Exception`、以及"有 `parsed` 行但一条都没构建出事件"三种情况都 ⇒ 不可信 ⇒ 不读台账、不撤销、不回写台账。**这条是本次实现最大的风险面**：`live = ∅` 在撤销语义下等于"取消整学期"。
- **候选必须用未经日期过滤的 live 集（spec D19）**：`exporter.py:726-727` 的 `--from-date/--to-date` 会连考试一起裁；用过滤后的集合会把窗口外考试撤销并从台账剪掉。
- **UID 不在 SEQUENCE 基线里的候选一律不下发（spec D20）**：`sequence.py:188-189` 会给 `SEQUENCE:0`，低于客户端已握有的序号 ⇒ 会被忽略，幽灵永存。这类候选丢弃、记 warning，并**从台账里剪掉**（不留下次重来的假象）。
- `--no-exams` 是**整块关闭**：不读台账、不算撤销、不回写台账，撤销条目与 live 考试一同缺席（spec D4/D21）。
- 保留期用**墙钟**（`datetime.now(TZ_XIAN)`），只有测试注入 `cancel_expiry_at`；**不许**用 `_stamp_from_snapshot` 的快照口径判过期（spec D9）。
- 台账**永不发布**：只落本地、`private=True`、含个人信息；台账文本里**不许**出现 `STATUS`、`SEQUENCE`、`LAST-MODIFIED`、`METHOD`、`X-WR-CALNAME`（spec D10/D22）。
- 台账读写都必须容忍 naive datetime：`sequence.py:81-85` 的 `norm_dt` 对 naive 值不加 tz，与 aware 的 `now` 比较会 `TypeError` ⇒ 读取端**丢弃 naive 条目**并 warning（spec D22）。
- 台账写盘前 `mkdir(parents=True, exist_ok=True)`；台账写入的 `OSError` **只记 warning，不改退出码**（spec D24①，`ensure_dirs()` 刻意不改，见 `config.py:118-127`）。
- 产物/留底/台账一律用 `newline=""` 写出（`atomic_write_text` 用 `newline="\n"`，不会破坏 icalendar 自带的 CRLF；`write_text` 用默认 `newline=None` 会得到 `\r\r\n`，见 `cli.py:540-542` 的注释）。
- 面向用户文案为中文，风格对照 README 既有章节（fail-closed、给补救指引、不假装成功）。
- **循环依赖约束**：`exams.py` 顶层 `from .exporter import PRODID, UID_DOMAIN`；`exporter.py` 只能在函数体内 `from .exams import ...`（沿用 `exporter.py:682-685` 的写法）。
- `tests/` 没有 `__init__.py`，`pythonpath` 只配了 `src` ⇒ 测试里写 `from exam_support import ...`，**不是** `from tests.exam_support import ...`。import 分组按 ruff `I`：第三方（`pytest`/`exam_support`/`subscribe_support`/`icalendar`）一组、空行、第一方（`from xjtu_calendar...`）一组，否则 `ruff check .` 报 I001。
- 抓日志一律用 `exam_support.captured_logs()`，**不要用 `caplog`**（`logging_setup.py:111` 把 `propagate` 关了）。
- `mypy --strict` 的 `files` 只有 `src/xjtu_calendar`（`pyproject.toml:80-84`），src 里每个新函数都要完整注解。
- `pyproject.toml:72-78` 是 `testpaths = ["tests", "src"]` + `--doctest-modules` ⇒ `src` 的 docstring 里**不要**放 `>>>` 示例。
- 每个任务收尾四条全绿（与 `.github/workflows/ci.yml` 一致）：
  ```bash
  PY=<装好 dev extras 的那个解释器的绝对路径；**本机路径写在 gitignored 的交接简报里，不进仓库**>
  "$PY" -m pytest -q --basetemp=_notes/pytest-tmp
  "$PY" -m mypy
  "$PY" -m ruff check .
  "$PY" -m ruff format --check .
  ```
  裸 `python` 是 3.14 且没装 pytest，**必须**用上面的全路径解释器。`ruff format --check .` 会连带检查 Markdown 里的 ```python 代码块——**改文档后也要跑**（本项目已被这条坑过三次）。
- 提交只到本地；**推送需要用户明确授权**（本机 `git push` 已实测可用，但「提交 ≠ 推送」）。
- 公开仓库的跟踪内容里不许出现真实个人信息、本机绝对路径、AI 工具品牌名；派工文本里的本机路径**不许被抄进任何被跟踪文件**（含测试 docstring 与文档代码块）。
- 测试固件日期必须是能 `date.fromisoformat` 的合成日期（`exam_support.DEMO_DAY = "2030-06-17"` 一系），文档里的 `YYYY-MM-DD` 是打码口径，不可执行。

**文件总览**

| 动作 | 路径 | 职责 |
|---|---|---|
| Modify | `src/xjtu_calendar/models.py` | `CalendarEvent` 末尾追加 `status: str \| None = None` |
| Modify | `src/xjtu_calendar/config.py` | `+subscribe_dir` property、`+exam_ledger_path(semester_key)` |
| Modify | `src/xjtu_calendar/subscribe.py` | `subscribe_dir(cfg)` 改为委托 `Settings` |
| Modify | `src/xjtu_calendar/exams.py` | `+LedgerEntry`、`+load_exam_ledger`、`+cancel_candidates`、`+build_cancellation_events`、`+render_exam_ledger` |
| Modify | `src/xjtu_calendar/exporter.py` | `render_ics` 的 STATUS/空 SUMMARY 条件化；`build_ics_for_semester` 的门槛与撤销注入；`ExportResult +exam_ledger_text`；`info["exam_cancellations"]` |
| Modify | `src/xjtu_calendar/cli.py` | export/push/rotate 的台账回写与摘要行；rotate 探针警告；diff 文案改口 |
| Create | `tests/test_exam_ledger.py` | 台账 writer/reader 与候选算法（T3-T6） |
| Create | `tests/test_exporter_cancellation.py` | 门槛、撤销注入、幂等、stats 口径（T7-T8） |
| Create | `tests/test_cli_cancellation.py` | 台账落盘时序、目录缺失、rotate 警告（T9-T11） |
| Create | `tests/fixtures/legacy_exam_inputs.json` | 升级安全 golden 的全合成输入（含考试快照） |
| Create | `tests/fixtures/legacy_exam_export_v050.ics` | 由 tag `v0.5.0` 渲染的考试侧 golden |
| Create | `tests/test_legacy_exam_export_golden.py` | D14 的字节钉子 + 行尾契约 |
| Modify | `tests/exam_support.py` | `+exam_home(tmp_path, *rows)` 共享固件 |
| Modify | `README.md` / `CHANGELOG.md` | 取消通路、限制改口、`[Unreleased]` |

---

## Task 1: `CalendarEvent.status` 与 `render_ics` 的两处条件化

**Files:**
- Modify: `src/xjtu_calendar/models.py:280-296`
- Modify: `src/xjtu_calendar/exporter.py:473-487`
- Modify: `tests/exam_support.py`（+模块级 `STAMP`，后续任务的台账固件共用）
- Test: `tests/test_exam_ledger.py`（新建，本任务只放渲染条件化的两条）

**Interfaces:**
- Consumes: 无（起点任务）
- Produces: `CalendarEvent(..., status: str | None = None)`；`render_ics` 对 `status` 非空者写 `STATUS:`，对 `summary` 为空者**不写** `SUMMARY` 行

- [ ] **Step 1: 写失败测试**

新建 `tests/test_exam_ledger.py`，文件头（按 ruff 的 import 分组，第三方组含 `tests/` 下的
本地模块）：

```python
"""考试台账与撤销事件的单元层：渲染条件化（T1）、writer/reader（T3/T4）、候选算法（T5/T6）。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date

from exam_support import STAMP, captured_logs
from xjtu_calendar.exporter import render_ics
from xjtu_calendar.models import CalendarEvent
from xjtu_calendar.schedules import combine
```

（`STAMP` 由 `exam_support.py` 提供：`STAMP = combine(date(2030, 1, 1), "00:00")`，
**本任务就把它加进 `exam_support.py`**，Task 7 之后所有台账固件共用它。）

两条渲染条件化用例：

```python
def test_status_line_only_appears_when_set():
    """`status` 默认 None ⇒ 产物里没有 STATUS 行；给了才出现。"""
    event = CalendarEvent(
        uid="a@xjtu-timetable-calendar",
        summary="示例课程甲（结课考试）",
        start=combine(date(2030, 6, 17), "15:00"),
        end=combine(date(2030, 6, 17), "17:30"),
    )
    plain = render_ics([event], calendar_name="课表", dtstamp=STAMP)
    assert "STATUS" not in plain

    cancelled = replace(event, status="CANCELLED")
    out = render_ics([cancelled], calendar_name="课表", dtstamp=STAMP)
    assert "STATUS:CANCELLED" in out


def test_empty_summary_writes_no_summary_line():
    """实测：`add("summary", "")` 会写出字面 `SUMMARY:` 空值行，必须靠不调用 add 来省略。

    spec D3 的「撤销条目无标题」只有这条成立；若将来有人改成 `summary or " "`
    之类，本用例先红。
    """
    event = CalendarEvent(
        uid="b@xjtu-timetable-calendar",
        summary="",
        start=combine(date(2030, 6, 17), "15:00"),
        end=combine(date(2030, 6, 17), "17:30"),
        status="CANCELLED",
    )
    out = render_ics([event], calendar_name="课表", dtstamp=STAMP)
    assert "SUMMARY" not in out
    assert "STATUS:CANCELLED" in out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `TypeError: CalendarEvent.__init__() got an unexpected keyword argument 'status'`

- [ ] **Step 3: 写最小实现**

`models.py` 的 `CalendarEvent` 末尾追加（**必须追加在 `meeting` 之后**：四个构造点全用关键字，见 `exporter.py:274,337`、`exams.py:530`、`tests/test_sequence.py:46`）：

```python
    #: 仅撤销事件用，取值 ``"CANCELLED"``；课程与 live 考试一律不传（默认 ``None``
    #: ⇒ 产物字节与不引入该字段时完全一致）。见 docs/design/2026-10-09-exam-cancellation.md D3。
    status: str | None = None
```

`exporter.render_ics` 的事件循环里，把无条件 `component.add("summary", item.summary)` 改成条件化，并在 `description` 之后追加 `status`：

```python
        component.add("dtend", item.end)
        if item.summary:
            # 撤销事件刻意不带 SUMMARY（spec D3 的最小字段）：实测
            # ``add("summary", "")`` 会写出 ``SUMMARY:`` 空值行，所以只能不调用 add。
            component.add("summary", item.summary)
        if item.location:
            component.add("location", item.location)
        if item.description:
            component.add("description", item.description)
        if item.status:
            component.add("status", item.status)
```

> 属性的**输出顺序**由 icalendar 自己决定（实测 `add` 顺序与落盘顺序不一致），不要在代码里
> 猜顺序；字节稳定性由 Step 4 的两支 golden 与本任务的"两次渲染相同"断言兜住。

- [ ] **Step 4: 跑测试确认通过，并确认课程侧字节未变**

Run: `"$PY" -m pytest tests/test_exam_ledger.py tests/test_legacy_course_export_golden.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（golden 四条全绿，含 `test_course_side_bytes_equal_pre_feature_export`）

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/models.py src/xjtu_calendar/exporter.py tests/test_exam_ledger.py
git commit -m "feat(models): 给 CalendarEvent 加 status 字段并在渲染里条件化"
```

---

## Task 2: `Settings.subscribe_dir` 与 `exam_ledger_path`

**Files:**
- Modify: `src/xjtu_calendar/config.py:104-131`（目录 property 区）与 `:146-159`（文件定位区）
- Modify: `src/xjtu_calendar/subscribe.py:114-115`
- Test: `tests/test_config.py`（已存在，追加）

**Interfaces:**
- Consumes: 无
- Produces: `Settings.subscribe_dir -> Path`（`home/"subscribe"`）、`Settings.exam_ledger_path(semester_key: str) -> Path`

- [ ] **Step 1: 写失败测试**

```python
def test_exam_ledger_path_lives_under_subscribe_dir(tmp_path):
    cfg = Settings(home=tmp_path)
    assert cfg.subscribe_dir == tmp_path / "subscribe"
    assert (
        cfg.exam_ledger_path("2026-2027-1") == tmp_path / "subscribe" / "last-exams-2026-2027-1.ics"
    )


def test_subscribe_function_delegates_to_settings(tmp_path):
    """目录字面量只能有一处（spec §6.1）。"""
    from xjtu_calendar import subscribe

    cfg = Settings(home=tmp_path)
    assert subscribe.subscribe_dir(cfg) == cfg.subscribe_dir
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_config.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'subscribe_dir'`

- [ ] **Step 3: 写最小实现**

`config.py` 在 `raw_dir` property 之后加：

```python
    @property
    def subscribe_dir(self) -> Path:
        """订阅状态、工作副本与发布留底（含 token 与 .ics，含个人信息，**不提交**）。

        目录字面量的唯一归属地在这里；`subscribe.subscribe_dir()` 只做转发。
        **不在 `ensure_dirs()` 里创建**：台账是可选特性，写入点自己 `mkdir`（spec D24①）。
        """
        return self.home / "subscribe"
```

在 `raw_exams_prev_path` 之后加：

```python
    def exam_ledger_path(self, semester_key: str) -> Path:
        """考试台账：曾以 live 形态发布过、原定时刻还没过去的考试账本。

        含考试原名/考场/座位，**永不发布**、只留本地、写盘 `private=True`
        （docs/design/2026-10-09-exam-cancellation.md §8）。
        """
        return self.subscribe_dir / f"last-exams-{semester_key}.ics"
```

`subscribe.py:114-115` 改为：

```python
def subscribe_dir(cfg: Settings) -> Path:
    """订阅目录（字面量已收敛到 :attr:`Settings.subscribe_dir`）。"""
    return cfg.subscribe_dir
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_config.py tests/test_subscribe.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/config.py src/xjtu_calendar/subscribe.py tests/test_config.py
git commit -m "feat(config): 收敛订阅目录并给出考试台账路径"
```

---

## Task 3: `LedgerEntry` 与台账 writer

**Files:**
- Modify: `src/xjtu_calendar/exams.py`（在 `build_exam_events` 之前插入新节）
- Test: `tests/test_exam_ledger.py`

**Interfaces:**
- Consumes: `PRODID`（`exporter.py:60`）、`CalendarEvent`
- Produces:
  - `LedgerEntry(uid: str, start: datetime, end: datetime, summary: str, location: str | None = None, description: str | None = None)`（frozen dataclass）
  - `render_exam_ledger(live: Sequence[CalendarEvent], pending: Sequence[LedgerEntry], *, dtstamp: datetime) -> str`

- [ ] **Step 1: 写失败测试**

`tests/test_exam_ledger.py` 的 import 组补两行（`from xjtu_calendar.exams import LedgerEntry,
render_exam_ledger`）；**只补用得上的**，`ruff check .` 会因 F401 直接报未用 import。

```python
def entry(uid: str, day: date, start: str = "15:00", end: str = "17:30") -> LedgerEntry:
    return LedgerEntry(
        uid=uid,
        start=combine(day, start),
        end=combine(day, end),
        summary=f"示例课程{uid}（结课考试）",
        location="兴庆 A-1001",
        description="座位号：NN",
    )


def test_render_exam_ledger_is_minimal_but_parseable():
    text = render_exam_ledger([], [entry("a", date(2030, 6, 17))], dtstamp=STAMP)
    assert text.startswith("BEGIN:VCALENDAR\r\n")
    assert "BEGIN:VEVENT" in text
    # spec D10/D22：台账不是发布产物，不许带这些字段
    assert "STATUS" not in text
    assert "SEQUENCE" not in text
    assert "LAST-MODIFIED" not in text
    assert "METHOD" not in text
    assert "X-WR-CALNAME" not in text
    # 台账必须自带 VTIMEZONE：读取端要拿回 aware datetime（Task 4 的 naive 门槛靠它）
    assert "BEGIN:VTIMEZONE" in text
    assert "TZID=Asia/Shanghai" in text


def test_render_exam_ledger_dedupes_with_live_winning():
    live = CalendarEvent(
        uid="dup",
        summary="新的 live 形态",
        start=combine(date(2030, 6, 18), "09:00"),
        end=combine(date(2030, 6, 18), "11:00"),
    )
    text = render_exam_ledger(
        [live],
        [
            LedgerEntry(
                uid="dup",
                start=combine(date(2030, 6, 17), "15:00"),
                end=combine(date(2030, 6, 17), "17:30"),
                summary="旧形态",
            )
        ],
        dtstamp=STAMP,
    )
    assert text.count("UID:dup") == 1
    assert "新的 live 形态" in text
    assert "旧形态" not in text


def test_render_exam_ledger_orders_by_start_then_uid():
    entries = [entry("b", date(2030, 6, 20)), entry("a", date(2030, 6, 17))]
    text = render_exam_ledger([], entries, dtstamp=STAMP)
    assert text.index("UID:a") < text.index("UID:b")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `ImportError: cannot import name 'LedgerEntry'`

- [ ] **Step 3: 写最小实现**

`exams.py` 顶部 import 补 `PRODID`（同 `UID_DOMAIN` 那行，仍是顶层）、并补 `from datetime import date, datetime`：

```python
from .exporter import PRODID, UID_DOMAIN  # 顶层导入；exporter 反向只在函数内 import（避免循环）
```

新增（放在 `build_exam_events` 之前，节标题注释写明"台账"）：

```python
@dataclass(frozen=True)
class LedgerEntry:
    """台账里的一条：某场**曾以 live 形态发布**的考试，保留最后一次发布的字段。

    只在本地存在（spec §8），`start`/`end` 必须是带 tz 的 datetime —— 保留期要和 aware 的
    墙钟比较（spec D9/D22）。
    """

    uid: str
    start: datetime
    end: datetime
    summary: str
    location: str | None = None
    description: str | None = None


def render_exam_ledger(
    live: Sequence[CalendarEvent],
    pending: Sequence[LedgerEntry],
    *,
    dtstamp: datetime,
) -> str:
    """渲染台账：``live`` 的全字段 ∪ ``pending`` 的原字段，按 ``(start, uid)`` 全序。

    台账不是发布产物（spec D10/D22）：**不写** STATUS / SEQUENCE / LAST-MODIFIED /
    METHOD / X-WR-CALNAME，也不走 :func:`xjtu_calendar.exporter.render_ics`（那个函数
    无条件写 DTSTAMP/SEQUENCE，会把 D10 立刻推翻）。同 UID 时 live 形态胜出。
    ``add_missing_timezones()`` 必须调用：读取端要靠 TZID 拿回 aware datetime。
    """
    from icalendar import Calendar, Event

    chosen: dict[str, LedgerEntry] = {}
    for item in pending:
        chosen.setdefault(item.uid, item)
    for event in live:
        chosen[event.uid] = LedgerEntry(
            uid=event.uid,
            start=event.start,
            end=event.end,
            summary=event.summary,
            location=event.location,
            description=event.description,
        )

    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    for item in sorted(chosen.values(), key=lambda e: (e.start, e.uid)):
        component = Event()
        component.add("uid", item.uid)
        component.add("dtstamp", dtstamp)
        component.add("dtstart", item.start)
        component.add("dtend", item.end)
        if item.summary:
            component.add("summary", item.summary)
        if item.location:
            component.add("location", item.location)
        if item.description:
            component.add("description", item.description)
        cal.add_component(component)
    if chosen:
        cal.add_missing_timezones()
    raw: bytes = cal.to_ical()
    return raw.decode("utf-8")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/exams.py tests/test_exam_ledger.py
git commit -m "feat(exams): 台账数据类与 writer（不含 STATUS/SEQUENCE）"
```

---

## Task 4: 台账 reader（含 naive 丢弃与坏文本降级）

**Files:**
- Modify: `src/xjtu_calendar/exams.py`（紧接 `render_exam_ledger`）
- Test: `tests/test_exam_ledger.py`

**Interfaces:**
- Consumes: `LedgerEntry`（Task 3）、`render_exam_ledger`（往返测试用）
- Produces: `load_exam_ledger(text: str) -> dict[str, LedgerEntry]` —— **不抛异常**

- [ ] **Step 1: 写失败测试**

```python
def test_ledger_round_trip_keeps_uids_and_aware_times():
    entries = [entry("a", date(2030, 6, 17)), entry("b", date(2030, 6, 20))]
    back = load_exam_ledger(render_exam_ledger([], entries, dtstamp=STAMP))
    assert sorted(back) == ["a", "b"]
    assert back["a"].start.tzinfo is not None, "读回 naive 会让 D9 的比较 TypeError"
    assert back["a"].start == entries[0].start
    assert back["a"].location == "兴庆 A-1001"


def test_load_exam_ledger_drops_naive_entries():
    """手工造一条 `DTSTART;VALUE=DATE` 式的无 tz 行：必须丢弃并 warning，不许抛。"""
    text = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:x\r\nBEGIN:VEVENT\r\n"
        "UID:naive\r\nDTSTART:20300617T150000\r\nDTEND:20300617T173000\r\n"
        "SUMMARY:无 tz\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with captured_logs() as records:
        got = load_exam_ledger(text)
    assert got == {}
    assert any("naive" in rec.getMessage() or "时区" in rec.getMessage() for rec in records)


def test_load_exam_ledger_survives_garbage_text():
    """spec D15：台账坏文本 ⇒ 当作没有台账，一条 warning，绝不炸穿导出。"""
    with captured_logs() as records:
        assert load_exam_ledger("BEGIN:VCALENDAR\r\n\xff\xfe 不是 ICS") == {}
    assert any("台账" in rec.getMessage() for rec in records)


def test_load_exam_ledger_keeps_first_of_duplicate_uids():
    one = render_exam_ledger([], [entry("a", date(2030, 6, 17))], dtstamp=STAMP)
    two = one.replace("示例课程a", "后来的形态")
    merged = one.replace("\r\nEND:VCALENDAR\r\n", "") + two.replace(
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n", ""
    )
    got = load_exam_ledger(merged)
    assert len(got) == 1
    assert got["a"].summary == "示例课程a"
```

> 上面那条重复 UID 用例是"两段 VEVENT 同 UID"的拼法。若实现里 icalendar 对拼出来的文本
> 直接报错，就改用 `render_exam_ledger` 之外手工写完整文档（两条 VEVENT 都放进同一个
> `VCALENDAR`），断言不变：**保留第一条**。

> 本任务往 import 组里补 `load_exam_ledger`。

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `ImportError: cannot import name 'load_exam_ledger'`

- [ ] **Step 3: 写最小实现**

```python
def load_exam_ledger(text: str) -> dict[str, LedgerEntry]:
    """台账文本 → ``{UID: LedgerEntry}``。

    **不抛异常**（spec D15）：不可解析 ⇒ 一条 warning + 空 dict，调用方按"没有台账"继续。
    丢弃：无 UID、起止缺失或相等、**起止为 naive datetime**（spec D22：D9 要和 aware
    墙钟比较，naive 值会 ``TypeError``）、重复 UID（保留第一条）。
    """
    from icalendar import Calendar

    try:
        cal = Calendar.from_ical(text)
    except Exception as exc:  # 截断、编码坏、结构走样都归这一类
        logger.warning("考试台账不可解析，本次按没有台账处理：%s", exc)
        return {}

    entries: dict[str, LedgerEntry] = {}
    for component in cal.walk("VEVENT"):
        uid = str(component.get("uid") or "").strip()
        if not uid:
            logger.warning("考试台账里有一条没有 UID，已丢弃")
            continue
        if uid in entries:
            logger.warning("考试台账里 UID 重复：%s，保留第一条", uid)
            continue
        start = getattr(component.get("dtstart"), "dt", None)
        end = getattr(component.get("dtend"), "dt", None)
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            logger.warning("考试台账条目 %s 的起止不是 DATE-TIME，已丢弃", uid)
            continue
        if start.tzinfo is None or end.tzinfo is None:
            logger.warning("考试台账条目 %s 的起止没有时区，已丢弃（否则保留期比较会失败）", uid)
            continue
        if end <= start:
            logger.warning("考试台账条目 %s 的结束不晚于开始，已丢弃", uid)
            continue
        entries[uid] = LedgerEntry(
            uid=uid,
            start=start,
            end=end,
            summary=str(component.get("summary") or ""),
            location=_optional_text(component.get("location")),
            description=_optional_text(component.get("description")),
        )
    return entries
```

`_optional_text` 是本任务新加的模块级私有小函数（`exams.py` 里已有 `_text`，但那个吃 Mapping 且把缺失写成 `""`，台账要的是"缺失即 `None`"，两件事不要混用）：

```python
def _optional_text(value: object) -> str | None:
    """icalendar 属性值 → 可选文本（空值一律 ``None``，与渲染端的条件化对齐）。"""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（四条 + Task 3 的三条全绿）

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/exams.py tests/test_exam_ledger.py
git commit -m "feat(exams): 台账读取端（丢 naive、坏文本降级）"
```

---

## Task 5: `cancel_candidates`（四个筛子与全序）

**Files:**
- Modify: `src/xjtu_calendar/exams.py`
- Test: `tests/test_exam_ledger.py`

**Interfaces:**
- Consumes: `LedgerEntry`
- Produces: `cancel_candidates(*, ledger: Mapping[str, LedgerEntry], live_uids: Collection[str], baseline_uids: Collection[str], now: datetime) -> tuple[list[LedgerEntry], list[LedgerEntry]]`（返回 `(deliverable, unresolvable)`）

- [ ] **Step 1: 写失败测试**

```python
NOW = combine(date(2030, 6, 1), "08:00")  # 所有 2030-06-1x 的考试都还没到


def ledger_of(*uids_and_days: tuple[str, date]) -> dict[str, LedgerEntry]:
    return {uid: entry(uid, day) for uid, day in uids_and_days}


def test_candidate_only_when_absent_from_live():
    ledger = ledger_of(("a", date(2030, 6, 17)), ("b", date(2030, 6, 20)))
    deliverable, _ = cancel_candidates(
        ledger=ledger, live_uids={"b"}, baseline_uids={"a", "b"}, now=NOW
    )
    assert [e.uid for e in deliverable] == ["a"]


def test_expired_entries_are_not_cancelled():
    """spec D2/D9：原定时刻已过 ⇒ 什么都不发，也不留在结果里。"""
    ledger = ledger_of(("past", date(2030, 5, 1)))
    deliverable, unresolved = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids={"past"}, now=NOW
    )
    assert deliverable == [] and unresolved == []


def test_missing_from_baseline_is_unresolvable_not_emitted():
    """spec D20：基线里没有该 UID ⇒ resolve_sequence 会给 0，宁可不撤销。"""
    ledger = ledger_of(("ghost", date(2030, 6, 17)))
    deliverable, unresolved = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids=set(), now=NOW
    )
    assert deliverable == []
    assert [e.uid for e in unresolved] == ["ghost"]


def test_total_order_by_start_then_uid():
    ledger = ledger_of(("b", date(2030, 6, 17)), ("a", date(2030, 6, 17)), ("c", date(2030, 6, 10)))
    deliverable, _ = cancel_candidates(
        ledger=ledger, live_uids=set(), baseline_uids={"a", "b", "c"}, now=NOW
    )
    assert [e.uid for e in deliverable] == ["c", "a", "b"]  # 同日按 uid 升序，全序
```

> 本任务往 import 组里补 `cancel_candidates`。

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `ImportError: cannot import name 'cancel_candidates'`

- [ ] **Step 3: 写最小实现**

```python
def cancel_candidates(
    *,
    ledger: Mapping[str, LedgerEntry],
    live_uids: Collection[str],
    baseline_uids: Collection[str],
    now: datetime,
) -> tuple[list[LedgerEntry], list[LedgerEntry]]:
    """台账 − live ⇒ ``(可下发的撤销候选, 无法安全下发的候选)``，都按 ``(start, uid)`` 全序。

    三道筛子对应设计文档的三条决策：

    - ``live_uids``：**未经日期过滤**的考试 UID（D19）。过滤后的集合会把窗口外考试
      当成"消失"，一次局部导出就剪掉全局订阅状态。
    - ``now``：墙钟。原定开始时刻已过 ⇒ 既不下发也不保留（D2/D9），条目就此退出台账。
    - ``baseline_uids``：上一次发布产物的 UID 集合。不在其中的候选进 ``unresolvable``：
      ``resolve_sequence`` 会给 ``SEQUENCE:0``，而客户端对更低序号应当忽略 ⇒ 发了等于
      没发，还骗自己"撤销过了"（D20）。调用方要把 ``unresolvable`` 记 warning 并**从
      台账剪掉**。

    同日两场考试是实测见过的（spec §4.1），所以排序必须是全序。
    """
    deliverable: list[LedgerEntry] = []
    unresolvable: list[LedgerEntry] = []
    for item in ledger.values():
        if item.uid in live_uids:
            continue
        if item.start < now:
            continue
        if item.uid not in baseline_uids:
            unresolvable.append(item)
            continue
        deliverable.append(item)
    key = lambda e: (e.start, e.uid)  # noqa: E731 —— 两处排序同一口径，不提公共函数
    return sorted(deliverable, key=key), sorted(unresolvable, key=key)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/exams.py tests/test_exam_ledger.py
git commit -m "feat(exams): 撤销候选算法（过滤前 live 集 + 墙钟过期 + 基线可发性）"
```

---

## Task 6: `build_cancellation_events`（最小字段）

**Files:**
- Modify: `src/xjtu_calendar/exams.py`
- Test: `tests/test_exam_ledger.py`

**Interfaces:**
- Consumes: `LedgerEntry`、`CalendarEvent(status=...)`（Task 1）
- Produces: `build_cancellation_events(entries: Sequence[LedgerEntry]) -> list[CalendarEvent]`
- Produces: 模块常量 `EXAM_STATUS_CANCELLED = "CANCELLED"`

- [ ] **Step 1: 写失败测试**

```python
def test_cancellation_event_copies_uid_and_times_verbatim():
    src = entry("a", date(2030, 6, 17))
    (event,) = build_cancellation_events([src])
    assert event.uid == src.uid
    assert event.start == src.start and event.end == src.end
    assert event.status == EXAM_STATUS_CANCELLED
    # spec D3：最小字段。考场/座位/教师都不再公开。
    assert event.summary == ""
    assert event.location is None
    assert event.description is None


def test_cancellation_events_keep_input_order():
    entries = [entry("b", date(2030, 6, 20)), entry("a", date(2030, 6, 17))]
    assert [e.uid for e in build_cancellation_events(entries)] == ["b", "a"]
```

> 本任务往 import 组里补 `build_cancellation_events` 与 `EXAM_STATUS_CANCELLED`。

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `ImportError: cannot import name 'build_cancellation_events'`

- [ ] **Step 3: 写最小实现**

```python
#: 撤销事件的 ``STATUS`` 值。台账里**不**写这个字段（spec D10）。
EXAM_STATUS_CANCELLED = "CANCELLED"


def build_cancellation_events(entries: Sequence[LedgerEntry]) -> list[CalendarEvent]:
    """台账条目 → 待发布的撤销事件（最小字段，spec D3）。

    UID **照抄不重算**：撤销的全部前提就是复用已发布出去的那个 UID，客户端按 UID 匹配
    才谈得上删除。``summary=""`` 配合 :func:`xjtu_calendar.exporter.render_ics` 的条件化
    即"不写 SUMMARY 行"（实测 ``add("summary", "")`` 会写出空值行）。
    顺序保持调用方给的全序，本函数不重排。
    """
    return [
        CalendarEvent(
            uid=item.uid,
            summary="",
            start=item.start,
            end=item.end,
            location=None,
            description=None,
            meeting=None,
            status=EXAM_STATUS_CANCELLED,
        )
        for item in entries
    ]
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/exams.py tests/test_exam_ledger.py
git commit -m "feat(exams): 撤销事件构建（最小字段，UID 照抄）"
```

---

## Task 7: 共享固件 + exporter 的门槛与撤销注入

**Files:**
- Modify: `tests/exam_support.py`（新增 `exam_home`）
- Modify: `src/xjtu_calendar/exporter.py:680-744`
- Test: `tests/test_exporter_cancellation.py`（新建）

**Interfaces:**
- Consumes: `load_exam_ledger` / `cancel_candidates` / `build_cancellation_events` / `render_exam_ledger` / `Settings.exam_ledger_path`
- Produces: 产物里出现 `STATUS:CANCELLED`；`CalendarEvent` 集合不变量（全局 UID 唯一）继续成立

- [ ] **Step 1: 加共享固件**

`tests/exam_support.py` 末尾追加（**不要**去改 `tests/test_exporter_exams.py:32` 的 `_home_with_exams`，那个文件保持原样）：

```python
def exam_home(tmp_path: Path, *rows: dict[str, str], semester: str = SEMESTER) -> Settings:
    """带考试快照的 home：课表走真信封，考试快照手工落盘。

    `subscribe_support.make_home` 写的是 ``{"kbList": rows}`` 简写信封，
    `campus_names_from_timetable` 不认（设计文档 §9 点名的固件改造点，经裁定不改那个
    文件）。撤销通路的用例需要「课表 + 考试」都在真信封里，故在此提供第二份固件。
    """
    cfg = make_home(tmp_path, semester=semester)
    cfg.raw_timetable_path(semester).write_text(
        json.dumps(timetable_envelope([payload_row()]), ensure_ascii=False), encoding="utf-8"
    )
    save_raw(exam_payload(list(rows) or [exam_row()]), cfg, semester, kind="exams")
    return cfg


def write_ledger(cfg: Settings, semester: str, entries: Sequence[LedgerEntry]) -> Path:
    """把台账条目直接落成文件（writer 由 Task 3 提供，这里不重造）。"""
    path = cfg.exam_ledger_path(semester)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_exam_ledger([], list(entries), dtstamp=STAMP), encoding="utf-8", newline=""
    )
    return path


#: 保留期比较用的墙钟。`exam_row()` 的默认考期是 2030-06-17，所以 `NOW` 之下"时刻未过"
#: 是默认态，`PAST` 用来测过期退场（spec D2/D9）。
NOW = combine(date(2030, 6, 1), "08:00")
PAST = combine(date(2030, 7, 1), "08:00")


def exam_uids(cfg: Settings, *rows: dict[str, str]) -> list[str]:
    """走**真实管线**拿本次会发布的考试 UID。

    不要在测试里重算 sha256：那是把 UID 配方抄第二份，配方一改测试跟着改，看守就废了
    （与 spec 里"diff 的配对键必须委托 `_uid_token`"同一个理由）。
    """
    parsed = parse_exam_rows(
        exam_payload(list(rows) or [exam_row()]),
        campus_names={},
        report=ParseReport(),
    )
    return [make_exam_uid(SEMESTER, exam) for exam in parsed]


def published_ledger(cfg: Settings, *rows: dict[str, str]) -> list[LedgerEntry]:
    """把"上次发布过的考试"抄成台账条目（UID 与起止都取真实管线口径）。"""
    parsed = parse_exam_rows(
        exam_payload(list(rows) or [exam_row()]), campus_names={}, report=ParseReport()
    )
    return [
        LedgerEntry(
            uid=make_exam_uid(SEMESTER, exam),
            start=combine(date.fromisoformat(exam.date_str), exam.start_time),
            end=combine(date.fromisoformat(exam.date_str), exam.end_time),
            summary=f"{exam.course_name}（结课考试）",
            location=f"兴庆 {exam.location}" if exam.location else None,
            description=f"座位号：{exam.seat}" if exam.seat else None,
        )
        for exam in parsed
    ]
```

需要的补充 import（按 ruff 分组）：`json`、`pathlib.Path`、`datetime.date`、
`typing.Sequence`、`from xjtu_calendar.exams import LedgerEntry, make_exam_uid,
parse_exam_rows, render_exam_ledger`、`from xjtu_calendar.fetcher import save_raw`、
`from xjtu_calendar.parser import ParseReport`、`from xjtu_calendar.schedules import combine`、
`from subscribe_support import make_home, payload_row`、`from xjtu_calendar.config import Settings`，
以及模块级 `STAMP = combine(date(2030, 1, 1), "00:00")`。

> `exam_uids` / `published_ledger` / `NOW` / `PAST` 放在 `exam_support.py` 而不是某个测试
> 文件里，因为 T7、T9、T10、T11 四个测试文件都要用；测试文件之间互不 import。

- [ ] **Step 2: 建 `tests/test_exporter_cancellation.py` 并写撤销正例**

```python
"""撤销注入：门槛（D18）、过滤前候选（D19）、可发性（D20）、关开关（D4/D21）。

这个文件是整条通路**最该被证伪**的地方：`live = ∅` 在这里等于"取消整学期"，
所以三条降级用例与撤销正例**同权重**，一条都不许省。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from exam_support import (
    NOW,
    PAST,
    SEMESTER,
    captured_logs,
    exam_home,
    exam_payload,
    exam_row,
    exam_uids,
    published_ledger,
    write_ledger,
)
from icalendar import Calendar

from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.exams import LedgerEntry
from xjtu_calendar.fetcher import save_raw
from xjtu_calendar.schedules import combine
```

（`NOW` / `PAST` / `exam_uids` / `published_ledger` 全部来自 Step 1 的 `exam_support.py`，
本文件不重复定义，测试文件之间也不互 import。）

正例（两场考试都发布过，其中一场从快照里消失）：

```python
def test_vanished_exam_is_cancelled_in_the_next_export(tmp_path: Path) -> None:
    gone_row = exam_row(WID="WID-GONE")
    stay_row = exam_row(WID="WID-STAYS", KCM="示例课程乙")
    cfg = exam_home(tmp_path, gone_row, stay_row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, gone_row, stay_row))

    # 下次 fetch 之后 GONE 不再出现：直接覆写考试快照
    save_raw(exam_payload([stay_row]), cfg, SEMESTER, kind="exams")
    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    cancelled_uid = exam_uids(cfg, gone_row)[0]
    vevent = next(
        v
        for v in Calendar.from_ical(result.ics).walk("VEVENT")
        if str(v.get("uid")) == cancelled_uid
    )
    assert str(vevent.get("status")) == "CANCELLED"
    assert vevent.get("location") is None, "spec D3：撤销条目不许继续公开考场"
    assert vevent.get("description") is None, "spec D3：座位号与主考教师不许再出现"
    assert result.info["exam_cancellations"] == 1
```

- [ ] **Step 3: 再补五条用例（每条先跑确认红或确认它有牙）**

```python
def test_missing_exam_snapshot_cancels_nothing(tmp_path: Path) -> None:
    """D18：没抓过考试 ≠ 没有考试。台账非空也必须一条不撤。"""
    cfg = exam_home(tmp_path)
    ledger = write_ledger(cfg, SEMESTER, published_ledger(cfg))
    before = ledger.read_bytes()
    cfg.raw_exams_path(SEMESTER).unlink()

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is None
    assert ledger.read_bytes() == before, "门槛没过时台账一个字节都不许动（spec §6.6）"
    assert any("考试" in rec.getMessage() for rec in records)


def test_corrupt_exam_snapshot_cancels_nothing(tmp_path: Path) -> None:
    """D18：GBK 坏字节这类 `except Exception` 降级同样收回撤销权。

    形状对应 ``tests/test_exporter_exams.py:204-215``（那条断言的是"产物等于无考试"）；
    这里断言的是**撤销侧**：不得出现 STATUS:CANCELLED，也不得改写台账。
    """
    cfg = exam_home(tmp_path)
    before = write_ledger(cfg, SEMESTER, published_ledger(cfg)).read_bytes()
    cfg.raw_exams_path(SEMESTER).write_bytes(b"\xff\xfe\x00not-utf-8-at-all")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert cfg.exam_ledger_path(SEMESTER).read_bytes() == before


def test_all_rows_unparseable_cancels_nothing(tmp_path: Path) -> None:
    """D18：快照有行、但一条都构建不出来 ⇒ 不可信（与"确认无考试"区分开）。"""
    cfg = exam_home(tmp_path, exam_row(KSSJMS="明天下午"))
    write_ledger(cfg, SEMESTER, published_ledger(cfg))

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is None


def test_confirmed_empty_snapshot_cancels_everything_published(tmp_path: Path) -> None:
    """NO_EXAMS（`rows` 为空的可信快照）**应当**撤销全部已发布考试。"""
    rows = [exam_row(WID="WID-A"), exam_row(WID="WID-B", KCM="示例课程乙")]
    cfg = exam_home(tmp_path, *rows)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, *rows))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert result.ics.count("STATUS:CANCELLED") == 2
    assert result.info["exam_cancellations"] == 2


def test_expired_entries_are_neither_cancelled_nor_kept(tmp_path: Path) -> None:
    """D2/D9：原定时刻已过 ⇒ 什么都不发，条目就此退出台账。"""
    cfg = exam_home(tmp_path)
    write_ledger(cfg, SEMESTER, published_ledger(cfg))

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=PAST)

    assert "STATUS:CANCELLED" not in result.ics
    assert result.exam_ledger_text is not None  # 读到过台账 ⇒ 必须回写（把过期条目剪掉）
    assert result.info["exam_cancellations"] == 0


def test_from_date_filter_does_not_cancel_out_of_window_exams(tmp_path: Path) -> None:
    """D19：窗口外的考试不算"消失"，既不撤销也不剪台账。"""
    inside = exam_row(WID="WID-IN")
    outside = exam_row(
        WID="WID-OUT",
        KCM="示例课程乙",
        KSRQ="2030-09-01 00:00:00",
        KSSJMS="2030-09-01 09:00-11:00(星期日)",
    )
    cfg = exam_home(tmp_path, inside, outside)
    ledger_entries = [
        *published_ledger(cfg, inside),
        LedgerEntry(
            uid=exam_uids(cfg, outside)[0],
            start=combine(date(2030, 9, 1), "09:00"),
            end=combine(date(2030, 9, 1), "11:00"),
            summary="示例课程乙（结课考试）",
        ),
    ]
    write_ledger(cfg, SEMESTER, ledger_entries)

    result = build_ics_for_semester(
        cfg, SEMESTER, from_date="2030-06-01", to_date="2030-06-30", cancel_expiry_at=NOW
    )

    assert "STATUS:CANCELLED" not in result.ics


def test_uid_absent_from_baseline_is_dropped_and_warned(tmp_path: Path) -> None:
    """D20：留底/基线里没有该 UID ⇒ 不发 `SEQUENCE:0` 的撤销，warning + 移出台账。"""
    cfg = exam_home(tmp_path)
    entries = published_ledger(cfg)
    write_ledger(cfg, SEMESTER, entries)
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    with captured_logs() as records:
        # baseline_probe 指向不存在的文件 ⇒ `baseline` 为空 ⇒ UID 不在基线里
        result = build_ics_for_semester(
            cfg, SEMESTER, baseline_probe=str(tmp_path / "no-such.ics"), cancel_expiry_at=NOW
        )

    assert "STATUS:CANCELLED" not in result.ics
    assert any("无法安全下发撤销" in rec.getMessage() for rec in records)
    assert result.exam_ledger_text is not None
    assert entries[0].uid not in result.exam_ledger_text


def test_no_exams_emits_neither_live_nor_cancellations(tmp_path: Path) -> None:
    """D4/D21：关开关时撤销与 live 考试一同缺席，且**不产出**台账文本。"""
    cfg = exam_home(tmp_path)
    before = write_ledger(cfg, SEMESTER, published_ledger(cfg)).read_bytes()
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, include_exams=False)

    assert "STATUS:CANCELLED" not in result.ics
    assert "VEVENT" in result.ics  # 课程侧照常
    assert result.exam_ledger_text is None
    assert cfg.exam_ledger_path(SEMESTER).read_bytes() == before


def test_cancellations_do_not_mask_the_empty_calendar_warning(tmp_path: Path) -> None:
    """课程与 live 考试全空、台账非空 ⇒ 仍要打"没有生成任何事件"。

    判空看的是**撤销之前**的集合（`exporter.py:745-747` 的既有语义：课表才是主功能，
    那句告警的措辞本身就写着"可能全部落在停课日期或被日期过滤排除"）。撤销条目
    不该把一份其实没什么内容的日历撑成"正常"。
    """
    cfg = exam_home(tmp_path)
    write_ledger(cfg, SEMESTER, published_ledger(cfg))
    cfg.raw_timetable_path(SEMESTER).unlink()
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    assert any("没有生成任何事件" in rec.getMessage() for rec in records)
    assert "STATUS:CANCELLED" in result.ics  # 撤销照发，只是不许掩盖告警
```

Run: `"$PY" -m pytest tests/test_exporter_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: 撤销正例与 `test_confirmed_empty_*` FAIL（还没有注入）；三条 D18 降级用例此时会
**意外通过**——这是预期的：它们的看守价值在 Step 6 的破坏性验证里才兑现。

- [ ] **Step 4: 写实现（`exporter.build_ics_for_semester`）**

在 `exam_events: list[CalendarEvent] = []`（`:680`）之后加门槛标志，并在三个降级点各赋值一次：

```python
    exam_events: list[CalendarEvent] = []
    #: 撤销通路的准入门槛（spec D18）：只有"快照读到、解析没炸、且不是一批全失败"才可信。
    #: `live = ∅` 在撤销语义下是"取消整学期"，所以这里必须是**白名单**而不是默认放开。
    exams_trusted = False
```

`if exams_source is not None:` 分支内，`parsed = parse_exam_rows(...)` 之后、
`exam_events = build_exam_events(parsed, semester)` 之后：

```python
exams_trusted = True  # 读到了且解析没炸：包括"确认本学期无考试"的空快照
exam_events = build_exam_events(parsed, semester)
if not exam_events:
    if parsed:
        # 有行却一条都构建不出来 ⇒ 字段形态变了/解析层出事，这不是"没有考试"。
        exams_trusted = False
        logger.warning("考试快照有 %d 行但一条都没解析出来，本次不启用撤销通路", len(parsed))
    else:
        logger.info("本学期暂无考试安排（考试快照为空）")
```

`except Exception as exc:` 分支里 `exam_events = []` 之后补一行 `exams_trusted = False`。

在日期过滤块**之前**（`:719` 之前）捕获过滤前的 live UID，并写清为什么不能放在过滤之后：

```python
    #: 撤销候选要用**未经日期过滤**的 live 集（spec D19）：下面的 `--from-date/--to-date`
    #: 会连考试一起裁，用过滤后的集合就会把"窗口外"当成"已消失"而撤销并剪台账。
    #: 台账回写也用同一份**未过滤**的事件列表，两处必须同源（Task 8 的 `live=` 参数）。
    pre_filter_exam_events = list(exam_events)
    live_exam_uids = {event.uid for event in exam_events}
```

撤销注入放在**基线算出来之后**（`:787` 之后、`stamp` 之前），因为 D20 需要 `baseline`：

```python
# --- 撤销注入（docs/design/2026-10-09-exam-cancellation.md §6.5）---
cancellation_events: list[CalendarEvent] = []
pending_entries: list[LedgerEntry] = []
if include_exams and exams_trusted:
    from .exams import (
        build_cancellation_events,
        cancel_candidates,
        load_exam_ledger,
    )

    ledger_path = cfg.exam_ledger_path(semester)
    ledger_exists = ledger_path.is_file()
    ledger: dict[str, LedgerEntry] = {}
    if ledger_exists:
        ledger = load_exam_ledger(ledger_path.read_text(encoding="utf-8"))
    else:
        logger.info("本地还没有 %s 的考试台账，本次不撤销任何已发布考试", semester)
    if ledger:
        deliverable, unresolvable = cancel_candidates(
            ledger=ledger,
            live_uids=live_exam_uids,
            baseline_uids=set(baseline) if baseline else set(),
            now=cancel_expiry_at or now_local(),
        )
        for item in unresolvable:
            logger.warning(
                "考试 %s 无法安全下发撤销（发布留底里没有这个 UID，序号只能从 0 起，"
                "客户端会忽略更低的序号），本次放弃并已移出台账",
                item.uid,
            )
        cancellation_events = build_cancellation_events(deliverable)
        pending_entries = deliverable
        # 全局 UID 唯一性：撤销条目**不豁免**，处置与考试事件一致（丢弃后来者 + warning），
        # 写法与本函数 :734-742 的 `kept_exam` 同形。
        seen_uids = {event.uid for event in render_events}
        kept_cancellations: list[CalendarEvent] = []
        for event in cancellation_events:
            if event.uid in seen_uids:
                logger.warning("撤销事件 UID 与已有事件冲突，已丢弃：%s", event.uid)
                continue
            seen_uids.add(event.uid)
            kept_cancellations.append(event)
        cancellation_events = kept_cancellations
        render_events = [*render_events, *cancellation_events]
        for item in unresolvable:
            logger.warning(
                "考试 %s 无法安全下发撤销（发布留底里没有这个 UID），本次放弃并移出台账", item.uid
            )
```

> 判空告警（`exporter.py:745-747`）位置在注入点**之前**，因此天然只看课程 + live 考试；
> 撤销条目不许让它失声 —— 该语义由
> `test_cancellations_do_not_mask_the_empty_calendar_warning` 钉住。
> `render_ics(...)` 的调用点（`:794`）继续吃扩展后的 `render_events`，本身不改。

- [ ] **Step 5: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_exporter_cancellation.py tests/test_exporter_exams.py tests/test_legacy_course_export_golden.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（含 `--no-exams` 与降级相关的既有用例，一支都不许改断言）

- [ ] **Step 6: 破坏性验证（必须做，写进提交信息）**

1. 把 `exams_trusted` 的默认值改成 `True` ⇒ `test_missing_exam_snapshot_cancels_nothing` 与
   `test_corrupt_exam_snapshot_cancels_nothing` 必须同时红。
2. 把 `live_exam_uids` 改为在日期过滤之后取 ⇒ `test_from_date_filter_does_not_cancel_out_of_window_exams` 必须红。
3. 去掉 `baseline_uids` 筛子 ⇒ `test_uid_absent_from_baseline_is_not_emitted` 必须红。
4. 恢复原状，四条门禁全绿。

- [ ] **Step 7: 提交**

```bash
git add tests/exam_support.py tests/test_exporter_cancellation.py src/xjtu_calendar/exporter.py
git commit -m "feat(export): 撤销注入，并给撤销通路加数据可信门槛"
```

---

## Task 8: `sequence_stats` 口径、`exam_ledger_text` 与计数

**Files:**
- Modify: `src/xjtu_calendar/exporter.py:524-535`（`ExportResult`）、`:794-810`（渲染与 stats）
- Modify: `src/xjtu_calendar/exams.py`（`render_exam_ledger` 的调用点即 Task 7 注入块）
- Test: `tests/test_exporter_cancellation.py`

**Interfaces:**
- Consumes: `render_exam_ledger(live, pending, *, dtstamp)`、`stamp`（`:789`）
- Produces: `ExportResult.exam_ledger_text: str | None`、`info["exam_cancellations"]: int`；`sequence_stats` **不含**撤销条目

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_exporter_cancellation.py`（沿用 Task 7 的 `NOW` / `published_ledger` /
`exam_uids` 等辅助；**不要**新建 fixture 名，本文件没有 pytest fixture）：

```python
def test_stats_updated_not_polluted_by_cancellations(tmp_path: Path) -> None:
    """D23：撤销条目的 UID 在基线里是 live 形态，混进 stats 就凭空抬高 `updated`。

    这条必须可反证：把 stats 的入参改回 `render_events`（含撤销），
    `updated == 0` 立即变 `updated == 1`。
    """
    cfg = exam_home(tmp_path)
    published = build_ics_for_semester(cfg, SEMESTER)  # 此刻考试还是 live
    baseline = tmp_path / "published.ics"
    baseline.write_text(published.ics, encoding="utf-8", newline="")
    write_ledger(cfg, SEMESTER, published_ledger(cfg))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")  # 确认无考试 ⇒ 全部撤销

    result = build_ics_for_semester(
        cfg, SEMESTER, baseline_probe=str(baseline), cancel_expiry_at=NOW
    )

    assert result.info["exam_cancellations"] == 1
    stats = result.sequence_stats
    assert stats is not None
    published_events = published.ics.count("BEGIN:VEVENT")
    assert stats["preserved"] == published_events - 1  # 少的那条正是被撤销的考试
    assert stats["updated"] == 0
    assert stats["added"] == 0


def test_ledger_text_carries_live_plus_in_window_pending(tmp_path: Path) -> None:
    cfg = exam_home(tmp_path, exam_row(WID="WID-A"), exam_row(WID="WID-B", KCM="示例课程乙"))
    entries = published_ledger(cfg, exam_row(WID="WID-A"), exam_row(WID="WID-B"))
    write_ledger(cfg, SEMESTER, entries)
    save_raw(exam_payload([exam_row(WID="WID-A")]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=NOW)

    text = result.exam_ledger_text
    assert text is not None
    assert "STATUS" not in text and "SEQUENCE" not in text  # D10
    assert entries[0].uid in text  # 本次仍 live
    assert entries[1].uid in text  # 已撤销但仍在窗口内 ⇒ 留在台账里以便下次复述


def test_ledger_text_none_when_neither_ledger_nor_exams(tmp_path: Path) -> None:
    """`None` ⟺ 调用方不得创建也不得改写文件（spec §6.6 的空值口径）。"""
    cfg = exam_home(tmp_path)
    cfg.raw_exams_path(SEMESTER).unlink()
    assert not cfg.exam_ledger_path(SEMESTER).exists()

    assert build_ics_for_semester(cfg, SEMESTER).exam_ledger_text is None


def test_stale_ledger_is_pruned_into_text_when_everything_expired(tmp_path: Path) -> None:
    """读到过台账 ⇒ 必须回写，哪怕剪完什么都不剩（否则过期条目永不退场）。"""
    cfg = exam_home(tmp_path)
    entries = published_ledger(cfg)
    write_ledger(cfg, SEMESTER, entries)
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")

    result = build_ics_for_semester(cfg, SEMESTER, cancel_expiry_at=PAST)

    assert result.exam_ledger_text is not None
    assert entries[0].uid not in result.exam_ledger_text
```

既有 R1 用例（`tests/test_exporter_exams.py:218-244`，断言新增考试 `added == 1`）**不改、
必须原样通过**——D23 的口径调整正是为它保的。

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_exporter_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `AttributeError: 'ExportResult' object has no attribute 'exam_ledger_text'`，
以及 `KeyError: 'exam_cancellations'`

- [ ] **Step 3: 写实现**

`ExportResult`（非 frozen，字段**追加在 `sequence_stats` 之后**，构造点 `:810` 用关键字）：

```python
    ics: str
    info: dict[str, object]
    sequence_stats: dict[str, int] | None
    #: 本次应回写的考试台账文本；``None`` ⇒ 调用方**不得创建也不得改写**该文件
    #: （`--no-exams`、门槛没过、或既没读到也没东西可记）。spec §6.6。
    exam_ledger_text: str | None = None
```

注入块之后、渲染之前补台账文本（必须排在 `stamp` 之后，DTSTAMP 要与产物同源）：

```python
    ledger_text: str | None = None
    if include_exams and exams_trusted and (ledger_exists or exam_events):
        from .exams import render_exam_ledger

        ledger_text = render_exam_ledger(
            live=pre_filter_exam_events,  # **过滤前**的 live 集（D19），与候选同源
            pending=pending_entries,
            dtstamp=stamp,
        )
    info["exam_cancellations"] = len(cancellation_events)
```

> `ledger_exists` 在 Task 7 的读取分支里置一次（`ledger_path.is_file()`），用来区分
> "读到过台账"与"根本没读到"。返回条件即 spec §6.6 的空值口径：读到过台账 ⇒ 必须回写
> （哪怕剪完只剩空台账）；既没读到、本次也没有任何 live 考试 ⇒ `None`，调用方不创建文件。
> **不存在"算出空台账但不回写"的中间状态。**

`stats` 计算改口径（D23）：

```python
    stats: dict[str, int] | None = None
    if baseline is not None:
        # 撤销条目**不进** stats：它们的 UID 在基线里以 live 形态存在，指纹必变，
        # 混进来会凭空抬高 `updated`（spec D23）。数量单独走 info。
        stats = sequence_stats([*events, *kept_exam], baseline)
```

返回点补字段：`return ExportResult(ics=ics, info=info, sequence_stats=stats, exam_ledger_text=ledger_text)`

- [ ] **Step 4: 跑测试确认通过 + 破坏性验证**

1. 把 `sequence_stats([*events, *kept_exam], ...)` 改回 `sequence_stats(render_events, ...)`
   ⇒ `test_stats_updated_not_polluted_by_cancellations` 必须红。
2. 把 `ledger_text` 无条件算 ⇒ `test_ledger_text_is_none_when_untrusted_or_no_exams` 必须红。
3. 恢复原状；`build_ics_for_semester` 直接调用点的全部既有用例（`tests/test_exporter_exams.py`、
   `tests/test_cli_exams.py`、`tests/test_subscribe.py`）必须全绿 —— 新字段有默认值，
   `tests/test_cli_exams.py:57`、`tests/test_exams_fetch.py:528` 那两处构造点不需改。

Run: `"$PY" -m pytest tests/test_exporter_cancellation.py tests/test_exporter_exams.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/exporter.py tests/test_exporter_cancellation.py
git commit -m "feat(export): 台账文本出口与撤销计数，stats 口径排除撤销条目"
```

---

## Task 9: CLI `export` 的台账回写

**Files:**
- Modify: `src/xjtu_calendar/cli.py:538-542`（`cmd_export` 写产物处）
- Test: `tests/test_cli_cancellation.py`（新建）

**Interfaces:**
- Consumes: `ExportResult.exam_ledger_text`、`Settings.exam_ledger_path`、`fileutil.atomic_write_text`
- Produces: `home/subscribe/last-exams-<学期>.ics` 在产物写成功后落盘；`private=True`；写失败只 warning

- [ ] **Step 1: 写失败测试**

`tests/test_cli_cancellation.py`（新建；T9/T10/T11 共用这一个文件）：

```python
"""台账的 CLI 侧行为：写盘时序（D11）、目录缺失与退出码（D24①）、rotate 探针（D24②）。"""

from __future__ import annotations

from pathlib import Path

import pytest
from exam_support import (
    SEMESTER,
    captured_logs,
    exam_home,
    exam_payload,
    exam_row,
    published_ledger,
    write_ledger,
)

from xjtu_calendar import fileutil, subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.fetcher import save_raw


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """课表 + 台账（记着一场已发布的考试）+ 本次"确认本学期无考试" ⇒ 恰有 1 条待撤销。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    row = exam_row(WID="WID-A")
    cfg = exam_home(tmp_path, row)
    write_ledger(cfg, SEMESTER, published_ledger(cfg, row))
    save_raw(exam_payload([]), cfg, SEMESTER, kind="exams")
    return cfg


def _export(cfg: Settings, *extra: str) -> int:
    return main(["export", "--semester", SEMESTER, "-o", str(cfg.home / "out.ics"), *extra])


def _register(cfg: Settings) -> None:
    """跳过 `subscribe init`（它要真远端），直接落一份订阅状态。

    与 ``tests/test_cli_exams.py`` 的同名辅助同形；那两个文件刻意不互 import。
    """
    subscribe.save_state(
        cfg,
        subscribe.SubscriptionState(
            semester=SEMESTER,
            repo_url="https://example.invalid/calendar.git",
            branch="main",
            token="deadbeef",
            url_base="https://me.github.io/timetable/",
        ),
    )


def _stub_publish(monkeypatch: pytest.MonkeyPatch, outcome=None) -> list[str]:
    """真 publish 要动 git；这里换成 no-op，并把调用记进 order 以便断言时序。"""
    order: list[str] = []
    monkeypatch.setattr(
        subscribe,
        "publish",
        lambda _cfg, _state, _ics: (
            order.append("publish")
            or subscribe.PublishResult(
                outcome=outcome or subscribe.PublishOutcome.PUSHED,
                url="https://e.invalid/x.ics",
                content_sha256="0" * 64,
            )
        ),
    )
    return order


def test_export_writes_ledger_after_the_output(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    assert _export(cfg) == 0
    ledger = cfg.exam_ledger_path(SEMESTER)
    assert ledger.is_file()
    assert "SEQUENCE" not in ledger.read_text(encoding="utf-8")
    assert (cfg.home / "out.ics").is_file()


def test_export_creates_no_ledger_when_text_is_none(tmp_path, monkeypatch):
    """`None` ⟺ 不得创建文件（spec §6.6）：这里制造"既没台账也没考试快照"。"""
    cfg = _env(tmp_path, monkeypatch)
    cfg.raw_exams_path(SEMESTER).unlink()
    cfg.exam_ledger_path(SEMESTER).unlink()
    assert _export(cfg) == 0
    assert not cfg.exam_ledger_path(SEMESTER).exists()


def test_export_with_no_exams_leaves_ledger_untouched(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    ledger = cfg.exam_ledger_path(SEMESTER)
    before = ledger.read_bytes()
    assert _export(cfg, "--no-exams") == 0
    assert ledger.read_bytes() == before


def test_export_survives_ledger_write_failure(tmp_path, monkeypatch):
    """D24①：台账写不进去属于考试侧增量，**不得**让 export 非零退出。"""
    cfg = _env(tmp_path, monkeypatch)

    def boom(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr("xjtu_calendar.cli.atomic_write_text", boom)
    with captured_logs() as records:
        assert _export(cfg) == 0
    assert any("台账" in rec.getMessage() for rec in records)
    assert (cfg.home / "out.ics").is_file()  # 产物照写，主功能不受牵连


def test_ledger_write_requests_private(tmp_path, monkeypatch):
    """kwargs 捕获口径（照 ``tests/test_fetcher.py`` 的 private 用例）：只断 mode 证伪不了。"""
    cfg = _env(tmp_path, monkeypatch)
    seen: dict[str, object] = {}
    real = fileutil.atomic_write_text

    def spy(path, text, **kwargs):
        seen[path.name] = kwargs.get("private")
        return real(path, text, **kwargs)

    monkeypatch.setattr("xjtu_calendar.cli", "atomic_write_text", spy)
    assert _export(cfg) == 0
    assert seen[f"last-exams-{SEMESTER}.ics"] is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_cli_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — `AttributeError: module 'xjtu_calendar.cli' has no attribute 'atomic_write_text'`
（实现必须先 `from .fileutil import atomic_write_text`，见 Step 3）与"台账文件不存在"

- [ ] **Step 3: 写实现**

`cli.py` 的 import 区补 `from .fileutil import atomic_write_text`（当前 `cli.py` **没有**引入它，
`grep -n atomic_write_text src/xjtu_calendar/cli.py` 为空——测试正是按"模块属性"来 monkeypatch 的）。

`cli.py` 在 `output.write_text(result.ics, encoding="utf-8", newline="")`（`:542`）之后：

```python
    # 考试台账：产物写成功之后才回写（spec D11），且 `None` 时**不创建文件**。
    # 目录可能不存在（ensure_dirs 刻意不含 subscribe/，D24①），写入失败一律不许
    # 影响 export 的退出码——课程是主功能。
    if result.exam_ledger_text is not None:
        _write_exam_ledger(cfg, semester, result.exam_ledger_text)
```

模块级私有函数（`cli.py` 的"辅助"区，`_payload_size` 附近）：

```python
def _write_exam_ledger(cfg: Settings, semester: str, text: str) -> None:
    """回写考试台账；失败只记 warning，不改调用方的成败。"""
    path = cfg.exam_ledger_path(semester)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text, private=True)
    except OSError as exc:
        logger.warning("考试台账没能写入 %s（%s），下次仍按原台账判断撤销", path, exc)
        return
    logger.debug("考试台账已更新：%s", path)
```

> `atomic_write_text` 的 `newline="\n"` 不会破坏台账里的 CRLF（`os.fdopen(..., newline="\n")`
> 不做换行翻译）；与 `cli.py:540-542` 对产物的处理等价，但**不要**改用 `write_text`
> 默认 `newline=None`——那会在 Windows 上得到 `\r\r\n`。
> `import` 面：`from .fileutil import atomic_write_text`（若 cli.py 已引入则不重复）。
> `private=True` 的断言口径照 `tests/test_fetcher.py` 里 `test_both_snapshot_kinds_request_the_private_write`
> 的 **kwargs 捕获**写法；只断 mode 在 Windows 上会 skip、在 POSIX 上也证伪不了 `private` 参数。

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_cli_cancellation.py tests/test_cli_exams.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/cli.py tests/test_cli_cancellation.py
git commit -m "feat(cli): export 之后回写考试台账，写失败不改退出码"
```

---

## Task 10: CLI `subscribe push` 的台账回写与摘要行

**Files:**
- Modify: `src/xjtu_calendar/cli.py:1228-1256`（`_subscribe_push` 路径）
- Test: `tests/test_cli_cancellation.py`

**Interfaces:**
- Consumes: `PublishOutcome`（`subscribe.py:153`）、Task 9 的 `_write_exam_ledger`
- Produces: 台账与 `last-<学期>.ics` **同一时刻**写入；NO_CHANGE 时两者都不写

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_cli_cancellation.py`（复用本文件的 `_env` / `_register` / `_stub_publish`）：

```python
def test_push_writes_ledger_after_publish_succeeds(tmp_path, monkeypatch):
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    order = _stub_publish(monkeypatch)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert order == ["publish", "ledger-write"]  # D11：先发布成功，再动留底与台账


def test_push_failure_leaves_ledger_untouched(tmp_path, monkeypatch):
    """D11 的反证：把台账写入挪到 publish 之前，这条立刻红。"""
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    ledger = cfg.exam_ledger_path(SEMESTER)
    before = ledger.read_bytes()

    def blow_up(_cfg, _state, _ics):
        raise XjtuCalendarError("远端拒绝")

    monkeypatch.setattr(subscribe, "publish", blow_up)
    monkeypatch.setattr("xjtu_calendar.cli._write_exam_ledger", lambda *a, **k: None)
    main(["subscribe", "push", "--semester", SEMESTER])
    assert ledger.read_bytes() == before


def test_no_change_push_leaves_ledger_untouched(tmp_path, monkeypatch):
    """NO_CHANGE 早退（`cli.py:1249-1251`）时台账同样不写：此时台账本来也未变。"""
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    ledger = cfg.exam_ledger_path(SEMESTER)
    _stub_publish(monkeypatch, outcome=subscribe.PublishOutcome.NO_CHANGE)
    before = ledger.read_bytes()

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert ledger.read_bytes() == before


def test_push_summary_reports_pending_cancellations(tmp_path, monkeypatch, capsys):
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    _stub_publish(monkeypatch)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "撤销：1 条" in out
    assert "一次性导入" in out  # 不许把话说满：这类客户端仍不会自动删


def test_push_summary_omits_zero_cancellations(tmp_path, monkeypatch, capsys):
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    cfg.exam_ledger_path(SEMESTER).unlink()  # 没有台账 ⇒ 没什么可撤销
    _stub_publish(monkeypatch)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "撤销" not in capsys.readouterr().out
```

本文件顶部还需要 `from xjtu_calendar.errors import XjtuCalendarError`。
`_stub_publish` 要在 `publish` 之后追加 `"ledger-write"`：实现里通过给
`_write_exam_ledger` 套一层记录调用的 monkeypatch 完成——**Step 1 先把 `_stub_publish`
改成同时 patch `xjtu_calendar.cli._write_exam_ledger`**，把两次调用按顺序 append 进
同一个 `order` 列表，这样 `order == ["publish", "ledger-write"]` 才真的在断言时序。

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_cli_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — 台账未写 / 摘要没有撤销行

- [ ] **Step 3: 写实现**

在 `last_local.write_text(result_ics.ics, encoding="utf-8", newline="")`（`:1254`）**之后**：

```python
    if result_ics.exam_ledger_text is not None:
        _write_exam_ledger(cfg, semester, result_ics.exam_ledger_text)
```

打印区（`print(f"已发布：{res.url}")` 附近）加摘要行，口径与 `info` 一致：

```python
    cancelled = int(str(result_ics.info.get("exam_cancellations", 0)))
    if cancelled:
        print(
            f"撤销：{cancelled} 条（已发布的考试事件在本次产物中标记为取消，"
            "支持删除的客户端会移除它们；一次性导入的客户端仍需手动删除）"
        )
```

- [ ] **Step 4: 跑测试确认通过 + 破坏性验证**

把台账写入挪到 `subscribe.publish(...)` **之前** ⇒ `test_push_failure_leaves_ledger_untouched` 必须红。

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/cli.py tests/test_cli_cancellation.py
git commit -m "feat(cli): push 后回写台账并报告撤销条数"
```

---

## Task 11: CLI `subscribe rotate` 的探针警告

**Files:**
- Modify: `src/xjtu_calendar/cli.py:1268-1302`
- Test: `tests/test_cli_cancellation.py`

**Interfaces:**
- Consumes: `build_ics_for_semester`（探针调用）、`info["exam_cancellations"]`
- Produces: 换 token **之前**的警告文案；探针渲染**不落**台账

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_cli_cancellation.py`：

```python
def _ready_for_rotate(tmp_path, monkeypatch):
    """rotate 的两个前置：订阅状态已注册 + 本地有发布留底（无留底它会直接 return）。"""
    cfg = _env(tmp_path, monkeypatch)
    _register(cfg)
    last = subscribe.subscribe_dir(cfg) / f"last-{SEMESTER}.ics"
    last.parent.mkdir(parents=True, exist_ok=True)
    last.write_text("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", encoding="utf-8", newline="")
    return cfg


def test_rotate_warns_before_changing_token(tmp_path, monkeypatch, capsys):
    """D12 + D24②：警告必须在 `rotate_token` 之前，且探针**不许**抢先落台账。"""
    cfg = _ready_for_rotate(tmp_path, monkeypatch)
    order = _stub_publish(monkeypatch)  # 已含 publish / _write_exam_ledger 两处埋点
    real_rotate = subscribe.rotate_token
    monkeypatch.setattr(
        subscribe,
        "rotate_token",
        lambda *a, **k: (order.append("rotate"), real_rotate(*a, **k))[1],
    )

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    assert "永远收不到撤销" in capsys.readouterr().out
    # 探针只算不写：台账写入必须排在 rotate 与 publish 之后
    assert order[order.index("rotate") + 1 :] == ["publish", "ledger-write"]
    assert "ledger-write" not in order[: order.index("rotate")]


def test_rotate_stays_silent_when_nothing_to_cancel(tmp_path, monkeypatch, capsys):
    cfg = _ready_for_rotate(tmp_path, monkeypatch)
    cfg.exam_ledger_path(SEMESTER).unlink()
    _stub_publish(monkeypatch)

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    assert "撤销" not in capsys.readouterr().out


def test_rotate_survives_a_failing_probe(tmp_path, monkeypatch):
    """探针失败不许拖崩 rotate：撤销警告是增量，rotate 是主功能。"""
    cfg = _ready_for_rotate(tmp_path, monkeypatch)
    cfg.raw_exams_path(SEMESTER).write_bytes(b"\xff\xfe\x00not-utf-8")
    _stub_publish(monkeypatch)

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_cli_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — 没有警告 / 探针写了台账

- [ ] **Step 3: 写实现**

在 `subscribe.rotate_token(cfg, state)`（`:1271`）**之前**插入：

```python
    # rotate 会换新 token = 换订阅 URL，旧地址此后永远收不到撤销（spec D12）。
    # 这里是**探针渲染**：只取撤销数量，返回的台账文本必须丢弃（D24②——此刻产物
    # 还没发布出去，落台账等于"假装撤销过"）。
    probe = build_ics_for_semester(cfg, semester, include_exams=include_exams)
    probe_cancel = int(str(probe.info.get("exam_cancellations", 0)))
    if probe_cancel:
        print(
            f"注意：本次有 {probe_cancel} 条考试事件尚未从旧订阅地址撤销，"
            "rotate 之后旧地址将永远收不到撤销；确认要继续请重新运行 subscribe rotate。"
        )
```

> 探针失败（例如快照坏掉）不许让 rotate 直接崩：把 `build_ics_for_semester(...)` 包在
> `except XjtuCalendarError` 里，取 `probe_cancel = 0` 并 `logger.warning` —— 撤销警告
> 是"锦上添花"，rotate 本身是主功能。**注意**这条 try 不许吞掉后面真实渲染的异常。

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_cli_cancellation.py tests/test_subscribe.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/cli.py tests/test_cli_cancellation.py
git commit -m "feat(cli): rotate 换 token 前用探针渲染警告未撤销的考试"
```

---

## Task 12: 用户可见文案改口（diff 与三处注释/docstring）

**Files:**
- Modify: `src/xjtu_calendar/cli.py:1094-1099`
- Modify: `src/xjtu_calendar/exams.py:392-408`（`_exam_key` docstring）
- Modify: `tests/test_diff_exams.py:192-193`、`tests/test_exporter_exams.py:250`
- Test: `tests/test_cli_diff.py`（追加断言）

**Interfaces:**
- Consumes: 无
- Produces: 不再有"本工具不发布取消事件"的陈述

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_cli_diff.py`（复用该文件既有的 `home` fixture、`_row`、
`_write_timetable_snapshots`、`_write_exam_snapshots(home, new_rows, old_rows)`；
**不新建脚手架**）：

```python
def test_diff_cancel_note_points_at_the_new_cancel_path(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D13：取消现在会自动下发；"一次性导入型客户端仍需手动删"这半句不许丢。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, [], [exam_row(WID="WID-GONE")])  # 新快照为空 ⇒ 一条取消

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "考试变更" in out and "取消" in out
    assert "不会从已订阅的日历里自动消失" not in out, "旧断言还挂着（spec D13）"
    assert "STATUS:CANCELLED" in out, "要告诉用户取消会真的下发"
    assert "手动删除" in out, "一次性导入型客户端那半句事实没变，不许顺手删掉"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `"$PY" -m pytest tests/test_cli_diff.py::test_diff_cancel_note_points_at_the_new_cancel_path -q --basetemp=_notes/pytest-tmp`
Expected: FAIL — 旧文案里"不会从已订阅的日历里自动消失"仍在

- [ ] **Step 3: 改文案**

`cli.py` 的取消提示改为：

```python
            print(
                "注意：本次 diff 报出的取消会在下次 export/subscribe push 时以 "
                "STATUS:CANCELLED 下发；不再回源的订阅客户端会自动移除，"
                "而一次性导入的客户端（部分国产 ROM 系统日历）仍需手动删除。"
            )
```

`exams.py:398-399` 的 docstring 陈述同步改口（原句"v1 没有 `STATUS:CANCELLED` /
`METHOD:CANCEL` 通路（§7.1），旧事件会永久留在每个订阅者的日历里"已不成立），
`tests/test_diff_exams.py:192-193` 与 `tests/test_exporter_exams.py:250` 的同款陈述一并改；
四处都指向 `docs/design/2026-10-09-exam-cancellation.md`。

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_cli_diff.py tests/test_diff_exams.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS

- [ ] **Step 5: 全仓 grep 确认没有残留断言**

```bash
git grep -n "没有取消通路\|不发布取消事件\|没有 .STATUS:CANCELLED" -- src tests README.md docs
```
Expected: 只剩 `docs/design/2026-10-08-exam-schedule.md` §7.1（历史叙述，保留）与新 spec。

- [ ] **Step 6: 四条门禁 + 提交**

```bash
git add src/xjtu_calendar/cli.py src/xjtu_calendar/exams.py tests/test_diff_exams.py tests/test_exporter_exams.py tests/test_cli_diff.py
git commit -m "docs(cli): 取消通路已上线，改口 diff 提示与四处旧断言"
```

---

## Task 13: 升级安全 golden（v0.5.0 考试侧产物）

**Files:**
- Create: `tests/fixtures/legacy_exam_inputs.json`
- Create: `tests/fixtures/legacy_exam_export_v050.ics`
- Create: `tests/test_legacy_exam_export_golden.py`

**Interfaces:**
- Consumes: tag `v0.5.0` 的 `src/`（detached worktree）
- Produces: D14 的字节钉子

- [ ] **Step 1: 生成输入固件**

`tests/fixtures/legacy_exam_inputs.json` 的键与 `legacy_course_inputs.json` 完全一致
（`semester_key` / `semester_config` / `schedule_config` / `timetable_payload` /
`snapshot_mtime`），**再加一个 `exam_payload` 键**。用现成固件助手生成，不要手写 JSON：

```bash
cd <仓库根>
PYTHONPATH=src:tests "$PY" - <<'EOF'
import json
from pathlib import Path

from exam_support import SEMESTER, exam_payload, exam_row, timetable_envelope
from subscribe_support import payload_row, semester_config

bundle = {
    "_disclaimer": (
        "全合成数据：课程名/教师/教室/座位均为占位，日期是编出来的（2030-*），"
        "不对应任何真实日程。"
    ),
    "semester_key": SEMESTER,
    "semester_config": semester_config(SEMESTER),
    "schedule_config": json.loads(
        (Path("examples") / "schedule.example.json").read_text(encoding="utf-8")
    ),
    "timetable_payload": timetable_envelope([payload_row()]),
    "exam_payload": exam_payload(
        [
            exam_row(WID="WID-DEMO-1"),  # 半角冒号式：2030-06-17 15:00-17:30(星期一)
            exam_row(
                WID="WID-DEMO-2",
                KCM="示例课程乙",
                KSRQ="2030-06-20 00:00:00",
                KSSJMS="考试时间为：9.30-11.30(星期四)",  # 点分式，实测见过
            ),
        ]
    ),
    # 与 legacy_course_inputs.json 同一个钉住值：1913025600 = 2030-08-15T12:00:00Z。
    # DTSTAMP 与所有新事件的 LAST-MODIFIED 都由它推导，不钉住就没有可复现的 golden。
    "snapshot_mtime": 1913025600,
}
Path("tests/fixtures/legacy_exam_inputs.json").write_text(
    json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("wrote tests/fixtures/legacy_exam_inputs.json")
EOF
```

两条考试行都必须能被 `parse_exam_rows` 吃下（Step 2 的输出里 `exams: 2` 就是这件事的证据）。
`subscribe_support.semester_config` / `payload_row` 与 `exam_support` 的助手都在
`tests/` 里，所以 `PYTHONPATH` 要带 `tests`。

- [ ] **Step 2: 用 v0.5.0 的代码渲染基线**

在**仓库根**执行，全部路径保持仓库相对（本机绝对路径不进仓库）：

```bash
git worktree add --detach _notes/wt-v050 v0.5.0
cp tests/fixtures/legacy_exam_inputs.json _notes/wt-v050/tests/fixtures/
cd _notes/wt-v050
PYTHONPATH=src "$PY" - <<'EOF'
# 先打印来历再动手：基线的出处必须留在输出里（沿用既有 golden 的生成惯例）
import json, os, subprocess
from pathlib import Path

import xjtu_calendar
from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import build_ics_for_semester

print("module:", xjtu_calendar.__file__)
print("HEAD:", subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip())

bundle = json.loads(Path("tests/fixtures/legacy_exam_inputs.json").read_text(encoding="utf-8"))
sem = str(bundle["semester_key"])
home = Path("_notes/v050-exam-home")
home.mkdir(parents=True, exist_ok=True)
cfg = Settings(home=home)
cfg.ensure_dirs()
cfg.semester_config_path(sem).write_text(json.dumps(bundle["semester_config"], ensure_ascii=False), encoding="utf-8")
cfg.schedule_config_path().write_text(json.dumps(bundle["schedule_config"], ensure_ascii=False), encoding="utf-8")
stamp = float(bundle["snapshot_mtime"])
timetable = cfg.raw_timetable_path(sem)
timetable.write_text(json.dumps(bundle["timetable_payload"], ensure_ascii=False), encoding="utf-8")
os.utime(timetable, (stamp, stamp))
exams = cfg.raw_exams_path(sem)
exams.write_text(json.dumps(bundle["exam_payload"], ensure_ascii=False), encoding="utf-8")
os.utime(exams, (stamp, stamp))

result = build_ics_for_semester(cfg, sem)
Path("tests/fixtures/legacy_exam_export_v050.ics").write_text(result.ics, encoding="utf-8", newline="")
print("events:", result.info["events"], "exams:", result.info["exam_events"], "bytes:", len(result.ics.encode()))
EOF
cp tests/fixtures/legacy_exam_export_v050.ics ../../tests/fixtures/
cd ../..
git worktree remove _notes/wt-v050
```

Expected: `module` 落在 `_notes/wt-v050/src/...`（**跑的是 v0.5.0 的代码**，证据在输出里），
`HEAD` 等于 `v0.5.0` 那笔提交，`exams: 2`。再连跑一次并 `cmp` 两次产物：必须逐字节相同
（不可复现就没有 golden）。`_notes/wt-v050` 里的临时改动不提交，worktree 用完即删。

- [ ] **Step 3: 写 golden 用例**

`tests/test_legacy_exam_export_golden.py`：

```python
"""升级安全钉：新代码在**没有台账**时必须逐字节复现 v0.5.0 的考试侧产物（spec D14）。

基线来历：由 tag ``v0.5.0`` 的 ``src/`` 对 ``legacy_exam_inputs.json`` 渲染而成，
两次独立运行逐字节一致。本机的 venv 是 editable 安装、指向主检出的 ``src/``，
所以**必须在 detached worktree 里跑旧代码**，并靠 ``xjtu_calendar.__file__`` 与
``git rev-parse HEAD`` 的输出确认跑的是哪一份——不靠版本号（``__version__`` 读的是
已安装的 dist-info，两边都是旧号）。

重新生成（依赖升级等正当理由导致产物确实该变时）：
``git worktree add --detach <路径> v0.5.0``，在该目录以 ``PYTHONPATH=src`` 照
``lay_out_home()`` 写一次性脚本渲染，先打印 ``xjtu_calendar.__file__`` 与 HEAD 再动手；
逐行检查新产物不含真实个人信息（``LOCATION`` / ``DESCRIPTION`` / ``X-WR-CALNAME``
是真实数据会落进去的三个位置），再连同本用例一起提交。脚本绑定本机路径，故不进仓库。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import build_ics_for_semester

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INPUTS = FIXTURES / "legacy_exam_inputs.json"
GOLDEN = FIXTURES / "legacy_exam_export_v050.ics"


def lay_out_home(tmp_path: Path) -> tuple[Settings, str]:
    """按固件布置 home：三件套 + 课表快照 + **考试快照**，两份快照打同一个 mtime。"""
    bundle = json.loads(INPUTS.read_text(encoding="utf-8"))
    semester = str(bundle["semester_key"])
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    cfg.semester_config_path(semester).write_text(
        json.dumps(bundle["semester_config"], ensure_ascii=False), encoding="utf-8"
    )
    cfg.schedule_config_path().write_text(
        json.dumps(bundle["schedule_config"], ensure_ascii=False), encoding="utf-8"
    )
    stamp = float(bundle["snapshot_mtime"])
    timetable = cfg.raw_timetable_path(semester)
    timetable.write_text(
        json.dumps(bundle["timetable_payload"], ensure_ascii=False), encoding="utf-8"
    )
    os.utime(timetable, (stamp, stamp))
    exams = cfg.raw_exams_path(semester)
    exams.write_text(json.dumps(bundle["exam_payload"], ensure_ascii=False), encoding="utf-8")
    os.utime(exams, (stamp, stamp))
    return cfg, semester


def test_upgrade_first_render_matches_v050_bytes(tmp_path: Path) -> None:
    cfg, semester = lay_out_home(tmp_path)
    assert not cfg.exam_ledger_path(semester).exists()  # 前置：测的就是"升级后第一次渲染"

    assert build_ics_for_semester(cfg, semester).ics.encode("utf-8") == GOLDEN.read_bytes()


def test_render_is_reproducible_without_a_ledger(tmp_path: Path) -> None:
    """同一输入连跑两次字节必须相同——撤销通路不许把挂钟带进产物。

    这条是上面那条的前提：`cancel_expiry_at` 默认取墙钟，若实现时让它影响了
    保留期**之外**的东西（比如每次都重排事件），两次就会分叉，本用例先红，
    而不是让 golden 以"莫名其妙变了"的形式红。
    """
    cfg, semester = lay_out_home(tmp_path)
    first = build_ics_for_semester(cfg, semester).ics
    second = build_ics_for_semester(cfg, semester).ics
    assert first == second


def test_v050_golden_fixture_keeps_its_crlf_bytes() -> None:
    """行尾契约（同 ``tests/test_legacy_course_export_golden.py`` 的最后一条）。

    ``.gitattributes`` 的例外是 glob ``tests/fixtures/*.ics -text``，固件**必须留在这个
    目录**；挪进子目录会被全局 ``* text=auto eol=lf`` 压平，新克隆、CI、本机工作树
    同时红成一场几千字节的假"课程侧回归"。
    """
    raw = GOLDEN.read_bytes()
    assert b"\r\n" in raw, "golden 固件里一个 CRLF 都没有：行尾归一化把它改了"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "固件里混进了裸 LF"
    assert raw.startswith(b"BEGIN:VCALENDAR\r\n")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `"$PY" -m pytest tests/test_legacy_exam_export_golden.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（若红：先确认 Step 2 的 `module` 路径与 `git rev-parse HEAD` 是不是 `v0.5.0`）

- [ ] **Step 5: 在干净检出里复验一次**

```bash
git worktree add --detach _notes/wt-clean HEAD
cd _notes/wt-clean && "$PY" -m pytest tests/test_legacy_exam_export_golden.py tests/test_legacy_course_export_golden.py -q --basetemp=../pt-clean
```
Expected: PASS —— 这是 CI 的条件（`_notes/` 在干净检出里不存在，`--basetemp` 要指到外面）。

- [ ] **Step 6: 四条门禁 + 提交**

```bash
git add tests/fixtures/legacy_exam_inputs.json tests/fixtures/legacy_exam_export_v050.ics tests/test_legacy_exam_export_golden.py
git commit -m "test(exam): 用 v0.5.0 的产物钉住「升级后第一次渲染字节不变」"
```

---

## Task 14: README 与 CHANGELOG

**Files:**
- Modify: `README.md:472-484`、`:1102`、`:1168`，以及「考试安排」小节与旗标表
- Modify: `CHANGELOG.md`（`[Unreleased]`）

- [ ] **Step 1: README 三处改口 + 新增一小节**

新小节要点（中文，fail-closed 口径，逐条对照 spec §10）：
取消会自动下发、保留到原定时刻过去；`--no-exams` 时**既不发布考试也不发布撤销**；
撤销需要 SEQUENCE 基线，从新 home/`--no-exams` 发布过的情况下**放弃该条并记日志**；
一次性导入型客户端仍需手动删除；换设备/换学期时旧台账要么为空要么遗留，
前者无从撤销、后者请手动删除 `~/.xjtu-timetable-calendar/subscribe/last-exams-*.ics`。
**不许**写"真实数据从未进入公开仓库"。

- [ ] **Step 2: CHANGELOG 的 `[Unreleased]`**

`### Added` 加撤销通路（含台账、门槛、最小字段、保留期口径）；`### Changed` 加
`diff` 提示改口与 `rotate` 警告；`### Fixed` 无（本特性不是修 bug）。

- [ ] **Step 3: 门禁（文档改动必须跑 format）**

Run: `"$PY" -m ruff format README.md CHANGELOG.md docs/ && "$PY" -m ruff format --check .`
Expected: `--check` 全绿（Markdown 里的 ```python 块会被连带检查）

- [ ] **Step 4: 提交**

```bash
git add README.md CHANGELOG.md
git commit -m "docs: 写明考试撤销通路与其边界"
```

---

## Task 15: 全分支收尾与终审门禁

**Files:**
- Modify: `docs/design/2026-10-09-exam-cancellation.md`（把 Task 7 D20 的推论"被跳过的候选移出台账"
  写回 §7 表与 D20 理由——这是实现期新增的口径，spec 现在只说了"跳过并 warning"）

- [ ] **Step 1: 四条门禁（全绿才算完成）**

```bash
PY=<装好 dev extras 的解释器绝对路径；本机值见 gitignored 的交接简报，不写进仓库>
"$PY" -m pytest -q --basetemp=_notes/pytest-tmp
"$PY" -m mypy
"$PY" -m ruff check .
"$PY" -m ruff format --check .
```
Expected: pytest 通过数**只增不减**（记录基线：v0.5.0 为 611 passed / 4 skipped）；
mypy `Success: no issues found in 24 source files`（新增 src 文件才会变数，本特性只改既有文件）；
ruff 两项全绿；format 文件数含新增测试文件。

- [ ] **Step 2: 干净检出复验**（CI 的条件）

```bash
git worktree add --detach _notes/wt-final HEAD
cd _notes/wt-final && "$PY" -m pytest -q --basetemp=../pt-final
```
Expected: 与 Step 1 同样的通过数。

- [ ] **Step 3: 真数据离线复验**（副本 home 放 `_notes/`，跑完即删）

用真实课表快照 + 合成考试快照跑一次 `export`：断言 `--no-exams` 的产物与 v0.5.0 的产物
逐字节相同（人肉 `cmp` + golden 用例双保险），并确认撤销块没动课程侧一行。

- [ ] **Step 4: 破坏性验证台账（评审门要用）**

逐条记录"改坏哪一处 → 哪几条先红"，至少覆盖：D18 三个降级点、D19 过滤前集合、
D20 基线筛子、D21 `--no-exams`、D23 stats 口径、D11 写入时序、D3 最小字段。

- [ ] **Step 5: 提交**

```bash
git add docs/design/2026-10-09-exam-cancellation.md
git commit -m "docs(exam): 把实现期的 D20 推论写回取消通路规格"
```

- [ ] **Step 6: 停下来，等用户决定整合与推送**

**推送与发布各需用户明确授权**（`0.6.0`：bump → CHANGELOG → CI 绿 → annotated tag →
GitHub Release → PyPI，PyPI 不可撤回）。真机演练（spec D5）排在整合之后，需要用户的设备。

---

## 验收清单（对照 spec 的 24 条决策）

| 决策 | 落在哪个任务 | 钉它的用例 |
|---|---|---|
| D1 同一份 PUBLISH 文件 | T1 + T7 | `test_status_line_only_appears_when_set`；产物仍只有一个 `METHOD:PUBLISH` |
| D2 保留到原定时刻过去 | T5 | `test_expired_entries_are_not_cancelled` |
| D3 最小字段、不写 SUMMARY | T1 + T6 | `test_empty_summary_writes_no_summary_line`、`test_cancellation_event_copies_uid_and_times_verbatim` |
| D4/D21 `--no-exams` 整块关闭 | T7 + T8 | `test_no_exams_neither_reads_nor_writes_the_ledger` |
| D5 真机演练 | T15 Step 6 之后，**不在本计划内** | 见 spec §11 |
| D6/D7 台账来源与累积 | T3 + T8 | `test_ledger_text_is_returned_and_carries_live_plus_pending` |
| D8 幂等靠既有指纹规则 | T7 + T8 | 「连渲两次撤销条目字节与 SEQUENCE 相同」用例 |
| D9 墙钟保留期 | T5 + T7 | `test_expired_entries_are_not_cancelled`（注入 `now`） |
| D10/D22 台账格式 | T3 + T4 | `test_render_exam_ledger_is_minimal_but_parseable`、`test_load_exam_ledger_drops_naive_entries` |
| D11 回写时序 | T9 + T10 | `test_push_failure_leaves_ledger_untouched` |
| D12/D24② rotate 警告 | T11 | `test_rotate_warns_before_changing_token` |
| D13 文案改口 | T12 | `test_diff_cancel_note_no_longer_denies_the_cancel_path` + 全仓 grep |
| D14 升级 golden | T13 | `test_upgrade_first_render_matches_v050_bytes` |
| D15 坏台账降级 | T4 | `test_load_exam_ledger_survives_garbage_text` |
| D16 版本 0.6.0 | 发布阶段（不在本计划内） | — |
| D17 不做课程取消 | 全局 | 课程 golden 未变 |
| **D18 可信门槛** | **T7** | 三条降级用例（缺快照 / 坏字节 / 全批解析失败） |
| **D19 过滤前 live 集** | **T7** | `test_from_date_filter_does_not_cancel_out_of_window_exams` |
| **D20 基线可发性** | **T5 + T7** | `test_uid_absent_from_baseline_is_not_emitted` |
| **D23 stats 口径** | **T8** | `test_stats_updated_not_polluted_by_cancellations` |
| **D24① 目录与退出码** | **T9** | `test_export_succeeds_when_ledger_write_fails` |
