# 思源学堂作业 DDL 接入（`lms_todo` 数据源）实现计划

> **For agentic workers:** 逐任务实现、每个任务收尾过一次独立评审、全部完成后再做全分支终审；
> 派发预算与门禁口径按 `AGENTS.md`「派发边界规则」执行。
>
> **本计划不再由本仓库执行（2026-10-23）。** 规格 `docs/design/2026-10-21-homework-deadlines.md`
> 的状态已改为**本仓库不排期实现，转为公开接手材料**：D4/D5/D7 未拍，§5 读数里承重的那条
> （本文件「读数 S2」）还是空的。接手人可以把它当现成的施工骨架——它的作用是把
> 「哪些活儿已经能钉死、哪些必须等人拍」落到具体文件与行号上：拍板之后删掉落选分支即可开工，
> 不需要重写。但绑在这套门禁与派发纪律上的假设只有本仓库成立，换人接手要自己决定跟不跟。
> 计划撰写于 2026-10-22 00:4x +0800。

**Goal:** 把思源学堂（`lms.xjtu.edu.cn`）未提交作业的截止时间接进现有 `fetch → 快照 → 解析 → 导出 → diff → subscribe` 管线，让同一份 .ics 里同时有课程、考试与作业 DDL 三类事件。

**Architecture:** 新建 `src/xjtu_calendar/homework.py` 承担作业侧全部逻辑（三态判定、UTC→上海换算、UID、事件构建、变更比对），`models.py` 只加一个 `HomeworkItem` 数据类。抓取层要第一次支持**第二个 host**：`Endpoint` 加 base、`Settings` 加 `lms_base`、`auth.load_cookies` 的 host 过滤参数化。导出侧完全照考试那一支的形状并入（日期过滤之前、全局 UID 唯一断言之内、迁移 `live_uids` 并集里加第四源）。退场通路是本议题唯一没有先例的部分，两支都写出来（Task 10A/10B），拍板后删一支。

**Tech Stack:** Python 3.11+（stdlib：`datetime`/`hashlib`/`dataclasses`/`enum`/`re`；HTTP 用已有的 `httpx` 懒加载），`icalendar>=6.1.0`，pytest，mypy strict，ruff。零新增运行时依赖。

**Spec:** `docs/design/2026-10-21-homework-deadlines.md`（D1–D9 与 §5 前置读数是硬约束；本计划逐节对应，§3 的「只做 `type=homework`」是不可扩大的范围线）
**取证:** `_notes/siyuan-lms-probe-2026-10-21.md`（gitignored，含真实字段样本与可复跑脚本）

## Global Constraints

- **范围锁死在 `type=homework`**。考试与公告不在本期（spec §3）：各自缺一条决定性读数，且公告走的是另一个端点、形状未知。`/api/announcement` 与 `/api/bulletins/latest` 一个字都不许解析。
- **不在本议题里重开 §12**（搬到过去时段）—— 那是独立一条线，状态仍是「不批准实现」。
- 零新增运行时依赖；**不触碰课程与考试的 UID 配方与导出内容**（`tests/test_legacy_course_export_golden.py`、`tests/test_legacy_exam_export_golden.py` 会逐字节比对）。
- **端点路径必须来自真实观测**（`fetcher.PLACEHOLDER_MARKER`，`fetcher.py:64`；`require_endpoint` 在 `fetcher.py:155-158` 拒绝占位路径）。本计划里 `/api/todos` 已实测存在，其余端点不许顺手加。
- **LMS 响应只落自己的快照文件**（`raw/lms-todo-*.json`），**不并进门户那两份 raw 快照**（spec §6）：两个源、两套失效，混在一起会让相容性检查与 `repair` 的裁决依据变模糊。
- **第二个 host 是新的结构事实**：`Endpoint.url()` 把 `cfg.ehall_base` 写死在拼接里（`fetcher.py:105-107`），`auth.load_cookies()` 按 `urlparse(cfg.ehall_base).hostname` 过滤 cookie（`auth.py:418`）并把非 eHall 域的 cookie **丢掉**（`auth.py:425-427`）。改这两处之前，任何「LMS 请求复用门户会话」的写法都是假的。
- **作业事件必须是带 `Asia/Shanghai` 的 DATE-TIME**。本计划**不采用**全天事件形态，理由是硬约束不是偏好：台账只收计时事件，全天事件进 `render_ledger` 会直接 `raise CalendarExportError`（`cancellation.py:66-69`），选全天等于让 D4 自动只剩「直接不发」一支。若用户明确拍「全天 + 不退场」，用 T6-B 并连带改写决策依赖表。
- **本计划豁免一条既有口径**：考试那一支的约束是「拼装一律走 `schedules.combine(day, hhmm)`」。作业不行——`combine()` 只接受「某日 + 某钟点」，而作业是一个**绝对瞬时点往前减一个窗口**；截止落在 `00:00`~`00:30` 时窗口跨到前一天，按日拼会算出 `end <= start`，撞 `models.py:314`。作业侧改走 `datetime.fromisoformat(...).astimezone(TZ_XIAN)`，并由 `HomeworkItem.__post_init__` 强制「aware 且偏移 = `+08:00`」兜住（T1、T3）。
- **宁缺毋滥**：三态判 `UNKNOWN` 时**绝不覆盖**已有快照；`fetch`/`export` 绝不因为作业失败而非零退出。`todo_list` 为**空数组**不自动等于「没有作业」——它和「会话/权限异常」都能回空，判据必须能切开（读数 S2，承重）。
- **§8 隐私口径原样适用**：`ParseReport` 的跳过/提醒、构建期的丢弃日志、diff 行文案**只给行号与字段名**，绝不带作业标题、课程名原文或时间原文；这三条通道的字符串会被逐条打进日志。
- **跟踪内容与测试固件一律用合成值**：日期用 `2030-*`（`2030-06-17` 确实是星期一，与既有固件同源），课程号用 `203020301DEMO00000001` 式，名称用 `示例课程甲`/`示例作业甲`。真实的 `course_code`、`id`、`end_time` 只留在 `_notes/`。工具仓是公开的，具体到某周某门课的截止时刻属于课表派生个人信息。
- **`--no-homework` 必须独立**，不得复用 `--no-exams`（spec D5）。`cli.py:1967` 那段注释记的就是「旗标没透传到 rotate ⇒ 用户明确关掉的被悄悄塞回订阅 URL」。四个子命令都要带：fetch / export / push / rotate。
- 测试 import 分组按 ruff `I`：`tests/` 无 `__init__.py`，写 `from homework_support import ...`（第三方一组），空一行再 `from xjtu_calendar...`。抓日志复用 `exam_support.captured_logs()`，它是**context manager、yield 出 `list[LogRecord]`**（`with captured_logs() as records:` + `rec.getMessage()`）；**不要用 `caplog`**（`logging_setup` 把 `propagate` 设成 False，同会话跑过 `main()` 后 caplog 随机抓不到）。
- `mypy --strict` 的 `files` 只有 `src/xjtu_calendar`（`pyproject.toml:80-84`），测试不查类型——但 `src` 里每个新函数都要完整注解。`pyproject.toml:72-78` 有 `--doctest-modules`：`homework.py` 的 docstring 里**不要**放 `>>>` 示例。`line-length = 100`。
- 循环依赖：`homework.py` 顶层 `from .exporter import UID_DOMAIN`（照 `exams.py:36`），因此 `exporter.py` 只能在 `build_ics_for_semester` **函数体内**局部 import 作业侧符号。
- 台账/留底文件的写盘一律 `newline=""` + CRLF（RFC 5545），照 `tests/exam_support.py:161-169` 的 `write_ledger`；测试里的空白产物常量用 `"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"`。
- 每个任务迭代期只跑定向 `pytest -k` 或单文件，**四条全量门禁只在收尾跑一次**（`AGENTS.md` 派发规则 3）。全量口径 = `pytest -q` + `mypy` + `ruff check .` + `ruff format --check .`（与 `.github/workflows/ci.yml` 一致），另加第五条 `git diff --check <远端旧 tip> HEAD`——四条不覆盖空白，10-16 与 10-21 同型踩过。
- 本机解释器与 venv 路径**不写进跟踪文档**（`AGENTS.md` 红线 2：不得含本机绝对路径与工具品牌名）；见项目记忆里的测试环境条目。pytest 加 `--basetemp=_notes/pytest-tmp`（仓库内、已 gitignore）。找不到解释器时说「我没找到」，不要宣布环境丢了。
- 提交只到本地；**推送、打 tag、GitHub Release、PyPI 上传各需要单独明确授权**。

---

## 决策依赖表

| 决策 | 卡住哪些任务 | 现在能写到什么程度 | 已知的代码级约束（本计划新查出来的，不是复述 spec） |
| --- | --- | --- | --- |
| **D1** UID 配方 | T6 | 配方已可写死；只「要不要带版本号段」待定 | 命名空间常量的先例是 `calendar_events.py:76` 的 `UID_NAMESPACE = "cal"`，作业用 `"hw"`；拍「带版本号」只改一个常量，牵动面 = 常量 + 两处测试期望 |
| **D2** 事件形态 | T1 T6 T10 | 两支都给出（T6 计时窗口 / T6-B 全天） | **spec 里的「零时长」那一支已被代码否证**：`models.py:314` 是 `if self.end <= self.start: raise ValueError`，`DTSTART == DTEND` 连构造都过不去 ⇒ §5 第 4 条读数不必再取「零时长怎么显示」，问题变成「30 分钟窗口 vs 全天」 |
| **D4** 消失即退场 | T10 | 两支都写（10A 直接不发 / 10B 发墓碑） | **D4 依赖 D2**：全天进不了台账（`cancellation.py:66-69` 抛），所以选全天 ⇒ 只能 10A。**D4 还依赖读数 S2**（空数组语义），S2 未取到之前两支都不许开工 |
| **D5** 开关默认值 | T8 | plumbing 全部可以现在写（四个旗标位置与 `--no-exams` 逐一对应） | 默认并入 ⇒ 老用户下次 push 突然多出 N 条，属用户可见产物变化，要走 minor（D8）并先彩排。默认不带 ⇒ D8 的判定时点挪到「用户第一次开启」那次 |
| **D7** 标题脱敏 | T6 的 `summary` 一行 | 两种写法都已给出 | 不脱敏 ⇒ D5 的开关从可选变必选；脱敏 ⇒ 标题进 `sequence.event_fingerprint`（`sequence.py:71-105`），将来改一次文案等于每条作业 `SEQUENCE`+1 |
| **D9** 登录态 | T5 | host/base/cookie 三处机械层可以写；login UX 待定 | 今天实测：未登录时 `/api/todos` **不回 401**，是 `302 → 400` 的 Keycloak 登录页 ⇒ 现有 `AuthenticationExpired` 通路（`fetcher.py:391-392`）与 `LOGIN_PAGE_MARKERS`（`fetcher.py:68-78`，九条零命中）**两条都接不住**。见「读数 S1/S3」 |

## 前置读数

spec §5 六条仍然有效，此处只记增量。

