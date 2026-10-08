# 考试安排接入（`exams` 数据源）实现计划

> **执行方式：** 按任务逐条实现（TDD），每个任务收尾过一次独立评审，全部完成后再做全分支终审。
>
> **执行结果（2026-10-09）：** 12 个任务全部完成，分支 `exam-schedule`（HEAD `20ad6a6`，基点 `0bd9414`），
> 每任务均经独立评审；终审后的一轮合并修复关闭了 7 项（含 §8 `private=True` 无法证伪这条 Important）。
> 门禁：pytest 601 passed / 4 skipped（跳过项全是 Windows 不执行 POSIX 权限位）、mypy strict 干净、
> ruff check 干净、ruff format 68 文件已格式化。
> 两处与本文原稿不同，以代码为准：Task 7 的端点缺失判定走 `require_endpoint` + `EndpointNotConfigured`；
> 翻页护栏（spec §6.4）原稿漏列，已补进 `cli._fetch_exams`。
> 下面步骤框已勾满；其中「断网/会话过期跑 fetch」与 push/rotate 两项是**离线等价验证**
> （stubbed transport 用例 + `--from-file` 真跑），未打真实请求，也不需要为此登录任何账号。

**Goal:** 把 eHall「我的考试安排」接进现有 `fetch → 快照 → 解析 → 导出 → diff → subscribe` 管线，让同一份 .ics 同时包含课程事件与考试事件。

**Architecture:** 新建 `src/xjtu_calendar/exams.py` 承担考试侧全部逻辑（信封遍历、时间文本解析、三态判定、事件构建、变更比对），`models.py` 只加一个 `ExamSchedule` 数据类；`fetcher.save_raw/load_raw` 泛化出 `kind=` 参数，`exporter.build_ics_for_semester` 在**日期过滤之前**把考试事件并进课程事件并做一次全局 UID 断言。SEQUENCE/留底机制原样复用，不新增第二份日历、第二个 token。

**Tech Stack:** Python 3.11+（stdlib only：`re`/`hashlib`/`dataclasses`/`enum`/`pathlib`），`icalendar>=6.1.0`，pytest，mypy --strict，ruff。

**Spec:** `docs/design/2026-10-08-exam-schedule.md`（本计划逐节对应；§4.1 的四轮实测形态与 §6.4 的红线是硬约束，不可在实现时"顺手简化"）

## Global Constraints

- 零新增运行时依赖；**不触碰课程 UID 算法与课程事件导出内容**（legacy UID contract，见 `exporter.make_uid` docstring）。
- 端点路径必须来自真实观测。`exam_schedule` 的 path 是 `/jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do`（`fetcher.PLACEHOLDER_MARKER` 会拒绝占位路径）。
- **考试事件必须是带 `TZID` 的 `DATE-TIME`，禁止 `VALUE=DATE` 全天事件**：`sequence.parse_baseline`（`sequence.py:139-141`）会静默丢弃非 datetime 事件，一旦被踢出基线，该考试每次重发布 SEQUENCE 恒为 0，客户端永不更新。拼装一律走 `schedules.combine(day, hhmm)`。
- **宁缺毋滥**：三态判定为「状态未知」时**绝不覆盖**已有考试快照；`fetch`/`export` 绝不因为考试失败而非零退出。
- 判据只看 `datas.<模块>.extParams.code`；**外层 `code` 恒为字符串 `"0"`，不可当成败标志**。
- `raw/exams-*.json` 含学号/姓名/教师名，只落在 `~/.xjtu-timetable-calendar/raw/`（已被 `.gitignore` 排除），写盘必须 `private=True`。
- 测试固件里的人员字段一律占位（`示例课程甲` / `教师甲` / `D-1`），但**字段形态保留真实脏数据**（全角冒号、em dash、点分式）。
- **固件里的日期必须是能 `date.fromisoformat` 的合成日期**（`2030-*`，见 T1 的 `DEMO_DAY`）。设计文档 §4 用 `YYYY-MM-DD` 打码，那是文档口径；搬进可执行固件会让解析层直接跳过该条，测试全红。
- 测试文件里的 import 分组必须按 ruff `I`（isort）的顺序，否则 `ruff check .` 直接报 I001：
  `import pytest` / `from exam_support import ...` / `from subscribe_support import ...` 归**第三方**一组（`tests/` 没有 `__init__.py`，`pythonpath` 只加了 `src`，所以写 `from exam_support import ...`，**不是** `from tests.exam_support import ...`），空一行再写 `from xjtu_calendar...` 第一方一组。
- 抓日志一律用 `exam_support.captured_logs()`，**不要用 `caplog`**：`setup_logging` 把 `xjtu_calendar` logger 的 `propagate` 设成 False（`logging_setup.py:111`），同一会话里只要有用例跑过 `main()`，挂在 root 上的 caplog 就抓不到本项目的日志。
- `mypy --strict` 的 `files` 只有 `src/xjtu_calendar`（`pyproject.toml:80-84`），测试文件不做类型检查——但 `src` 里的每个新函数都要有完整注解。
- 面向用户文案为中文，风格对照 README 既有章节（fail-closed、给补救指引、不假装成功）。
- **循环依赖约束**：`exams.py` 在模块顶层 `from .exporter import UID_DOMAIN`；因此 `exporter.py` 只能在 `build_ics_for_semester` **函数体内**局部 `from .exams import ...`（沿用该函数已有的 `from .fetcher import load_raw` 写法）。
- `pyproject.toml:72-78` 的 `testpaths = ["tests", "src"]` + `--doctest-modules`：`exams.py` 的 docstring 里**不要**放 `>>>` 示例，除非它真能跑通。
- 每个任务收尾：`python -m pytest -q` + `python -m mypy` + `python -m ruff check .` + `python -m ruff format --check .` 四条全绿（与 `.github/workflows/ci.yml` 一致）。`ruff format --check .` 会连带检查 Markdown 里的 ```python 代码块——改文档后也要跑。
- 本机临时目录受限时给 pytest 加 `--basetemp=<仓库内可写目录>`（如 `_notes/pytest-tmp`，已 gitignore）；这是环境差异，不是仓库缺陷。
- 提交只到本地；**推送需要用户明确授权**（本机 `git push` 走不通，用 `_notes/` 下的 API 重放脚本）。

**文件总览**

| 动作 | 路径 | 职责 |
|---|---|---|
| Create | `src/xjtu_calendar/exams.py` | 考试侧全部逻辑：候选表、时间解析、三态、事件构建、变更比对 |
| Create | `tests/exam_support.py` | 共享考试固件：真信封 payload、打码行、假 transport |
| Create | `tests/test_exams.py` | T2–T5 的单元与红线测试 |
| Create | `tests/test_exams_fetch.py` | 三态落盘行为（状态未知不覆盖快照） |
| Create | `tests/test_exporter_exams.py` | 合并、UID 唯一性、SEQUENCE 基线、`--no-exams` |
| Create | `tests/test_diff_exams.py` | 考试变更五类 |
| Modify | `src/xjtu_calendar/models.py` | +`ExamSchedule`（frozen） |
| Modify | `src/xjtu_calendar/config.py` | +`raw_exams_path` / `raw_exams_prev_path` |
| Modify | `src/xjtu_calendar/fetcher.py` | `save_raw`/`load_raw` 加 `kind=`；补 `private=True` |
| Modify | `src/xjtu_calendar/data/ehall_endpoints.json` | +`exam_schedule` 端点 |
| Modify | `src/xjtu_calendar/exporter.py` | `build_ics_for_semester` 并入考试事件 + 全局 UID 断言 |
| Modify | `src/xjtu_calendar/subscribe.py` | `snapshot_age_days(..., kind=)` 泛化 |
| Modify | `src/xjtu_calendar/cli.py` | `--no-exams`（fetch/export/push/rotate）、diff 考试小节 |
| 未改（按落地修订） | `tests/subscribe_support.py` | 原计划「`make_home` 支持真信封与考试快照」**没有实施**：任何任务都没动过该文件。真信封形态由 `tests/exam_support.py` 提供（`exam_payload`/`timetable_envelope`），需要真信封的用例在**各测试内部直接覆盖** `make_home` 写出的课表 payload 文件——spec §9 对固件改造点的要求以此方式满足 |
| Modify | `README.md` / `CHANGELOG.md` | 「考试安排」小节、旗标表、`[Unreleased]` |

---

## Task 1: `ExamSchedule` 模型与考试固件

**Files:**
- Modify: `src/xjtu_calendar/models.py`（接在 `CalendarEvent` 之后，`:298` 前）
- Create: `tests/exam_support.py`
- Create: `tests/test_exams.py`（先只有固件自检一条）

**Interfaces:**
- Consumes: `models._validate_hhmm(value, label)`（同文件私有工具，`:352`，`SchedulePeriods` 已在用；`ExamSchedule.__post_init__` 直接调用，不需要 import）
- Produces: `models.ExamSchedule`（frozen dataclass，字段见下）；`tests/exam_support` 里的 `exam_row(**over)`、`exam_payload(rows, *, code=1, msg="查询成功", module="wdksap")`、`timetable_envelope(rows)`、`captured_logs()`（日志抓取，替代 caplog）、`DEMO_DAY = "2030-06-17"`、`SEMESTER = "2026-2027-1"`

- [x] **Step 1: 写失败测试**（固件本身也要被测，否则后面所有断言都建在沙子上）

```python
# tests/test_exams.py
from dataclasses import FrozenInstanceError, replace

import pytest
from exam_support import DEMO_DAY, exam_payload, exam_row

from xjtu_calendar.models import ExamSchedule


def test_exam_row_has_the_observed_dirty_shapes():
    """固件必须保留实测形态，不许"洗干净"。"""
    rows = [
        exam_row(KSRQ="2030-06-18 00:00:00", KSSJMS="2030-06-18 15:00-17:30(星期二)"),
        exam_row(
            KSRQ="2030-06-20 00:00:00", KSSJMS="2030-06-20 9：00-11：30(星期四)"
        ),  # 全角+个位小时
        exam_row(KSRQ="2030-06-23 00:00:00", KSSJMS="2030-06-23 15:00—17:30(星期日)"),  # em dash
        exam_row(
            KSRQ="2030-06-23 00:00:00", KSSJMS="2030-06-23 考试时间为：9.30-12.00(星期日)"
        ),  # 点分式
    ]
    payload = exam_payload(rows)
    module = payload["datas"]["wdksap"]
    assert module["rows"] == rows
    assert module["extParams"] == {"msg": "查询成功", "code": 1, "logId": "DEMO"}
    assert payload["code"] == "0"  # 外层恒为字符串 "0"，与成败无关


