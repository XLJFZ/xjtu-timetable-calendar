# 课程侧撤销通路与 UID v2 迁移 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"这次不该存在"的课程事件带着 `STATUS:CANCELLED` 下发，同时把课程 UID 配方里
参与身份的课程名去掉（配一次性迁移），并保证"没抓到课表"永远不会被实现成"课全没了"。

**Architecture:** 三块。①`cancellation.py` 承接考试/课程**同构**的撤销机制（台账条目、
render/load、`cancel_candidates`、`build_cancellation_events`），`exams.py` 只 re-export，
既有调用点与测试 import 一字不改；②`courses.py` 放课程侧独有的三个纯函数：门 1 数据可信、
门 2 比例熔断、迁移候选集合运算；③`exporter.build_ics_for_semester` 把撤销块扩成
课程 + 考试两支，迁移判定读订阅状态文件的 `uid_recipe` 字段，回写一律在发布成功之后。

**Tech Stack:** Python 3.11+（stdlib + `icalendar`，**不新增依赖**）、pytest、mypy strict、ruff。

**Spec:** `docs/design/2026-10-12-course-cancellation-uid-v2.md`（CD1-CD19 是该文件的决策表；
本文每个任务都标了自己实现哪几条 CD/D）。执行者必须同时读那份规格——计划只写"怎么建"，
"为什么这样建"和全部取舍在规格里。

## Global Constraints

每条都对**所有任务**生效，值一律逐字抄自规格与 `AGENTS.md`：

- 解释器：本机私有 venv 里的 `python.exe`（绝对路径只记在 `_notes/` 的私有备忘里，
  不进跟踪内容）。裸 `python` 是 3.14、没装 pytest。跑测试必须带 `--basetemp=_notes/pytest-tmp`。
- **迭代阶段只跑定向测试**（`-k` 或单个测试文件）。四条全量门禁
  （`pytest -q` / `mypy` / `ruff check .` / `ruff format --check .`）**只在 Task 12 收尾跑一次**。
- 跟踪内容**不得**包含真实个人信息、本机绝对路径、AI 工具品牌名；文档里的课程/教室一律用
  `示例课程甲` / `A-1001` 这套占位值。
- 未做真机验证的客户端表现一律写「通常会」并标明未验证（Task 13 之前，ColorOS/Outlook 对
  整学期残条的行为都属此类）。
- 撤销条目的标题**只有**一个固定文案 `已取消的活动`，不新增变体、不引用台账原文（CD5）。
- `exporter.py` **绝不写文件**；所有落盘都在 CLI（`_write_exam_ledger` 那种形状）。
- 循环依赖规则：`exams.py` 顶层 `from .exporter import ...`，所以 `exporter.py` 里对
  `exams`/`cancellation` 的引用必须留在**函数体内**。
- 台账/留底/状态文件含个人信息：`atomic_write_text(..., private=True)`，日志与 warning
  **不引用其内容**，异常只记 `type(exc).__name__`。
- 版本号单一来源 `pyproject.toml`；升版后 `python -m pip install -e . --no-deps` 重装 editable。
- 每个任务结束时本地 commit 一笔，**不 push**（`AGENTS.md`：提交 ≠ 推送，推送需单独授权）。
- 派发预算：单任务约 60 次工具调用；到顶就报"部分完成/被卡住"，不要磨。

## File Structure

| 文件 | 责任 | 任务 |
|---|---|---|
| Create `src/xjtu_calendar/cancellation.py` | 考试/课程**共用**的撤销机制：`LedgerEntry`、台账 render/load、`cancel_candidates`、`build_cancellation_events`、`CANCELLATION_SUMMARY` | 1 |
| Modify `src/xjtu_calendar/exams.py` | 删掉上述定义，改为 re-export（CD9） | 1 |
| Modify `src/xjtu_calendar/exporter.py` | `make_uid` 换 v2 配方；`UID_RECIPE_VERSION`；过滤前课程 live 集；撤销块扩两支 | 2、6 |
| Create `src/xjtu_calendar/courses.py` | 课程侧三个纯函数：`assess_courses`（门 1）、`mass_cancellation_check`（门 2）、`migration_candidates` | 3、4 |
| Modify `src/xjtu_calendar/errors.py` | `MassCancellationBlocked`（`exit_code = 6`） | 3 |
| Modify `src/xjtu_calendar/config.py` | `course_ledger_path()` | 5 |
| Modify `src/xjtu_calendar/subscribe.py` | `SubscriptionState.uid_recipe` + `from_dict` 容错 | 7 |
| Modify `src/xjtu_calendar/cli.py` | 迁移预告、`uid_recipe` 回写、课程台账落盘、`--allow-mass-cancellation` | 8、9 |
| Modify `src/xjtu_calendar/doctor.py` | 新检查项「UID 配方与迁移状态」 | 10 |
| Create `tests/fixtures/legacy_course_export_v2.ics` | v2 课程侧 golden | 2 |
| Create `tests/test_courses_gates.py` | 门 1 / 门 2 / 迁移集合 | 3、4 |
| Create `tests/test_exporter_course_cancellation.py` | 构建层两支撤销 + 裁剪零影响 + 幂等 | 6 |
| Create `tests/test_cli_course_cancellation.py` | 退出码 6、旗标、迁移回写时序 | 8、9 |
| Modify `tests/test_legacy_course_export_golden.py` | v1 golden 从"字节相等"改为"只有 UID 不同" | 2 |
| Modify `tests/test_cancellation_module.py`（Create） | re-export 同一性守卫 | 1 |

---

### Task 1: 把通用撤销机制搬到 `cancellation.py`（纯重构，CD9）

**Files:**
- Create: `src/xjtu_calendar/cancellation.py`
- Modify: `src/xjtu_calendar/exams.py`（搬走 `:515-733` 那一段）
- Test: `tests/test_cancellation_module.py`（Create）；既有 `tests/test_exam_ledger.py`、
  `tests/test_exporter_cancellation.py`、`tests/test_cli_cancellation.py` **一律不改**

**Interfaces:**
- Produces：
  - `cancellation.LedgerEntry(uid: str, start: datetime, end: datetime, summary: str, location: str | None = None, description: str | None = None)`
  - `cancellation.STATUS_CANCELLED: str`（值 `"CANCELLED"`）、`cancellation.CANCELLATION_SUMMARY: str`（值 `"已取消的活动"`）
  - `cancellation.render_ledger(live: Sequence[CalendarEvent], pending: Sequence[LedgerEntry], *, dtstamp: datetime) -> str`
  - `cancellation.load_ledger(text: str, *, label: str = "台账") -> dict[str, LedgerEntry]`
  - `cancellation.cancel_candidates(*, ledger, live_uids, baseline_uids, now) -> tuple[list[LedgerEntry], list[LedgerEntry]]`
  - `cancellation.build_cancellation_events(entries: Sequence[LedgerEntry]) -> list[CalendarEvent]`
  - `exams` 侧继续提供原名：`render_exam_ledger`、`load_exam_ledger`、`EXAM_STATUS_CANCELLED`

- [ ] **Step 1: 写"搬迁不许改行为"的守卫用例**

