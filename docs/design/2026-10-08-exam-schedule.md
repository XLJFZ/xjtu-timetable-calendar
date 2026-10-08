# 考试安排接入（`exams` 数据源）— 设计规格

日期：2026-10-08　状态：待评审
作者：与用户逐条确认而成（决策见 §2）
关联：`docs/design/2026-10-07-url-subscribe-ics.md`（订阅通道）、`_notes/exam-endpoint-findings.md`（真实观测记录）

---

## 1. 背景与目标

用户的手写课表只覆盖"上课"，而期末考试的时间、教室、座位号同样只在教务系统里，
每学期末要手工抄一遍。目标：**把「我的考试安排」接进现有管线**，让 `fetch` → `export`
→ `subscribe push` 一次跑完就同时得到课程事件与考试事件。

接口路径与字段形态来自 2026-10-08 的真实观测（浏览器旁观捕获 + 用同一份隔离会话
直查历史学期），**不是猜测**；观测记录见 `_notes/exam-endpoint-findings.md`。

## 2. 已锁定的需求决策（用户逐条确认）

| # | 决策 | 用户选择 |
| --- | --- | --- |
| D1 | 考试事件放哪 | **并进同一份 .ics / 同一个订阅 URL**，不新增第二份日历（`subscribe` 的"一学期一 token"形状不变） |
| D2 | 默认开关 | **默认包含**，`--no-exams` 关闭；数据为空时静默跳过，只在日志说明 |
| D3 | 提醒 | **不写 VALARM**，提醒交给客户端设置 |
| D4 | 考试源抓取失败 | **宁缺毋滥**：不导出考试；但"状态未知"时不得删除已有信息（见 §7 三态判定） |
| D5 | UID 依据 | 用接口的行 ID `WID`（已实测跨请求逐字符稳定），改时间/换教室不产生新事件 |

## 3. Non-goals（明确不做）

- **不做考试与上课之间的冲突检测**（考试撞上别的课，客户端自己看得见）。
- **不把考试纳入教学周展开、`excluded_dates`、调课 override**：考试是接口直接给出的
  绝对日期，套用停课/调课逻辑是错的。
- **不做考试座位的可视化/选座提醒**，座位号只进 `DESCRIPTION`。
- **不支持缓考/补考的独立数据源**（`KSMC` 里若出现"补考"，按普通考试事件导出，
  名称原样带上）。
- **不把 `cxwapdksrw.do`（按考试批次查询）纳入 v1**：主接口已含全部所需字段，
  批次接口留作扩展点（§11）。

## 4. 接口观测事实（2026-10-08）

```
POST /jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do
form: XNXQDM=<学期代码>            （前端另带 *order，省略仍返回 200）
resp: {"code":"0","datas":{"wdksap":{"totalSize","pageSize","rows":[...],
       "extParams":{"msg","code","logId"}}}}
```

外壳与课表 `xskcb.do` **同构**，`fetcher.fetch_via_http` 未改动即可取到数据。
真值样例（`2025-2026-2`，7 行）：

| 字段 | 含义 | 观测值 |
| --- | --- | --- |
| `KCM` / `KCH` | 课程名 / 课程号 | `<课程名>` / `ARCH000000` |
| `KSMC` | 考试名称 | `2025-2026学年 第二学期 结课考试` |
| `KSRQ` | 考试日期 | `YYYY-MM-DD 00:00:00`（**时间部分恒 00:00:00**） |
| `KSSJMS` | 时间描述 | `YYYY-MM-DD 15:00-17:30(星期二)` |
| `JASMC` | 教室 | `N-6NNN` / `<中心名>-3NN` / `N-1WNNN` |
| `ZWH` | 座位号 | `NN` |
| `XXXQDM` | 校区代码 | `5`（创新港） |
| `WID` / `KSDM` / `KSRWID` | 行 ID / 批次 / 任务 ID | 两次独立查询逐字符一致 |

两个只有真数据才暴露的坑：

1. **`KSSJMS` 冒号全角/半角不统一**：`YYYY-MM-DD 9：00-11：30(星期四)`（全角 + 单位数小时）
   与 `YYYY-MM-DD 15:00-17:30(星期二)`（半角）同时存在。
2. **`KSRQ` 不含时间**，起止时刻只能从 `KSSJMS` 解析，两者必须配合。

`extParams` 的语义也实测过（这是 §7 三态判定的依据）：

```
2025-2026-2（已排考）→ totalSize=7  extParams={'msg': '查询成功', 'code': 1}
2026-2027-1（未排考）→ totalSize=0  extParams={'msg': '查询失败', 'code': 0}
```

## 5. 总体架构