def test_exam_schedule_is_frozen_and_rejects_end_before_start():
    exam = ExamSchedule(
        course_id="ARCH000000",
        course_name="示例课程甲",
        exam_name="结课考试",
        date_str=DEMO_DAY,
        start_time="09:00",
        end_time="11:30",
        location="A-1001",
        campus=None,
        seat="NN",
        credits=2.0,
        teacher="教师甲",
        row_id="WID-DEMO-1",
        task_id=None,
        exam_code="KSDM-1",
    )
    assert exam.course_name == "示例课程甲"
    with pytest.raises(FrozenInstanceError):
        exam.course_name = "改名"  # type: ignore[misc]
    with pytest.raises(ValueError):
        replace(exam, end_time="08:00")
    with pytest.raises(ValueError):
        replace(exam, end_time="9:00")  # 非零补齐但仍是合法时刻，须能参与比较
    assert replace(exam, start_time="9:00").start_time == "09:00"  # 归一化后再比较
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exams.py -q`
Expected: FAIL — `ImportError: cannot import name 'ExamSchedule'`（或 `No module named 'tests.exam_support'`）

- [x] **Step 3: 写最小实现**

> `tests/exam_support.py` 不是包（`tests/` 下没有 `__init__.py`，`pythonpath` 只配了 `src`），
> 所以用例里一律写 `from exam_support import ...`，与既有 `from subscribe_support import ...`
> 同一口径；写成 `from tests.exam_support import ...` 会直接 `ModuleNotFoundError`。

`models.py` 末尾加（时间用 `"HH:MM"` 字符串持有，交给 `schedules.combine` 落地，避免 naive datetime）：

```python
@dataclass(frozen=True)
class ExamSchedule:
    """一场考试。字段来自 `studentWdksapApp` 的 `wdksap` 行，形态见设计文档 §4。

    时间一律持有为 ``"HH:MM"`` 字符串：拼装统一走 :func:`xjtu_calendar.schedules.combine`，
    它强制 ``Asia/Shanghai`` 且拒绝 naive datetime。``grade`` 一类可改字段
    **绝不进 UID**（同 :class:`CourseMeeting` 的约定）。
    """

    course_id: str | None
    course_name: str
    exam_name: str | None
    date_str: str  # ISO 日期（来自 KSRQ，时间部分恒 00:00:00，已丢弃）
    start_time: str  # "HH:MM"，来自 KSSJMS
    end_time: str  # "HH:MM"，来自 KSSJMS
    location: str | None
    campus: str | None
    seat: str | None
    credits: float | None
    teacher: str | None
    row_id: str | None  # WID：UID 首选依据
    task_id: str | None  # KSRWID：WID 缺失时的退路
    exam_code: str | None  # KSDM：批次

    def __post_init__(self) -> None:
        _validate_hhmm(self.start_time, "start_time")
        _validate_hhmm(self.end_time, "end_time")
        # 归一化成零补齐的 "HH:MM"：_validate_hhmm 允许 1 位小时，而下面要比字符串，
        # "9:00" < "11:30" 会给出相反的答案。combine 本身两种都吃，归一化不损失信息。
        for field_name in ("start_time", "end_time"):
            raw = getattr(self, field_name).strip()
            hour, minute = raw.split(":")
            object.__setattr__(self, field_name, f"{int(hour):02d}:{int(minute):02d}")
        if self.end_time <= self.start_time:
            raise ValueError(f"考试结束时间必须晚于开始：{self.start_time} -> {self.end_time}")
```

`tests/exam_support.py`：

```python
"""考试侧共享固件：真实字段形态 + **合成**取值。

固件里的日期是编出来的（2030-*），不是"打码后的真实日期"——设计文档 §4 的样例才用
`YYYY-MM-DD` 占位。原因：`build_exam_events` 要 `date.fromisoformat(...)`，
占位串会把构造层测试全卡死。形态（全角冒号 / em dash / 点分式）照 §4.1 保留。
"""

from __future__ import annotations

import contextlib
import logging

SEMESTER = "2026-2027-1"
#: 合成基准日：2030-06-17 确实是星期一，与 KSSJMS 括号里的星期自洽。
DEMO_DAY = "2030-06-17"


@contextlib.contextmanager
def captured_logs(logger_name: str = "xjtu_calendar"):
    """抓 `xjtu_calendar` logger 的记录，**不用 caplog**。

    为什么：`logging_setup.setup_logging` 会把该 logger 的 ``propagate`` 置 False
    （`:111`），而 caplog 的 handler 挂在 root 上——只要同一 pytest 会话里有别的
    用例跑过 `main()`，caplog 就再也抓不到本项目日志了（表现为随机失败）。
    直接给该 logger 挂 handler 与 propagate 无关，永远收得到。
    """
    logger = logging.getLogger(logger_name)
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Collect()
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield records
    finally:
        logger.setLevel(previous_level)
        logger.removeHandler(handler)


def exam_row(**over: str) -> dict[str, str]:
    """一行考试记录，字段名与真实响应一致，值全部为合成数据。"""
    row = {
        "KCM": "示例课程甲",
        "KCH": "ARCH000000",
        "KSMC": "2029-2030学年 第二学期 结课考试",
        "KSRQ": f"{DEMO_DAY} 00:00:00",
        "KSSJMS": f"{DEMO_DAY} 15:00-17:30(星期一)",
        "JASMC": "A-1001",
        "ZWH": "NN",
        "XXXQDM": "5",
        "XF": "2.0",
        "ZJJSXM": "教师甲",
        "WID": "WID-DEMO-1",
        "KSDM": "KSDM-1",
        "KSRWID": "KSRWID-1",
    }
    row.update(over)
    return row


def timetable_envelope(rows: list[dict[str, str]]) -> dict[str, object]:
    """课表快照的**真实信封**形态（`datas.xskcb.rows`），不是 `{"kbList": ...}` 那种简写。

    `campus_names_from_timetable` 只认真信封；`subscribe_support.make_home` 写的是
    `{"kbList": rows}`（设计文档 §9 点名的固件改造点），所以要测校区对照就得用这个。
    """
    return {"datas": {"xskcb": {"totalSize": len(rows), "rows": list(rows)}}}


def exam_payload(
    rows: list[dict[str, str]], *, code: int = 1, msg: str = "查询成功", module: str = "wdksap"
) -> dict[str, object]:
    """完整响应信封：外层 code 恒为字符串 "0"，成败标志在 extParams.code。"""
    return {
        "code": "0",
        "datas": {
            module: {
                "totalSize": len(rows),
                "pageSize": 999,
                "rows": list(rows),
                "extParams": {"msg": msg, "code": code, "logId": "DEMO"},
            }
        },
    }
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exams.py -q`
Expected: PASS（2 passed）

- [x] **Step 5: 四条门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/models.py tests/exam_support.py tests/test_exams.py
git commit -m "feat(models): add ExamSchedule with HH:MM time holders"
```

---

## Task 2: `parse_exam_time_text` —— 四种实测形态

**Files:**
- Create: `src/xjtu_calendar/exams.py`
- Test: `tests/test_exams.py`

**Interfaces:**
- Consumes: `models.ExamSchedule`（T1）
- Produces: `exams.parse_exam_time_text(text: str | None) -> tuple[str, str] | None`（返回两个 `"HH:MM"`；解析不出返回 `None`）

- [x] **Step 1: 写失败测试**（表格化覆盖 §4.1 四种形态 + 四类脏输入）

```python
import pytest

from xjtu_calendar.exams import parse_exam_time_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2030-06-17 15:00-17:30(星期一)", ("15:00", "17:30")),  # 基准：半角 + '-'
        ("2030-01-08 9：00-11：30(星期二)", ("09:00", "11:30")),  # 全角 + 个位小时
        ("2030-06-16 15:00—17:30(星期日)", ("15:00", "17:30")),  # em dash 连接
        ("2030-12-15 16:00—18:00(星期日)", ("16:00", "18:00")),
        ("2030-06-20 考试时间为：9.30-12.00(星期四)", ("09:30", "12:00")),  # 自由文本 + 点分
        ("15:00-17:00", ("15:00", "17:00")),  # 无日期前缀
    ],
)
def test_parse_exam_time_text_accepts_observed_shapes(text, expected):
    assert parse_exam_time_text(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "待定",  # 没有任何时刻
        "15:00",  # 只有一个时刻：起止无法确定
        "2030-01-08 25:00-26:30",  # 非法小时
        "2030-01-08 09:99-11:00",  # 非法分钟
        "2030-01-08 11:30-09:00",  # 结束早于开始
        "2030-01-08 09:00-09:00",  # 相等：CalendarEvent 会硬拒
    ],
)
def test_parse_exam_time_text_returns_none_on_unusable_text(text):
    assert parse_exam_time_text(text) is None


def test_date_prefix_is_stripped_before_matching():
    """日期里的 `.` / `-` 不能被当成时刻分隔符（`2030.06.17` 会被误读成 `30.06`）。"""
    assert parse_exam_time_text("2030.06.17 09:00-11:00(星期一)") == ("09:00", "11:00")
    assert parse_exam_time_text("2030/06/17 09:00-11:00") == ("09:00", "11:00")
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exams.py -q -k exam_time_text`
Expected: FAIL — `ModuleNotFoundError: No module named 'xjtu_calendar.exams'`

- [x] **Step 3: 写最小实现**

```python
"""考试安排（`studentWdksapApp`）的解析与导出。设计文档：docs/design/2026-10-08-exam-schedule.md。"""

from __future__ import annotations

import re

#: 日期前缀：`-` / `.` / `/` 三种连接符都见过或可能见到，必须先剥掉，
#: 否则 `2030.06.17` 里的 `30.06` 会被下面那条时刻正则吃掉。
_DATE_PREFIX = re.compile(r"^\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")

#: 两组「时:分」；分隔符实测见过 ``:``／全角 ``：``／半角点 ``.``／全角点 ``．`` 四种。
_HHMM = re.compile(r"(\d{1,2})[:：.．](\d{2})")


def parse_exam_time_text(text: str | None) -> tuple[str, str] | None:
    """从 ``KSSJMS`` 抓出起止时刻，返回零补齐的 ``("HH:MM", "HH:MM")``。

    抓不出两组合法时刻、或结束不晚于开始时返回 ``None`` —— 调用方**跳过该条**，
    绝不退化成 00:00 的假事件（设计文档 §6.4）。输出交给
    :func:`xjtu_calendar.schedules.combine` 落地成带 ``Asia/Shanghai`` 的 datetime。
    """
    if not text:
        return None
    body = _DATE_PREFIX.sub("", text)
    found: list[str] = []
    for hour_text, minute_text in _HHMM.findall(body):
        hour, minute = int(hour_text), int(minute_text)
        if hour > 23 or minute > 59:
            return None
        found.append(f"{hour:02d}:{minute:02d}")
        if len(found) == 2:
            break
    if len(found) < 2 or found[1] <= found[0]:
        return None
    return found[0], found[1]
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exams.py -q -k exam_time_text`
Expected: PASS（6 + 8 + 1 条）

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/exams.py tests/test_exams.py
git commit -m "feat(exams): parse the four observed KSSJMS time shapes"
```

---

## Task 3: `parse_exam_rows` —— 真信封遍历 + 校区对照

**Files:**
- Modify: `src/xjtu_calendar/exams.py`
- Test: `tests/test_exams.py`

**Interfaces:**
- Consumes: `exam_support.exam_payload/exam_row/timetable_envelope`；`parse_exam_time_text`（T2）；`models.ExamSchedule`（T1）；`datetime.date.fromisoformat`（星期一致性检查）
- Produces: `exams.iter_exam_rows(payload) -> list[dict]`（结构遍历，模块名不硬编码）；`exams.parse_exam_rows(payload, *, campus_names=None, report=None) -> list[ExamSchedule]`；`exams.campus_names_from_timetable(timetable_payload) -> dict[str, str]`

- [x] **Step 1: 写失败测试**

```python
from exam_support import exam_payload, exam_row

from xjtu_calendar.exams import campus_names_from_timetable, iter_exam_rows, parse_exam_rows
from xjtu_calendar.parser import ParseReport


def test_iter_exam_rows_finds_module_without_hardcoding_name():
    payload = exam_payload([exam_row()], module="someOtherModule")
    payload["datas"] = {"whatever": payload["datas"].pop("someOtherModule")}
    assert len(iter_exam_rows(payload)) == 1


def test_iter_exam_rows_keeps_empty_list_but_rejects_shape():
    assert iter_exam_rows(exam_payload([])) == []  # 空 rows 是合法结构，交给三态判定
    assert iter_exam_rows({"code": "0"}) == []


def test_parse_exam_rows_maps_fields_and_skips_unparsable():
    rows = [
        exam_row(),
        exam_row(WID="WID-DEMO-2", KSSJMS="待定"),  # 解析不出 → 跳过
        exam_row(WID="WID-DEMO-3", KCM=""),  # 缺课程名 → 跳过
    ]
    report = ParseReport()
    exams = parse_exam_rows(exam_payload(rows), report=report)
    assert [e.row_id for e in exams] == ["WID-DEMO-1"]
    assert len(report.skipped) == 2


def test_campus_names_from_timetable_builds_code_to_display_map():
    timetable = {
        "datas": {
            "xskcb": {
                "rows": [
                    {"XXXQDM": "5", "XXXQDM_DISPLAY": "创新港校区"},
                    {"XXXQDM": "1", "XXXQDM_DISPLAY": "兴庆校区"},
                    {"XXXQDM": "5", "XXXQDM_DISPLAY": "创新港校区"},
                ]
            }
        }
    }
    assert campus_names_from_timetable(timetable) == {"5": "创新港校区", "1": "兴庆校区"}