```python
# tests/test_cancellation_module.py
"""CD9：通用机制搬到 cancellation.py 后，考试侧必须还是同一批对象（不是复制品）。"""

from __future__ import annotations


def test_exams_reexports_the_same_objects() -> None:
    from xjtu_calendar import cancellation, exams

    assert exams.render_exam_ledger is cancellation.render_ledger
    assert exams.load_exam_ledger is cancellation.load_ledger
    assert exams.cancel_candidates is cancellation.cancel_candidates
    assert exams.build_cancellation_events is cancellation.build_cancellation_events
    assert exams.CANCELLATION_SUMMARY == cancellation.CANCELLATION_SUMMARY == "已取消的活动"
    assert exams.EXAM_STATUS_CANCELLED == cancellation.STATUS_CANCELLED == "CANCELLED"
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_cancellation_module.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL —`ModuleNotFoundError: No module named 'xjtu_calendar.cancellation'`

- [ ] **Step 3: 建模块并整体剪切**

把 `exams.py` 里这几段**逐字剪切**到 `cancellation.py`（顺序保持）：`LedgerEntry`
（含其 docstring）、`render_exam_ledger`、`_optional_text`、`load_exam_ledger`、
`cancel_candidates`、`EXAM_STATUS_CANCELLED`、`CANCELLATION_SUMMARY`、
`build_cancellation_events`。剪切时只做三处机械改名，**逻辑一个字都不许动**：

```python
# src/xjtu_calendar/cancellation.py 顶部
"""撤销机制：考试与课程**共用**的那一份（docs/design/2026-10-12-… §6.1）。

权威在这里，不在 `exams.py`。`exams.py` 保留原名 re-export，既有调用点与测试的
import 路径一律不改（CD9：零回归面）。课程侧独有的判据（门 1/门 2/迁移候选）
在 `courses.py`；**一旦某段逻辑需要按考试/课程分叉，就把它挪出本模块**，
别往这里塞 if 分支。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import hashlib  # 仅当被搬函数用到时才保留；本模块实际不需要 → 删掉这行
from typing import Any

from .models import CalendarEvent
from .timeutil import TZ_XIAN  # 按 exams.py 原有 import 实际名称对齐
```

改名三处：`render_exam_ledger` → `render_ledger`；`load_exam_ledger` → `load_ledger`
（**新增关键字参数 `label`**，见下）；`EXAM_STATUS_CANCELLED` → `STATUS_CANCELLED`。
`load_ledger` 里三条 warning 的"考试台账"字样换成 `label`：

```python
def load_ledger(text: str, *, label: str = "台账") -> dict[str, LedgerEntry]:
    ...
    logger.warning("%s条目 %s 读不出（%s），已丢弃", label, uid, type(exc).__name__)
    ...
    logger.warning("%s里有一条读不出（%s），已丢弃", label, type(exc).__name__)
```

`exams.py` 里加 re-export（**放在文件顶部的 import 区**，不要放文件末尾），保持
`__all__` 里原有名字：

```python
# 撤销机制的权威已搬到 cancellation.py（CD9）。这几个别名是**兼容层**：
# 既有调用点与测试仍用考试侧原名，行为逐字节不变。
from functools import partial

from .cancellation import (
    CANCELLATION_SUMMARY,
    LedgerEntry,
    STATUS_CANCELLED,
    build_cancellation_events,
    cancel_candidates,
    load_ledger,
    render_ledger,
)

EXAM_STATUS_CANCELLED = STATUS_CANCELLED
render_exam_ledger = render_ledger
#: 用 partial 而不是 lambda：`load_ledger` 的日志措辞按侧区分（"考试台账" / "课程台账"），
#: partial 不需要 `# noqa: E731`，也不会被 RUF100 判成"未启用的 noqa"。
load_exam_ledger = partial(load_ledger, label="考试台账")
```

> `cancellation.py` 只依赖 `models` / `timeutil`，**不** import `exams`，所以顶部 import
> 不会成环。若 `test_load_exam_ledger_*` 那组用例断言的是 `caplog` 全文措辞，
> `label="考试台账"` 必须逐字保持。

- [ ] **Step 4: 跑考试侧全部既有撤销用例**

Run: `"$PY" -m pytest tests/test_exam_ledger.py tests/test_exporter_cancellation.py tests/test_cli_cancellation.py tests/test_cancellation_module.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（一条都不许改测试来迁就实现；红了说明搬坏了）

- [ ] **Step 5: 类型与风格自查**

Run: `"$PY" -m mypy && "$PY" -m ruff check src/xjtu_calendar/cancellation.py src/xjtu_calendar/exams.py && "$PY" -m ruff format --check src/xjtu_calendar/cancellation.py`
Expected: 三条都零输出/`already formatted`

- [ ] **Step 6: Commit**

```bash
git add src/xjtu_calendar/cancellation.py src/xjtu_calendar/exams.py tests/test_cancellation_module.py
git commit -m "refactor(cancellation): 通用撤销机制搬到独立模块，exams 只 re-export"
```

---

### Task 2: UID v2 配方与两代 golden（CD2、CD15、CD16）

**Files:**
- Modify: `src/xjtu_calendar/exporter.py:75-122`（`make_uid`）、`:1-40`（模块常量区）
- Modify: `tests/test_legacy_course_export_golden.py`
- Create: `tests/fixtures/legacy_course_export_v2.ics`
- Test: `tests/test_exporter.py`（改名免疫那两条挂这里，`make_uid` 的用例归属不变）

**Interfaces:**
- Consumes：Task 1 无依赖（可并行）
- Produces：`exporter.UID_RECIPE_VERSION: int = 2`；`make_uid(semester_key, meeting, day) -> str`
  签名不变、payload 少一项

- [ ] **Step 1: 写"改名免疫"与它的反向用例**

```python
# 追加到 tests/test_exporter.py
def test_uid_v2_ignores_course_rename() -> None:
    """CD2 的核心卖点：同一 course_id 改名，UID 必须一模一样。"""
    from xjtu_calendar.exporter import make_uid
    from xjtu_calendar.models import CourseMeeting

    before = CourseMeeting("DEMO-1001", "大学英语（2）", 5, [1, 2], [1, 2, 3])
    after = CourseMeeting("DEMO-1001", "大学英语Ⅱ", 5, [1, 2], [1, 2, 3])
    assert make_uid("2026-2027-1", before, "2026-09-11") == make_uid(
        "2026-2027-1", after, "2026-09-11"
    )


def test_uid_v2_still_namesensitive_without_course_id() -> None:
    """CD16：没有 course_id 时 stable_course_key 退回 `name:课程名`，
    改名**仍**产生新 UID。这条不许被"改名问题已解决"的说法顺手删掉。"""
    from xjtu_calendar.exporter import make_uid
    from xjtu_calendar.models import CourseMeeting

    a = CourseMeeting(None, "示例课程甲", 5, [1, 2], [1, 2, 3])
    b = CourseMeeting(None, "示例课程乙", 5, [1, 2], [1, 2, 3])
    assert make_uid("2026-2027-1", a, "2026-09-11") != make_uid("2026-2027-1", b, "2026-09-11")
```

- [ ] **Step 2: 跑它，确认第一条失败**

Run: `"$PY" -m pytest tests/test_exporter.py -q -k "uid_v2" --basetemp=_notes/pytest-tmp`
Expected: `test_uid_v2_ignores_course_rename` FAIL，`..._without_course_id` PASS（v1 下两条都不同名）

- [ ] **Step 3: 改配方**

`make_uid` 的 payload 去掉 `meeting.course_name.strip()` 那一行，并在函数上方加常量：

```python
#: 课程 UID 的配方版本。v2（0.8.0）去掉了 `course_name`；v1 的形状只保留在文档里，
#: 代码不再能产出它——迁移读的是发布留底，不需要重算旧 UID（CD15）。
#: `subscribe` 状态文件的 `uid_recipe` 字段与 `doctor` 都读这个常量，别写第二处字面量。
UID_RECIPE_VERSION = 2
```

docstring 里那段「`course_name` 参与身份是刻意的向后兼容选择（legacy UID contract）」
整块替换为：

```python
    """为「某学期、某课程的某一次实际上课」生成稳定 UID（配方 v2）。

    payload = ``semester_key | stable_course_key | day | periods_token``。
    刻意**不纳入**时间、地点与课程名：作息调整、换教室、**改名**都应当被视作
    「同一事件的新版本」由客户端原地更新（v0.8.0 起改名不再产生新事件，CD2）。

    v1 配方（≤ 0.7.x，形状存档于此，代码里已无对应实现）在 ``stable_course_key``
    之后多了一项 ``course_name.strip()``，即
    ``semester | key | name | day | periods``。v1→v2 的一次性迁移见
    docs/design/2026-10-12-course-cancellation-uid-v2.md §6.4：旧 UID 从发布留底
    里读，不重算。

    已知边界（CD16）：``course_id`` 缺失时 ``stable_course_key`` 退回
    ``f"name:{course_name}"``（models.py:263），那种行**仍**对改名敏感。
    """
```

- [ ] **Step 4: 生成 v2 golden 并改写 v1 golden 用例**

先跑一次现有 golden 用例看它红成什么样：
`"$PY" -m pytest tests/test_legacy_course_export_golden.py -q --basetemp=_notes/pytest-tmp`
预期：字节比对那条 FAIL（UID 全变）。

用与 `lay_out_home` 相同的 `INPUTS` 与 `os.utime(1913025600)` 钉桩，写一个一次性脚本
（放 `_notes/gen_v2_golden.py`，**不提交**）产出新产物，逐行确认不含真实个人信息后
存为 `tests/fixtures/legacy_course_export_v2.ics`。

然后把 `test_legacy_course_export_golden.py` 里那条字节相等用例改成**结构比对**，
并在 docstring 里写清这不是"为了让测试变绿"：

```python
def test_v2_golden_matches_v1_except_uids() -> None:
    """v1 golden 的作用从"字节相等"降级为"除 UID 外全等"——这是规格批准的变更（CD15）。

    原用例守的是「课程侧不许被无声改坏」。UID v2 是一次**有声**的、经规格批准的改动，
    所以字节相等不再是判据；但"除 UID 之外一切不变"必须钉住：事件数、SUMMARY、
    DTSTART/DTEND、SEQUENCE、VTIMEZONE、X-WR-CALNAME 任何一项跟着变，都是越界。
    """
    v1 = Calendar.from_ical(GOLDEN_V1.read_bytes())
    v2 = Calendar.from_ical(GOLDEN_V2.read_bytes())
    ev1, ev2 = list(v1.walk("VEVENT")), list(v2.walk("VEVENT"))
    assert len(ev1) == len(ev2)
    for a, b in zip(ev1, ev2, strict=True):
        assert a["SUMMARY"] == b["SUMMARY"]
        assert a["DTSTART"].dt == b["DTSTART"].dt
        assert a["DTEND"].dt == b["DTEND"].dt
        assert a["SEQUENCE"] == b["SEQUENCE"]
        assert a["UID"] != b["UID"], "UID 没变说明配方没换"
```

`test_course_side_rendering_is_reproducible` 与 `test_golden_fixture_keeps_its_crlf_bytes`
**原样保留**（前者仍管自洽，后者管 `.gitattributes` 的 `-text` 例外）。

- [ ] **Step 5: 跑定向套件**

Run: `"$PY" -m pytest tests/test_exporter.py tests/test_legacy_course_export_golden.py tests/test_exporter_exams.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS。若 `test_exporter_exams.py` 里有"全局 UID 唯一性"用例因新配方撞车而红，
**停下来报告**——那说明 v2 去掉了区分维度，是规格级问题，不许改测试。

- [ ] **Step 6: Commit**

```bash
git add src/xjtu_calendar/exporter.py tests/test_exporter.py tests/test_legacy_course_export_golden.py tests/fixtures/legacy_course_export_v2.ics
git commit -m "feat(uid): 课程 UID 配方升到 v2，去掉参与身份的课程名"
```

---

### Task 3: 门 1 / 门 2 与退出码 6（CD4、CD12、CD13）

**Files:**
- Create: `src/xjtu_calendar/courses.py`
- Modify: `src/xjtu_calendar/errors.py`（新异常）
- Test: `tests/test_courses_gates.py`（Create）

**Interfaces:**
- Produces：
  - `courses.CoursesTrust(trusted: bool, reason: str | None = None)`
  - `courses.assess_courses(*, total_candidates: int, skipped: Sequence[str], meetings: Sequence[CourseMeeting]) -> CoursesTrust`
  - `courses.MASS_CANCELLATION_MIN_EVENTS: int = 10`、`courses.MASS_CANCELLATION_RATIO: float = 0.30`
  - `courses.MassCancellation(candidates: int, ledger_total: int, ratio: float)`
  - `courses.mass_cancellation_check(*, candidates: int, ledger_total: int) -> MassCancellation | None`
  - `errors.MassCancellationBlocked(exit_code = 6)`，`hint` 指向 `--allow-mass-cancellation`

- [ ] **Step 1: 写门 1 的失败用例**

```python
# tests/test_courses_gates.py
"""课程侧三道判据（docs/design/2026-10-12-course-cancellation-uid-v2.md CD12/CD13）。"""

from __future__ import annotations

from xjtu_calendar.courses import assess_courses
from xjtu_calendar.models import CourseMeeting


def _meetings(n: int) -> list[CourseMeeting]:
    return [
        CourseMeeting(f"DEMO-{i}", f"示例课程{'甲乙丙丁'[i % 4]}", 1, [1], [1]) for i in range(n)
    ]


def test_gate1_untrusted_when_no_records() -> None:
    got = assess_courses(total_candidates=0, skipped=[], meetings=[])
    assert not got.trusted and "没有" in (got.reason or "")


def test_gate1_untrusted_when_any_row_skipped() -> None:
    """有行读不懂 ⇒ 这份数据**不完整**，不能拿它判"某门课消失了"。"""
    got = assess_courses(
        total_candidates=9, skipped=["第 3 行：无法识别星期"], meetings=_meetings(8)
    )
    assert not got.trusted and got.reason


def test_gate1_trusted_when_everything_represented() -> None:
    assert assess_courses(total_candidates=9, skipped=[], meetings=_meetings(9)).trusted
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_courses_gates.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL —`ModuleNotFoundError: No module named 'xjtu_calendar.courses'`

- [ ] **Step 3: 实现 `courses.py` 的门 1**

```python
# src/xjtu_calendar/courses.py
"""课程侧独有的判据：数据可信（门 1）、比例熔断（门 2）、迁移候选。