| 编号 | 状态 | 内容 |
| --- | --- | --- |
| §5-3 前半 | **今天取到** | 不带任何 cookie 直接 `GET https://lms.xjtu.edu.cn/api/todos`：`302 FOUND` →（再一跳）`identity1.xjtu.edu.cn/auth/realms/xjtu/broker/cas-client/login`（`client_id=tronclass`）→ 最终 **HTTP 400**，`text/html; charset=utf-8`，1798 字节，`<title>Sign in to xjtu</title>`、`<html class="login-pf">`；正文里 `todo_list` 出现 0 次。⇒ 未登录的失败形态是 **400 + 登录页 HTML**，不是 401，也不是 JSON 错误。任何人用 `curl -sSL` 都能复跑 |
| **S1** | **新增，承重（T5、D9）** | 门户 `login` 建的那个本机 profile 里，有没有 `lms.xjtu.edu.cn` 域的 cookie / 能不能直接拿到 200？两边 IDP 名字不同（Keycloak realm `xjtu` + CAS broker vs 门户侧 CAS），但可能是同一套 SSO。答案决定 D9 是「复用 `login` + 一次静默访问」还是「多一个 login 入口」。取证成本：跑一次 `login` 后数 `session/storage_state.json` 里的 cookie 域名，**不要把 cookie 值抄进任何文档** |
| **S2** | **新增，承重（T10）** | `todo_list == []` 到底是「全交完了」还是「会话/权限异常」？今天的 400 回的是 HTML 所以走不到 JSON 分支，但 `200 + 空数组` 这第三种形态一次都没测过。不切开这条，10A 等于「一次会话异常把用户所有作业事件从日历抹掉」，10B 等于「凭空发一批墓碑」 |
| **S3** | **新增，不承重** | 带**过期** cookie 时 `/api/todos` 回什么。今天测的是「完全不带 cookie」，不许外推。取不到不拦实现，但 T2 的 `UNKNOWN` 判据要按最保守的形状写 |

---

## 文件总览

| 动作 | 路径 | 职责 |
| --- | --- | --- |
| Create | `src/xjtu_calendar/homework.py` | 作业侧全部逻辑：三态判定、UTC→上海换算、UID、事件构建、变更比对 |
| Create | `tests/homework_support.py` | 共享固件：`todo_row()`、`todos_payload()`、`homework_home()`、打码行、泄漏判据、CLI spy |
| Create | `tests/test_homework.py` | T1–T3、T6、T9 的单元与红线 |
| Create | `tests/test_homework_fetch.py` | 三态落盘（UNKNOWN 不覆盖快照）、第二 host、400/登录页失败形态 |
| Create | `tests/test_exporter_homework.py` | 并入、UID 唯一、取自未过滤集合、`--no-homework` |
| Create | `tests/test_cli_homework.py` | 旗标贯穿 fetch/export/push/rotate |
| Modify | `src/xjtu_calendar/models.py` | +`HomeworkItem`（frozen，接在 `ExamSchedule` 之后 `:375`） |
| Modify | `src/xjtu_calendar/config.py` | +`lms_base`、`raw_homework_path`/`raw_homework_prev_path`、`homework_ledger_path` |
| Modify | `src/xjtu_calendar/fetcher.py` | `Endpoint.url(cfg, base_key=)`、`_raw_paths` 加 `kind="lms_todo"`、`_parse_endpoints` 认 `base` 键 |
| Modify | `src/xjtu_calendar/auth.py` | `load_cookies(cfg, *, host=None)`：host 过滤参数化 |
| Modify | `src/xjtu_calendar/data/ehall_endpoints.json` | +`lms_todos` 条目（带 `base` 键） |
| Modify | `src/xjtu_calendar/exporter.py` | `build_ics_for_semester` 并入作业事件 + 第四源 UID 断言 + 迁移并集 |
| Modify | `src/xjtu_calendar/subscribe.py` | `snapshot_age_days` 认 `kind="lms_todo"` |
| Modify | `src/xjtu_calendar/cli.py` | `--no-homework` 四子命令、`_fetch_homework`、diff 作业小节 |
| Modify | `README.md` / `CHANGELOG.md` / `docs/user-guide/subscription.md` / `skills/xjtu-timetable-setup/SKILL.md` | 作业小节、旗标表、`[Unreleased]`、版本号按 D8 走 minor |

---

## Task 1: `HomeworkItem` 模型与作业固件

**Files:**
- Modify: `src/xjtu_calendar/models.py`（接在 `ExamSchedule` 之后，`:375` 前）
- Create: `tests/homework_support.py`
- Create: `tests/test_homework.py`（先只有固件自检与模型判据）

**Interfaces:**
- Consumes: 无（本任务是地基）
- Produces: `models.HomeworkItem`（frozen，字段见 Step 3）；`tests/homework_support` 里的
  `SEMESTER = "2026-2027-1"`（沿用 `exam_support.py:30`，真实形态的 key 才不会让标题推导走偏）、
  `DEMO_DEADLINE_UTC = "2030-06-17T15:59:00Z"`、`todo_row(**over)`、`todos_payload(rows)`、
  `marker_row(**over)`、`leaked(messages)`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_homework.py
from datetime import datetime, timedelta, timezone

import pytest

from homework_support import DEMO_DEADLINE_UTC, HOURS8, leaked, marker_row, todo_row, todos_payload
from xjtu_calendar.models import HomeworkItem


def _item(**over: object) -> HomeworkItem:
    fields: dict[str, object] = {
        "homework_id": "900001",
        "course_code": "203020301DEMO00000001",
        "course_name": "示例课程甲",
        "title": "示例作业甲",
        "start": datetime(2030, 6, 17, 23, 30, tzinfo=TZ_SH),
        "deadline": datetime(2030, 6, 17, 23, 59, tzinfo=TZ_SH),
    }
    fields.update(over)
    return HomeworkItem(**fields)  # type: ignore[arg-type]


def test_item_keeps_the_shanghai_wall_clock_and_window_order() -> None:
    item = _item()
    assert item.deadline.utcoffset() == HOURS8
    assert item.deadline > item.start


def test_naive_datetimes_are_rejected() -> None:
    with pytest.raises(ValueError, match="时区"):
        _item(start=datetime(2030, 6, 17, 23, 30))


def test_utc_offset_that_is_not_shanghai_is_rejected() -> None:
    with pytest.raises(ValueError, match="Asia/Shanghai"):
        _item(deadline=datetime(2030, 6, 17, 15, 59, tzinfo=timezone.utc))


def test_inverted_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="晚于"):
        _item(start=datetime(2030, 6, 18, 0, 0, tzinfo=TZ_SH))


def test_empty_homework_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="id"):
        _item(homework_id="   ")


def test_fixture_helpers_self_check() -> None:
    rows = [todo_row(), todo_row(title="示例作业乙")]
    assert todos_payload(rows) == {"todo_list": rows}
    assert todo_row()["end_time"] == DEMO_DEADLINE_UTC
    assert leaked([todo_row()["title"], "无害的一行"]) == []
    assert leaked([marker_row(id=None)["title"]]) == [_LEAK]
```

固件里 `_LEAK` 就是 `marker_row` 写进标题与课程名的唯一标记串，测试文件顶部 `from homework_support import LEAK_MARKER as _LEAK` 取它。`TZ_SH` 也从 `homework_support` 导入。

- [ ] **Step 2: 跑测试确认失败**

Run: `pytest tests/test_homework.py -q --basetemp=_notes/pytest-tmp`
Expected: `ModuleNotFoundError: homework_support` / `ImportError: cannot import name 'HomeworkItem'`

- [ ] **Step 3: 写最小实现**

`models.py` 接在 `ExamSchedule` 之后。这里存 **aware datetime** 而不是 `"HH:MM"` 串，
理由是 Global Constraints 里那条 `combine()` 豁免：

```python
@dataclass(frozen=True)
class HomeworkItem:
    """思源学堂（LMS）一条未提交作业的规范化形态。

    与 :class:`ExamSchedule` 的关键差别：这里存 **aware datetime**，不是 ``"HH:MM"`` 串。
    作业的 ``end_time`` 是一个绝对瞬时点，事件窗口从它往前减。先减再落地成
    「某日 + 某钟点」会在截止落在 ``00:00``~``00:30`` 时跨到前一天，
    减出来的 ``start`` 与 ``deadline`` 分属两日，按日拼必然算反。

    ``start``/``deadline`` 的偏移必须是 ``+08:00``。偏移不对时产物会写成 ``TZID=UTC``
    或浮动时间（设计文档 D3 要拦的就是这个），所以在构造期就拒绝，不留给导出侧猜。
    """

    homework_id: str
    course_code: str
    course_name: str
    title: str
    start: datetime
    deadline: datetime

    def __post_init__(self) -> None:
        for label, value in (("start", self.start), ("deadline", self.deadline)):
            if value.tzinfo is None or value.utcoffset() != timedelta(hours=8):
                raise ValueError(f"作业 {label} 必须是 Asia/Shanghai（+08:00）的计时")
        if self.deadline <= self.start:
            raise ValueError("作业截止必须晚于窗口起点")
        if not self.homework_id.strip():
            raise ValueError("作业缺少稳定 id")
        if not self.course_code.strip():
            raise ValueError("作业缺少课程号")
```

`tests/homework_support.py` 要点（字段名照 `_notes/siyuan-lms-probe-2026-10-21.md` §2 的真实 11 键，值全合成）：

```python
# tests/homework_support.py
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

HOURS8 = timedelta(hours=8)
TZ_SH = timezone(HOURS8)
SEMESTER = "2026-2027-1"
DEMO_DEADLINE_UTC = "2030-06-17T15:59:00Z"
DEMO_DEADLINE = datetime(2030, 6, 17, 23, 59, tzinfo=TZ_SH)
LEAK_MARKER = "LEAKED-UNIQUE-7f3a"


def todo_row(**over: object) -> dict[str, object]:
    """一条 LMS `todo_list` 行。键名与实测 11 键一致，值全是合成的。"""
    row: dict[str, object] = {
        "course_code": "203020301DEMO00000001",
        "course_id": 33001,
        "course_name": "示例课程甲",
        "course_type": 1,
        "end_time": DEMO_DEADLINE_UTC,
        "id": 900001,
        "is_locked": False,
        "is_student": True,
        "prerequisites": [],
        "title": "示例作业甲",
        "type": "homework",
    }
    row.update(deepcopy(over))
    return row


def todos_payload(rows) -> dict[str, object]:
    return {"todo_list": list(rows)}


def marker_row(**over) -> dict[str, object]:
    return todo_row(title=LEAK_MARKER, course_name=LEAK_MARKER, **over)


def leaked(messages) -> list[str]:
    return [m for m in messages if LEAK_MARKER in m]


def uid_lines(ics: str) -> list[str]:
    """产物里所有 `UID:` 行的值，按出现顺序。（T7 的碰撞用例要用，先在这儿备好。）"""
    return re.findall(r"^UID:(.+)$", ics, re.M)