def test_parse_exam_rows_uses_campus_map_and_falls_back_silently():
    exam = parse_exam_rows(exam_payload([exam_row(XXXQDM="5")]), campus_names={"5": "创新港校区"})[
        0
    ]
    assert exam.campus == "创新港校区"
    unknown = parse_exam_rows(exam_payload([exam_row(XXXQDM="9")]), campus_names={})[0]
    assert unknown.campus is None  # 对照不到就留空，绝不硬编码代码表


def test_weekday_in_time_text_conflicts_with_date_warns_but_keeps_row():
    """§6.4 的**防御性**检查：22 行实测样本里两处星期全一致，没见过反例。

    仍然要检查，因为 `KSRQ` 与 `KSSJMS(星期X)` 是两个独立字段，谁先腐化不可知，
    而客户端显示的是 `KSRQ` 推出来的那个 —— 所以**以 KSRQ 为准**，只记 warning，
    不丢行（丢了才是真的把考试信息弄没了）。
    """
    report = ParseReport()
    # 2030-06-17 是星期一，文本却写(星期二)
    rows = [exam_row(KSSJMS="2030-06-17 15:00-17:30(星期二)")]
    exams = parse_exam_rows(exam_payload(rows), report=report)
    assert len(exams) == 1  # 保留
    assert exams[0].date_str == "2030-06-17"  # 以 KSRQ 为准
    assert any("星期" in w for w in report.warnings)


def test_weekday_consistent_produces_no_warning():
    report = ParseReport()
    parse_exam_rows(exam_payload([exam_row()]), report=report)  # DEMO_DAY=星期一，固件自洽
    assert report.warnings == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exams.py -q -k "exam_rows or campus"`
Expected: FAIL — `ImportError: cannot import name 'parse_exam_rows'`

- [x] **Step 3: 写最小实现**

```python
from collections.abc import Mapping
from datetime import date
from typing import Any

from .exporter import UID_DOMAIN  # noqa: F401  （T5 用；顶层导入，exporter 反向只在函数内 import）
from .models import ExamSchedule
from .parser import ParseReport

#: 考试行的字段候选表。**不复用 parser.FIELD_CANDIDATES**：那张表按课程写
#: （teacher 认 SKJS，考试行是 ZJJSXM），且报错文案写死「课程」。
EXAM_FIELDS: dict[str, tuple[str, ...]] = {
    "course_id": ("KCH", "courseId"),
    "course_name": ("KCM", "courseName"),
    "exam_name": ("KSMC",),
    "date": ("KSRQ",),
    "time_text": ("KSSJMS", "KSSJ"),
    "location": ("JASMC",),
    "campus_code": ("XXXQDM",),
    "seat": ("ZWH", "KSWZ"),
    "credits": ("XF",),
    "teacher": ("ZJJSXM",),
    "row_id": ("WID",),
    "task_id": ("KSRWID",),
    "exam_code": ("KSDM",),
}


def _text(record: Mapping[str, Any], key: str) -> str:
    for candidate in EXAM_FIELDS[key]:
        value = record.get(candidate)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
    return ""


def iter_exam_rows(payload: Any) -> list[dict[str, Any]]:
    """按结构找出考试行；**不硬编码模块名**，也不看 rows 是否为空（空 rows 交给三态判定）。"""
    datas = payload.get("datas") if isinstance(payload, Mapping) else None
    if not isinstance(datas, Mapping):
        return []
    for module in datas.values():
        if isinstance(module, Mapping) and isinstance(module.get("rows"), list):
            return [row for row in module["rows"] if isinstance(row, Mapping)]
    return []


def campus_names_from_timetable(timetable_payload: Any) -> dict[str, str]:
    """同学期课表快照里的 ``XXXQDM -> XXXQDM_DISPLAY`` 对照（考试行没有 DISPLAY 字段）。"""
    out: dict[str, str] = {}
    for row in iter_exam_rows(timetable_payload):
        code, name = row.get("XXXQDM"), row.get("XXXQDM_DISPLAY")
        if isinstance(code, str) and isinstance(name, str) and code and name:
            out[code] = name
    return out


def parse_exam_rows(
    payload: Any,
    *,
    campus_names: Mapping[str, str] | None = None,
    report: ParseReport | None = None,
) -> list[ExamSchedule]:
    """把考试行转成 :class:`ExamSchedule`。不可用的行跳过并计入 ``report``，绝不造 00:00 假事件。"""
    rep = report or ParseReport()
    out: list[ExamSchedule] = []
    for row in iter_exam_rows(payload):
        rep.total_candidates += 1
        name = _text(row, "course_name")
        raw_date = _text(row, "date")
        pair = parse_exam_time_text(_text(row, "time_text"))
        if not name:
            rep.skip("考试行缺少课程名（KCM），已跳过")
            continue
        if pair is None:
            rep.skip(f"考试「{name}」的时间描述无法解析：{_text(row, 'time_text')!r}")
            continue
        if len(raw_date) < 10:
            rep.skip(f"考试「{name}」缺少考试日期（KSRQ），已跳过")
            continue
        day = raw_date[:10]
        _check_weekday_consistency(rep, name, day, _text(row, "time_text"))
        code = _text(row, "campus_code")
        credits_raw = _text(row, "credits")
        out.append(
            ExamSchedule(
                course_id=_text(row, "course_id") or None,
                course_name=name,
                exam_name=_text(row, "exam_name") or None,
                date_str=day,
                start_time=pair[0],
                end_time=pair[1],
                location=_text(row, "location") or None,
                campus=(campus_names or {}).get(code) if code else None,
                seat=_text(row, "seat") or None,
                credits=float(credits_raw) if credits_raw.replace(".", "", 1).isdigit() else None,
                teacher=_text(row, "teacher") or None,
                row_id=_text(row, "row_id") or None,
                task_id=_text(row, "task_id") or None,
                exam_code=_text(row, "exam_code") or None,
            )
        )
        rep.parsed += 1
    return out


#: `KSSJMS` 括号里的星期后缀，实测形态：`(星期二)`。
_WEEKDAY_IN_TEXT = re.compile(r"星期([一二三四五六日天])")
_WEEKDAY_CHARS = "一二三四五六日"  # 下标 0 = 星期一，与 date.isoweekday() 对齐


def _check_weekday_consistency(rep: ParseReport, name: str, day: str, time_text: str) -> None:
    """`KSRQ` 推出来的星期 vs `KSSJMS(星期X)`：不一致只 warning，**不丢行**。

    §4.1 的 22 行真样本逐行核对过，两处**全部一致**；这条是防御性的（两个独立字段，
    谁先腐化不可知）。取舍方向：以 `KSRQ` 为准（客户端显示的就是它推出来的那天），
    丢掉整行等于把一次真实考试信息抹掉，比一条噪音严重得多。
    """
    match = _WEEKDAY_IN_TEXT.search(time_text)
    if match is None:
        return
    try:
        actual = date.fromisoformat(day)
    except ValueError:
        return  # 日期本身有问题，交给下游 build_exam_events 的跳过分支
    claimed = _WEEKDAY_CHARS.index(match.group(1).replace("天", "日"))
    if claimed != actual.isoweekday() - 1:
        rep.warn(
            f"考试「{name}」的时间文本写的是星期{match.group(1)}，"
            f"但考试日期 {day} 是星期{actual.isoweekday()}；按日期为准"
        )
```

> 注意 `date_str` 持有 ISO 日期字符串而不是 `date`，与 `ExamSchedule` 的 `start_time/end_time` 一致，交给 `schedules.combine` 统一转换——避免 `date.fromisoformat` 与 `academic_calendar._parse_date` 对 `"YYYY-MM-DD 00:00:00"` 直接抛 `ValueError`。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exams.py -q`
Expected: PASS

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/exams.py tests/test_exams.py
git commit -m "feat(exams): parse exam rows with campus mapping"
```

---

## Task 4: `classify_exam_payload` —— 三态判定

**Files:**
- Modify: `src/xjtu_calendar/exams.py`
- Test: `tests/test_exams.py`

**Interfaces:**
- Consumes: `iter_exam_rows`（T3）
- Produces: `exams.ExamState`（`Enum`：`HAS_EXAMS` / `NO_EXAMS` / `UNKNOWN`）、`exams.ExamOutcome`（frozen dataclass：`state`、`rows`、`msg`、`code`）、`exams.classify_exam_payload(payload) -> ExamOutcome`

- [x] **Step 1: 写失败测试**（判据逐条对应 §7 表）

```python
from exam_support import exam_payload, exam_row

from xjtu_calendar.exams import ExamState, classify_exam_payload


def test_state_has_exams():
    out = classify_exam_payload(exam_payload([exam_row()]))
    assert out.state is ExamState.HAS_EXAMS
    assert len(out.rows) == 1


def test_state_no_exams_is_distinct_from_unknown():
    """code==1 + 空 rows：真实存在但主接口尚未观测到，判据得留得住。"""
    out = classify_exam_payload(exam_payload([], code=1, msg="操作成功"))
    assert out.state is ExamState.NO_EXAMS
    assert out.rows == ()  # rows 是 tuple，不是 list


def test_state_unknown_when_query_failed():
    """实测：本学期未排考 → code 0 / 查询失败。归入未知，**不许**覆盖快照。"""
    out = classify_exam_payload(exam_payload([], code=0, msg="查询失败"))
    assert out.state is ExamState.UNKNOWN
    assert out.msg == "查询失败"


def test_state_unknown_when_envelope_missing_or_string_code():
    assert classify_exam_payload({}).state is ExamState.UNKNOWN
    assert classify_exam_payload({"code": "0"}).state is ExamState.UNKNOWN
    assert classify_exam_payload(exam_payload([exam_row()], code="1")).state is ExamState.UNKNOWN


def test_state_unknown_when_module_missing():
    assert classify_exam_payload({"code": "0", "datas": {}}).state is ExamState.UNKNOWN
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exams.py -q -k state`
Expected: FAIL — `ImportError: cannot import name 'classify_exam_payload'`

- [x] **Step 3: 写最小实现**

```python
from dataclasses import dataclass
from enum import Enum


class ExamState(Enum):
    """§7 三态。`UNKNOWN` 的判据是"能不能确定地写出结论"，不是"看起来像不像空"。"""

    HAS_EXAMS = "has_exams"
    NO_EXAMS = "no_exams"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ExamOutcome:
    state: ExamState
    rows: tuple[Mapping[str, Any], ...] = ()
    msg: str = ""
    code: Any = None


def _exam_module(payload: Any) -> Mapping[str, Any] | None:
    """返回含 `rows` 与 `extParams` 的那个 module（模块名不硬编码）。"""
    datas = payload.get("datas") if isinstance(payload, Mapping) else None
    if not isinstance(datas, Mapping):
        return None
    for module in datas.values():
        if isinstance(module, Mapping) and isinstance(module.get("rows"), list):
            if isinstance(module.get("extParams"), Mapping):
                return module
    return None


def classify_exam_payload(payload: Any) -> ExamOutcome:
    """判据**只**看 `datas.<模块>.extParams`；外层 `code` 恒为字符串 ``"0"``，不作依据。

    网络层失败（非 200 / 401 / 登录页 / 非 JSON）在 `fetch_via_http` 里就已经抛异常，
    走不到这里 —— 所以本函数返回 `UNKNOWN` 只代表"响应到了但内容不可判定"。
    """
    module = _exam_module(payload)
    if module is None:
        return ExamOutcome(ExamState.UNKNOWN, msg="响应缺少 datas.<module>.extParams")
    ext = module["extParams"]
    code, msg = ext.get("code"), str(ext.get("msg") or "")
    rows = tuple(row for row in module["rows"] if isinstance(row, Mapping))
    if code == 1 and rows:
        return ExamOutcome(ExamState.HAS_EXAMS, rows, msg, code)
    if code == 1:
        return ExamOutcome(ExamState.NO_EXAMS, (), msg, code)
    return ExamOutcome(ExamState.UNKNOWN, (), msg, code)