通用撤销机制（台账、候选数学、撤销事件构建）在 `cancellation.py`，考试与课程共用。
本模块全是**纯函数**：不读文件、不发请求、不写盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import CourseMeeting


@dataclass(frozen=True)
class CoursesTrust:
    """门 1 的结论。`reason` 只允许是类别词，**不得**携带课程名/教师/教室（规格 §8）。"""

    trusted: bool
    reason: str | None = None


def assess_courses(
    *,
    total_candidates: int,
    skipped: Sequence[str],
    meetings: Sequence[CourseMeeting],
) -> CoursesTrust:
    """只有"响应里有课记录、每一行都解析成功、且至少解析出一门课"才允许算撤销（CD13）。

    这是考试侧 D18 的同构判据，但课程侧的后果是**整学期**：`live = ∅` 在撤销语义下
    等于"取消整学期"，所以必须是白名单（默认不放开）。
    `--from-file` 本身**不**取消信任——离线导入是常态操作，判据看的是数据完整性，
    不是它从哪来。
    """
    if total_candidates == 0:
        return CoursesTrust(False, "响应中没有任何课程记录")
    if skipped:
        return CoursesTrust(False, f"课表有 {len(skipped)} 行解析不了")
    if not meetings:
        return CoursesTrust(False, "零门课程安排")
    return CoursesTrust(True)
```

- [ ] **Step 4: 写并实现门 2**

```python
# tests/test_courses_gates.py 追加
from xjtu_calendar.courses import mass_cancellation_check


def test_gate2_small_calendar_not_blocked() -> None:
    """30 条台账里撤 12 条是正常调整：候选数够但**样本太小**，比例不作数（CD12 双条件）。"""
    assert mass_cancellation_check(candidates=12, ledger_total=30) is None


def test_gate2_triggers_on_both_conditions() -> None:
    got = mass_cancellation_check(candidates=40, ledger_total=120)
    assert got is not None and got.ratio > 0.30


def test_gate2_below_min_events_never_triggers() -> None:
    assert mass_cancellation_check(candidates=9, ledger_total=20) is None


def test_gate2_empty_ledger_never_triggers() -> None:
    assert mass_cancellation_check(candidates=50, ledger_total=0) is None
```

```python
# courses.py 追加
#: 熔断判据的两个常量（CD12）。**刻意不做成配置项**：可调的保险丝等于没有保险丝，
#: 真要改数值走规格，不走命令行。
MASS_CANCELLATION_MIN_EVENTS = 10
MASS_CANCELLATION_RATIO = 0.30


@dataclass(frozen=True)
class MassCancellation:
    candidates: int
    ledger_total: int
    ratio: float