```

文件顶部还需要 `import re`。其余 `Path` / `Settings` / `save_raw` / `make_home` 四个 import
**留到 T4 再加**（现在加了就是 ruff 的未使用导入，本任务收尾的 `ruff check .` 会红）。

`captured_logs()` **复用 `exam_support` 那一份**（`exam_support.py:37-61`），不要在作业固件里写第二遍。

- [ ] **Step 4: 跑测试确认通过**

Run: `pytest tests/test_homework.py -q --basetemp=_notes/pytest-tmp`
Expected: 7 passed

- [ ] **Step 5: 提交**

```bash
git add src/xjtu_calendar/models.py tests/homework_support.py tests/test_homework.py
git commit -m "feat(homework): HomeworkItem 模型与作业测试固件"
```

---

## Task 2: `classify_todo_payload` —— 三态，且空数组不许轻易成立

**Files:**
- Modify: `src/xjtu_calendar/homework.py`（新建）
- Modify: `tests/test_homework.py`

**Interfaces:**
- Consumes: 无（只吃原始 payload）
- Produces: `homework.TodoState`（`HAS` / `EMPTY` / `UNKNOWN`）、`homework.TodoOutcome`
  （`state`、`rows: tuple[Mapping[str, Any], ...]`、`msg: str`）、
  `homework.classify_todo_payload(payload: Any) -> TodoOutcome`

判据的取舍：结构可判定**且**列表为空才叫 `EMPTY`；任何「说不清」都落到 `UNKNOWN`。
因为 `EMPTY` 在 D4 那一支是有后果的（驱动退场），而 `UNKNOWN` 什么都不做。

- [ ] **Step 1: 写失败测试**

```python
@pytest.mark.parametrize(
    ("payload", "state"),
    [
        (todos_payload([todo_row()]), "has"),
        (todos_payload([]), "empty"),
        ({}, "unknown"),
        ({"todo_list": "x"}, "unknown"),
        ({"todos": [todo_row()]}, "unknown"),
        ([], "unknown"),
        (None, "unknown"),
        ({"todo_list": ["not-a-dict"]}, "unknown"),
    ],
)
def test_classify_todo_payload_states(payload, state) -> None:
    assert classify_todo_payload(payload).state.value == state


def test_empty_rows_still_report_empty_tuple() -> None:
    outcome = classify_todo_payload(todos_payload([]))
    assert outcome.state is TodoState.EMPTY and outcome.rows == ()