```

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exams.py -q -k state`
Expected: PASS（5 passed）

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/exams.py tests/test_exams.py
git commit -m "feat(exams): classify exam payload into the three states"
```

---

## Task 5: `make_exam_uid` + `build_exam_events`（含红线）

**Files:**
- Modify: `src/xjtu_calendar/exams.py`
- Test: `tests/test_exams.py`

**Interfaces:**
- Consumes: `models.CalendarEvent`；`schedules.combine(day: date, hhmm: str) -> datetime`；`exporter.UID_DOMAIN`；`date.fromisoformat`
- Produces: `exams.make_exam_uid(semester_key: str, exam: ExamSchedule) -> str`；`exams.build_exam_events(exams: Sequence[ExamSchedule], semester_key: str) -> list[CalendarEvent]`

- [x] **Step 1: 写失败测试**（红线断言写在构造结果上，不是写在文档里）

```python
from datetime import timedelta

from exam_support import DEMO_DAY, exam_payload, exam_row

from xjtu_calendar.exams import build_exam_events, make_exam_uid, parse_exam_rows
from xjtu_calendar.exporter import render_ics
from xjtu_calendar.parser import ParseReport


def _exams(*rows):
    return parse_exam_rows(exam_payload(list(rows)), report=ParseReport())


def test_uid_prefers_wid_and_survives_time_or_room_change():
    a = _exams(exam_row())[0]
    moved = _exams(exam_row(KSSJMS=f"{DEMO_DAY} 09:00-11:00(星期一)", JASMC="B-2002"))[0]
    assert make_exam_uid("2026-2027-1", a) == make_exam_uid("2026-2027-1", moved)
    assert make_exam_uid("2026-2027-1", a) != make_exam_uid("2025-2026-2", a)


def test_uid_falls_back_to_task_id_then_composite():
    by_task = _exams(exam_row(WID="", KSRWID="KSRWID-9"))[0]
    assert make_exam_uid("2026-2027-1", by_task).endswith("@xjtu-timetable-calendar")

    # 实测过的形态：同一门课同一天两场（上午 + 晚场）
    same_day_two_sessions = [
        exam_row(
            WID="", KSRWID="", KSRQ="2030-01-05 00:00:00", KSSJMS="2030-01-05 09:00-11:30(星期六)"
        ),
        exam_row(
            WID="", KSRWID="", KSRQ="2030-01-05 00:00:00", KSSJMS="2030-01-05 19:00-21:30(星期六)"
        ),
    ]
    uids = {make_exam_uid("2026-2027-1", e) for e in _exams(*same_day_two_sessions)}
    assert len(uids) == 2  # 降级键含起止时刻，同日两场不撞车


def test_exam_events_are_datetime_never_value_date():
    """红线：VALUE=DATE 会被 sequence.parse_baseline 静默丢弃 → SEQUENCE 永远归零。"""
    event = build_exam_events(_exams(exam_row()), "2026-2027-1")[0]
    assert event.start.utcoffset() == timedelta(hours=8)
    assert event.start.tzinfo is not None
    assert event.meeting is None  # 考试不冒充课程会议


def test_rendered_ics_uses_datetime_for_exams():
    text = render_ics(build_exam_events(_exams(exam_row()), "2026-2027-1"))
    assert "DTSTART;TZID=Asia/Shanghai:" in text
    assert "DTSTART;VALUE=DATE:" not in text
    assert "BEGIN:VALARM" not in text  # D3：不写提醒
    assert "RRULE:" not in text  # 单场事件，绝不周期化


def test_exam_event_summary_location_description_and_fields():
    exam = _exams(exam_row(ZJJSXM="教师甲", ZWH="NN", XF="3.0"))[0]
    event = build_exam_events([exam], "2026-2027-1")[0]
    assert event.summary == "示例课程甲（结课考试）"
    assert event.location == "A-1001"  # 没给校区对照表 -> 只有 JASMC，不硬编码校区
    assert "座位号：NN" in event.description
    assert "教师甲" in event.description
    assert "3.0" in event.description


def test_unparsable_exam_never_becomes_a_zero_oclock_event():
    assert build_exam_events(_exams(exam_row(KSSJMS="待定")), "2026-2027-1") == []
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exams.py -q -k "uid or exam_event or zero_oclock"`
Expected: FAIL — `ImportError: cannot import name 'build_exam_events'`

- [x] **Step 3: 写最小实现**

```python
import hashlib
import logging
from collections.abc import Sequence
from datetime import date

from .schedules import combine

logger = logging.getLogger(__name__)


def _uid_token(exam: ExamSchedule) -> str:
    if exam.row_id:
        return f"WID={exam.row_id}"
    if exam.task_id:
        logger.info("考试「%s」缺少 WID，UID 降级到 KSRWID", exam.course_name)
        return f"KSRWID={exam.task_id}"
    logger.warning(
        "考试「%s」缺少 WID 与 KSRWID，UID 降级到内容组合（改期会被视为新事件）",
        exam.course_name,
    )
    # 必须含 KSSJMS：实测同一门课同一天有两场（上午 + 晚场），只到日期粒度会撞车
    return "|".join(
        [exam.course_id or "", exam.exam_code or "", exam.date_str, exam.start_time, exam.end_time]
    )


def make_exam_uid(semester_key: str, exam: ExamSchedule) -> str:
    """``sha256("<学期>|<考试身份>")[:32]@xjtu-timetable-calendar``。

    与课程 UID 同一命名空间但不同配方：**刻意不含时间与教室**（换考场应当原地更新），
    也**绝不含年级/学号**。
    """
    payload = f"{semester_key.strip()}|{_uid_token(exam)}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{digest}@{UID_DOMAIN}"


def _exam_type_suffix(exam_name: str | None) -> str:
    """从 ``KSMC`` 取类型词。实测见过 期中/结课/期末考试；**不做枚举白名单**，
    将来出现「补考」「缓考」时原样带出（设计文档 §11）。"""
    if not exam_name:
        return "考试"
    tail = exam_name.rsplit("学期", 1)[-1].strip()
    return tail or "考试"


def build_exam_events(exams: Sequence[ExamSchedule], semester_key: str) -> list[CalendarEvent]:
    """考试 → 单场、绝对时刻、无 RRULE / 无 VALARM 的 VEVENT。"""
    events: list[CalendarEvent] = []
    for exam in exams:
        try:
            day = date.fromisoformat(exam.date_str)
            start = combine(day, exam.start_time)
            end = combine(day, exam.end_time)
        except ValueError as exc:
            logger.warning("考试「%s」日期无法解析（%s），已跳过", exam.course_name, exc)
            continue
        location = " ".join(part for part in (exam.campus, exam.location) if part) or None
        description_lines = [
            exam.exam_name,
            f"课程号：{exam.course_id}" if exam.course_id else None,
            f"座位号：{exam.seat}" if exam.seat else None,
            f"学分：{exam.credits}" if exam.credits is not None else None,
            f"主考教师：{exam.teacher}" if exam.teacher else None,
        ]
        events.append(
            CalendarEvent(
                uid=make_exam_uid(semester_key, exam),
                summary=f"{exam.course_name}（{_exam_type_suffix(exam.exam_name)}）",
                start=start,
                end=end,
                location=location,
                description="\n".join(line for line in description_lines if line),
                meeting=None,
            )
        )
    return events
```

（`CalendarEvent` 的导入加到文件顶部：`from .models import CalendarEvent, ExamSchedule`。）

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_exams.py -q`
Expected: PASS

- [x] **Step 5: 加一条"考试事件进得了 SEQUENCE 基线"的端到端断言**

Step 1 的 `test_rendered_ics_uses_datetime_for_exams` 只看渲染文本；红线真正的后果是
`sequence.parse_baseline`（`sequence.py:139-141`）会不会收下这条事件。直接把它喂给
`parse_baseline`，这才是"客户端能否更新这条考试"的判据：

```python
def test_exam_event_survives_baseline_roundtrip():
    """parse_baseline 会静默丢弃非 datetime 事件；被丢弃 = 每次重发布 SEQUENCE 恒为 0。"""
    from xjtu_calendar.sequence import parse_baseline

    text = render_ics(build_exam_events(_exams(exam_row()), "2026-2027-1"))
    baselines = parse_baseline(text)
    exam_uid = make_exam_uid("2026-2027-1", _exams(exam_row())[0])
    assert exam_uid in baselines  # 进不了基线的事件，客户端永远不会收到更新
```

Run: `python -m pytest tests/test_exams.py -q -k baseline_roundtrip`
Expected: PASS（若失败，说明 icalendar 把 tz-aware datetime 降级成了 date，必须回到 `combine`）

- [x] **Step 6: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/exams.py tests/test_exams.py
git commit -m "feat(exams): build DATE-TIME exam events with WID-based UIDs"
```

---

## Task 6: 快照路径与 `kind=` 落盘（含 `private=True`）

**Files:**
- Modify: `src/xjtu_calendar/config.py:146-151`
- Modify: `src/xjtu_calendar/fetcher.py:557-590`
- Test: `tests/test_config.py`、`tests/test_fetcher.py`（就近补）

**Interfaces:**
- Consumes: `fileutil.atomic_write_text(path, text, *, private=False)`
- Produces: `Settings.raw_exams_path(semester_key) -> Path`、`Settings.raw_exams_prev_path(semester_key) -> Path`、`save_raw(payload, cfg, semester_key, *, kind="timetable") -> Path`、`load_raw(cfg, semester_key, *, kind="timetable") -> Any`

- [x] **Step 1: 写失败测试**

```python
def test_raw_exams_paths_live_beside_timetable_snapshots(tmp_path):
    cfg = Settings(home=tmp_path)
    assert cfg.raw_exams_path("2026-2027-1").name == "exams-2026-2027-1.json"
    assert cfg.raw_exams_prev_path("2026-2027-1").name == "exams-2026-2027-1.prev.json"
    assert cfg.raw_exams_path("2026-2027-1").parent == cfg.raw_dir


def test_save_raw_kind_rotates_only_its_own_stream(tmp_path):
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    save_raw({"a": 1}, cfg, "2026-2027-1")
    save_raw({"exams": "v1"}, cfg, "2026-2027-1", kind="exams")
    save_raw({"exams": "v2"}, cfg, "2026-2027-1", kind="exams")
    assert cfg.raw_timetable_prev_path("2026-2027-1").exists() is False  # 课表只写过一次
    assert cfg.raw_exams_prev_path("2026-2027-1").read_text(encoding="utf-8").find("v1") >= 0
    assert load_raw(cfg, "2026-2027-1", kind="exams") == {"exams": "v2"}