```
fetch ─┬─ xskcb.do  → raw/timetable-<学期>.json      （不变）
       └─ wdksap.do → raw/exams-<学期>.json          （新增，失败时不覆盖）
                          │
export ── parser.parse_exams() → [ExamSchedule]     （新增模型 + 解析）
        ├─ exams.build_exam_events() → [CalendarEvent]（新增，单点事件）
        └─ 与课程事件合并 → render_ics() → .ics       （复用 SEQUENCE/留底机制）
diff  ──── diff_exams(old, new) → 考试变更小节        （新增）
subscribe push ── 复用同一条 build_ics_for_semester 管线（无改动）
```

新增模块 `src/xjtu_calendar/exams.py`：考试侧的解析与构建全在这里，
不把 `parser.py`（555 行）和 `exporter.py`（708 行）继续撑大。

## 6. 组件与接口

### 6.1 `config.Settings`

```python
def raw_exams_path(self, semester_key: str) -> Path:      # raw/exams-<学期>.json
def raw_exams_prev_path(self, semester_key: str) -> Path: # raw/exams-<学期>.prev.json
```

### 6.2 `data/ehall_endpoints.json`

新增端点 `exam_schedule`，`required: false`（缺它只影响考试，不影响课表导出）。
`fetcher.save_raw` 泛化为 `save_raw(payload, cfg, semester_key, kind="timetable")`，
`kind="exams"` 时写考试路径并做同样的单代轮转。

### 6.3 `models.ExamSchedule`（frozen dataclass）

```python
course_id: str | None      # KCH
course_name: str           # KCM
exam_name: str | None      # KSMC
date: date                 # 来自 KSRQ
start_time: time           # 来自 KSSJMS
end_time: time             # 来自 KSSJMS
location: str | None       # JASMC
campus: str | None         # 见下：考试行只有 XXXQDM 代码，没有 *_DISPLAY
seat: str | None           # ZWH
credits: float | None      # XF
teacher: str | None        # ZJJSXM
row_id: str | None         # WID（UID 首选依据）
task_id: str | None        # KSRWID（WID 缺失时的退路）
exam_code: str | None      # KSDM
```

**校区名不靠猜**：实测考试行只有 `XXXQDM='5'`，没有课表里那个 `XXXQDM_DISPLAY='创新港校区'`。
解析方式是取**同学期课表快照**里出现过的 `XXXQDM → XXXQDM_DISPLAY` 对照（同一数据源、
同一学期，属于已观测事实）；对照不到该代码时，`LOCATION` 只写 `JASMC`，不硬编码代码表。

### 6.4 `exams` 模块

```python
def parse_exam_rows(payload) -> list[ExamSchedule]     # 字段缺失/时间解析失败 → 跳过 + report.skip
def parse_exam_time_text(text: str) -> tuple[time, time] | None
      # 归一全角 ：→ :，容忍单位数小时；解析不出 → None（调用方跳过该条，绝不造 00:00 假事件）
def build_exam_events(exams, semester_key) -> list[CalendarEvent]
def make_exam_uid(semester_key: str, exam: ExamSchedule) -> str
      # sha256("<学期>|WID=<wid>")[:32]@xjtu-timetable-calendar
      # WID 缺失 → KSRWID → 再缺失 → "KCH|KSDM|KSRQ" 组合，并在日志标注降级
```

事件形态：`SUMMARY` = `<课程名>（结课考试）`（括号内取 `KSMC` 去掉"第X学期"后的类型词）；
`LOCATION` = `创新港校区 N-6NNN`；`DESCRIPTION` = 考试全称 / 课程号 / 座位号 / 学分 / 主考教师；
`DTSTART`/`DTEND` 用 `Asia/Shanghai` 的绝对时刻；**无 `RRULE`、无 `VALARM`**。
`KSSJMS` 括号里的星期与 `KSRQ` 推出的星期不一致时，以 `KSRQ` 为准并记 warning。

### 6.5 `exporter.build_ics_for_semester`

新增 `include_exams: bool = True` 与 `exams_payload: dict | None = None`；
考试快照存在且可解析时，把 `build_exam_events()` 的结果并进 `events` 一起渲染。
`calendar_title` 不变（考试不影响标题）。`ExportResult.info` 增加 `exam_events: int`。

### 6.6 `diff`

新增 `diff_exams(old_rows, new_rows)`，按 `WID` 配对，报告**新增 / 取消 / 时间变更 /
教室变更 / 座位变更**，输出并入 `diff` 子命令的既有报告格式（复用 `SlotChange` 形状）。

### 6.7 CLI