def mass_cancellation_check(*, candidates: int, ledger_total: int) -> MassCancellation | None:
    """候选数 ≥ 10 **且** 占台账事件总数 ≥ 30% ⇒ 熔断（CD12）。

    双条件是为了不误伤两种形状：小日历（30 条里撤 12 条是正常调整）与大日历的
    局部异常（500 条里撤 20 条 ≈ 4%，不该拦）。命中即由调用方拒绝发布——
    课程侧最坏的错位是"我们以为撤销过了，订阅端从没收到"（CD4）。
    """
    if ledger_total <= 0 or candidates < MASS_CANCELLATION_MIN_EVENTS:
        return None
    ratio = candidates / ledger_total
    if ratio < MASS_CANCELLATION_RATIO:
        return None
    return MassCancellation(candidates=candidates, ledger_total=ledger_total, ratio=ratio)
```

- [ ] **Step 5: 加异常与退出码**

```python
# errors.py 追加（放在 SubscribeGuardError 那一组附近）
class MassCancellationBlocked(XjtuCalendarError):
    """疑似数据不完整导致的整批撤销：拒绝发布，要求显式旗标放行（CD4/CD12）。"""

    exit_code = 6

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(
            message,
            hint=hint or "确认课表真的少了一批课，再带 --allow-mass-cancellation 重跑。",
        )
```

同时确认 `cli.main` 的异常出口按 `exc.exit_code` 返回（现有实现已如此，无需改）。

- [ ] **Step 6: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_courses_gates.py -q --basetemp=_notes/pytest-tmp && "$PY" -m mypy`

```bash
git add src/xjtu_calendar/courses.py src/xjtu_calendar/errors.py tests/test_courses_gates.py
git commit -m "feat(courses): 课程侧门 1 数据可信与门 2 比例熔断，新增退出码 6"
```

> 执行中修订：门 2 分母按终审改为未过期条目数，见 `7cc0ae2` 与规格 CD12/CD20。上面已跑过的
> 红绿代码块保持原样，不改写施工记录。

---

### Task 4: 迁移候选集合运算（CD7、CD10）

**Files:**
- Modify: `src/xjtu_calendar/courses.py`
- Test: `tests/test_courses_gates.py`

**Interfaces:**
- Consumes：`cancellation.LedgerEntry`（Task 1）
- Produces：`courses.migration_candidates(*, published: Mapping[str, LedgerEntry], live_uids: Collection[str], normal_cancel_uids: Collection[str], now: datetime) -> list[LedgerEntry]`

- [ ] **Step 1: 写用例（含 CD10 那条坑）**

```python
# tests/test_courses_gates.py 追加
from datetime import UTC, datetime, timedelta

from xjtu_calendar.cancellation import LedgerEntry
from xjtu_calendar.courses import migration_candidates


def _entry(uid: str, *, hours: int) -> LedgerEntry:
    start = datetime(2026, 10, 1, 8, tzinfo=UTC) + timedelta(hours=hours)
    return LedgerEntry(uid=uid, start=start, end=start + timedelta(hours=2), summary="示例课程甲")


NOW = datetime(2026, 10, 1, 0, tzinfo=UTC)


def test_migration_cancels_every_stale_published_uid() -> None:
    published = {u: _entry(u, hours=10 + i) for i, u in enumerate(["a", "b", "c"])}
    got = migration_candidates(
        published=published, live_uids={"b"}, normal_cancel_uids=set(), now=NOW
    )
    assert [e.uid for e in got] == ["a", "c"]


def test_migration_skips_entries_already_cancelled_this_run() -> None:
    """常态分支已经要撤的 UID，迁移分支不许再出一份（同 UID 双条目）。"""
    published = {"a": _entry("a", hours=10)}
    got = migration_candidates(
        published=published, live_uids=set(), normal_cancel_uids={"a"}, now=NOW
    )
    assert got == []


def test_migration_skips_expired_entries() -> None:
    """CD10：留底里含"上次已撤销"的条目，它们不在任何台账里；
    不过期筛就会把上学期的旧事件反复撤销、无限续命。"""
    published = {"old": _entry("old", hours=-5), "new": _entry("new", hours=5)}
    got = migration_candidates(
        published=published, live_uids=set(), normal_cancel_uids=set(), now=NOW
    )
    assert [e.uid for e in got] == ["new"]


def test_migration_output_is_fully_ordered() -> None:
    published = {u: _entry(u, hours=20 - i) for i, u in enumerate(["x", "y", "z"])}
    got = migration_candidates(published, set(), set(), NOW)  # 位置参数也要能吃
    assert [e.uid for e in got] == ["z", "y", "x"]
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_courses_gates.py -q -k migration --basetemp=_notes/pytest-tmp`
Expected: FAIL —`ImportError: cannot import name 'migration_candidates'`

- [ ] **Step 3: 实现**

```python
# courses.py 追加
def migration_candidates(
    published: Mapping[str, LedgerEntry],
    live_uids: Collection[str],
    normal_cancel_uids: Collection[str],
    now: datetime,
) -> list[LedgerEntry]:
    """v1→v2 那一次的全量撤销候选（CD7/CD10）。

    ``published`` 是从**发布留底**解析出来的 `{UID: LedgerEntry}`——留底本身就是上次
    发出去的完整事件清单，所以旧 UID 不需要重算配方就能拿到（CD15），而且这些 UID
    天然都在 SEQUENCE 基线里，D20 那道筛自动全过。

    减去两个集合：本次 live（同 UID 是原地更新，不该撤）、本次常态撤销（避免同 UID
    双条目）。再过一遍 D2 过期筛：时刻已过的条目**不撤**，否则留底里那些"上次已撤销"
    的条目会因为不在任何台账里而永远出不去（CD10）。
    输出按 ``(start, uid)`` 全序，与 `cancel_candidates` 同口径。
    """
    picked = [
        entry
        for uid, entry in published.items()
        if uid not in live_uids and uid not in normal_cancel_uids and entry.start >= now
    ]
    return sorted(picked, key=lambda e: (e.start, e.uid))
```

- [ ] **Step 4: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_courses_gates.py -q --basetemp=_notes/pytest-tmp && "$PY" -m mypy`

```bash
git add src/xjtu_calendar/courses.py tests/test_courses_gates.py
git commit -m "feat(courses): v1→v2 迁移候选集合运算，含过期筛与去重"
```

---

### Task 5: 课程台账落点与读写（CD8）

**Files:**
- Modify: `src/xjtu_calendar/config.py`（`exam_ledger_path` 旁边）
- Test: `tests/test_config.py`（若无该文件则并入 `tests/test_courses_gates.py`）

**Interfaces:**
- Produces：`Settings.course_ledger_path(semester_key: str) -> Path`
  = `<subscribe_dir>/last-courses-<学期>.ics`

- [ ] **Step 1: 写用例**

```python
def test_course_ledger_path_is_separate_from_exam_ledger(tmp_path) -> None:
    """CD8：两份台账必须分开——考试与课程 UID 同域同形状，从串上分不出彼此。"""
    from xjtu_calendar.config import Settings

    cfg = Settings(home=tmp_path / "h")
    assert cfg.course_ledger_path("2026-2027-1").name == "last-courses-2026-2027-1.ics"
    assert cfg.course_ledger_path("2026-2027-1") != cfg.exam_ledger_path("2026-2027-1")
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest -q -k course_ledger_path --basetemp=_notes/pytest-tmp`
Expected: FAIL —`AttributeError: 'Settings' object has no attribute 'course_ledger_path'`

- [ ] **Step 3: 实现（紧跟 `exam_ledger_path` 的写法与措辞）**

```python
    def course_ledger_path(self, semester_key: str) -> Path:
        """课程台账：曾以 live 形态发布过、原定时刻还没过去的课程事件账本。

        与考试台账**必须分开**（CD8）：两者 UID 同域同形状（`sha256[:32]@domain`），
        从串上分不出彼此——拿发布留底当课程 live 集时，带 `--no-exams` 的那次发布
        会让留底里的考试 UID 看起来"全消失了"。含课程名/教师/教室，**永不发布**、
        只留本地、写盘 `private=True`。目录不在 `ensure_dirs()` 里创建，写入点自己 mkdir。
        """
        return self.subscribe_dir / f"last-courses-{semester_key}.ics"