def test_exam_snapshot_is_owner_only(tmp_path):
    """§8：现状 save_raw 没传 private=True，考试快照含学号姓名，必须修。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    path = save_raw({"exams": 1}, cfg, "2026-2027-1", kind="exams")
    mode = path.stat().st_mode & 0o077
    if os.name != "posix":
        pytest.skip("Windows 不执行 POSIX 权限位")
    assert mode == 0


def test_load_raw_missing_exams_snapshot_hint(tmp_path):
    cfg = Settings(home=tmp_path)
    with pytest.raises(TimetableFetchError, match="fetch"):
        load_raw(cfg, "2026-2027-1", kind="exams")
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_config.py tests/test_fetcher.py -q -k "exams or kind or owner_only"`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'raw_exams_path'`

- [x] **Step 3: 写最小实现**

`config.py`（紧挨现有 `raw_timetable_prev_path`）：

```python
def raw_exams_path(self, semester_key: str) -> Path:
    """考试安排原始快照。含个人信息，只应留在 home 下（已 gitignore）。"""
    return self.raw_dir / f"exams-{semester_key}.json"


def raw_exams_prev_path(self, semester_key: str) -> Path:
    """`diff` 的考试比较基线（单代轮转，同课表口径）。"""
    return self.raw_dir / f"exams-{semester_key}.prev.json"
```

`fetcher.py`：

```python
def _raw_paths(cfg: Settings, semester_key: str, kind: str) -> tuple[Path, Path]:
    if kind == "exams":
        return cfg.raw_exams_path(semester_key), cfg.raw_exams_prev_path(semester_key)
    if kind != "timetable":
        raise ValueError(f"未知的快照类型：{kind}")
    return cfg.raw_timetable_path(semester_key), cfg.raw_timetable_prev_path(semester_key)


def save_raw(payload: Any, cfg: Settings, semester_key: str, *, kind: str = "timetable") -> Path:
    """把原始 JSON 缓存到本地，并把上一份轮转为 ``*.prev.json``。

    ``kind`` 决定落在课表还是考试路径；单代轮转语义两者一致。
    两个文件都含个人信息，**必须留在 .gitignore 排除目录内**，绝不可提交。
    """
    cfg.ensure_dirs()
    path, previous = _raw_paths(cfg, semester_key, kind)
    if path.is_file():
        path.replace(previous)  # 同目录 os.replace，原子
    atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n", private=True)
    label = "考试" if kind == "exams" else "课表"
    logger.debug("原始%s已缓存到 %s（含个人信息，请勿提交）", label, path)
    if previous.is_file():
        logger.debug("原始%s上一份快照已轮转到 %s", label, previous)
    return path


def load_raw(cfg: Settings, semester_key: str, *, kind: str = "timetable") -> Any:
    path, _ = _raw_paths(cfg, semester_key, kind)
    if not path.is_file():
        raise TimetableFetchError(
            f"本地没有 {semester_key} 的原始{'考试' if kind == 'exams' else '课表'}缓存：{path}",
            hint="请先运行 fetch 子命令获取数据。",
        )
    return json.loads(path.read_text(encoding="utf-8"))
```

> `kind=` 必须是**关键字参数且带默认值**，否则 `test_fetcher.py:271-289` 的逐字位置断言会破。`private=True` 对课表快照同样生效（同含个人信息），属预期收紧，若有测试断言旧权限需一并修正。

- [x] **Step 4: 跑测试确认通过（含既有快照轮转用例不破）**

Run: `python -m pytest tests/test_fetcher.py tests/test_config.py -q`
Expected: PASS

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/config.py src/xjtu_calendar/fetcher.py tests/test_fetcher.py tests/test_config.py
git commit -m "feat(fetcher): kind-aware raw snapshots with private writes"
```

---

## Task 7: 端点注册 + `cmd_fetch` 抓考试

**Files:**
- Modify: `src/xjtu_calendar/data/ehall_endpoints.json`
- Modify: `src/xjtu_calendar/cli.py:261-335`（HTTP 分支）与 `:87-95`（`fetch` 解析器）
- Test: `tests/test_exams_fetch.py`

**Interfaces:**
- Consumes: `fetch_via_http(endpoint, cfg=..., params=...)`、`classify_exam_payload`（T4）、`save_raw(..., kind="exams")`（T6）、`errors.EndpointNotConfigured`、`errors.AuthenticationExpired`
- Produces: 端点 `exam_schedule`；CLI 旗标 `fetch --no-exams`；行为矩阵（下表）

| 分支 | 是否抓考试 | 说明 |
|---|---|---|
| HTTP（默认） | 是 | `params={"XNXQDM": semester_code}`，与课表同一学期代码 |
| `--from-file` | 否 | 无会话；日志 info 说明"离线导入不含考试" |
| 浏览器拦截 | 否 | `fetch_via_browser` 的 `hint_keywords` 不含 `wdksap`，捕获不到 |
| `--no-exams` | 否 | 三条分支一律跳过；本地快照不动 |

- [x] **Step 1: 加端点定义**（`endpoints` 数组末尾）

```json
{
  "name": "exam_schedule",
  "method": "POST",
  "path": "/jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do",
  "description": "我的考试安排。form 参数 XNXQDM=<学期代码>；响应 datas.wdksap.rows[]（KCM/KCH/KSMC/KSRQ/KSSJMS/JASMC/ZWH/XXXQDM/WID/KSDM/KSRWID），成功标志在 extParams.code==1",
  "required": false
}
```

并在 `_readme` 数组里追加一行：`"exam_schedule 路径来自 2026-10-08 真实观测（studentWdksapApp）。"`

- [x] **Step 2: 写失败测试**

打桩点必须选 **`xjtu_calendar.fetcher` 模块上的 `fetch_via_http`**：`cmd_fetch` 是在函数体
内 `from .fetcher import fetch_via_http`（`cli.py:248-254`），每次调用都重新从模块取属性，
所以 `monkeypatch.setattr(cli, "fetch_via_http", ...)` **打不上**（`cli` 根本没有这个模块级
名字）。既有 `tests/test_cli_endpoints.py::no_endpoints` 打的正是 `fetcher.load_endpoints`，
同一口径。`transport=` 那层留给 http 层的测试用，CLI 级不必再套 httpx。

```python
"""`_fetch_exams` 的落盘行为（设计文档 §6.2 / §7）。

核心断言只有一条：**状态未知时既有快照的字节不许变**。
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from exam_support import SEMESTER, captured_logs, exam_payload, exam_row

from xjtu_calendar import fetcher
from xjtu_calendar.cli import _fetch_exams, main
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import AuthenticationExpired


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    return cfg


def _endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="exam_schedule",
        method="POST",
        path="/jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do",
        description="我的考试安排",
    )


def _endpoints(**over: object) -> dict[str, fetcher.Endpoint]:
    table: dict[str, fetcher.Endpoint] = {"exam_schedule": _endpoint()}
    table.update(over)  # 传 exam_schedule=None 再由调用方 pop 可模拟缺端点
    return table


def _write_old_snapshot(cfg: Settings) -> str:
    """预置一份"上次抓到的"快照，用来验证未知态不会覆盖它。"""
    payload = exam_payload([exam_row(KCM="旧课程")])
    cfg.raw_exams_path(SEMESTER).write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return cfg.raw_exams_path(SEMESTER).read_text(encoding="utf-8")


def _patch(monkeypatch: pytest.MonkeyPatch, impl) -> None:
    monkeypatch.setattr(fetcher, "fetch_via_http", impl)


def test_fetch_exams_saves_snapshot_when_scheduled(home: Settings, monkeypatch) -> None:
    calls = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        calls.append((endpoint.name, params))
        return exam_payload([exam_row()])

    _patch(monkeypatch, fake)
    path = _fetch_exams(_endpoints(), home, SEMESTER)

    assert path == home.raw_exams_path(SEMESTER)
    assert path is not None and path.is_file()
    assert calls == [("exam_schedule", {"XNXQDM": SEMESTER})]  # 参数由调用方传，不写进端点表


@pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 POSIX 权限位语义")
def test_exam_snapshot_is_owner_only(home: Settings, monkeypatch) -> None:
    """§8：考试快照落盘必须 0600。权限位断言按 test_fileutil.py:69 的同款 skipif 走。"""
    _patch(monkeypatch, lambda *a, **k: exam_payload([exam_row()]))
    path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_state_keeps_existing_snapshot_byte_for_byte(home, monkeypatch) -> None:
    before = _write_old_snapshot(home)
    _patch(monkeypatch, lambda *a, **k: exam_payload([], code=0, msg="查询失败"))  # 实测未排考形态
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    assert home.raw_exams_path(SEMESTER).read_text(encoding="utf-8") == before
    assert any("不覆盖" in rec.getMessage() for rec in records)


def test_expired_session_is_unknown_not_empty(home: Settings, monkeypatch) -> None:
    before = _write_old_snapshot(home)

    def boom(*a, **k):
        raise AuthenticationExpired("会话已过期")

    _patch(monkeypatch, boom)
    assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    assert home.raw_exams_path(SEMESTER).read_text(encoding="utf-8") == before


def test_confirmed_empty_writes_empty_snapshot(home: Settings, monkeypatch) -> None:
    """只有 code==1 + 空 rows（确认无考试）才允许把旧快照顶掉。"""
    _write_old_snapshot(home)
    _patch(monkeypatch, lambda *a, **k: exam_payload([], code=1, msg="操作成功"))
    path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["datas"]["wdksap"]["rows"] == []


def test_missing_endpoint_degrades_without_raising(home: Settings) -> None:
    with captured_logs() as records:
        assert _fetch_exams({}, home, SEMESTER) is None  # 没有 exam_schedule 键
    assert any("跳过考试安排" in rec.getMessage() for rec in records)
    assert not home.raw_exams_path(SEMESTER).exists()


def test_cmd_fetch_still_returns_zero_when_exams_explode(home: Settings, monkeypatch) -> None:
    """课表是主功能：考试侧任何异常都不能让 cmd_fetch 非零退出（§7 末条）。"""
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    monkeypatch.setattr(fetcher, "fetch_via_http", _timetable_then_boom)

    assert main(["fetch", "--semester", SEMESTER, "--source", "http"]) == 0
    assert not home.raw_exams_path(SEMESTER).exists()  # 考试失败没落任何半个快照


def _timetable_endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="timetable", method="POST", path="/jwapp/sys/xskcb/xskcb.do", description="我的课表"
    )


def _timetable_then_boom(endpoint, *, cfg=None, params=None, transport=None):
    """按端点分流：课表正常返回一份 `kbList`，考试直接抛未预期异常。"""
    if endpoint.name == "exam_schedule":
        raise RuntimeError("考试接口炸了")
    return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}
```

> `test_cmd_fetch_still_returns_zero_when_exams_explode` 里 `_fetch_exams` 只捕获
> `AuthenticationExpired` / `TimetableFetchError`，`RuntimeError` 会逃逸——所以实现必须
> 在 `cmd_fetch` 的调用点再包一层 `except Exception`（记 warning 后继续），这正是
> 「宁缺毋滥 + 绝不因为考试而非零退出」两条的交汇处。**不要**把 `_fetch_exams` 改成
> 裸 `except Exception`：那会把 `KeyboardInterrupt`/编程错误一起吞掉，让课表侧的 bug
> 变得不可见。两层各司其职：内层管"可预期的业务失败"，外层管"不可预期的一律降级"。

- [x] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_exams_fetch.py -q`
Expected: FAIL（`raw_exams_path` 文件不存在）

- [x] **Step 4: 写最小实现**（`fetch` 解析器加旗标 + HTTP 分支加一次多发）

```python
fetch.add_argument(
    "--no-exams",
    action="store_true",
    help="不抓考试安排（默认抓；抓失败不影响课表导出）",
)
```

`cmd_fetch` 的 HTTP 分支，在课表 `save_raw` 之后：

```python
def _exam_fallback_note(cfg, semester_code):
    """降级提示的后半句（Task 11 评审 F1 更正后的 shipped 口径）：
    有旧快照就报出**它是哪一天的**，没有就明说不含考试——**禁止**不查存在性
    就写「沿用已有快照」。"""
    path = cfg.raw_exams_path(semester_code)
    if not path.is_file():
        return "本地没有考试快照，本次导出不含考试"
    try:
        day = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except OSError:  # 快照刚被移走：宁可不报日期，也不谎称"没有快照"
        return "本地已有考试快照，本次导出继续沿用它"
    return f"不覆盖已有快照，沿用 {day} 的考试快照"


def _fetch_exams(endpoints, cfg, semester_code, *, reason_if_skipped=None):
    """宁缺毋滥：任何失败都只记日志，**绝不覆盖**已有快照，绝不非零退出。"""
    if reason_if_skipped:
        logger.info("%s，本次不抓考试安排", reason_if_skipped)
        return None
    try:
        endpoint = endpoints["exam_schedule"]
    except KeyError:
        logger.warning("未配置 exam_schedule 端点，跳过考试安排（课表不受影响）")
        return None
    try:
        payload = fetch_via_http(endpoint, cfg=cfg, params={"XNXQDM": semester_code})
    except (AuthenticationExpired, TimetableFetchError) as exc:
        logger.warning("考试安排获取失败（%s）；%s", exc, _exam_fallback_note(cfg, semester_code))
        return None
    outcome = classify_exam_payload(payload)
    if outcome.state is ExamState.UNKNOWN:
        logger.warning(
            "考试安排响应无法判定（extParams.code=%r msg=%r）；%s",
            outcome.code,
            outcome.msg,
            _exam_fallback_note(cfg, semester_code),
        )
        return None
    if outcome.state is ExamState.NO_EXAMS:
        logger.info("本学期暂无考试安排（接口确认：空）")
    path = save_raw(payload, cfg, semester_code, kind="exams")
    logger.info("考试安排已缓存到 %s", path)
    return path
```