```
fetch   [--semester S] [--no-exams]
export  [--semester S] [-o OUT] [--name N] [--no-exams]
subscribe push [--semester S] [--input F] [--no-exams]
diff    [--semester S]                    # 自动包含考试小节
```

`--no-exams` 在 `fetch` 侧是"不抓"，在 export/push 侧是"不并入"（本地快照不动）。

## 7. 错误处理汇总：考试响应的**三态判定**

`rows == []` 单独看无法区分"没排考"和"查询没成功"——本学期（2026-2027-1）实测返回的正是
`extParams.msg: "查询失败"` + `totalSize: 0`。因此判定必须建立在 `extParams.code` 上：

| 状态 | 判据 | 行为 |
| --- | --- | --- |
| **有考试** | HTTP 200 且 `extParams.code == 1` 且 `rows` 非空 | 写快照，导出事件 |
| **确认无考试** | HTTP 200 且 `extParams.code == 1` 且 `rows == []` | 写空快照（覆盖旧的），日志 info「本学期暂无考试安排」 |
| **状态未知** | HTTP 非 200 / 401 / 登录页 / JSON 解析失败 / `extParams.code != 1`（含 `msg: 查询失败`） | **不覆盖已有快照**，warning 说明；有旧快照则沿用并提示其日期，无快照则本次导出不含考试 |

其余错误处理：

- 单条记录时间解析失败 → 跳过该条 + `report.skip`，不阻断其余考试与课程导出。
- `CalendarEvent` 的 `end > start` 约束（models 里是硬校验）→ 起止相等的记录按解析失败处理。
- 考试快照比课表快照旧超过 7 天 → export 时 warning「考试数据来自 X 日的快照，可能已过期」
  （与 `subscribe` 现有的 7 天陈旧警告同一口径）。
- **绝不因为考试失败而让 `fetch`/`export` 非零退出**：课程是主功能，考试是增量。

## 8. 安全与隐私

- `raw/exams-*.json` 与 `.prev.json` 含学号、姓名、教师姓名 → 只落在
  `~/.xjtu-timetable-calendar/raw/`（已被 `.gitignore` 排除），并沿用
  `fileutil.atomic_write_text(private=True)`。
- 测试固件一律脱敏（学号/姓名替换为占位值），但**保留真实字段形态**（含全角冒号样本）。
- 日志不落完整响应体；沿用 `logging_setup.redact_by_key`，把 `XH`/`XM`/`SJBH`/`ZJJSXM`
  加入键上下文打码名单。
- 订阅 URL 的风险口径不变（一个学期一个 token；转发即授权）。

## 9. 测试策略

- `tests/test_exams_parsing.py`：全角/半角冒号、单位数小时、缺 `KSRQ`、`KSSJMS` 无法解析、
  星期与日期不一致、`WID` 缺失的 UID 降级、起止相等。
- `tests/test_exams_fetch.py`：三态判定（用假 transport 注入 401 / 登录页 / `code!=1` /
  `code==1`+空 rows），断言"状态未知不覆盖快照"这一条。
- `tests/test_exporter_exams.py`：考试事件与课程事件共存时的 UID 唯一性、`SEQUENCE` 基线
  行为（新增考试 → added，改时间 → 同 UID 且 SEQUENCE+1）、`--no-exams` 产物不含考试。
- `tests/test_cli_exams.py`：`fetch/export/subscribe push` 的 `--no-exams` 透传。
- `tests/test_diff_exams.py`：新增/取消/时间变更/教室变更/座位变更。
- 回归护栏：现有 476 条必须继续全绿；门禁以 `.github/workflows/ci.yml` 的四条命令为准
  （含 `ruff format --check .`）。

## 10. 文档交付

README 新增「考试安排」小节（默认包含、`--no-exams`、三态行为、座位号在哪看）、
命令参考表补旗标、CHANGELOG 走 `[Unreleased]`。

## 11. 发布与回滚 / 开放问题

作为 **0.5.0** 的 minor 功能发布。回滚 = `--no-exams`（不删数据，只是不并入），
或把 `exam_schedule` 端点从用户覆盖文件里删掉。

开放问题（12 月排考公布后必须回头验证）：

1. **三态判定的 `extParams.code` 语义**是我目前唯一的薄弱假设——"没排考"与"查询失败"
   在响应上同形。拿到一份真实的 `code == 1` + 空 `rows` 才能确认"确认无考试"这一态存在。
2. 考试是否会出现**跨午夜/连场**（`KSSJMS` 目前只见单日内区间）。
3. `cxwapdksrw.do`（批次接口）在排考后是否会给出主接口没有的字段。
4. 缓考/补考是否走同一 `wdksap.do`、`KSMC` 文案如何。