```

- [ ] **Step 4: 跑定向 + 提交**

Run: `"$PY" -m pytest -q -k "course_ledger or config" --basetemp=_notes/pytest-tmp`

```bash
git add src/xjtu_calendar/config.py tests/test_config.py
git commit -m "feat(config): 课程台账落点 last-courses-<学期>.ics"
```

---

### Task 6: exporter 接线——过滤前 live 集、两支撤销、新返回字段（CD11、CD13、CD14）

**Files:**
- Modify: `src/xjtu_calendar/exporter.py`（`ExportResult` `:535-556`、课程展开 `:716-718`、
  裁剪 `:792-806`、撤销块 `:870-976`、汇总 `:985-1006`）
- Test: `tests/test_exporter_course_cancellation.py`（Create）

**Interfaces:**
- Consumes：Task 1/3/4/5 的全部产出
- Produces：
  - `ExportResult.course_ledger_text: str | None = None`
  - `ExportResult.migration_summary: MigrationSummary | None = None`，
    `MigrationSummary(candidates: int, ratio: float)`
  - `info["course_cancellations"]: int`
  - `build_ics_for_semester(..., allow_mass_cancellation: bool = False)`

- [ ] **Step 1: 写构建层用例**

```python
# tests/test_exporter_course_cancellation.py
"""课程侧撤销的构建层契约（规格 §6.5、§9 第 9-18 条）。"""

from __future__ import annotations

import pytest

from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.cancellation import CANCELLATION_SUMMARY


def test_vanished_course_is_cancelled_in_next_export(
    booted_home, semester, publish_baseline
) -> None:
    """台账里有、本次没有 ⇒ 产物里出现该 UID 的 CANCELLED 条目，标题是固定文案。"""
    publish_baseline(ledger_courses=9)  # 布置 last-courses-*.ics + last-*.ics 留底
    result = build_ics_for_semester(booted_home.cfg, semester, drop_courses=("示例课程丁",))
    assert CANCELLATION_SUMMARY in result.ics
    assert "示例课程丁" not in result.ics, "撤销条目不许带课程名（CD5）"
    assert result.info["course_cancellations"] > 0


def test_untrusted_snapshot_cancels_nothing(booted_home, semester, publish_baseline) -> None:
    """门 1（CD13）：有一行解析不了 ⇒ 不读台账、不撤销、course_ledger_text 为 None。"""
    publish_baseline(ledger_courses=9)
    result = build_ics_for_semester(booted_home.cfg, semester, corrupt_row=True)
    assert "STATUS:CANCELLED" not in result.ics
    assert result.course_ledger_text is None


def test_mass_cancellation_blocks_the_build(booted_home, semester, publish_baseline) -> None:
    """门 2（CD4/CD12）：9 门课掉到 2 门 ⇒ 抛 MassCancellationBlocked。"""
    publish_baseline(ledger_courses=9)
    from xjtu_calendar.errors import MassCancellationBlocked

    with pytest.raises(MassCancellationBlocked):
        build_ics_for_semester(booted_home.cfg, semester, keep_only_courses=2)
    # 旗标放行后能正常产出
    result = build_ics_for_semester(
        booted_home.cfg, semester, keep_only_courses=2, allow_mass_cancellation=True
    )
    assert result.info["course_cancellations"] > 0


def test_date_range_does_not_change_course_cancellations(
    booted_home, semester, publish_baseline
) -> None:
    """CD14：裁剪是课程侧的常态操作，对撤销与台账必须零影响。"""
    publish_baseline(ledger_courses=9)
    full = build_ics_for_semester(booted_home.cfg, semester)
    windowed = build_ics_for_semester(booted_home.cfg, semester, to_date="2026-10-31")
    assert _cancelled_uids(full.ics) == _cancelled_uids(windowed.ics)


def test_cancellation_is_idempotent_across_repeated_renders(
    booted_home, semester, publish_baseline
) -> None:
    """D8：固定标题 ⇒ 指纹恒定 ⇒ 同一输入重渲染，撤销条目的 SEQUENCE 不漂移。"""
    publish_baseline(ledger_courses=9)
    first = build_ics_for_semester(booted_home.cfg, semester)
    second = build_ics_for_semester(booted_home.cfg, semester)
    assert _cancelled_sequences(first.ics) == _cancelled_sequences(second.ics)
```

> 测试固件（`booted_home` / `publish_baseline` / `drop_courses` / `keep_only_courses` /
> `corrupt_row` 这些 `build_ics_for_semester` 的测试专用入参）**照
> `tests/exam_support.py` 与 `tests/test_exporter_cancellation.py` 的现有形状写**。
> 那两个文件已经解决了"布置 home、造留底、造台账、注入坏行"的全部问题，
> 不要另起一套夹具。若 `build_ics_for_semester` 没有对应的入参位，
> 用改写 raw 快照的方式驱动，而不是给生产函数加测试专用参数。

- [ ] **Step 2: 跑它，确认全部失败**

Run: `"$PY" -m pytest tests/test_exporter_course_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL（`course_ledger_text` / `MassCancellationBlocked` 未接线）

- [ ] **Step 3: 捕获过滤前的课程 live 集**

在 `events = build_events(...)` 之后、`--from-date/--to-date` 那段之前加：

```python
    #: 课程撤销候选与台账回写都必须吃**未经日期过滤**的事件（CD14，D19 的对应物）。
    #: 裁剪是课程侧的常态操作，用过滤后的集合会把窗口外的课当成"消失"并撤销、
    #: 还从台账剪掉——一次局部导出动了全局订阅状态。
    pre_filter_course_events = list(events)
    live_course_uids = {event.uid for event in events}
```

- [ ] **Step 4: 门 1 与台账读取**

在撤销块之前算门 1（`parser.report.total_candidates` / `.skipped` 都是现成的）：

```python
    from .courses import CoursesTrust, assess_courses

    courses_trust = assess_courses(
        total_candidates=parser.report.total_candidates,
        skipped=parser.report.skipped,
        meetings=meetings,
    )
```

撤销块改成"考试一支、课程一支"。课程支的骨架（放在考试支同一个 `try` 内层之外，
**但异常处理口径不同**：课程支的熔断异常必须冒到 CLI，不能吞）：

```python
if courses_trust.trusted:
    from .cancellation import (
        LedgerEntry,
        build_cancellation_events,
        cancel_candidates,
        load_ledger,
        render_ledger,
    )
    from .courses import mass_cancellation_check, migration_candidates

    course_ledger: dict[str, LedgerEntry] = {}
    course_ledger_exists = cfg.course_ledger_path(semester).is_file()
    if course_ledger_exists:
        try:
            course_ledger = load_ledger(
                cfg.course_ledger_path(semester).read_text(encoding="utf-8"),
                label="课程台账",
            )
        except (OSError, UnicodeDecodeError) as exc:
            logger.warning(
                "课程台账 %s 读不出（%s），本次当作无台账处理并用当前课表覆盖它",
                semester,
                type(exc).__name__,
            )
    deliverable, unresolvable = cancel_candidates(
        ledger=course_ledger,
        live_uids=live_course_uids,
        baseline_uids=_cancellation_baseline_uids(baseline),
        now=cancel_expiry_at or now_local(),
    )
    blocked = (
        None
        if allow_mass_cancellation
        else mass_cancellation_check(candidates=len(deliverable), ledger_total=len(course_ledger))
    )
    if blocked:
        # 只报数量与占比，不列课程名（规格 §8）。冒到 CLI ⇒ 退出码 6、零写盘。
        raise MassCancellationBlocked(
            f"本次将撤销 {blocked.candidates} 条课程事件，"
            f"占已发布事件的 {blocked.ratio:.0%}——疑似课表没抓全"
        )
```

- [ ] **Step 5: 迁移分支与台账文本**

`build_ics_for_semester` 新增入参 `published_baseline_text: str | None = None`
（CLI 从留底读出来传进来；exporter 不读文件）。留底存在且状态文件缺 `uid_recipe` 时：

```python
        if published_baseline_text is not None:
            published = load_ledger(published_baseline_text, label="发布留底")
            migration = migration_candidates(
                published=published,
                live_uids=live_course_uids | live_exam_uids,
                normal_cancel_uids={e.uid for e in cancellation_events},
                now=cancel_expiry_at or now_local(),
            )
            if migration:
                migration_summary = MigrationSummary(
                    candidates=len(migration),
                    ratio=len(migration) / max(1, len(published)),
                )
                # 迁移豁免门 2（CD11），但必须让 CLI 有机会打印预告。
                cancellation_events += build_cancellation_events(migration)
                pending_entries += migration
```