> **本块已按 shipped 口径更正**（原稿的两条 warning 文案「考试安排获取失败，沿用已有
> 快照」「不覆盖已有快照」不带快照日期、也不区分"有没有旧快照"，Task 11 评审 F1 已修）。
> 落地版另有两处与此草图不同、以 `cli.py` 为准：缺端点走 `require_endpoint` +
> 捕获 `EndpointNotConfigured`（而非 `endpoints[...]` + `KeyError`），落盘前有
> `totalSize` 翻页护栏（§6.4，行数不符只 warning、照常保存）。

`--from-file` 与浏览器两条分支调用它时传 `reason_if_skipped="--from-file 导入没有会话，考试需在线获取"` / `"浏览器拦截只覆盖课表接口"`；HTTP 分支正常调用。**`required: false` 没有运行期效果**（`Endpoint.required` 无读取点），所以"缺端点不影响课表"靠的是上面这个 `try/KeyError`，不是那个字段。

- [x] **Step 5: 跑测试确认通过 + 手工确认异常类名**

Run: `python -m pytest tests/test_exams_fetch.py -q && python -c "from xjtu_calendar.errors import AuthenticationExpired, EndpointNotConfigured, TimetableFetchError"`
Expected: PASS / 无输出

- [x] **Step 6: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/data/ehall_endpoints.json src/xjtu_calendar/cli.py tests/test_exams_fetch.py
git commit -m "feat(fetch): fetch exam schedule over HTTP with three-state safety"
```

---

## Task 8: `exporter` 合并考试事件

**Files:**
- Modify: `src/xjtu_calendar/exporter.py:547-720`
- Test: `tests/test_exporter_exams.py`

**Interfaces:**
- Consumes: `exams.parse_exam_rows` / `build_exam_events`（T3/T5，**在函数体内 import**，避免与 `exams` 的顶层 `from .exporter import UID_DOMAIN` 成环）；`load_raw(cfg, semester, kind="exams")`（T6）；`campus_names_from_timetable`
- Produces: `build_ics_for_semester(..., include_exams: bool = True, exams_payload: dict | None = None)`；`ExportResult.info["exam_events"]`（int）；`ExportResult.info["events"]` 与 `date_range` **仍只统计课程事件**

- [x] **Step 1: 写失败测试**

```python
"""考试事件并进同一份 .ics（设计文档 §6.5）。"""

from __future__ import annotations

import json
import re
from pathlib import Path

from exam_support import (
    DEMO_DAY,
    SEMESTER,
    captured_logs,
    exam_payload,
    exam_row,
    timetable_envelope,
)
from subscribe_support import make_home, payload_row

from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.fetcher import save_raw


def _home_with_exams(tmp_path: Path, *rows: dict[str, str]):
    cfg = make_home(tmp_path, semester=SEMESTER)
    save_raw(exam_payload(list(rows) or [exam_row()]), cfg, SEMESTER, kind="exams")
    return cfg


def test_exams_merge_into_the_same_calendar(tmp_path):
    cfg = _home_with_exams(tmp_path)
    without = build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    with_exams = build_ics_for_semester(cfg, SEMESTER)

    assert with_exams.info["exam_events"] == 1
    # 课程口径一个字不变：Events / date_range 都不许被考试拉长
    assert with_exams.info["events"] == without.info["events"]
    assert with_exams.info["date_range"] == without.info["date_range"]
    assert "（结课考试）" in with_exams.ics
    assert "（结课考试）" not in without.ics


def test_exam_events_do_not_collide_with_course_uids(tmp_path):
    cfg = _home_with_exams(tmp_path)
    ics = build_ics_for_semester(cfg, SEMESTER).ics
    uids = re.findall(r"^UID:(.+)$", ics, re.MULTILINE)
    assert len(uids) == len(set(uids))  # 全局唯一，含课程与考试


def test_duplicate_exam_uid_is_dropped_with_warning(tmp_path):
    """同一 WID 出现两行（脏数据）时，后来者被丢弃而不是写出重复 UID。"""
    cfg = _home_with_exams(tmp_path, exam_row(WID="DUP"), exam_row(WID="DUP", KCM="示例课程乙"))
    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER)
    assert result.info["exam_events"] == 1
    assert any("UID" in rec.getMessage() for rec in records)


def test_missing_exam_snapshot_is_silent(tmp_path):
    cfg = make_home(tmp_path, semester=SEMESTER)
    result = build_ics_for_semester(cfg, SEMESTER)
    assert result.info["exam_events"] == 0  # 没抓过考试不是错误


def test_no_exams_flag_keeps_snapshot_on_disk(tmp_path):
    cfg = _home_with_exams(tmp_path)
    build_ics_for_semester(cfg, SEMESTER, include_exams=False)
    assert cfg.raw_exams_path(SEMESTER).is_file()  # 开关只影响导出，不删数据


def test_from_date_filter_also_cuts_exams(tmp_path):
    """v1 决定：日期范围一并裁剪考试（README 要写清）。"""
    cfg = _home_with_exams(
        tmp_path, exam_row(KSRQ="2099-01-04 00:00:00", KSSJMS="2099-01-04 09:00-11:00(星期一)")
    )
    result = build_ics_for_semester(cfg, SEMESTER, from_date="2098-01-01", to_date="2098-12-31")
    assert result.info["exam_events"] == 0


def test_sequence_stats_see_exam_events(tmp_path):
    """sequence_stats 必须吃合并后的全集，否则考试永远不计入 added。"""
    cfg = _home_with_exams(tmp_path)
    result = build_ics_for_semester(cfg, SEMESTER)
    assert result.sequence_stats is not None
    assert result.sequence_stats["added"] >= 1


def test_campus_name_comes_from_the_timetable_snapshot(tmp_path):
    """LOCATION 的校区名前缀来自**同学期课表快照**的 XXXQDM -> XXXQDM_DISPLAY 对照。

    考试行只有 `XXXQDM='5'`，没有 DISPLAY 字段（§6.3）。`make_home` 写的是
    `{'kbList': rows}` 简写信封，`campus_names_from_timetable` 认不出来，所以这条
    用例必须把课表快照换成真信封（设计文档 §9 点名的固件坑）。
    """
    cfg = _home_with_exams(tmp_path, exam_row(XXXQDM="5"))
    row = {**payload_row(), "XXXQDM": "5", "XXXQDM_DISPLAY": "创新港校区"}
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps(timetable_envelope([row]), ensure_ascii=False), encoding="utf-8"
    )
    ics = build_ics_for_semester(cfg, SEMESTER).ics
    assert "LOCATION:创新港校区 A-1001" in ics


def test_exam_only_in_range_does_not_warn_about_no_events(tmp_path):
    """日期窗口只框住考试那天（课程全在 2026 秋季）：`events` 为空但产物不是空的。

    钉的是「原来那句 `if not events:` 必须改成 `if not render_events:`」——
    否则每次只导出考试时段都会甩一条"没有生成任何事件"的假警告。
    """
    cfg = _home_with_exams(tmp_path)
    with captured_logs() as records:
        result = build_ics_for_semester(cfg, SEMESTER, from_date=DEMO_DAY, to_date=DEMO_DAY)
    assert result.info["events"] == 0  # 课程口径：窗口里确实一次课都没有
    assert result.info["exam_events"] == 1
    assert not any("没有生成任何事件" in rec.getMessage() for rec in records)
```

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_exporter_exams.py -q`
Expected: FAIL — `TypeError: build_ics_for_semester() got an unexpected keyword argument 'include_exams'`

- [x] **Step 3: 写最小实现**

改动全在 `build_ics_for_semester` 内部，**顺序很关键**：考试并进「日期过滤之前」，
但 `summarize` 与 `date_range` 只吃课程事件，`sequence_stats` 吃合并后的全集。

顺带补两个 `exporter.py` 现在**没有**的顶层导入，别撞在 NameError 上：
`from .errors import (... TimetableFetchError ...)`（现只导 `CalendarExportError`、
`ScheduleNotConfigured`、`SemesterNotConfigured`、`UnsupportedAdjustmentError`、
`XjtuCalendarError`，见 `:28-34`）、`from .parser import ParseReport`
（现只导 `TimetableParser`，见 `:38`）。`load_raw` 本来就在函数体内 import（`:578`），
沿用的写法就是这里加 `kind=` 的原因。

```python
# --- 展开（课程）---
events = build_events(meetings, academic, schedules)
logger.info("已展开：%d 次实际上课", len(events))

# --- 考试事件（并入日期过滤之前；失败一律静默降级）---
exam_events: list[CalendarEvent] = []
if include_exams:
    from .exams import build_exam_events, campus_names_from_timetable, parse_exam_rows

    try:
        exam_payload = (
            exams_payload if exams_payload is not None else load_raw(cfg, semester, kind="exams")
        )
    except TimetableFetchError:
        exam_payload = None  # 从没抓过考试：不是错误
    if exam_payload is not None:
        report = ParseReport()
        parsed = parse_exam_rows(
            exam_payload,
            campus_names=campus_names_from_timetable(payload),
            report=report,
        )
        for line in report.skipped:
            logger.warning("考试安排跳过一条：%s", line)
        for line in report.warnings:  # 星期与日期不一致这类"保留但可疑"的提示
            logger.warning("考试安排提醒：%s", line)
        exam_events = build_exam_events(parsed, semester)

# --- 可选日期过滤（课程与考试用同一组边界）---
lower = date.fromisoformat(from_date) if from_date else None
upper = date.fromisoformat(to_date) if to_date else None
if lower is not None or upper is not None:
    events = [e for e in events if _in_range(e, lower, upper)]
    exam_events = [e for e in exam_events if _in_range(e, lower, upper)]
```

模块级加一个小谓词（与现有内联条件等价，避免两处重复写边界判断）：

```python
def _in_range(event: CalendarEvent, lower: date | None, upper: date | None) -> bool:
    day = event.start.date()
    return (lower is None or day >= lower) and (upper is None or day <= upper)
```

渲染前合并 + 全局 UID 断言（现有 `seen_uids` 只在 `build_events` 内部生效，`:213`，管不到外面并进来的事件；`render_ics` 直接 `add_component`，重复 UID 会原样写进 .ics）：

```python
    kept_exam: list[CalendarEvent] = []
    if exam_events:
        seen = {event.uid for event in events}
        for event in exam_events:
            if event.uid in seen:
                logger.warning("考试事件 UID 与已有事件冲突，已丢弃：%s", event.summary)
                continue
            seen.add(event.uid)
            kept_exam.append(event)

    render_events = [*events, *kept_exam]
    if not render_events:
        logger.warning("没有生成任何事件（可能全部落在停课日期或被日期过滤排除）")
```

下游三处按此调整：`render_ics(render_events, ...)`；`sequence_stats(render_events, baseline)`；
`info` 里 `"events"` 与 `"date_range"` 继续由 `summarize(meetings, events)`（**只有课程事件**）产生，
新增 `"exam_events": len(kept_exam)`。原来那句 `if not events:` 的告警改成 `if not render_events:`，
否则"只有考试、没有课"的假期学期会误报。

- [x] **Step 4: 跑测试确认通过 + 基线不破**