```

- [ ] **Step 2: 跑测试确认失败** — Run: `pytest tests/test_homework.py -k classify -q`，Expected: ImportError

- [ ] **Step 3: 写实现**

```python
"""思源学堂作业（`/api/todos` 的 `todo_list`）的判定、解析与导出。
设计文档：docs/design/2026-10-21-homework-deadlines.md。"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

from .exporter import UID_DOMAIN  # 顶层导入；exporter 反向只在函数内 import（避免循环）
from .models import CalendarEvent, HomeworkItem
from .parser import ParseReport
from .timeutil import TZ_XIAN

logger = logging.getLogger(__name__)


class TodoState(Enum):
    """``EMPTY`` 只在「结构完整且列表为空」时成立；说不清一律 ``UNKNOWN``。

    ``EMPTY`` 会驱动退场（设计文档 D4），所以它的判据比考试侧 ``NO_EXAMS`` 更窄：
    网络层失败在这里根本到不了（``fetch_via_http`` 已经抛），但 ``200`` 的响应体
    仍可能因为服务端改版而缺 ``todo_list``——那一条必须读成 ``UNKNOWN``，
    绝不能读成「用户把作业全交完了」。
    """

    HAS = "has"
    EMPTY = "empty"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class TodoOutcome:
    state: TodoState
    rows: tuple[Mapping[str, Any], ...] = ()
    msg: str = ""


def classify_todo_payload(payload: Any) -> TodoOutcome:
    if not isinstance(payload, Mapping):
        return TodoOutcome(TodoState.UNKNOWN, msg="响应顶层不是对象")
    raw = payload.get("todo_list")
    if not isinstance(raw, list):
        return TodoOutcome(TodoState.UNKNOWN, msg="缺少 todo_list 数组")
    rows = tuple(row for row in raw if isinstance(row, Mapping))
    if len(rows) != len(raw):
        # 混进非对象条目说明形状已经变了；宁可当不知道，也不要按「少了几条」退场。
        return TodoOutcome(TodoState.UNKNOWN, msg="todo_list 含非对象条目")
    if not rows:
        return TodoOutcome(TodoState.EMPTY, (), "待办列表为空")
    return TodoOutcome(TodoState.HAS, rows, "")
```

- [ ] **Step 4: 跑测试确认通过** — Expected: 9 passed
- [ ] **Step 5: 提交** — `git commit -m "feat(homework): todo_list 三态判定"`

> **开工闸门**：读数 S2（`200 + 空数组` 是否只在「真的全交完」时出现）没取到之前，本任务可以合入，
> 但 **Task 10 不许开工**。`UNKNOWN` 兜住「说不清」只是挡住了最坏情况，不等于 S2 已解决。

---

## Task 3: `parse_todo_rows` —— 字段解析 + `end_time` UTC→上海换算（D3 落点）

**Files:**
- Modify: `src/xjtu_calendar/homework.py`
- Modify: `tests/test_homework.py`

**Interfaces:**
- Consumes: `classify_todo_payload`（T2）、`models.HomeworkItem`、`timeutil.TZ_XIAN`、`parser.ParseReport`
- Produces: `homework.parse_end_time(value: str) -> datetime | None`、
  `homework.parse_todo_rows(payload: Any, *, report: ParseReport | None = None, window: timedelta = timedelta(minutes=30)) -> list[HomeworkItem]`
  （`window` 就是 D2 的参数化，拍「全天」时本函数签名要改，见 T6-B）

`ParseReport` 的真实形状（`parser.py:143-167`）：`total_candidates: int`、`parsed: int`、
**`skipped: list[str]`**、`warnings: list[str]`，方法只有 `skip(reason)` / `warn(reason)` /
`summary()` / `ok` 属性。**没有 `skipped` 计数属性**，断言写 `len(report.skipped)`。

- [ ] **Step 1: 写失败测试**

```python
def test_end_time_is_read_as_instant_not_wall_clock() -> None:
    """D3 的方向：字段里的 15:59 是 UTC，上海墙钟是 23:59。
    写成「直接拿 15:59 当上海时间」就是每条提前 8 小时到期。"""
    item = parse_todo_rows(todos_payload([todo_row()]))[0]
    assert item.deadline.utcoffset() == HOURS8
    assert item.deadline.strftime("%Y-%m-%d %H:%M") == "2030-06-17 23:59"


def test_naive_or_broken_end_time_is_not_guessed() -> None:
    assert parse_end_time("2030-06-17T15:59:00") is None
    assert parse_end_time("not-a-time") is None
    assert parse_end_time("") is None


def test_window_crossing_midnight_keeps_ordering() -> None:
    row = todo_row(end_time="2030-06-17T16:15:00Z")  # 上海 2030-06-18 00:15
    item = parse_todo_rows(todos_payload([row]))[0]
    assert item.deadline.strftime("%Y-%m-%d %H:%M") == "2030-06-18 00:15"
    assert item.start.strftime("%Y-%m-%d %H:%M") == "2030-06-17 23:45"


def test_non_homework_type_and_missing_fields_are_skipped() -> None:
    report = ParseReport()
    rows = [todo_row(), todo_row(type="exam"), todo_row(id=None), todo_row(end_time="")]
    items = parse_todo_rows(todos_payload(rows), report=report)
    assert [i.homework_id for i in items] == ["900001"]
    assert report.parsed == 1
    assert len(report.skipped) == 3


def test_skip_messages_carry_no_original_text() -> None:
    report = ParseReport()
    parse_todo_rows(todos_payload([marker_row(title=""), marker_row(id=None)]), report=report)
    assert leaked(report.skipped) == []
    assert len(report.skipped) == 2
```

- [ ] **Step 2: 跑测试确认失败** — Run: `pytest tests/test_homework.py -k "instant or naive or midnight or skipped or original_text" -q`

- [ ] **Step 3: 写实现**

```python
def parse_end_time(value: str) -> datetime | None:
    """把 LMS 的 ``end_time`` 读成**一个瞬时点**，换算到 Asia/Shanghai。

    接口给的是 UTC 序列化（``...T15:59:00Z``），界面上同一时刻显示成「23:59」——
    设计文档 D3 记的就是这个方向：**不能**把串里的 15:59 当上海墙钟用。
    不带时区后缀的值一律返回 ``None``：按 UTC 猜会错 8 小时，按本机猜会让产物
    随运行环境变，两种都比丢掉一条作业糟。
    """
    if not value:
        return None
    try:
        instant = datetime.fromisoformat(value)  # 认尾缀 Z 需 3.11+（pyproject.toml:11 覆盖）
    except ValueError:
        return None
    if instant.tzinfo is None:
        return None
    return instant.astimezone(TZ_XIAN)


def _text(record: Mapping[str, Any], key: str) -> str:
    value = record.get(key)
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def parse_todo_rows(
    payload: Any,
    *,
    report: ParseReport | None = None,
    window: timedelta = timedelta(minutes=30),
) -> list[HomeworkItem]:
    """``todo_list`` → :class:`HomeworkItem`。跳过的行只记行号与字段名（spec §8 口径）。"""
    rep = report or ParseReport()
    outcome = classify_todo_payload(payload)
    out: list[HomeworkItem] = []
    for index, row in enumerate(outcome.rows, start=1):
        rep.total_candidates += 1
        if _text(row, "type") != "homework":
            rep.skip(f"第 {index} 行待办的类型不是作业，已跳过")
            continue
        homework_id = _text(row, "id")
        course_code = _text(row, "course_code")
        course_name = _text(row, "course_name")
        title = _text(row, "title")
        deadline = parse_end_time(_text(row, "end_time"))
        if not homework_id:
            rep.skip(f"第 {index} 行作业缺少 id，已跳过")
            continue
        if deadline is None:
            rep.skip(f"第 {index} 行作业的截止时刻（end_time）无法解析，已跳过")
            continue
        if not course_code:
            rep.skip(f"第 {index} 行作业缺少课程号（course_code），已跳过")
            continue
        if not course_name or not title:
            rep.skip(f"第 {index} 行作业缺少课程名或标题，已跳过")
            continue
        out.append(
            HomeworkItem(
                homework_id=homework_id,
                course_code=course_code,
                course_name=course_name,
                title=title,
                start=deadline - window,
                deadline=deadline,
            )
        )
        rep.parsed += 1
    return out
```

- [ ] **Step 4: 跑测试确认通过** — Run: `pytest tests/test_homework.py -q`，Expected: 全部 passed
- [ ] **Step 5: 提交** — `git commit -m "feat(homework): todo 行解析与 end_time 时区换算"`

> **不处理带秒/带毫秒的 `end_time`**：`fromisoformat` 能吃，窗口减法也吃得下，
> 但 `HomeworkItem` 不做秒级归一化。若将来出现 `...T15:59:59Z`，界面上显示的仍是 23:59，
> 我们会写出一条 `23:59:59`。这条**故意留白并在 T12 的已知限制里写一句**，
> 不在读数没覆盖的情况下加一个「四舍五入到分钟」的规则。

---

## Task 4: 快照路径与 `kind="lms_todo"` 落盘

**Files:**
- Modify: `src/xjtu_calendar/config.py:155-168`（照 `raw_exams_path` / `raw_exams_prev_path`）
- Modify: `src/xjtu_calendar/fetcher.py:568-573`（`_raw_paths` 的 kind 分支）
- Modify: `src/xjtu_calendar/subscribe.py:992-1010`（`snapshot_age_days` 认新 kind）
- Modify: `tests/homework_support.py`（加 `homework_home`）
- Modify: `tests/test_homework.py`

**Interfaces:**
- Consumes: `Settings.home` / `Settings.raw_dir` / `Settings.ensure_dirs`
- Produces: `Settings.raw_homework_path(semester_key) -> Path`（`raw/lms-todo-<key>.json`）、
  `Settings.raw_homework_prev_path(semester_key) -> Path`（`raw/lms-todo-<key>.prev.json`，
  **`prev` 在扩展名之前**，与 `config.py:158-160` 同形）；
  `save_raw(..., kind="lms_todo")` / `load_raw(..., kind="lms_todo")` 可用；
  `tests/homework_support.homework_home(tmp_path, *rows, semester=SEMESTER) -> Settings`

- [ ] **Step 1: 写失败测试**

```python
@pytest.fixture
def cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    return Settings(home=tmp_path)


def test_homework_raw_paths(cfg: Settings) -> None:
    assert cfg.raw_homework_path(SEMESTER).name == f"lms-todo-{SEMESTER}.json"
    assert cfg.raw_homework_prev_path(SEMESTER).name == f"lms-todo-{SEMESTER}.prev.json"


def test_save_and_load_roundtrip(cfg: Settings) -> None:
    cfg.ensure_dirs()
    saved = save_raw(todos_payload([todo_row()]), cfg, SEMESTER, kind="lms_todo")
    assert saved == cfg.raw_homework_path(SEMESTER)
    assert load_raw(cfg, SEMESTER, kind="lms_todo")["todo_list"]


def test_unknown_kind_still_raises(cfg: Settings) -> None:
    cfg.ensure_dirs()
    with pytest.raises(ValueError):
        save_raw({}, cfg, SEMESTER, kind="whatever")


def test_snapshot_rotation_keeps_one_prev_generation(cfg: Settings) -> None:
    cfg.ensure_dirs()
    save_raw(todos_payload([todo_row()]), cfg, SEMESTER, kind="lms_todo")
    save_raw(todos_payload([]), cfg, SEMESTER, kind="lms_todo")
    assert load_raw(cfg, SEMESTER, kind="lms_todo") == {"todo_list": []}
    prev = json.loads(cfg.raw_homework_prev_path(SEMESTER).read_text(encoding="utf-8"))
    assert len(prev["todo_list"]) == 1


def test_missing_homework_snapshot_says_homework(cfg: Settings) -> None:
    """`load_raw` 的缺失文案是 `'考试' if kind == 'exams' else '课表'` 的三元式
    （`fetcher.py:614-620`）⇒ 加 kind 时必须一起扩，否则作业缺快照会报
    「本地没有课表缓存」，把用户支去重跑一次没必要的课表 fetch。"""
    cfg.ensure_dirs()
    with pytest.raises(TimetableFetchError, match="作业"):
        load_raw(cfg, SEMESTER, kind="lms_todo")
```

T4 起在 `tests/test_homework.py` 顶部补 `import json`、`from pathlib import Path`、
`from xjtu_calendar.config import Settings`、`from xjtu_calendar.errors import TimetableFetchError`、
`from xjtu_calendar.fetcher import load_raw, save_raw`；
`tests/homework_support.py` 补 `from pathlib import Path`、`from xjtu_calendar.config import Settings`、
`from xjtu_calendar.fetcher import save_raw`、`from subscribe_support import make_home`。
（`TimetableFetchError` 的抛出点已核实：`fetcher.py:617`。）

- [ ] **Step 2: 确认失败**（`AttributeError: 'Settings' object has no attribute 'raw_homework_path'`）
- [ ] **Step 3: 实现** —— `_raw_paths`（`fetcher.py:568`）加分支，docstring 说明「含作业标题与课程名，只应留在 home 下」。
  `_raw_paths` 现在是「`exams` 分支 + `!= "timetable"` 就抛」的形状（`fetcher.py:569-573`），
  加第三条分支时**别把 timetable 那条兜底写成 `else`**，未知 kind 必须继续抛。
  `load_raw` 的缺失文案三元式改成一张小表（`{"timetable": "课表", "exams": "考试", "lms_todo": "作业"}`），
  未知 kind 落在 `fetcher.py:572` 的 `ValueError` 上。
  `snapshot_age_days`（`subscribe.py:992`）的 kind 分支同步加上，两处都对未知 kind 抛 `ValueError`。
  写盘仍走 `save_raw` 内部已有的 `atomic_write_text(..., private=True)`（`fileutil.py:33`，chmod 0600 后再 `os.replace`），**不要绕过它自己写文件**。
  轮换契约照 `fetcher.py:576-604`：序列化 → 先把当前文件读进内存 → 原子写新 → 最后写 `.prev`。

`tests/homework_support.py` 追加（形状照 `exam_support.py:146-158` 的 `exam_home`）：

```python
def homework_home(tmp_path: Path, *rows, semester: str = SEMESTER) -> Settings:
    """带作业快照的 home：课表走 `subscribe_support.make_home` 的简写信封，作业快照手工落盘。"""
    cfg = make_home(tmp_path, semester=semester)
    save_raw(todos_payload(list(rows) or [todo_row()]), cfg, semester, kind="lms_todo")
    return cfg
```

- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_homework.py -k "raw_paths or roundtrip or unknown_kind or rotation" -q`
- [ ] **Step 5: 提交** — `git commit -m "feat(homework): lms_todo 快照路径与落盘"`

---

## Task 5: 第二个 host —— `lms_base`、`Endpoint.url(base_key=)`、`load_cookies(host=)`

**Files:**
- Modify: `src/xjtu_calendar/config.py:60-61, 70-81`（+`lms_base` 字段与 `XJTU_LMS_BASE`，照 `ehall_base`）
- Modify: `src/xjtu_calendar/fetcher.py:105-107`（`url()`）、`:162-183`（`_parse_endpoints` 认 `base` 键）、`:318-374`（`fetch_via_http`）
- Modify: `src/xjtu_calendar/auth.py:396-430`（`load_cookies` 的 host 参数化）
- Modify: `src/xjtu_calendar/data/ehall_endpoints.json`（+`lms_todos`）
- Create: `tests/test_homework_fetch.py`

**Interfaces:**
- Consumes: `auth.load_cookies`、`Settings.lms_base`、`fetcher.fetch_via_http`
- Produces: `Endpoint(name, method, path, description="", required=False, base_key="ehall_base")`、
  `Endpoint.url(cfg, base_key=None)`、`load_cookies(cfg: Settings | None = None, *, host: str | None = None)`、
  `fetcher.fetch_todos(cfg: Settings, *, transport: Any = None) -> Any`（内部按
  `require_endpoint(endpoints, "lms_todos")` 取端点）；端点逻辑名 `lms_todos`，
  `method = "GET"`，`path = "/api/todos"`，`base = "lms_base"`

这一步只解决「请求能不能打到 `lms.xjtu.edu.cn`、cookie 从哪儿来」，**不动 login 的用户交互**；
那半属于 D9，见本任务末尾的闸门。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_homework_fetch.py
import json
from pathlib import Path

import pytest

from homework_support import SEMESTER
from xjtu_calendar.auth import load_cookies
from xjtu_calendar.config import Settings
from xjtu_calendar.fetcher import Endpoint


def test_endpoint_can_target_the_lms_host(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path, lms_base="https://lms.xjtu.edu.cn")
    endpoint = Endpoint(name="lms_todos", method="GET", path="/api/todos", base_key="lms_base")
    assert endpoint.url(cfg) == "https://lms.xjtu.edu.cn/api/todos"


def test_ehall_endpoints_keep_the_default_base(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    endpoint = Endpoint(name="timetable", method="POST", path="/jwapp/sys/x.do")
    assert endpoint.url(cfg) == f"{cfg.ehall_base}/jwapp/sys/x.do"


def test_load_cookies_filters_by_the_host_you_ask_for(tmp_path: Path) -> None:
    state = {
        "cookies": [
            {"name": "a", "value": "1", "domain": ".ehall.xjtu.edu.cn", "path": "/"},
            {"name": "b", "value": "2", "domain": "lms.xjtu.edu.cn", "path": "/"},
        ]
    }
    cfg = Settings(home=tmp_path, lms_base="https://lms.xjtu.edu.cn")
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    cfg.state_path().write_text(json.dumps(state), encoding="utf-8")
    assert load_cookies(cfg, host="lms.xjtu.edu.cn") == {"b": "2"}
    assert load_cookies(cfg) == {"a": "1"}  # 不传 host ⇒ 与今天逐字一致


def test_bundled_registry_has_the_lms_endpoint(tmp_path: Path) -> None:
    endpoints = load_endpoints(cfg=Settings(home=tmp_path))
    assert endpoints["lms_todos"].base_key == "lms_base"
    assert endpoints["lms_todos"].path == "/api/todos"
```

`cfg.session_dir` 是 **property**（`config.py:98-102`），`cfg.state_path()` 是 **方法**
（`config.py:141-143`）——上面那段就是按这个写的，别改成 `cfg.session_dir()`。

- [ ] **Step 2: 确认失败**（`TypeError: Endpoint.__init__() got an unexpected keyword 'base_key'`）
- [ ] **Step 3: 实现**

`Endpoint` 加 `base_key: str = "ehall_base"`，`url()` 变成 `f"{getattr(cfg, self.base_key)}{path}"`；
`_parse_endpoints`（`fetcher.py:162-183`）把 JSON 里的 `base` 键映射成 `base_key`，缺省 `"ehall_base"`
⇒ 现有三条端点行为逐字不变，`tests/test_fetcher.py` 一个字都不改。
`load_cookies(cfg, *, host=None)`：给了 `host` 就按它过滤，没给沿用
`urlparse(cfg.ehall_base).hostname`（`auth.py:418`），丢弃日志（`auth.py:425-427`）措辞跟着换 host。
`fetch_via_http` 从 endpoint 取 `base_key` 推出 host 再传进 `load_cookies`。
`fetch_todos` 复用 `fetch_via_http(endpoint, cfg=cfg)`，**不传 `params`**（`/api/todos` 实测是裸 GET）。

`data/ehall_endpoints.json` 的 `endpoints` 数组追加一条：

```json
{
  "name": "lms_todos",
  "method": "GET",
  "base": "lms_base",
  "path": "/api/todos",
  "description": "思源学堂待办（含未提交作业截止时间）"
}
```

- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_homework_fetch.py tests/test_fetcher.py tests/test_auth_session.py -q`（后两个是既有用例，用来证明 eHall 侧没被动过）
- [ ] **Step 5: 提交** — `git commit -m "feat(fetch): 端点与会话支持第二个 host（LMS）"`

> **本任务的验收不含真实请求。** 读数 S1（门户 `login` 建的那个 profile 里有没有 LMS 域 cookie）
> **没拿到之前，不许把 `login` 扩展成「一次登录两个站」**——那是 D9 的内容。
> 现在能落的只有：没有 LMS cookie 时请求会失败、失败会落进 T2 的 `UNKNOWN`、快照不动。
>
> **失败形态已知缺陷（本任务只记录，不在这里修）**：未登录打 `/api/todos` 实测回
> `302 → 400 + Keycloak 登录页`，而 `fetch_via_http` 的 `follow_redirects=True`
> （`fetcher.py:364-374`）会一路跟到那个 400。后果是 400 落在非重试分支（`fetcher.py:424-427`）
> 抛 `TimetableFetchError`，**不是** `AuthenticationExpired`（那条只认 401，`fetcher.py:391-392`），
> 用户看到的是「接口返回 400」而不是「请重新登录」。另：`LOGIN_PAGE_MARKERS`
> （`fetcher.py:68-78`）九条对这个页面**零命中**（页面标题是英文 `Sign in to xjtu`），
> 所以即使某些路径回 200 也判不成登录页。
> 修法（加 marker + 把该端点的 3xx/400 归到会话失效）与它的判据一起放在 **D9 拍板之后**做，
> 因为它牵动的是共享的 `fetch_via_http`，不是作业侧。

---

## Task 6: `make_homework_uid` + `build_homework_events`

**Files:**
- Modify: `src/xjtu_calendar/homework.py`
- Modify: `tests/test_homework.py`、`tests/homework_support.py`（`homework_uids()`）

**Interfaces:**
- Consumes: `models.HomeworkItem`、`exporter.UID_DOMAIN`、`models.CalendarEvent`
- Produces: `HW_UID_NAMESPACE = "hw"`、`make_homework_uid(semester_key: str, item: HomeworkItem) -> str`、
  `build_homework_events(items: Sequence[HomeworkItem], semester_key: str) -> list[CalendarEvent]`

- [ ] **Step 1: 写失败测试**

把 `UID_DOMAIN`（`from xjtu_calendar.exporter import UID_DOMAIN`）与
`build_homework_events` / `make_homework_uid` / `parse_todo_rows`
（`from xjtu_calendar.homework import ...`）补进 `tests/test_homework.py` 顶部 import，
分组顺序照 Global Constraints 的 ruff `I` 条目。

```python
def test_uid_is_stable_across_deadline_change() -> None:
    """改期是原地更新，不是新事件（与考试侧同一取舍：时刻不进 UID）。"""
    before = parse_todo_rows(todos_payload([todo_row()]))[0]
    after = parse_todo_rows(todos_payload([todo_row(end_time="2030-06-20T15:59:00Z")]))[0]
    assert make_homework_uid(SEMESTER, before) == make_homework_uid(SEMESTER, after)
    assert make_homework_uid(SEMESTER, before).endswith(f"@{UID_DOMAIN}")


def test_uid_separates_semester_id_and_source_namespace() -> None:
    item = parse_todo_rows(todos_payload([todo_row()]))[0]
    assert make_homework_uid("2031-2032-1", item) != make_homework_uid(SEMESTER, item)
    other = parse_todo_rows(todos_payload([todo_row(id=900002)]))[0]
    assert make_homework_uid(SEMESTER, item) != make_homework_uid(SEMESTER, other)


def test_uid_does_not_collide_with_course_or_exam_uids() -> None:
    item = parse_todo_rows(todos_payload([todo_row()]))[0]
    exam = parse_exam_rows(exam_payload([exam_row()]))[0]
    assert make_homework_uid(SEMESTER, item) != make_exam_uid(SEMESTER, exam)


def test_events_are_timed_shanghai_datetimes() -> None:
    events = build_homework_events(parse_todo_rows(todos_payload([todo_row()])), SEMESTER)
    assert len(events) == 1
    assert isinstance(events[0].start, datetime)
    assert events[0].start.utcoffset() == HOURS8
    assert events[0].end - events[0].start == timedelta(minutes=30)
    assert events[0].status is None
    assert events[0].meeting is None


def test_summary_and_description_shape() -> None:
    event = build_homework_events(parse_todo_rows(todos_payload([todo_row()])), SEMESTER)[0]
    assert event.summary == "示例作业甲（示例课程甲）"
    assert event.location is None
    assert event.description == "课程号：203020301DEMO00000001"


def test_every_parsed_item_becomes_exactly_one_event() -> None:
    items = parse_todo_rows(todos_payload([todo_row(), todo_row(id=900002), todo_row(id=900003)]))
    events = build_homework_events(items, SEMESTER)
    assert len(events) == len(items) == 3
    assert len({e.uid for e in events}) == 3
```

- [ ] **Step 2: 确认失败**（`ImportError: make_homework_uid`）
- [ ] **Step 3: 写实现**

这里**不照抄** `build_exam_events` 的逐行 `try/except` 跳过分支。考试侧那个分支存在是因为它在
构建期才做 `date.fromisoformat` 与 `combine()`（`exams.py:552-560`）；作业的窗口在
`HomeworkItem.__post_init__` 就算完并验完了，`CalendarEvent` 的两条判据（同形态、`end > start`）
都被构造期挡住 ⇒ 那个 `except` 在这一支进不去。**不为进不去的分支写测试，也不留一个吞异常的口子。**

```python
#: 作业侧的 UID 命名空间段。先例是 `calendar_events.py:76` 的 ``UID_NAMESPACE = "cal"``。
HW_UID_NAMESPACE = "hw"


def make_homework_uid(semester_key: str, item: HomeworkItem) -> str:
    """``sha256("<学期>|hw|<course_code>|<id>")[:32]@xjtu-timetable-calendar``。

    ``course_code`` 前缀自带学期号（取证 §2 第 2 条）⇒ v0.8 那次「跨学期撞号」的根因
    在这一支天然不存在。**配方刻意不含时刻与标题**：改截止日、改标题都应当原地更新。

    D1 若拍「配方带版本号」，只改 :data:`HW_UID_NAMESPACE`（例如 ``"hw2"``）并同步
    本任务与 `tests/test_homework.py` 的期望值。**不要**去动课程侧的
    `UID_RECIPE_VERSION`（`exporter.py:79`），那会牵动课程 UID 的 legacy golden 用例。
    """
    payload = "|".join(
        [semester_key.strip(), HW_UID_NAMESPACE, item.course_code.strip(), item.homework_id.strip()]
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{digest}@{UID_DOMAIN}"


def build_homework_events(items: Sequence[HomeworkItem], semester_key: str) -> list[CalendarEvent]:
    """作业 → 单场、绝对时刻、无 RRULE / 无 VALARM 的 VEVENT。

    与 :func:`xjtu_calendar.exams.build_exam_events` 同形，但**没有逐行跳过的
    ``except``**：理由见本任务 Step 3（坏时刻在 :class:`HomeworkItem` 构造期就进不来）。
    """
    return [
        CalendarEvent(
            uid=make_homework_uid(semester_key, item),
            summary=f"{item.title.strip()}（{item.course_name.strip()}）",
            start=item.start,
            end=item.deadline,
            location=None,
            description=f"课程号：{item.course_code}",
            meeting=None,
        )
        for item in items
    ]
```

D7 若拍「标题不公开」，`summary` 换成 `f"作业（{item.course_name.strip()}）"` 或纯固定文案
（例如 `"课程作业截止"`），并在本任务测试里把 `test_summary_and_description_shape` 的期望同步改掉。
**注意连锁**：`summary` 进 `sequence.event_fingerprint`（`sequence.py:71-105`），一旦将来再改文案，
每条作业的 `SEQUENCE` 都会 +1、所有订阅者收到一次全量刷新——这正是不给 `except` 之外再加
「顺手 normalize」的原因。

- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_homework.py -k "uid or events or summary or exactly" -q`
- [ ] **Step 5: 提交** — `git commit -m "feat(homework): 作业 UID 与 VEVENT 构建"`

> **T6-B（只有 D2 拍成「全天」才用）**：把 `start/end` 换成 `item.deadline.date()` 与
> `item.deadline.date() + timedelta(days=1)`（RFC 5545 全天 `DTEND` 是排他界，见 `models.py:288-292`，
> 渲染层不再加天）。`HomeworkItem` 的 `start` 字段随之没有意义，要从固件里去掉，别留着当装饰。
> **代价必须先读**：全天事件进不了台账（`cancellation.py:66-69` 抛 `CalendarExportError`），
> 等于 D4 只能选 10A。别一边选全天一边等墓碑。
> 另一条**不构成反对理由**：`sequence.parse_baseline` 从 0.9-P1-01 起也收全天事件
> （`sequence.py:152-161` 的注释记的就是「曾经丢非 datetime、导致全天事件 SEQUENCE 恒 0」这个老坑），
> 所以这一支不会再踩那个坑，别按考试那一支的理由否掉它。

---

## Task 7: `exporter` 并入作业事件

**Files:**
- Modify: `src/xjtu_calendar/exporter.py:724-745`（签名 +`include_homework: bool = True`、
  `+homework_payload: dict[str, Any] | None = None`、`+homework_window_minutes: int | None = None`）
- Modify: `src/xjtu_calendar/exporter.py`（并入块放在 `:905` 那块之后、`:946` 之前；
  过滤在 `:955-961`；UID 唯一在 `:963-976` 与 `:1008-1020`；迁移并集在 `:1307`；
  `info` 在 `:1354-1377`）
- Create: `tests/test_exporter_homework.py`

**Interfaces:**
- Consumes: `homework.parse_todo_rows` / `build_homework_events` / `classify_todo_payload`（函数体内局部 import）
- Produces: `ExportResult.info["homework_events"]: int`、`info["homework_trusted"]: bool`、
  `ExportResult.homework_ledger_text: str | None`（**仅 T10B 会写**；10A 恒为 `None`）

顺序是这一支的全部风险所在，三条硬约束：

1. 作业事件必须在**日期过滤之前**并入，并快照一份 `pre_filter_homework_events`（对照考试侧
   `exporter.py:946-952` 的 `pre_filter_exam_events` / `live_exam_uids`）——台账与撤销通路要用
   **未经过滤**的集合（D19/CD14）；用了过滤后的，会把「本次没导出」当成「已经退场」。
2. `build_events()` 内部的 `seen_uids` 看不见外部并入的事件，`render_ics` 只 `add_component`，
   所以第四源要进 `:963-976` 那个「丢弃后来者 + warning」的 UID 唯一性循环，**照考试侧的写法**。
3. `:1307` 的 `live_uids=live_course_uids | live_exam_uids | calendar_uids` 是迁移候选的 live 侧。
   作业 UID 不加进去，一次配方迁移就会**给每条作业发撤销**。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_exporter_homework.py
import re

import pytest

import xjtu_calendar.homework as homework
from exam_support import STAMP, captured_logs
from homework_support import SEMESTER, homework_home, todo_row, todos_payload
from xjtu_calendar.exporter import build_ics_for_semester


def test_homework_events_land_in_the_ics(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    result = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert result.info["homework_events"] == 1
    assert result.info["homework_trusted"] is True
    assert "示例作业甲（示例课程甲）" in result.ics
    assert "TZID=Asia/Shanghai:20300617T233000" in result.ics
    assert "TZID=UTC" not in result.ics
    assert result.ics.count("BEGIN:VTIMEZONE") == 1


def test_no_homework_flag_drops_events_but_keeps_snapshot(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    result = build_ics_for_semester(cfg, SEMESTER, include_homework=False, dtstamp=STAMP)
    assert "示例作业甲" not in result.ics
    assert cfg.raw_homework_path(SEMESTER).is_file()  # 开关只影响导出，不删数据


def test_missing_snapshot_degrades_without_failing(tmp_path) -> None:
    cfg = homework_home(tmp_path)
    cfg.raw_homework_path(SEMESTER).unlink()
    result = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert result.info["homework_events"] == 0
    assert result.info["homework_trusted"] is False
    with captured_logs() as records:
        build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert any("作业" in rec.getMessage() for rec in records)  # 降级必须可见


def test_unparseable_snapshot_leaves_courses_alone(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    courses_only = build_ics_for_semester(cfg, SEMESTER, include_homework=False, dtstamp=STAMP)
    cfg.raw_homework_path(SEMESTER).write_text("{ not json", encoding="utf-8")
    result = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert result.info["homework_events"] == 0
    assert re.sub(r"DTSTAMP.*", "", result.ics, flags=re.M) == re.sub(
        r"DTSTAMP.*", "", courses_only.ics, flags=re.M
    )


def test_homework_uid_collision_is_dropped(tmp_path, monkeypatch) -> None:
    cfg = homework_home(tmp_path, todo_row())
    base = build_ics_for_semester(cfg, SEMESTER, include_homework=False, dtstamp=STAMP)
    course_uid = uid_lines(base.ics)[0]
    monkeypatch.setattr(homework, "make_homework_uid", lambda *_a, **_k: course_uid)
    result = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert uid_lines(result.ics).count(course_uid) == 1
    assert result.info["homework_events"] == 0
    with captured_logs() as records:
        build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert any("作业事件 UID 与已有事件冲突" in rec.getMessage() for rec in records)
```

`uid_lines(ics)` 是 T1 就在 `tests/homework_support.py` 里备好的 helper
（`re.findall(r"^UID:(.+)$", ics, re.M)`），替代在测试里手写正则——`tests/test_exporter_exams.py:50-78`
用同样的做法绕过 icalendar 的输出形状不确定性，那四个 helper 是那个文件的私有件，
不在 `exam_support` 里，所以这里自己带一份，**不要跨测试文件 import 下划线名**。

- [ ] **Step 2: 确认失败**（`TypeError: build_ics_for_semester() got an unexpected keyword 'include_homework'`）
- [ ] **Step 3: 实现** —— 整块用 `try/except Exception` 包住并降级成 `homework_events = []` +
  `homework_trusted = False`，措辞与降级路径逐字照 `exporter.py:874-940`；
  `homework_payload` 优先于读快照文件（考试侧 `exams_payload` 的同一条注入接缝，`exporter.py:764-765`）。
  `include_homework=False` 时整块跳过，**不建台账、不动台账**（对照 `exporter.py:1071` 与 `:1174`）。
  日期过滤处加 `homework_events = [e for e in homework_events if _in_range(e, lower, upper)]`（`:961` 之后）。
  `render_events` 组装（`:1007` / `:1008-1020`）把 `kept_homework` 接进去，UID 唯一判据同一条循环。
  `:1307` 的并集加 `| live_homework_uids`。
  `info` 里加 `homework_events` 与 `homework_trusted`，`sequence_stats` 的入参列表
  （`exporter.py:1377`）加 `*kept_homework`，否则作业事件不进 SEQUENCE 统计。
- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_exporter_homework.py tests/test_export_build.py tests/test_legacy_course_export_golden.py tests/test_legacy_exam_export_golden.py -q`
  （后三个是既有用例，专门用来证明课程/考试产物一字未动）
- [ ] **Step 5: 提交** — `git commit -m "feat(export): 作业事件并入同一份 ics"`

---

## Task 8: `--no-homework` 贯穿 fetch / export / push / rotate

**Files:**
- Modify: `src/xjtu_calendar/cli.py:115`（fetch parser）、`:150`（export）、`:229`（push）、`:257`（rotate）
- Modify: `src/xjtu_calendar/cli.py:438`、`:496-508`（fetch 分支）、`:636`、`:1467`、`:1932`、`:1954-1962`、`:2031-2038`、`:2078-2085`
- Modify: `tests/homework_support.py`（CLI spy 与 argv helper）
- Create: `tests/test_cli_homework.py`

**Interfaces:**
- Consumes: `build_ics_for_semester(..., include_homework=...)`（T7）、`fetch_todos`（T5）
- Produces: `cli._fetch_homework(cfg: Settings, semester: str, *, reason_if_skipped: str | None = None) -> Path | None`、
  `_subscribe_rotate(..., include_homework: bool = True)` 关键字参数

四个旗标位置与 `--no-exams` **一一对应**，help 文案照同一口径写清「默认带 / 本地快照不删」。
两处最容易漏：`getattr(args, "include_homework", ...)` 的兜底（手工 `Namespace` 在用例里存在，
`cli.py:634-635` 的注释记的就是这事），以及 rotate（`cli.py:1967` 那条教训）。

- [ ] **Step 1: 先把 CLI helper 放进固件**

`tests/homework_support.py` 追加（形状照 `tests/test_cli_exams.py:50-95`，**不改那个文件**，
理由：它已全绿，跨测试文件 import 下划线名比留两份更脆）：

```python
BLANK_ICS = "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"


def register_subscription(cfg: Settings, semester: str = SEMESTER) -> None:
    """跳过 `subscribe init`（它要真远端），直接落一份订阅状态。"""
    subscribe.save_state(
        cfg,
        subscribe.SubscriptionState(
            semester=semester,
            repo_url="https://example.invalid/calendar.git",
            branch="main",
            token="deadbeef",
            url_base="https://me.github.io/timetable/",
        ),
    )


def export_argv(semester: str = SEMESTER, out: str = "out.ics") -> list[str]:
    return ["export", "--semester", semester, "-o", out]


def subscribe_argv(action: str, semester: str = SEMESTER) -> list[str]:
    return ["subscribe", action, "--semester", semester]
```

`tests/test_cli_homework.py` 的两个 fixture（`home` 与 `spy`）照 `test_cli_exams.py:36-47` 与
`:50-71` 写，差别只有：`home` 用 `homework_home(tmp_path)` 落快照，`STUB_INFO` 多一个
`"homework_events": 1` 与 `"homework_trusted": True`（`cmd_export` 打印用的键凑不齐就 KeyError，
那条注释在 `test_cli_exams.py:25-33`）。

- [ ] **Step 2: 写失败测试**

```python
# tests/test_cli_homework.py
import pytest

from homework_support import BLANK_ICS, SEMESTER, export_argv, register_subscription, subscribe_argv
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings


@pytest.mark.parametrize("command", ["export", "push", "rotate"])
def test_homework_flag_reaches_every_builder(command, spy, home) -> None:
    cfg = Settings.load()
    if command == "export":
        argv = export_argv(str(home / "out.ics"))
    else:
        register_subscription(cfg)
        argv = subscribe_argv(command)
        if command == "rotate":
            cfg.subscribe_dir.mkdir(parents=True, exist_ok=True)
            (cfg.subscribe_dir / f"last-{SEMESTER}.ics").write_text(
                BLANK_ICS, encoding="utf-8", newline=""
            )

    assert main(argv) == 0
    assert spy[-1]["include_homework"] is True
    spy.clear()

    assert main([*argv, "--no-homework"]) == 0
    assert spy[-1]["include_homework"] is False


def test_fetch_without_flag_calls_the_lms_endpoint(spy, home, monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        "xjtu_calendar.cli._fetch_homework",
        lambda *_a, **_k: called.append("homework") or None,
    )
    assert main(["fetch", "--semester", SEMESTER]) == 0
    assert called == ["homework"]


def test_fetch_no_homework_flag_never_requests_it(spy, home, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("带 --no-homework 不该打 LMS")

    monkeypatch.setattr("xjtu_calendar.fetcher.fetch_todos", boom)
    assert main(["fetch", "--semester", SEMESTER, "--no-homework"]) == 0
```

`cmd_fetch` 还需要既有的课表端点可用——`test_cli_exams.py` 走的是 `--from-file` 或 monkeypatch
`fetch_via_http`；照它那一个文件里已经跑通的那条路写，别在这里发明第三种。

- [ ] **Step 3: 确认失败**（`AttributeError: 'Namespace' object has no attribute 'no_homework'`）
- [ ] **Step 4: 实现** —— 四处 parser + 全部消费点改成 `include_homework=not args.no_homework`；
  `_fetch_homework` 仿 `_fetch_exams`（`cli.py:559-577`）：先看 `reason_if_skipped` ⇒ 记一行返回 `None`，
  再抓；**浏览器分支不抓作业**，与考试侧同一处置（`cli.py:496-508` 那条注释的理由原样搬）。
  `UNKNOWN` 不覆盖快照的落盘矩阵照 `test_exams_fetch.py::test_expired_session_is_unknown_not_empty`
  的写法补一条作业侧用例。
- [ ] **Step 5: 定向测试** — Run: `pytest tests/test_cli_homework.py tests/test_cli_exams.py tests/test_cli_subscribe.py -q`
- [ ] **Step 6: 提交** — `git commit -m "feat(cli): --no-homework 贯穿四个子命令"`

> **D5 的默认值不由本任务决定。** 上面写的是「默认并入」（`include_homework: bool = True`）。
> 若拍成「默认不带」，改动面 = 四个 parser 的默认 + `build_ics_for_semester` 默认参数 +
> 本任务测试的期望三处，**并且 D8 的判定时点跟着挪**到「用户第一次开启作业」那次发布
> （那才是一次用户可见的产物变化）。拍板时在本任务上留一句话即可，不必重写。

---

## Task 9: `diff_homework` 与「作业变更」小节

**Files:**
- Modify: `src/xjtu_calendar/homework.py`
- Modify: `src/xjtu_calendar/cli.py`（`cmd_diff` 里考试小节旁边；行号以实际代码为准，考试侧的入口在 `tests/test_diff_exams.py` 有用例可对照）
- Modify: `tests/test_homework.py`

**Interfaces:**
- Consumes: `HomeworkItem`、`make_homework_uid`
- Produces: `HW_KIND_ADDED = "新增"`、`HW_KIND_REMOVED = "消失"`、`HW_KIND_DEADLINE = "截止变更"`、
  `HW_KIND_TITLE = "标题变更"`、`HW_CHANGE_KINDS` 元组、`HomeworkChange`
  （`kind`、`course_name`、`title`、`when`、`old`、`new`）、`HomeworkDiff`（`.changes`、`.is_empty`）、
  `diff_homework(old: Sequence[HomeworkItem], new: Sequence[HomeworkItem]) -> HomeworkDiff`、
  `describe_homework_change(change: HomeworkChange) -> str`

配对键**就是 UID 的身份令牌**（照 `_exam_key` 与 `_uid_token` 同源那条评审结论 R-G7）：
本侧即 `f"{HW_UID_NAMESPACE}|{course_code}|{homework_id}"`，且 UID 与键两处必须调同一个私有
helper `_hw_token(item)`，不许各写一遍配方。

- [ ] **Step 1: 写失败测试**

```python
def test_submitted_homework_reads_as_removed() -> None:
    before = parse_todo_rows(todos_payload([todo_row(), todo_row(id=900002)]))
    after = parse_todo_rows(todos_payload([todo_row(id=900002)]))
    diff = diff_homework(before, after)
    assert [c.kind for c in diff.changes] == [HW_KIND_REMOVED]


def test_deadline_change_is_not_a_new_event() -> None:
    before = parse_todo_rows(todos_payload([todo_row()]))
    after = parse_todo_rows(todos_payload([todo_row(end_time="2030-06-20T15:59:00Z")]))
    assert [c.kind for c in diff_homework(before, after).changes] == [HW_KIND_DEADLINE]


def test_title_change_is_its_own_kind() -> None:
    before = parse_todo_rows(todos_payload([todo_row()]))
    after = parse_todo_rows(todos_payload([todo_row(title="示例作业丙")]))
    assert [c.kind for c in diff_homework(before, after).changes] == [HW_KIND_TITLE]


def test_first_snapshot_lists_every_item_as_added() -> None:
    items = parse_todo_rows(todos_payload([todo_row(), todo_row(id=900002)]))
    assert [c.kind for c in diff_homework([], items).changes] == [HW_KIND_ADDED, HW_KIND_ADDED]
    assert not diff_homework([], []).changes


def test_change_lines_carry_no_original_text() -> None:
    before = parse_todo_rows(todos_payload([marker_row()]))
    after = parse_todo_rows(todos_payload([marker_row(end_time="2030-06-20T15:59:00Z")]))
    lines = [describe_homework_change(c) for c in diff_homework(before, after).changes]
    assert leaked(lines) == []
    assert lines and lines[0]  # 「为空」不能是因为什么都没输出
```

- [ ] **Step 2: 确认失败** — Run: `pytest tests/test_homework.py -k "removed or deadline_change or title_change or as_added or original_text" -q`
- [ ] **Step 3: 实现** —— `_hw_group` 的排序键**必须包含被比较的字段**（`when`、`title`），
  否则同键多行只是换了顺序时，逐位配对会报出幻影变更（`exams.py:448-466` 的评审 F4 就是这条）。
  输出顺序 = `HW_CHANGE_KINDS.index(kind)`，再按时刻、再按课程名（照 `_exam_sort_key`）。
  首次快照（旧侧为空）不需要特殊分支：同一套算法自然得到「全部新增」，
  这是 spec 里考试侧 §6.6 禁止「没基线就静默跳过」的同一条理由。
- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_homework.py -k diff tests/test_diff.py tests/test_diff_exams.py -q`
- [ ] **Step 5: 提交** — `git commit -m "feat(diff): 作业变更小节"`

> **`HW_KIND_REMOVED` 的可见语义取决于 D4**：diff 只负责「说出来」，但把「消失」印成一行，
> 用户在 10A 下读到的是「日历上那条会自己没了」，在 10B 下读到的是「会变成一条已取消的活动」。
> 本任务先把数据结构与配对算法落定，**`describe_homework_change` 的文案随 T10 的拍板结果定**，
> 现在写成中性的「消失：〈课程名〉〈标题〉」并在函数 docstring 里写明这一句随 D4 改。

---

## Task 10A: 退场通路 A —— 消失即不发（依赖 D4 + 读数 S2）

**Files:**
- Modify: `src/xjtu_calendar/cli.py`（`_fetch_homework` 的 HAS/EMPTY/UNKNOWN 落盘矩阵）
- Modify: `tests/test_exporter_homework.py`、`tests/test_homework_fetch.py`

**语义**：这一版 `todo_list` 里没有 ⇒ 产物里就没有。清理完全外包给客户端 prune。
作业侧**不建台账文件**，`ExportResult.homework_ledger_text` 恒为 `None`。

**已知代价（拍板时必须一起读）**：ICSx⁵ 真机判过会 prune（followups §13 第 2 格，10-19）；
ColorOS 原生订阅**永不删消失项**（同节第 4 格反向）；手表那类镜像端一次都没验过、
且连手删入口都没有（§13 补记 10-21）。⇒ 10A 不是「最干净」，是
「强客户端上真干净、弱客户端上留残渣」，而且残渣是**已完成作业的真实标题**
（不像墓碑那样是固定文案，脱敏与否在这里差别最大）。
（10-25 更新：**显示层已收回**——用户报手表现在也不显示「已取消的活动」，与手机一致；
`prune` 仍无读数，手删入口仍然没有。本节其余判据不变。）

- [ ] **Step 1: 写失败测试**

```python
def test_absent_homework_is_neither_live_nor_cancelled(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    first = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert "示例作业甲（示例课程甲）" in first.ics

    save_raw(todos_payload([]), cfg, SEMESTER, kind="lms_todo")  # 模拟「已经交了」
    second = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP)
    assert "示例作业甲" not in second.ics
    assert "STATUS:CANCELLED" not in second.ics
    assert second.homework_ledger_text is None
    assert not (cfg.subscribe_dir / f"last-homework-{SEMESTER}.ics").exists()


def test_failed_fetch_leaves_snapshot_untouched(tmp_path, monkeypatch) -> None:
    cfg = homework_home(tmp_path, todo_row())
    before = cfg.raw_homework_path(SEMESTER).read_bytes()
    before_prev = cfg.raw_homework_prev_path(SEMESTER).read_bytes()

    def boom(*_a, **_k):
        raise TimetableFetchError("接口返回 400：lms_todos")

    monkeypatch.setattr("xjtu_calendar.fetcher.fetch_todos", boom)
    assert _fetch_homework(cfg, SEMESTER) is None
    assert cfg.raw_homework_path(SEMESTER).read_bytes() == before
    assert cfg.raw_homework_prev_path(SEMESTER).read_bytes() == before_prev
```

`before_prev` 那条要求 fixture 里 `homework_home` 至少落过两次快照（第一次没有 `.prev`）；
在 `homework_home` 里加一个 `generations: int = 2` 参数，循环 `save_raw` 落两代，别在断言里跳过。

- [ ] **Step 2: 确认失败** — Run: `pytest tests/test_exporter_homework.py tests/test_homework_fetch.py -k "absent or untouched" -q`
- [ ] **Step 3: 实现** —— 要点只有一条：**不建台账**。落盘矩阵照 `_fetch_exams`（`cli.py:559-577`）：
  `HAS`/`EMPTY` ⇒ `save_raw`；`UNKNOWN` ⇒ 记一行返回 `None`，旧快照与 `.prev` 都不动。
  `homework_trusted` 只用于告警与 diff 措辞，**不用于注入墓碑**。
- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_exporter_homework.py tests/test_homework_fetch.py tests/test_cli_homework.py -q`
- [ ] **Step 5: 提交** — `git commit -m "feat(homework): 消失即不发（退场通路 A）"`

---

## Task 10B: 退场通路 B —— 消失即发墓碑（依赖 D4 + 读数 S2）

**Files:**
- Modify: `src/xjtu_calendar/homework.py`（`load_homework_ledger = partial(load_ledger, label="作业台账")`）
- Modify: `src/xjtu_calendar/config.py:170-188`（+`homework_ledger_path(semester_key)` =
  `subscribe/last-homework-<key>.ics`，docstring 照 `exam_ledger_path` 那一段：含作业原名，**永不发布**、只留本地、写盘 `private=True`）
- Modify: `src/xjtu_calendar/exporter.py`（把 `:1081-1175` 那整块考试撤销结构复制成作业侧，
  参数换成 `live_homework_uids` / `pre_filter_homework_events` / `homework_ledger_text`；
  课程侧迁移那块的分母用 `upcoming_ledger_count`，见 `:1263-1264`）
- Modify: `src/xjtu_calendar/cli.py`（`_write_homework_ledger`，照 `cli.py:773` 的 `_write_exam_ledger`）
- Modify: `tests/homework_support.py`（`write_homework_ledger`）
- Create: `tests/test_homework_ledger.py`

**语义**：上一版快照有、这一版没有 ⇒ 台账留一条 ⇒ 产物里下发同 UID 的 `STATUS:CANCELLED`；
原定时刻已过的条目既不下发也不保留（`cancellation.py:199` 与 `:212-213`，D2/D9）。

**两条必须先读到的代价**：

1. 墓碑标题是固定文案「已取消的活动」（`cancellation.py:233`）。所以手机上会多出和现在那 69 条
   同形的一批；而「幽灵顺 feed → 手机 → 手表 这条镜像链往下沉，每往下一层少一个清理入口」
   是 10-21 已经写进 spec D4 的代价。
   （10-25 更新：显示层已收回——用户报手表现在也不显示「已取消的活动」，与手机一致；
   `prune` 与手删出口仍无读数，D4 的判据不变。）
2. **作业特有的错配**：窗口只有 30 分钟，`cancel_candidates` 的过期判据看的是 `item.start`，
   所以「交了但截止已过」那条会直接退出台账、**连墓碑都不发**。语义上这是对的
   （过期条目不打扰，与 §8 同一口径），但它意味着 10B 的墓碑数远小于消失数，
   **验收时不要按消失条数对账**，要按「未过期且消失」条数对账。

- [ ] **Step 1: 先把台账 helper 放进固件**

`tests/homework_support.py` 追加（照 `exam_support.py:161-169` 的 `write_ledger`，
`newline=""` 那一条不能省——CRLF 是 RFC 5545 的要求，被 git 的 `core.autocrlf` 压过就废了）：

```python
def write_homework_ledger(cfg: Settings, semester: str, text: str) -> Path:
    path = cfg.homework_ledger_path(semester)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")
    return path
```

- [ ] **Step 2: 写失败测试**

```python
# tests/test_homework_ledger.py
import pytest

from exam_support import NOW, PAST, STAMP
from homework_support import SEMESTER, homework_home, todo_row, todos_payload, write_homework_ledger
from xjtu_calendar.cancellation import CANCELLATION_SUMMARY
from xjtu_calendar.errors import MassCancellationBlocked
from xjtu_calendar.exporter import build_ics_for_semester
from xjtu_calendar.fetcher import save_raw


def test_submitted_homework_gets_a_tombstone(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    first = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW)
    assert first.homework_ledger_text is not None
    write_homework_ledger(cfg, SEMESTER, first.homework_ledger_text)

    save_raw(todos_payload([]), cfg, SEMESTER, kind="lms_todo")
    second = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW)
    assert CANCELLATION_SUMMARY in second.ics
    assert "STATUS:CANCELLED" in second.ics
    assert "示例作业甲" not in second.ics  # 墓碑不带原标题


def test_expired_item_emits_nothing(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    first = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW)
    write_homework_ledger(cfg, SEMESTER, first.homework_ledger_text)
    save_raw(todos_payload([]), cfg, SEMESTER, kind="lms_todo")
    result = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=PAST)
    assert "STATUS:CANCELLED" not in result.ics


def test_mass_disappearance_is_blocked_then_allowed(tmp_path) -> None:
    rows = [todo_row(id=900000 + n) for n in range(12)]
    cfg = homework_home(tmp_path, *rows)
    first = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW)
    write_homework_ledger(cfg, SEMESTER, first.homework_ledger_text)
    save_raw(todos_payload([]), cfg, SEMESTER, kind="lms_todo")

    with pytest.raises(MassCancellationBlocked):
        build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW)
    ok = build_ics_for_semester(
        cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW, allow_mass_cancellation=True
    )
    assert ok.ics.count("STATUS:CANCELLED") == 12


def test_no_homework_flag_leaves_ledger_untouched(tmp_path) -> None:
    cfg = homework_home(tmp_path, todo_row())
    first = build_ics_for_semester(cfg, SEMESTER, dtstamp=STAMP, cancel_expiry_at=NOW)
    path = write_homework_ledger(cfg, SEMESTER, first.homework_ledger_text)
    before = path.read_bytes()
    build_ics_for_semester(cfg, SEMESTER, include_homework=False, dtstamp=STAMP)
    assert path.read_bytes() == before
```

`NOW`/`PAST` 复用 `exam_support.py:173-174`（它们本来就是为 2030-06-17 这个基准日配的墙钟）。
`MASS_CANCELLATION_MIN_EVENTS = 10`（`courses.py:49`），所以批量那条用例必须有 ≥10 条；
判据调用形状是 `mass_cancellation_check(candidates=len(...), ledger_total=upcoming_ledger_count(...))`
——两个参数都是 **int**，不是集合（`courses.py:84`）。

- [ ] **Step 3: 确认失败** — Run: `pytest tests/test_homework_ledger.py -q`，Expected: `AttributeError: homework_ledger_path`
- [ ] **Step 4: 实现** —— 撤销注入块的条件照考试侧两条：`if include_homework and homework_trusted:`，
  台账文本产出的条件照 `exporter.py:1158` 的 `if ledger_exists or pre_filter_homework_events:`，
  读台账失败就地降级成「当作没有台账」（`exporter.py:1095-1102` 那段红线的理由，一字不改地搬）。
  `ExportResult.homework_ledger_text` 加进 dataclass（`:604-608` 那两条同形注释）。
  `cli._write_homework_ledger` 里 `None ⇒ 这次别动文件`（对照 `cli.py:1756` 与 `:1806` 的两条注释）。
- [ ] **Step 5: 定向测试** — Run: `pytest tests/test_homework_ledger.py tests/test_exporter_cancellation.py tests/test_cli_cancellation.py tests/test_exam_ledger.py -q`
- [ ] **Step 6: 提交** — `git commit -m "feat(homework): 消失即发墓碑（退场通路 B）"`

---

## Task 11: 陈旧口径、告警与 doctor

**Files:**
- Modify: `src/xjtu_calendar/homework.py`（`homework_snapshot_lag_days`）
- Modify: `src/xjtu_calendar/cli.py:650-671`（考试陈旧告警那一块旁边加作业侧一条）
- Modify: `src/xjtu_calendar/doctor.py`（作业快照缺失 / LMS 会话缺失的检查项）
- Modify: `tests/test_homework.py`、`tests/test_doctor.py`

**Interfaces:**
- Consumes: `subscribe.snapshot_age_days(cfg, semester, kind="lms_todo")`（T4/T5 之后可用）
- Produces: `homework_snapshot_lag_days(cfg: Settings, semester: str) -> float | None`

mtime→天数的算式唯一归属点是 `subscribe.snapshot_age_days`，作业侧**只作差**，
不要自己再读一次 mtime（`exams.py:593-595` 那段「两处不合并」的理由同样适用，别抄第三份）。

- [ ] **Step 1: 写失败测试**

```python
def test_lag_is_zero_when_homework_is_fresher_than_timetable(cfg: Settings) -> None:
    cfg.ensure_dirs()
    save_raw(todos_payload([todo_row()]), cfg, SEMESTER, kind="lms_todo")
    save_raw({"kbList": [payload_row()]}, cfg, SEMESTER, kind="timetable")
    old = cfg.raw_timetable_path(SEMESTER).stat().st_mtime - 5 * 86400
    os.utime(cfg.raw_timetable_path(SEMESTER), (old, old))
    assert homework_snapshot_lag_days(cfg, SEMESTER) == 0.0


def test_lag_reports_positive_gap(cfg: Settings) -> None:
    cfg.ensure_dirs()
    save_raw(todos_payload([todo_row()]), cfg, SEMESTER, kind="lms_todo")
    save_raw({"kbList": [payload_row()]}, cfg, SEMESTER, kind="timetable")
    old = cfg.raw_homework_path(SEMESTER).stat().st_mtime - 3 * 86400
    os.utime(cfg.raw_homework_path(SEMESTER), (old, old))
    assert 2.9 < homework_snapshot_lag_days(cfg, SEMESTER) < 3.1


def test_missing_snapshot_returns_none(cfg: Settings) -> None:
    cfg.ensure_dirs()
    assert homework_snapshot_lag_days(cfg, SEMESTER) is None


def test_no_homework_flag_silences_the_staleness_warning(spy, home) -> None:
    assert main([*export_argv(str(home / "out.ics")), "--no-homework"]) == 0
    with captured_logs() as records:
        assert main(export_argv(str(home / "out.ics"))) == 0
    assert not [rec for rec in records if "作业快照" in rec.getMessage()]
```

最后一条的两段顺序不能换：旗标关掉时**任何**「作业可能已过期」的提示对本次运行都是谎话
（`cli.py:650-651` 对考试侧写的就是这条理由，`--no-exams` 那时整块跳过）。

- [ ] **Step 2: 确认失败** — Run: `pytest tests/test_homework.py -k "lag or none or staleness" -q`
- [ ] **Step 3: 实现** —— `homework_snapshot_lag_days` 照 `exams.py:586-604`；
  doctor 里两条检查项的措辞给补救指引（缺快照 ⇒ 提示 `fetch`；缺 LMS 会话 ⇒ 提示 `login`，
  但**不许**在 D9 未拍前提示任何不存在的子命令或旗标）。
- [ ] **Step 4: 定向测试** — Run: `pytest tests/test_homework.py tests/test_doctor.py -q`
- [ ] **Step 5: 提交** — `git commit -m "feat(homework): 快照陈旧口径与 doctor 检查项"`

---

## Task 12: 文档、版本与发布就绪

**Files:**
- Modify: `README.md`（「作业截止」小节 + 旗标表加 `--no-homework`）
- Modify: `docs/user-guide/subscription.md`（「先选一条托管路」那段不动，只在数据来源处补一句）
- Modify: `skills/xjtu-timetable-setup/SKILL.md`（第 7 步那三条硬约束旁边加作业侧的一条）
- Modify: `CHANGELOG.md` 的 `[Unreleased]`
- Modify: `pyproject.toml`（版本号按 D8 走 **minor**）

内容口径五条：

- 写清「这一支的数据来自思源学堂，与门户课表是两个源、两套会话」，以及 LMS **没有「已提交」字段**
  ⇒ 日历上那条的退场语义取决于 D4。按拍板结果写，**不写「会自动消失」这种跨客户端通用承诺**
  （ICSx⁵ 会 prune、ColorOS 不会、镜像端未知，这三条在 followups §13 里有账）。
- 不写「任何上游改动都能到手表」这类未验结论；镜像端的 prune 行为至今没有读数。
- 不出现真实课程号 / 真实截止时刻 / 真实作业标题；示例一律 `2030-*` 合成值。
- 已知限制里写一句：`end_time` 带秒时产物保留秒，我们不做分钟级归一化（T3 的留白）。
- 面向用户文案为中文，风格对照 README 既有章节（fail-closed、给补救指引、不假装成功）。
  成稿前按 `humanizer-zh` 过一遍表达，只改措辞，事实、表格、路径与代码围栏不动。

- [ ] **Step 1: 逐条打勾下面的「验收清单」**
- [ ] **Step 2: 全量四条门禁 + 第五条空白检查**（在主检出、合并态上跑）

```bash
python -m pytest -q --basetemp=_notes/pytest-tmp
python -m mypy
python -m ruff check .
python -m ruff format --check .
git diff --check <远端旧 tip> HEAD
```

- [ ] **Step 3: 隐私 grep** —— 跟踪内容里搜绝对路径盘符、姓名/学号类字段、真实 `course_code`
  前缀（`20262027 1COMP` 这类）、真实时刻 `15:59:00Z`、编辑器与自动化工具品牌名。
- [ ] **Step 4: 干净检出复验** —— `git worktree add --detach <临时目录> HEAD` 后在里面跑四条门禁；
  `_notes/` 在新检出里不存在，`--basetemp` 要指到仓库外，用完删掉临时 worktree。
- [ ] **Step 5: 真实序列彩排**（AGENTS.md 收尾自检点名那道，单元测试全绿不算）——
  一次性 home + 本地裸仓库（`file://`）、零网络；顺序 = 旧版本发布 → 迁移通路 → 数据不变再发布 →
  **带过期退场的发布贴着 push 重跑彩排**（退场判据是日期依赖，10-21 那两天里 3 条自己跨过了时刻）。
  要发到真机观察时，把身份字段（课程名 / 课程号 / 教师 / 教室 / 姓名 / 学号 / 作业标题）换成
  **单射**的合成值、星期/节次/周次保持真实，并对线上字节做子串断言证明零命中。
- [ ] **Step 6: 提交** — `git commit -m "docs(homework): 作业接入说明、旗标与变更记录"`
- [ ] **Step 7: 推送 / tag / Release / PyPI** —— 每一步各自等用户明确授权，PyPI 不可撤回。

---

## 验收清单（全分支终审前）

- [ ] 课程与考试的 legacy golden 用例逐字节不变（新增第四源最容易碰的就是这个）。
- [ ] 产物里作业事件是 `TZID=Asia/Shanghai` 的 DATE-TIME，VTIMEZONE 只有一块，无 `TZID=UTC`。
- [ ] `include_homework=False` 时：产物无作业事件、无作业墓碑、台账文件不创建也不改写、快照不删。
- [ ] 任何 fetch 失败 / `UNKNOWN` ⇒ 快照与 `.prev` 字节不变；`fetch`/`export`/`push` 不因作业失败而非零退出。
- [ ] `exporter.py:1307` 的迁移 `live_uids` 里含作业 UID（漏了就会在一次配方迁移里给每条作业发撤销）。
- [ ] `sequence_stats` 的入参列表里含作业事件（否则作业永远不进统计，等于没并进来）。
- [ ] `--no-homework` 在 fetch / export / push / **rotate** 四处都生效，`tests/test_cli_homework.py` 有参数化用例。
- [ ] 三条泄漏判据用例全过：`report.skipped`、构建期丢弃日志、`describe_homework_change` 输出。
- [ ] D2 与 D4 的组合**自洽**（选了全天就不能同时要墓碑）。
- [ ] 若走 10B：墓碑条数按「未过期且消失」对账，不按消失总数对账；`MassCancellationBlocked` 在 ≥10 条批量消失时确实拦。
- [ ] 文档里没有真实课程号/截止时刻，没有本机绝对路径与工具品牌名。
- [ ] 四条门禁 + `git diff --check` 在**主检出**的合并结果上是绿的；CI 那侧另看（Linux + shallow checkout 会暴露本地看不到的缺陷）。