台账文本与考试侧同形：`render_ledger(pre_filter_course_events, course_pending, dtstamp=stamp)`
→ `ExportResult.course_ledger_text`；`info["course_cancellations"]` 只数**真正进了产物**的
课程撤销条目。`sequence_stats` 的口径**不变**（D23：撤销不进统计）。

- [ ] **Step 6: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_exporter_course_cancellation.py tests/test_exporter_cancellation.py tests/test_exam_ledger.py -q --basetemp=_notes/pytest-tmp`
Expected: PASS（考试侧一条都不许红）

```bash
git add src/xjtu_calendar/exporter.py tests/test_exporter_course_cancellation.py
git commit -m "feat(exporter): 课程侧撤销接线——过滤前 live 集、两道门、迁移分支"
```

---

### Task 7: 状态文件的 `uid_recipe` 字段（CD7、CD17）

**Files:**
- Modify: `src/xjtu_calendar/subscribe.py:79-111`
- Test: `tests/test_subscribe_state.py`（Create；若已有同名主题文件则并入）

**Interfaces:**
- Produces：`SubscriptionState.uid_recipe: int | None = None`；
  `subscribe.needs_uid_migration(state) -> bool`（`state.uid_recipe is None`）

- [ ] **Step 1: 写用例，含 CD17 那条自愈**

```python
def test_uid_recipe_round_trips(tmp_path) -> None:
    from xjtu_calendar.subscribe import SubscriptionState, load_state, save_state

    state = _make_state(tmp_path)  # 复用现有测试的构造助手
    save_state(state.cfg, replace(state, uid_recipe=2))
    assert load_state(state.cfg, state.semester).uid_recipe == 2


def test_old_state_file_reads_as_missing_recipe(tmp_path) -> None:
    """v0.7.x 写的状态文件没有这个字段 ⇒ 读出来是 None ⇒ 触发迁移（CD7）。"""
    ...
    assert state.uid_recipe is None


def test_downgrade_drops_the_field_and_migration_is_a_noop(tmp_path) -> None:
    """CD17：旧版 `from_dict` 忽略未知键、`to_dict` 用 `asdict` ⇒ 字段被抹掉。
    后果必须是**自愈**：下次 push 重走迁移分支，而此刻留底已是 v2 产物 ⇒ 候选空集。"""
    ...
    assert loaded.uid_recipe is None
    assert (
        migration_candidates(
            published=v2_baseline_entries, live_uids=all_v2_uids, normal_cancel_uids=set(), now=NOW
        )
        == []
    )
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_subscribe_state.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL —`TypeError: SubscriptionState.__init__() got an unexpected keyword argument 'uid_recipe'`

- [ ] **Step 3: 实现**

`SubscriptionState` 加字段与判定，`from_dict` 用 `.get` 容错：

```python
    #: 课程 UID 的配方版本（`exporter.UID_RECIPE_VERSION`）。
    #: **None = 这个 URL 上最后一次发布出自 v1** ⇒ 下次 push 走一次性迁移（CD7）。
    #: 旧版本工具读不出也写不回这个字段（`from_dict` 忽略未知键、`to_dict` 走 `asdict`），
    #: 降级再升回来会重走一次迁移分支，而那时留底已是 v2 产物 ⇒ 候选空集、无副作用（CD17）。
    uid_recipe: int | None = None


def needs_uid_migration(state: SubscriptionState) -> bool:
    """迁移判定的唯一归属地：看字段，不猜 UID（UID 是 hash，反推不了配方）。"""
    return state.uid_recipe is None
```

`from_dict` 里：`uid_recipe=(int(data["uid_recipe"]) if "uid_recipe" in data else None)`。

- [ ] **Step 4: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_subscribe_state.py tests/test_subscribe_git.py -q --basetemp=_notes/pytest-tmp`

```bash
git add src/xjtu_calendar/subscribe.py tests/test_subscribe_state.py
git commit -m "feat(subscribe): 状态文件记录 uid_recipe，作为迁移判定唯一依据"
```

---

### Task 8: CLI push/rotate——预告、回写时序、失败不动（CD3、CD11）

**Files:**
- Modify: `src/xjtu_calendar/cli.py`（`_write_exam_ledger:668` 旁、`_subscribe_push:1338-1370`、
  `_subscribe_rotate:1374-1468`）
- Test: `tests/test_cli_course_cancellation.py`（Create）

**Interfaces:**
- Consumes：Task 6 的 `build_ics_for_semester(..., published_baseline_text=…, allow_mass_cancellation=…)`、
  Task 7 的 `needs_uid_migration`
- Produces：`cli._write_course_ledger(cfg, semester, text)`；`cli._print_migration_notice(summary)`

- [ ] **Step 1: 写时序用例（最容易写错的地方）**

```python
# tests/test_cli_course_cancellation.py
"""迁移的三条时序铁律（规格 §7、CD7/D11）：预告在前、回写在后、失败一律不动。"""


def test_push_prints_migration_notice_before_publishing(home, capsys, patched_origin) -> None:
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "v1 UID 配方" in out and "预期行为" in out


def test_state_file_gets_the_recipe_after_a_successful_publish(home, patched_origin) -> None:
    main(["subscribe", "push", "--semester", SEMESTER])
    assert load_state(cfg, SEMESTER).uid_recipe == UID_RECIPE_VERSION


def test_failed_publish_leaves_the_marker_absent(home, failing_publish) -> None:
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 1
    assert load_state(cfg, SEMESTER).uid_recipe is None
    assert not cfg.course_ledger_path(SEMESTER).exists()


def test_second_push_does_not_migrate_again(home, capsys, patched_origin) -> None:
    main(["subscribe", "push", "--semester", SEMESTER])
    capsys.readouterr()
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "v1 UID 配方" not in capsys.readouterr().out


def test_mass_cancellation_exit_code_is_six_and_writes_nothing(home, patched_origin) -> None:
    """CD4：熔断必须**零写盘**——留底、两份台账、状态文件字节全不变。"""
    before = _sha_of_all_side_effects(cfg)
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 6
    assert _sha_of_all_side_effects(cfg) == before
```

> `capsys.readoutenc()` 那行是笔误示范，写的时候直接用
> `capsys.readouterr().out`；上面 `if False else` 的写法**不要提交**。

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_cli_course_cancellation.py -q --basetemp=_notes/pytest-tmp`
Expected: FAIL（无预告、无回写、退出码是 1）

- [ ] **Step 3: 实现**

`_write_exam_ledger` 旁边加同形的 `_write_course_ledger`（`private=True`、先 `mkdir`、
`OSError` 只 warning）。`_subscribe_push` 里：

```python
baseline_text = last_local.read_text(encoding="utf-8") if last_local.is_file() else None
migrating = needs_uid_migration(state) and baseline_text is not None
result = build_ics_for_semester(
    cfg,
    semester,
    baseline_probe=...,
    published_baseline_text=baseline_text if migrating else None,
    allow_mass_cancellation=args.allow_mass_cancellation,
)
if result.migration_summary:
    # 自动但不静默（CD11）：迁移豁免门 2，但必须在这一刻把规模说清楚。
    _print_migration_notice(result.migration_summary)
...
publish(...)  # 现有位置不动
atomic_write_text(last_local, result.ics)  # 现有
_write_exam_ledger(...)  # 现有
_write_course_ledger(cfg, semester, result.course_ledger_text)  # 新增
if migrating:
    save_state(cfg, replace(state, uid_recipe=UID_RECIPE_VERSION))  # 最后一步
```

`_print_migration_notice` 的措辞必须包含"这是升级的预期行为"与"不需要额外操作"，
并按红线写「通常会」：

```python
def _print_migration_notice(summary: MigrationSummary) -> None:
    print(
        f"提示：检测到上次发布使用 v1 UID 配方，本次将一次性撤销 {summary.candidates} 条"
        "旧事件并按新配方原地重建——这是升级到 v0.8 的预期行为，不需要额外操作。\n"
        "  会回源的订阅客户端通常会随之移除旧事件（Google 网页版已实测）；"
        "忽略该字段的客户端（Outlook.com、ColorOS）会留下一批"
        f"「{CANCELLATION_SUMMARY}」占位，直到原定时刻过去——"
        "整学期规模的残条在真机上尚未演练，退订再重新添加是唯一确定有效的清理手段。"
    )
```