Run: `python -m pytest tests/test_exporter_exams.py tests/test_exporter.py tests/test_sequence.py -q`
Expected: PASS

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/exporter.py tests/test_exporter_exams.py
git commit -m "feat(exporter): merge exam events with a global UID guard"
```

---

## Task 9: CLI 旗标透传（export / push / rotate）

**Files:**
- Modify: `src/xjtu_calendar/cli.py`（`export` 解析器 `:96-120`、`_subscribe_push` `:944-968`、`_subscribe_rotate` `:973-1008`）
- Test: `tests/test_cli_exams.py`

**Interfaces:**
- Consumes: `build_ics_for_semester(..., include_exams=...)`（T8）
- Produces: `export --no-exams`、`subscribe push --no-exams`、`subscribe rotate --no-exams`；`_subscribe_rotate(cfg, state, semester, *, include_exams=True)`（**签名改动**，不是只加旗标）

- [x] **Step 1: 写失败测试**

捕获式假实现只锁 **CLI 接线**（旗标有没有落到 `build_ics_for_semester`）；exporter 侧
`include_exams=False` 的真实产物行为在 T8 的 `tests/test_exporter_exams.py` 已锁死。

```python
"""``--no-exams`` 是否真的透到构建函数。重点锁 rotate：

`_subscribe_rotate` 走同一条 build_ics_for_semester，但现状既不接 --input 也不接旗标，
用户在 push 上明确关掉考试后随手 rotate 一次就会把考试塞回订阅 URL
（设计文档 §6.7：「用户明确关掉的开关被后台重新打开」）。

这里用捕获式假实现**只锁接线**；`include_exams=False` 的真实产物形状在
T8 的 `tests/test_exporter_exams.py` 里已经断过，不重复。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from exam_support import SEMESTER, exam_payload, exam_row
from subscribe_support import make_home

from xjtu_calendar import subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import ExportResult

#: 假的 info 必须凑齐 cmd_export 打印用的键，否则它会 KeyError 而不是走到断言。
STUB_INFO = {
    "semester_name": "示例学期",
    "courses": 1,
    "meetings": 1,
    "events": 1,
    "date_range": "2030-02-25 ~ 2030-06-21",
}


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """校历/作息/课表快照 + 一份有考试的 raw/exams-<学期>.json。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    cfg = make_home(tmp_path, semester=SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.parent.mkdir(parents=True, exist_ok=True)
    exam_path.write_text(
        json.dumps(exam_payload([exam_row()]), ensure_ascii=False), encoding="utf-8"
    )
    return tmp_path


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """拦下 build_ics_for_semester 记录参数；publish 换成 no-op（真 publish 要动 git）。"""
    calls: list[dict[str, Any]] = []

    def fake(_cfg: Settings, semester: str, **kwargs: Any) -> ExportResult:
        calls.append({"semester": semester, **kwargs})
        return ExportResult(
            ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", info=dict(STUB_INFO), sequence_stats=None
        )

    monkeypatch.setattr("xjtu_calendar.cli.build_ics_for_semester", fake)
    monkeypatch.setattr(
        subscribe,
        "publish",
        lambda _cfg, _state, _ics: subscribe.PublishResult(
            outcome=subscribe.PublishOutcome.PUSHED,
            url="https://e.invalid/x.ics",
            content_sha256="0" * 64,
        ),
    )
    return calls


def _register(cfg: Settings) -> None:
    """跳过 `subscribe init`（它要真远端），直接落一份订阅状态。"""
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


def _export_argv(home: Path, extra: str | None = None) -> list[str]:
    argv = ["export", "--semester", SEMESTER, "-o", str(home / "out.ics")]
    return [*argv, *([extra] if extra else [])]


def _subscribe_argv(action: str, extra: str | None = None) -> list[str]:
    argv = ["subscribe", action, "--semester", SEMESTER]
    return [*argv, *([extra] if extra else [])]


def test_export_forwards_include_exams(home: Path, spy: list[dict[str, Any]]) -> None:
    cfg = Settings(home=home)

    assert main(_export_argv(home)) == 0
    assert spy[-1]["include_exams"] is True  # 默认包含（D2）
    assert main(_export_argv(home, "--no-exams")) == 0
    assert spy[-1]["include_exams"] is False
    assert cfg.raw_exams_path(SEMESTER).is_file()  # 关导出不删本地快照


@pytest.mark.parametrize("action", ["push", "rotate"])
def test_subscribe_actions_forward_the_flag(
    home: Path, spy: list[dict[str, Any]], action: str
) -> None:
    cfg = Settings(home=home)
    _register(cfg)
    if action == "rotate":
        # rotate 只在本地有留底时才重新发布（无留底分支直接 return，压根不调 build）
        last = subscribe.subscribe_dir(cfg) / f"last-{SEMESTER}.ics"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text("BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", encoding="utf-8")

    assert main(_subscribe_argv(action, "--no-exams")) == 0
    assert spy[-1]["include_exams"] is False, f"{action} 没把 --no-exams 传到位"
```

实现侧口径：**三个调用点都显式传 `include_exams=not args.no_exams`**（不靠默认值），
所以上面的 `is True` / `is False` 断言是确定的。

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_cli_exams.py -q`
Expected: FAIL — `unrecognized arguments: --no-exams`（argparse 先报错，压根到不了断言）

- [x] **Step 3: 写最小实现**

三个解析器各加同一个旗标（help 文案一致），`cmd_export` 与两个 subscribe 动作把 `include_exams=not args.no_exams` 传下去：

```python
for parser in (export, push, rotate):
    parser.add_argument(
        "--no-exams",
        action="store_true",
        help="不并入考试安排（默认并入；本地快照不删）",
    )
```

`_subscribe_rotate` 现在**不接收 `args`**（`cli.py:973` 的签名是 `(cfg, state, semester)`），按上面的关键字参数改签名并更新调用点。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_cli_exams.py -q`
Expected: PASS

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/cli.py tests/test_cli_exams.py
git commit -m "feat(cli): pass --no-exams through export, push and rotate"
```

---

## Task 10: `diff_exams` 与「考试变更」小节

**Files:**
- Modify: `src/xjtu_calendar/exams.py`
- Modify: `src/xjtu_calendar/cli.py:738-830`（`cmd_diff`，含 `:782-785` 的短路）
- Test: `tests/test_diff_exams.py`

**Interfaces:**
- Consumes: `parse_exam_rows`（T3）、`raw_exams_path` / `raw_exams_prev_path`（T6）
- Produces: `exams.ExamChange`（frozen：`kind`/`course_name`/`field`/`old`/`new`/`when`）、`exams.ExamDiff`（`added`/`removed`/`changes` + `is_empty`）、`exams.diff_exams(old_exams, new_exams) -> ExamDiff`
- **不复用 `diff.SlotChange`**：它强制 `weekday: int` 与 `periods: tuple[int, ...]`，而 `cli.py:810-814` 用 `'一二三四五六日'[change.weekday - 1]` 打印 —— 传 0 会**静默印成「日」**，传越界值直接 `IndexError` 让 `diff` 崩掉。

- [x] **Step 1: 写失败测试**

```python
import json
from pathlib import Path

import pytest
from exam_support import DEMO_DAY, SEMESTER, exam_payload, exam_row
from subscribe_support import payload_row

from xjtu_calendar.cli import main
from xjtu_calendar.exams import diff_exams, parse_exam_rows


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """与 tests/test_cli_diff.py 同一个约定：XJTU_CALENDAR_HOME 指向 tmp_path。"""
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    (tmp_path / "raw").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _exam(**over):
    return parse_exam_rows(exam_payload([exam_row(**over)]))[0]


def test_added_and_removed_pair_by_wid():
    old = [_exam(WID="W1")]
    new = [_exam(WID="W1"), _exam(WID="W2", KCM="示例课程乙")]
    diff = diff_exams(old, new)
    assert [c.course_name for c in diff.added] == ["示例课程乙"]
    assert diff.removed == []


def test_time_room_seat_changes_are_reported():
    old = [_exam(WID="W1", KSSJMS=f"{DEMO_DAY} 15:00-17:30(星期一)", JASMC="A-1001", ZWH="NN")]
    new = [_exam(WID="W1", KSSJMS=f"{DEMO_DAY} 09:00-11:00(星期一)", JASMC="B-2002", ZWH="MM")]
    fields = {c.field for c in diff_exams(old, new).changes}
    assert fields == {"时间", "教室", "座位"}


def test_removed_exam_is_reported_when_row_disappears():
    diff = diff_exams([_exam(WID="W1")], [])
    assert [c.course_name for c in diff.removed] == ["示例课程甲"]


def test_exam_is_empty_tracks_the_three_lists():
    """考试侧 is_empty 语义：三类都空才算空。"""
    assert diff_exams([], []).is_empty is True
    assert diff_exams([_exam(WID="W1")], [_exam(WID="W1", JASMC="B-2002")]).is_empty is False


def test_cli_diff_reports_exam_change_when_courses_are_identical(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """§6.6 短路修复的 e2e：课程逐字相同、只有考试换了教室 -> 必须打印「考试变更」。

    课程走显式 ``--old/--new``（同 test_cli_diff 的约定），考试走 ``--semester``
    下的默认快照路径，两条来源互不干扰。
    """
    course_payload = json.dumps({"kbList": [payload_row()]}, ensure_ascii=False)
    old = tmp_path / "course-old.json"
    new = tmp_path / "course-new.json"
    old.write_text(course_payload, encoding="utf-8")
    new.write_text(course_payload, encoding="utf-8")

    raw = home / "raw"
    (raw / f"exams-{SEMESTER}.prev.json").write_text(
        json.dumps(exam_payload([exam_row(WID="W1", JASMC="A-1001")]), ensure_ascii=False),
        encoding="utf-8",
    )
    (raw / f"exams-{SEMESTER}.json").write_text(
        json.dumps(exam_payload([exam_row(WID="W1", JASMC="B-2002")]), ensure_ascii=False),
        encoding="utf-8",
    )

    assert main(["diff", "--old", str(old), "--new", str(new), "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "考试变更" in out and "B-2002" in out and "无变化" not in out
```

> 写这条 e2e 前先确认课程侧确实为空：`diff_meetings([], [])` 之外，用同一份
> `payload_row()` 两次解析再比对必然 `is_empty`（既有 `test_diff.py` 已锁同快照无变化）。
> 断言里的 `"无变化" not in out` 就是钉住「只改了考试也被当成有变化」。

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_diff_exams.py -q`
Expected: FAIL — `ImportError: cannot import name 'diff_exams'`

- [x] **Step 3: 写最小实现**

```python
@dataclass(frozen=True)
class ExamChange:
    kind: str  # "added" | "removed" | "changed"
    course_name: str
    field: str = ""  # 时间 | 教室 | 座位 | 名称
    old: str = ""
    new: str = ""
    when: str = ""  # 考试日期，仅用于人读


@dataclass(frozen=True)
class ExamDiff:
    added: list[ExamChange] = field(default_factory=list)
    removed: list[ExamChange] = field(default_factory=list)
    changes: list[ExamChange] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changes)


def _exam_key(exam: ExamSchedule) -> str:
    return exam.row_id or exam.task_id or f"{exam.course_id}|{exam.date_str}|{exam.start_time}"


def diff_exams(old_exams: Sequence[ExamSchedule], new_exams: Sequence[ExamSchedule]) -> ExamDiff:
    """按 `WID` 配对（缺失时退到 `KSRWID`/组合键），比对时间、教室、座位。"""
    old_by, new_by = {_exam_key(e): e for e in old_exams}, {_exam_key(e): e for e in new_exams}
    out = ExamDiff()
    for key in sorted(set(new_by) - set(old_by)):
        e = new_by[key]
        out.added.append(ExamChange("added", e.course_name, when=e.date_str))
    for key in sorted(set(old_by) - set(new_by)):
        e = old_by[key]
        out.removed.append(ExamChange("removed", e.course_name, when=e.date_str))
    for key in sorted(set(old_by) & set(new_by)):
        before, after = old_by[key], new_by[key]
        for label, lhs, rhs in (
            (
                "时间",
                f"{before.start_time}-{before.end_time}",
                f"{after.start_time}-{after.end_time}",
            ),
            ("教室", before.location or "", after.location or ""),
            ("座位", before.seat or "", after.seat or ""),
            ("考试名称", before.exam_name or "", after.exam_name or ""),
        ):
            if lhs != rhs:
                out.changes.append(
                    ExamChange("changed", after.course_name, label, lhs, rhs, after.date_str)
                )
    return out
```

`cmd_diff` 里：先算 `exam_diff`（读 `raw_exams_path` / `raw_exams_prev_path`，任一缺失则 `ExamDiff()` 并 info 说明），把 `:782-785` 改成

```python
    if result.is_empty and exam_diff.is_empty:
        print("无变化：两份快照的课程、时段、周次、教室与教师完全一致。")
        return 0
```

再在六个小节之后追加「考试变更」一节（新增 / 取消 / 时间变更 / 教室变更 / 座位变更），文案中文、不出现学号姓名。

- [x] **Step 4: 跑测试确认通过 + CLI 冒烟**

Run: `python -m pytest tests/test_diff_exams.py tests/test_cli_diff.py -q`
Expected: PASS（既有课表 diff 用例不得因短路改动而破）

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/exams.py src/xjtu_calendar/cli.py tests/test_diff_exams.py
git commit -m "feat(diff): report exam changes"
```

---

## Task 11: 陈旧口径与打码名单

**Files:**
- Modify: `src/xjtu_calendar/subscribe.py:304-311`（`snapshot_age_days`）
- Modify: `src/xjtu_calendar/cli.py`（`cmd_export` 的考试陈旧 warning）
- Modify: `src/xjtu_calendar/logging_setup.py:31-60`（`_SENSITIVE_KEYS` 补 `sjbh`/`zjjsxm`）
- Test: `tests/test_subscribe.py`、`tests/test_exams_fetch.py`

**Interfaces:**
- Consumes: `config.raw_exams_path`（T6）、既有 `logging_setup.redact`（按键打码，`:145`）
- Produces: `snapshot_age_days(cfg, semester, *, kind="timetable") -> float | None`（默认值保持既有行为，`cli.py:940-942` 调用点不破）；`exams.exam_snapshot_lag_days(cfg, semester) -> float | None`（考试快照落后课表快照的天数，考试更新则 `0.0`，任一缺失则 `None`）

- [x] **Step 1: 写失败测试**

放进 `tests/test_exams_fetch.py`（该文件已为 T7 建好，`snapshot_age_days` 的既有测试在
`tests/test_subscribe.py`，本任务只往里补一条 kind 断言）。

```python
import json
import os
import time
from pathlib import Path

from exam_support import SEMESTER, exam_payload, exam_row

from xjtu_calendar.config import Settings
from xjtu_calendar.exams import exam_snapshot_lag_days
from xjtu_calendar.logging_setup import redact
from xjtu_calendar.subscribe import snapshot_age_days


def _touch(path: Path, *, days_ago: float) -> None:
    """把 mtime 拨到 N 天前；跨平台都用 os.utime，不靠 sleep。"""
    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def test_snapshot_age_days_defaults_to_timetable_kind(tmp_path: Path) -> None:
    """既有调用点（cli.py:940）不传 kind，行为必须与改造前逐字一致。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    assert snapshot_age_days(cfg, SEMESTER) is None  # 还没 fetch 过

    cfg.raw_timetable_path(SEMESTER).write_text('{"kbList": []}', encoding="utf-8")
    assert snapshot_age_days(cfg, SEMESTER) < 1
    assert snapshot_age_days(cfg, SEMESTER, kind="exams") is None  # 考试侧仍为空


def test_exam_kind_reads_the_exam_snapshot(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    _touch(exam_path, days_ago=3)

    assert 2.9 < snapshot_age_days(cfg, SEMESTER, kind="exams") < 3.1
    assert snapshot_age_days(cfg, SEMESTER, kind="timetable") is None


def test_lag_is_exam_snapshot_behind_timetable_snapshot(tmp_path: Path) -> None:
    """§7 选定口径：考试快照落后于**课表快照**多少天，不是「距今几天」。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    timetable = cfg.raw_timetable_path(SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=10)
    _touch(timetable, days_ago=1)

    assert 8.9 < exam_snapshot_lag_days(cfg, SEMESTER) < 9.1


def test_lag_is_none_when_either_snapshot_is_missing(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    assert exam_snapshot_lag_days(cfg, SEMESTER) is None
    cfg.raw_exams_path(SEMESTER).write_text("{}{}", encoding="utf-8")  # 只有考试，没有课表
    assert exam_snapshot_lag_days(cfg, SEMESTER) is None


def test_exam_ahead_of_timetable_is_not_stale(tmp_path: Path) -> None:
    """考试比课表新（先 fetch 考试再动课表）不该报警：滞后为负按 0 处理。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    timetable = cfg.raw_timetable_path(SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=1)
    _touch(timetable, days_ago=5)

    assert exam_snapshot_lag_days(cfg, SEMESTER) == 0.0


def test_teacher_and_log_id_keys_are_redacted() -> None:
    """redact 是**按键**打码，不是按值：喂 dict 而不是喂 json 字符串。"""
    assert redact({"ZJJSXM": "张三"})["ZJJSXM"] == "***"
    assert redact({"SJBH": "007"})["SJBH"] == "***"
    assert redact({"XM": "李四"})["XM"] == "***"  # 既有能力，回归护栏
    assert redact({"KCM": "示例课程甲"})["KCM"] == "示例课程甲"  # 课程名不敏感
```

> `cmd_export` 里那条 warning 不在单测里断（本任务只补**函数级**行为）。若实现者想加
> CLI 级断言，用 `exam_support.captured_logs()` 抓日志，**不要用 `caplog`**——
> `setup_logging` 把 `propagate` 置了 False，caplog 在跑过 `main()` 的会话里抓不到东西。

- [x] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_subscribe.py tests/test_exams_fetch.py -q -k "exam or zjjsxm"`
Expected: FAIL — 意外关键字参数 `kind` / 断言失败

- [x] **Step 3: 写最小实现**

`subscribe.py`（`snapshot_age_days` 现状 `:304-311` 硬读 `raw_timetable_path`）：

```python
def snapshot_age_days(cfg: Settings, semester: str, *, kind: str = "timetable") -> float | None:
    """raw 快照距今的天数；快照不存在返回 ``None``。

    ``kind`` 与 :func:`xjtu_calendar.fetcher.save_raw` 同一口径（``"timetable"`` /
    ``"exams"``）。默认值刻意保持不变——`cli.py:940` 的既有调用点不传 kind。
    """
    path = cfg.raw_exams_path(semester) if kind == "exams" else cfg.raw_timetable_path(semester)
    if not path.is_file():
        return None
    import time

    return (time.time() - path.stat().st_mtime) / 86400.0
```

`exams.py` 里新增滞后口径（放考试侧而不是 subscribe 侧：它读的是两个快照，属于考试特性）：

```python
def exam_snapshot_lag_days(cfg: Settings, semester: str) -> float | None:
    """考试快照落后于课表快照的天数（设计文档 §7 选定的陈旧口径）。

    用**相对**差而不是「距今几天」：课表与考试在同一次 fetch 里落盘，两者一起变老
    是正常的；只有考试没跟着更新时才说明缓存过期。考试比课表新（先抓到考试、之后
    才动课表）返回 ``0.0``，任一快照缺失返回 ``None``（此时没什么可提醒的）。
    """
    from .subscribe import snapshot_age_days

    exam_age = snapshot_age_days(cfg, semester, kind="exams")
    timetable_age = snapshot_age_days(cfg, semester, kind="timetable")
    if exam_age is None or timetable_age is None:
        return None
    return max(0.0, exam_age - timetable_age)
```

> `from .subscribe import snapshot_age_days` 必须放在**函数体内**：`subscribe.py` 已经
> `from .exporter import ...`，`exams.py` 顶层再 import 它会成环（Global Constraints 里
> 那条循环依赖约束同样适用于 subscribe）。

`cmd_export` 在构建完成后加一段（`cli.py` 的 `cmd_export`，紧跟现有 `result = build_ics_for_semester(...)` 之后）：

```python
from .exams import exam_snapshot_lag_days

lag = exam_snapshot_lag_days(cfg, semester)
if lag is not None and lag > 7:
    logger.warning(
        "考试快照比课表快照旧 %.0f 天，考试可能已改期，建议重新 fetch（本次仍按现有快照导出）",
        lag,
    )
```

`_SENSITIVE_KEYS` 追加 `"sjbh"`（座位号在部分响应里用这个键）、`"zjjsxm"`（主考教师）。
`xh` / `xm` / `skjs` / `teacher` 已在名单里，别重复加。

- [x] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_subscribe.py tests/test_exams_fetch.py -q`
Expected: PASS

- [x] **Step 5: 门禁 + 提交**

```bash
python -m pytest -q && python -m mypy && python -m ruff check . && python -m ruff format --check .
git add src/xjtu_calendar/subscribe.py src/xjtu_calendar/cli.py src/xjtu_calendar/logging_setup.py
git commit -m "feat(export): exam snapshot staleness warning and redaction keys"
```

---

## Task 12: 文档与发布就绪

**Files:**
- Modify: `README.md`（「考试安排」小节 + 命令参考表 + 模块树加 `exams.py`）
- Modify: `CHANGELOG.md`（`[Unreleased]` → `Added` / `Changed`）

**Interfaces:**
- Consumes: 前十一个任务的产物
- Produces: 用户可见的说明与已知限制

- [x] **Step 1: README 新增小节**（必须写清四条，缺一不可）

1. 默认包含考试，`--no-exams` 关闭；关闭不删本地快照。
2. **只有 HTTP 路径能拿到考试**：`--from-file` 与浏览器拦截路径不抓（`fetch_via_browser` 的关键词不含 `wdksap`）。
3. 三态行为：状态未知时**不覆盖**已有快照，沿用旧数据并提示其日期。
4. **已发布的考试事件不会因关闭开关而消失**（`render_ics` 只发 `method: PUBLISH`，全仓没有 `STATUS:CANCELLED` 通路）；订阅端可能需要手动删除旧考试事件。座位号在事件的 `DESCRIPTION` 里看。

命令参考表补：`fetch/export/subscribe push/subscribe rotate` 的 `[--no-exams]`；模块树补 `exams.py`。

- [x] **Step 2: CHANGELOG**

```markdown
## [Unreleased]

### Added
- 考试安排接入：`fetch` 同时抓 `wdksap.do`，同一份 .ics 内含考试事件
  （单场绝对时刻、无提醒；UID 依据行 ID `WID`）。`--no-exams` 可关闭。
- `diff` 新增「考试变更」小节（时间/教室/座位/考试名称）。

### Changed
- `save_raw`/`load_raw` 增加 `kind=`；原始快照一律以 `private=True` 落盘（含课表快照）。
```

- [x] **Step 3: 文档门禁（Markdown 里的 ```python 块也归 `ruff format --check .` 管）**

```bash
python -m ruff format --check . && python -m ruff check . && python -m mypy && python -m pytest -q
```

Expected: 四条全绿。若 `ruff format --check .` 报本文档或 `docs/design/…md`，先 `ruff format <该文件>` 再提交（本项目已被这条门禁坑过两次）。

- [x] **Step 4: 真实快照离线复验**（不联网，用 gitignored 的真实缓存）

```bash
python -m xjtu_calendar export --semester <真实学期代码> -o _notes/exam-real.ics
```
Expected: 事件数 = 课程事件 + 考试行数；打开 .ics 抽查 `DTSTART;TZID=` 全部带时刻；**产物文件绝不提交**（含个人信息，`_notes/` 已 gitignore）。

- [x] **Step 5: 提交**

```bash
git add README.md CHANGELOG.md
git commit -m "docs(exam): document exam schedule integration"
```

---

## 验收清单（全分支终审前）

- [x] 四条门禁全绿；`git log --oneline` 每任务一笔提交。
- [x] 未新增运行时依赖；`pyproject.toml` 除 `package-data` 外未动。
- [x] 课程事件的 UID / SEQUENCE / 字节输出与接入前**完全一致**（用同一快照 diff 两份 .ics 验证；
  事后已自动化为 golden 回归用例 `tests/test_legacy_course_export_golden.py`，
  基线由 `0bd9414681` 的代码对全合成输入导出）。
- [x] 考试事件全部是 `DATE-TIME`，`grep -c "VALUE=DATE" <产物.ics>` 为 0。
- [x] 断开网络/会话过期状态下跑 `fetch`：课表成功、考试记 warning、旧快照未被覆盖、退出码 0。
- [x] `--no-exams` 在 fetch/export/push/rotate 四条路径上都有效，且不删本地快照。
- [x] `_notes/` 与 `raw/` 里的真实快照没有出现在 `git status`。
- [x] 公开侧文档（README/CHANGELOG/design/plan）不含真实课程名、学号、教师名、考试日期。