`_subscribe_rotate` 同样接 `allow_mass_cancellation`；它的**只读探针**渲染
（`cli.py:1406` 那段）必须**不**产生迁移副作用：探针传 `published_baseline_text=None`，
并在换 token 前把迁移预告打出来（照 D24 的"先警告再换"）。

- [ ] **Step 4: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_cli_course_cancellation.py tests/test_cli_cancellation.py tests/test_cli_subscribe.py -q --basetemp=_notes/pytest-tmp`

```bash
git add src/xjtu_calendar/cli.py tests/test_cli_course_cancellation.py
git commit -m "feat(cli): push/rotate 接迁移预告与 uid_recipe 回写，熔断零写盘"
```

---

### Task 9: `export` 路径与 `--allow-mass-cancellation` 三处接线（CD3、CD4）

**Files:**
- Modify: `src/xjtu_calendar/cli.py`（`cmd_export`、三个子命令的 `add_argument`）
- Test: `tests/test_cli_course_cancellation.py`（追加）

- [ ] **Step 1: 写用例**

```python
def test_export_blocked_writes_no_output_file(home, tmp_path) -> None:
    """export 触发熔断 ⇒ 不写产物、不写台账、退出码 6（与 push 同一判据同一心智）。"""
    out = tmp_path / "timetable.ics"
    assert main(["export", "--semester", SEMESTER, "-o", str(out)]) == 6
    assert not out.exists()


def test_export_writes_course_ledger_after_the_output(home) -> None:
    assert main(["export", "--semester", SEMESTER, "-o", str(out)]) == 0
    assert cfg.course_ledger_path(SEMESTER).is_file()


def test_export_never_carries_migration_cancellations(home) -> None:
    """CD3：迁移只在 subscribe push。手工导入的人把几百条撤销当新事件导进去更糟。"""
    assert main(["export", "--semester", SEMESTER, "-o", str(out)]) == 0
    assert "v1 UID 配方" not in capsys.readouterr().out


def test_flag_accepted_by_three_commands(home) -> None:
    for argv in (["export"], ["subscribe", "push"], ["subscribe", "rotate"]):
        assert main([*argv, "--semester", SEMESTER, "--allow-mass-cancellation"]) in (0, 1)
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_cli_course_cancellation.py -q -k "export or flag" --basetemp=_notes/pytest-tmp`
Expected: FAIL —`unrecognized arguments: --allow-mass-cancellation`

- [ ] **Step 3: 实现**

`export` / `subscribe push` / `subscribe rotate` 三个 parser 上各加一条
`--allow-mass-cancellation`（`action="store_true"`，help 里点名"疑似课表没抓全时会拒绝发布"）。
`cmd_export` 里：不传 `published_baseline_text`（迁移与 export 无关，CD3），
`MassCancellationBlocked` 沿 `main()` 的异常出口自然变成退出码 6，且**写产物与写台账都在
`build_ics_for_semester` 返回之后**——异常时一行都没落盘（Step 1 第一条用例的依据）。

- [ ] **Step 4: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_cli_course_cancellation.py tests/test_cli_exams.py -q --basetemp=_notes/pytest-tmp`

```bash
git add src/xjtu_calendar/cli.py tests/test_cli_course_cancellation.py
git commit -m "feat(cli): export 路径接课程台账回写，熔断旗标三处统一"
```

---

### Task 10: `doctor` 新项「UID 配方与迁移状态」（CD18）

**Files:**
- Modify: `src/xjtu_calendar/doctor.py`（`_check_exam_snapshot:159` 附近、`run_checks:231`）
- Test: `tests/test_doctor.py`（追加）

**Interfaces:**
- Consumes：`subscribe.needs_uid_migration`、`exporter.UID_RECIPE_VERSION`、`Settings.course_ledger_path`
- Produces：`Check(name="uid_recipe", ...)`，`STATUS_OK` / `STATUS_WARN` 两态，**永不 `STATUS_FAIL`**

- [ ] **Step 1: 写用例**

```python
def test_doctor_warns_when_migration_pending(cfg, semester) -> None:
    got = _check_uid_recipe(cfg, semester, _state(uid_recipe=None))
    assert got.status == STATUS_WARN and "迁移" in got.message


def test_doctor_ok_after_migration(cfg, semester) -> None:
    got = _check_uid_recipe(cfg, semester, _state(uid_recipe=UID_RECIPE_VERSION))
    assert got.status == STATUS_OK


def test_doctor_message_carries_no_personal_data(cfg, semester) -> None:
    """输出要过隐私 grep：无课程名/教师/教室/座位/token（规格 §8）。"""
    got = _check_uid_recipe(cfg, semester, None)
    assert "示例课程" not in got.message and "@" not in got.message
```

- [ ] **Step 2: 跑它，确认失败**

Run: `"$PY" -m pytest tests/test_doctor.py -q -k uid_recipe --basetemp=_notes/pytest-tmp`
Expected: FAIL —`ImportError: cannot import name '_check_uid_recipe'`

- [ ] **Step 3: 实现并挂进 `run_checks`**

```python
def _check_uid_recipe(cfg: Settings, semester: str, state: Any) -> Check:
    """UID 配方与迁移状态（CD18）。警告级——迁移是预期动作，不是故障。"""
    ledger = cfg.course_ledger_path(semester)
    count = (
        len(load_ledger(ledger.read_text(encoding="utf-8"), label="课程台账"))
        if ledger.is_file()
        else 0
    )
    if state is None:
        return Check("uid_recipe", STATUS_OK, f"配方 v{UID_RECIPE_VERSION}；未配置订阅，无迁移")
    if needs_uid_migration(state):
        return Check(
            "uid_recipe",
            STATUS_WARN,
            f"上次发布使用 v1 配方；下次 subscribe push 会一次性撤销旧事件并原地重建"
            f"（课程台账现有 {count} 条）",
        )
    return Check("uid_recipe", STATUS_OK, f"配方 v{state.uid_recipe}；课程台账 {count} 条")
```

用 `_safe("uid_recipe", _check_uid_recipe, cfg, semester, state)` 挂进 `run_checks`
（判据内部抛错必须降级成一条检查项，这是 doctor 的既有契约）。检查项总数从 9 变 10，
`tests/test_doctor.py` 里若有硬编码条数的断言，改成断言"包含该项"而不是数总数。

- [ ] **Step 4: 跑定向 + 提交**

Run: `"$PY" -m pytest tests/test_doctor.py -q --basetemp=_notes/pytest-tmp`

```bash
git add src/xjtu_calendar/doctor.py tests/test_doctor.py
git commit -m "feat(doctor): 新增 UID 配方与迁移状态检查项"
```

---

### Task 11: 文档交付

**Files:**
- Modify: `README.md`（「URL 订阅 → 已知边界」、「考试安排」、命令参考、退出码表、FAQ、路线图）
- Modify: `index.html`（`#why` 的 UID 那张卡、`#subscribe` 的口径块）
- Modify: `skills/xjtu-timetable-setup/SKILL.md`（第 7 步升级说明）
- Modify: `docs/design/2026-10-09-exam-cancellation.md`（D17 加指针）
- Modify: `docs/design/2026-10-11-subscription-followups.md`（§2 标注落地）
- Modify: `CHANGELOG.md`（0.8.0 条目）

**Interfaces:** 无代码接口。产出的是"用户会读到的事实"，所以每条陈述都要能指回规格 CD 或真机读数。

- [ ] **Step 1: README 已知边界改写**

把「课程改名会表现为新事件」那条整段替换：v0.8.0 起改名走**原地更新**（`course_id`
缺失的行例外，仍会产生新事件且 `diff` 会先报出来）；真退课/教学班变化会以
`STATUS:CANCELLED` 下发，客户端表现与考试侧同（Google 网页移除、Outlook/ColorOS 留
「已取消的活动」占位到原定时刻过去）。**未演练前一律写"通常会"**。

- [ ] **Step 2: README 新增一节「升级到 v0.8（UID 配方变更）」**

必须写清四件事：①订阅端由工具自动迁移，用户零操作；②`export` 手工导入者需要
**删除旧日历重新导入一次**（迁移撤销不在 export 下发，CD3）；③忽略取消字段的客户端
会看到一次整学期「已取消的活动」，唯一确定有效的清理是退订重加；④回滚不是免费的
（退回 v0.7.x 会双份），所以真要出问题停在 0.8.0 前修，别去动 `uid_recipe`。

- [ ] **Step 3: 命令表与退出码表**

命令参考加 `--allow-mass-cancellation`（export/push/rotate 三处）；退出码表加
`6 = 疑似数据不完整的整批撤销被拒绝发布`。FAQ 加一条：
「为什么 push 拒绝发布并说占了多少比例？」——答：门 2 的判据、先 `diff` 确认、
确认无误再带旗标。

- [ ] **Step 4: 站点与 skill**

`index.html` 里「UID 稳定 + SEQUENCE 版本」那张卡补一句"课程名不再参与身份"；
`#subscribe` 的口径块加一行升级提示。`skills/xjtu-timetable-setup/SKILL.md` 第 7 步
加一段升级说明，**不新增判据** ⇒ `MIN_DOC_VERSION` 保持 `(0, 7, 0)`（CD18）。

- [ ] **Step 5: 两处规格指针 + CHANGELOG**

`docs/design/2026-10-09-exam-cancellation.md` 的 D17 行就地加
「**已被 v0.8 推翻**，见 `2026-10-12-course-cancellation-uid-v2.md` CD1」，原文保留；
followups §2 标注落地形态。CHANGELOG 的 0.8.0 条目把 **UID 配方变更放在最前面**——
它是唯一会让老用户第一眼困惑的东西。

- [ ] **Step 6: 格式与隐私自查 + 提交**

Run: `"$PY" -m ruff format --check . && "$PY" -m ruff check .`
（`ruff format` 会格式化 Markdown 里的 ```python 围栏，纯文档改动也要过它。）
Run: `git ls-files -z | xargs -0 grep -niF -f _notes/privacy-patterns.txt`
Expected: 只剩既有的 `XLJFZ` / `Program Files` 类命中，无新增。

```bash
git add README.md index.html skills docs CHANGELOG.md
git commit -m "docs: v0.8 升级口径、退出码 6 与 D17 的推翻指针"
```

---

### Task 12: 收尾——升版、全量门禁、干净检出复验

**Files:**
- Modify: `pyproject.toml`（`version = "0.8.0"`）

- [ ] **Step 1: 升版并重装 editable**

```bash
"$PY" -m pip install -e . --no-deps
"$PY" -m xjtu_calendar --version
```
Expected: `0.8.0`（`importlib.metadata` 读的是安装时生成的元数据，不重装会报旧号）

- [ ] **Step 2: 四条全量门禁（本计划唯一一次）**

```bash
"$PY" -m pytest -q --basetemp=_notes/pytest-tmp
"$PY" -m mypy
"$PY" -m ruff check .
"$PY" -m ruff format --check .
```
Expected: 全绿。测试总数会比 0.7.0 的 715 明显增加（本计划新增约 35-45 条用例）。

- [ ] **Step 3: 真实环境跑一次只读体检**

Run: `"$PY" -m xjtu_calendar doctor --semester 2026-2027-1 --verify`
Expected: 退出码 0，检查项 10 条，新增的 `uid_recipe` 项按当前状态给 OK 或警告；
输出过一遍隐私黑名单。

- [ ] **Step 4: 干净检出复验**

```bash
git worktree add --detach _notes/wt-080 HEAD
cd _notes/wt-080
"$PY" -m pytest -q --basetemp=../../_notes/pytest-tmp-080
"$PY" -m mypy && "$PY" -m ruff check . && "$PY" -m ruff format --check .
cd ../.. && git worktree remove _notes/wt-080
```
（新检出里没有 `_notes/`，`--basetemp` 必须指到外面。）

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "chore: prepare v0.8.0 release"
```

**到这里停。** 打 tag、发 Release、传 PyPI、push 各需单独授权；`AGENTS.md` 的
「提交 ≠ 推送」在这一版尤其要紧——UID 配方变更一旦发布就回不去（规格 §11）。

---

### Task 13: 隔离真机演练（CD6，发版前必做）

**Files:**
- Create: `_notes/drill-uid-v2-protocol.md`（gitignored，演练配方）
- Modify（演练结论回写）: `docs/design/2026-10-12-course-cancellation-uid-v2.md` §11、
  `README.md` 客户端矩阵、`skills/.../references/client-matrix.md`（含同步 sha）

- [ ] **Step 1: 布置隔离 URL**

用一次性 token 的专用公开仓库（**不要**碰日常订阅），按 README 的 Pages 步骤建好。
用**当前 main（v1 配方）**发布一次；手机侧 Google 网页 + ColorOS 各订阅这条腿，确认 v1 事件到位。

- [ ] **Step 2: 先看"要公开什么"再发布**

演练产物是真实课表。按 `AGENTS.md` 与规格的隐私门，push 前逐条给用户看：课程名、
教师、教室、（若含考试）考场与座位号会进入那份公开文件，并明确给出 `--no-exams` 出口。

- [ ] **Step 3: 触发迁移并记录读数**

实现完成后 `subscribe push`（状态文件天然缺 `uid_recipe` ⇒ 自动走迁移）。记录：
①Google 侧旧事件是否消失、新事件是否到位、耗时；②ColorOS 侧整学期「已取消的活动」
长什么样、能否辨认、多久退场；③顺手补矩阵欠的"产物改名后已订阅客户端会不会跟着改
显示名"那一格（这次会新增一批同 UID 事件，是白捡的观测）。

- [ ] **Step 4: 清理并回写**

演练产物从分支移除、URL 归 404（照 v0.6 演练的做法），删除隔离仓库或清空分支。
结论回写规格 §11 与 README 矩阵，**每一格都带实测日期 + 设备/版本**；没验的写"未验"，
不许照抄别家、不许把"通常会"升级成事实。矩阵更新后同步 `references/client-matrix.md`
并更新其同步 sha。

- [ ] **Step 5: Commit**

```bash
git add docs/design/2026-10-12-course-cancellation-uid-v2.md README.md skills
git commit -m "docs(drill): v1→v2 迁移的隔离真机读数回写矩阵与规格 §11"
```

---

## Self-Review

**1. Spec coverage（CD → 任务）**

| CD | 任务 | | CD | 任务 |
|---|---|---|---|---|
| CD1/CD2 | 2 | | CD11 | 6、8 |
| CD3 | 3、6、8、9、11 | | CD12 | 3、6 |
| CD4 | 3、6、8、9、11 | | CD13 | 3、6 |
| CD5 | 1、6、11 | | CD14 | 6 |
| CD6 | 13 | | CD15 | 2 |
| CD7 | 7、8 | | CD16 | 2 |
| CD8 | 5、6 | | CD17 | 7 |
| CD9 | 1 | | CD18 | 10、11 |
| CD10 | 4 | | CD19 | 12 |

规格 §9 的 19 条用例全部有归属：1-3→Task 2、4-7→Task 4/7/8、8→Task 6、9-12→Task 3/6/8/9、
13→Task 1、14→Task 6、15-18→Task 6、19→Task 12 Step 3。

**2. Placeholder scan**：无 TBD/TODO；Task 6 与 Task 8 的用例代码里对夹具的依赖写成
"照 `tests/exam_support.py` 的现有形状"，是指明复用哪份既有固件（具体文件、具体函数），
不是留空。Task 9 的用例片段里 `out` / `cfg` / `capsys` 沿用上文夹具与既有 fixture 名，
执行时补全为完整用例（该文件已有同名夹具可用）。

**3. Type consistency**：跨任务签名的单一事实来源已核对——
`render_ledger` / `load_ledger(label=…)` / `cancel_candidates` / `build_cancellation_events`
（Task 1 定义，Task 4/6 消费）；`migration_candidates(published, live_uids,
normal_cancel_uids, now)`（Task 4 定义，Task 6/7 消费，Task 7 用例按位置参数调用以验证
签名稳定）；`CoursesTrust.trusted` / `MassCancellation.ratio`（Task 3 定义，Task 6 消费）；
`uid_recipe` 与 `needs_uid_migration`（Task 7 定义，Task 8/10 消费）；
`UID_RECIPE_VERSION`（Task 2 定义，Task 7/8/10 消费）。
