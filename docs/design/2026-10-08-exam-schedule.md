# 考试安排接入（`exams` 数据源）— 设计规格

日期：2026-10-08　状态：待评审（已按**代码复核 + 五个学期二轮实测**修订，修订点集中在
§4.1、§6.2、§6.4、§6.5–§6.7、§7、§9、§11）
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

> 本文所有样例的**日期、课程名、课程号一律写成占位**（`YYYY-MM-DD` / `<课程名>` /
> `ARCH000000`）：保留字段形态与字符集特征，不保留能定位到具体某个人的考试日程。
> 未打码的原始观测记录只存在 gitignored 的 `_notes/exam-endpoint-findings.md`
> 与 `_notes/exam-open-2027-1.txt`，公开仓库里不出现。

| 字段 | 含义 | 观测值 |
| --- | --- | --- |
| `KCM` / `KCH` | 课程名 / 课程号 | `<课程名>` / `ARCH000000`（形态：学院代码 + 6 位数字） |
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

**注意外层 `code` 与 `extParams.code` 不是一回事**：成功响应里外层恒为字符串 `"0"`
（`{"code":"0", ...}`），而 `extParams.code` 是整数 1；失败时才是 `extParams.code == 0`。
判据必须落在 `datas.<模块名>.extParams.code`，任何拿外层 `code` 当成败标志的写法都会反过来。
`<模块名>` 与课表一样随接口而变（这里是 `wdksap`），实现时要按结构遍历而不是硬编码
——`fetcher.fetch_current_semester` 已有这种遍历范式可参照。

### 4.1 第二轮实测（2026-10-08，五个学期单发只读查询）

`_notes/exam_open_questions.py` 查了 `2026-2027-1 / 2025-2026-2 / 2025-2026-1 /
2024-2025-2 / 2024-2025-1`，共 23 条考试行。新增事实：

| 观测 | 对设计的影响 |
| --- | --- |
| `KSSJMS` 有 **4 种写法**：`15:00-17:30`、`9：00-11：30`（全角 + 个位小时）、`15:00—17:30`（**em dash `—` 连接**）、`考试时间为：9.30-12.00`（**自由文本前缀 + 用点代替冒号**） | §6.4 的解析器必须按"提取两个 `H[:：.．]MM` 时刻"来写，不能 `strptime` 死格式；见 §6.4 形态表 |
| 同一 `KSRQ` 出现两场（同一天 `09:00-11:30` 与 `19:00-21:30`，同课） | 一天多场是常态 → UID **绝不能含日期**（D5 用 `WID` 正好规避），且降级键要能区分同日两场 |
| 一个学期可有 **多个 `KSDM` 批次**（`2024-2025-1` = 期中 1 门 + 结课 6 门） | 批次不是唯一键；`KSMC` 才是给人看的类型词 |
| `KSMC` 实测取值：`期中考试` / `结课考试` / `期末考试`（未见「补考」「缓考」） | §6.4 的类型词提取只做过剥「X 学年 第 X 学期」前缀，**不要**按这三种枚举白名单过滤；补考/缓考仍未观测，见 §11 |
| `JASMC` 有非房间号形态：`随堂考试-`、`<中心名>-3NN` | `LOCATION` 只做拼接，不做格式校验/正则清洗 |
| `XXXQDM` 同时出现 `1` 与 `5`，考试行始终没有 `XXXQDM_DISPLAY` | §6.3 的"同学期课表快照取对照"是必须的，且一学期内要能映射多个校区 |
| 统计接口 `cxwapdksrwtj.do`（form 只有 `XNXQDM`）：空数据时返回 `extParams={'msg':'操作成功','code':1}` + `rows: []`，行内字段 `YAPKSKC`（已排考试门数）/`WAPKSKC`（未排门数） | **`code==1` + 空 rows 是真实存在的态**（在统计接口上）。§7 的"确认无考试"因此有旁证；是否引入这第二个接口见 §11 的取舍 |
| 本学期 `2026-2027-1`：`wdksap` 是 `code:0 查询失败`，而同一时刻 `cxwapdksrwtj` 是 `code:1` + `totalSize:0` | 说明 `wdksap` 的 `code:0` 语义是「这个学期没有你的考试任务」而非「请求出错」——三态表把它归入"状态未知"是**保守但不误删**的选择，理由见 §7 |


## 5. 总体架构

```
fetch ─┬─ xskcb.do  → raw/timetable-<学期>.json      （不变）
       └─ wdksap.do → raw/exams-<学期>.json          （新增，状态未知时不覆盖）
                          │
export ── exams.parse_exam_rows() → [ExamSchedule]   （新增模型 + 解析）
        ├─ exams.build_exam_events() → [CalendarEvent]（新增，单场绝对时刻事件）
        ├─ 与课程事件合并 → **全局 UID 唯一性断言** → render_ics() → .ics
        └─ 复用 SEQUENCE / 留底机制（要求考试事件是 DATE-TIME，见 §6.4 红线）
diff  ──── exams.diff_exams(old, new) → 考试变更小节  （新增独立形状，不复用 SlotChange）
subscribe push / rotate ── 同一条 build_ics_for_semester 管线，但**两个子命令都要透传
                              `--no-exams`（rotate 现状连 `--input` 都没接，见 §6.7）**
```

新增模块 `src/xjtu_calendar/exams.py`：考试侧的解析、事件构建与变更检测全在这里，
不把 `parser.py`（555 行）和 `exporter.py`（708 行）继续撑大。**考试侧不借用
`TimetableParser`**：`FIELD_CANDIDATES` 与 `_iter_records`/`_get` 都是那个类的私有成员，
且候选表按课程字段写（`teacher` 认的是 `SKJS`，考试行是 `ZJJSXM`），`_iter_records` 还会
取 `datas` 的第一个 module 并把报错文案写死成「课程」。`exams.py` 自带候选表与取行函数。

## 6. 组件与接口

### 6.1 `config.Settings`

```python
def raw_exams_path(self, semester_key: str) -> Path:      # raw/exams-<学期>.json
def raw_exams_prev_path(self, semester_key: str) -> Path: # raw/exams-<学期>.prev.json
```

### 6.2 `data/ehall_endpoints.json` 与 fetcher 落盘

新增端点 `exam_schedule`（`method: POST`，`path` 来自 §4 的实测，`body` 只有 `XNXQDM`）。

`required: false` **当前只是文档性标注**：`Endpoint.required` 在生产代码里没有任何读取点
（`fetcher.py:103/172` 声明并解析后即被丢弃），所以「端点缺失只影响考试」这句话必须由
`cmd_fetch` 自己实现——**捕获 `EndpointNotConfigured` 并降级为「本次不抓考试」**，不能指望
`required` 字段。

`fetcher.save_raw` 泛化为 `save_raw(payload, cfg, semester_key, kind="timetable")`，
`kind="exams"` 时写考试路径并做同样的单代轮转。**`load_raw` 必须同步加 `kind`**，否则
export 侧根本取不到考试快照。另外现状 `save_raw`（`fetcher.py:571`）调
`atomic_write_text` 时**没有传 `private=True`**，与 §8 的要求不符——本次一并修，不只考试侧。

改动面比「加个旗标」大：`cmd_fetch`（`cli.py:261-333`）目前是**单一 payload 变量 + 单次
save_raw**，`--from-file`、浏览器抓取、HTTP 三条分支各自都要决定"要不要顺手抓考试"，
实现时按分支逐个交代，不要在最外层加一个 if 就算完。

### 6.3 `models.ExamSchedule`（frozen dataclass）

```python
course_id: str | None  # KCH
course_name: str  # KCM
exam_name: str | None  # KSMC
date: date  # 来自 KSRQ
start_time: time  # 来自 KSSJMS
end_time: time  # 来自 KSSJMS
location: str | None  # JASMC
campus: str | None  # 见下：考试行只有 XXXQDM 代码，没有 *_DISPLAY
seat: str | None  # ZWH
credits: float | None  # XF
teacher: str | None  # ZJJSXM
row_id: str | None  # WID（UID 首选依据）
task_id: str | None  # KSRWID（WID 缺失时的退路）
exam_code: str | None  # KSDM
```

**校区名不靠猜**：实测考试行只有 `XXXQDM='5'`，没有课表里那个 `XXXQDM_DISPLAY='创新港校区'`。
解析方式是取**同学期课表快照**里出现过的 `XXXQDM → XXXQDM_DISPLAY` 对照（同一数据源、
同一学期，属于已观测事实）；对照不到该代码时，`LOCATION` 只写 `JASMC`，不硬编码代码表。
同学期同时出现 `1` 和 `5` 两个代码（§4.1 实测），所以对照表按**多校区**建，不能假设只有一个。
另外 `parser.FIELD_CANDIDATES["campus"]` 现在只有 `XXXQDM_DISPLAY`，取代码 `XXXQDM` 需要
新增条目——实现者不要以为现成可用。

### 6.4 `exams` 模块

```python
def parse_exam_rows(payload) -> list[ExamSchedule]
# 自带候选表与取行逻辑；字段缺失/时间解析失败 → 跳过 + report.skip
def parse_exam_time_text(text: str) -> tuple[time, time] | None
# 解析不出 → None（调用方跳过该条，绝不造 00:00 假事件）
def build_exam_events(exams, semester_key) -> list[CalendarEvent]
def make_exam_uid(semester_key: str, exam: ExamSchedule) -> str
# sha256("<学期>|WID=<wid>")[:32]@xjtu-timetable-calendar
# WID 缺失 → KSRWID → 再缺失 → "KCH|KSDM|KSRQ|KSSJMS" 组合，并在日志标注降级
def diff_exams(old_rows, new_rows) -> list[ExamChange]
```

`parse_exam_time_text` 的判据来自 §4.1 实测的**四种形态**，所以写法只能是"扫描出两个
`H[:：.．]M(M)` 时刻"，不能 `strptime` 死格式，也不能按 `-` 一个分隔符 split：

| 实测原文 | 起止 | 要点 |
| --- | --- | --- |
| `YYYY-MM-DD 15:00-17:30(星期二)` | 15:00–17:30 | 基准形态：半角冒号 + `-` |
| `YYYY-MM-DD 9：00-11：30(星期四)` | 09:00–11:30 | 全角冒号 **且** 个位数小时 |
| `YYYY-MM-DD 15:00—17:30(星期日)` | 15:00–17:30 | 连接符是 **em dash `—`**（另有 `YYYY-MM-DD 16:00—18:00`） |
| `YYYY-MM-DD 考试时间为：9.30-12.00(星期日)` | 09:30–12:00 | **自由文本前缀 + 用 `.` 当时分分隔符** |

规则：先剥掉日期前缀与 `(...)` 星期后缀与任意非数字前导，再用一个正则抓两组
`(\d{1,2})[:：.．](\d{2})`，中间允许 `[-—~～至]`；抓到不足两组、或小时 > 23、或分 > 59 →
返回 `None`。全仓**没有**可复用的时刻解析工具（`academic_calendar._parse_date` 只吃
`date.fromisoformat`，喂 `KSRQ='YYYY-MM-DD 00:00:00'` 会直接 `ValueError`），所以这段是新写的，
只有 `timeutil.TZ_XIAN` 可复用。

**红线：考试事件必须是带 `TZID` 的 `DATE-TIME`，绝不能用 `VALUE=DATE` 的全天事件。**
理由写死在这里，免得实现时被"考试标个全天更直观"的直觉推翻：`sequence.parse_baseline`
（`sequence.py:139-141`）会**静默丢弃** DTSTART/DTEND 不是 datetime 的事件（现有测试
`test_sequence.py:201` 把这条当规范锁死）。全天考试事件因此永远进不了留底基线 →
每次重发布 SEQUENCE 恒为 0 → 客户端**永不更新**这条考试。这是本特性最可能翻车的一处。

事件形态：`SUMMARY` = `<课程名>（结课考试）`（括号内取 `KSMC` 去掉"第X学期"后的类型词）；
`LOCATION` = `创新港校区 N-6NNN`；`DESCRIPTION` = 考试全称 / 课程号 / 座位号 / 学分 / 主考教师；
`DTSTART`/`DTEND` 用 `Asia/Shanghai` 的绝对时刻；**无 `RRULE`、无 `VALARM`**。
`KSSJMS` 括号里的星期与 `KSRQ` 推出的星期不一致时，以 `KSRQ` 为准并记 warning。
**这条是防御性的**：§4.1 的 22 行样本逐行核对过，括号内星期与 `KSRQ` **全部一致**，
目前没有实测到反例——保留检查是因为两处星期本来就是两个字段，谁先腐化不可知，
而客户端显示的又是 `KSRQ` 推出来的那个。
降级 UID 键必须带 `KSSJMS`：§4.1 实测同一门课同一 `KSRQ` 会有两场（上午场 + 晚场），
只到日期粒度会撞车。

### 6.5 `exporter.build_ics_for_semester`

新增 `include_exams: bool = True` 与 `exams_payload: dict | None = None`；
考试快照存在且可解析时，把 `build_exam_events()` 的结果并进 `events` 一起渲染。
`calendar_title` 不变（考试不影响标题，`exporter.py:707` 只从 `meetings` 的 `grade_year`
推导）。`ExportResult.info` 增加 `exam_events: int`。

三条必须写进实现的约束：

1. **合并后做一次全局 UID 唯一性断言**。现有 `seen_uids` 去重只在 `build_events` 内部生效
   （`exporter.py:213/255/320`），考试事件在外面并进**不会被查重**；而 `render_ics` 直接
   `add_component`，重复 UID 会原样写进 .ics，违反 RFC 5545 并让客户端行为不可预期。
   断言失败时丢弃后来者 + warning，不整轮中止。
2. **`--input` 只喂课表 payload**（`exporter.py:580-585` 的现有形状），考试 payload 仍然
   从 `raw/exams-<学期>.json` 读。`--input - --no-exams` 是"完全离线且不含考试"的唯一组合，
   文档要写清，别让实现者去猜 `--input` 里能不能带 `datas.wdksap`。
3. **`--from-date/--to-date` 会连考试一起裁**（过滤发生在事件合并之后，`exporter.py:651-659`）。
   v1 采用"一并裁剪"并在 README 说明，不特殊放行考试事件。

### 6.6 `exams.diff_exams`

按 `WID` 配对，报告**新增 / 取消 / 时间变更 / 教室变更 / 座位变更**。

**不能复用 `diff.SlotChange`**：该形状强制 `weekday: int` 与 `periods: tuple[int, ...]`
（`diff.py:52-61`），CLI 打印侧硬编码「星期X」「第N节」（`cli.py:811-813`），`field` 标签
集合还写死在 `("周次","教室","教师")`（`diff.py:129-133`）。考试既没有周次也没有节次，
套上去会打出「星期undefined」。改为 `exams.py` 里独立的 `ExamChange` dataclass +
`diff` 子命令新增一节「考试变更」，与课程变更小节并列。

### 6.7 CLI

```
fetch            [--semester S] [--no-exams]
export           [--semester S] [-o OUT] [--name N] [--no-exams] [--from-date D] [--to-date D]
subscribe push   [--semester S] [--input F] [--no-exams]
subscribe rotate [--semester S] [--no-exams]        # 现状连 --input 都没透传，见下
diff             [--semester S]                     # 自动包含考试小节
```

`--no-exams` 在 `fetch` 侧是"不抓"，在 export/push 侧是"不并入"（本地快照不动）。

**`rotate` 也必须接 `--no-exams`**：`_subscribe_rotate`（`cli.py:993`）走的同样是
`build_ics_for_semester`，但当前既不接 `--input` 也不接该旗标——用户在 `push` 上加了
`--no-exams` 之后随手 rotate 一次，就会把考试悄悄塞回订阅 URL，属于"用户明确关掉的开关被
后台重新打开"，必须一起补。

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
- 考试快照比课表快照旧超过 7 天 → export 时 warning「考试数据来自 X 日的快照，可能已过期」。
  现有 `snapshot_age_days`（`subscribe.py:306`）**只读 `raw_timetable_path`**，"同一口径"
  意味着要先把它泛化成按 kind 取路径，不是白送的。
- **绝不因为考试失败而让 `fetch`/`export` 非零退出**：课程是主功能，考试是增量。

**网络层可行性已核对（结论：三态判得住）**：`fetch_via_http` 返回的是**完整 payload**
（`fetcher.py:440-443`），`extParams` 原样可取；非 200 / 401 / 403 / 登录页 HTML / 空体 /
非 JSON 全部**先抛异常、绝不返回 payload**，且 401 与"200 + 登录页"都归为
`AuthenticationExpired`（`fetcher.py:390,424-429`）。所以登录态失效**不会伪装成
`code == 0`**，只会走异常分支——"状态未知"这一态是可区分的。

`code == 0 / msg: 查询失败` 按 §4.1 的旁证（同一学期统计接口返回 `code:1` 且 `totalSize:0`）
更可能是「本学期没有考试任务」而非"出错"。本表仍把它归入**状态未知**是刻意的保守选择：
误判成"确认无考试"会**覆盖掉已有快照**，真遇到一次网络抖动就把考试信息抹掉；
而归入未知只多留一条 warning，最坏结果是"考试晚一天更新"。方向上不可逆的操作要选后者。

## 8. 安全与隐私

- `raw/exams-*.json` 与 `.prev.json` 含学号、姓名、教师姓名 → 只落在
  `~/.xjtu-timetable-calendar/raw/`（已被 `.gitignore` 排除）。
- **权限位要本次一并修**：现状 `save_raw`（`fetcher.py:571`）调 `atomic_write_text` 时
  **没有传 `private=True`**，考试快照落盘会继承 umask。修的时候课程快照同样受益，
  不要写成"沿用现有私有化"——它并不存在。
- 测试固件一律脱敏（学号/姓名替换为占位值），但**保留真实字段形态**（含全角冒号样本）。
- 日志不落完整响应体；沿用 `logging_setup.redact_by_key`，把 `XH`/`XM`/`SJBH`/`ZJJSXM`
  加入键上下文打码名单。
- 订阅 URL 的风险口径不变（一个学期一个 token；转发即授权）。

## 9. 测试策略

- `tests/test_exams_parsing.py`：**§4.1 那四种 `KSSJMS` 形态各一条**（半角、全角+个位小时、
  em dash、`考试时间为：9.30-12.00` 点分式）、缺 `KSRQ`、解析不出、星期与日期不一致、
  `WID` 缺失的 UID 降级、同日两场不撞 UID、起止相等。
- `tests/test_exams_fetch.py`：三态判定（用现成的 `transport(status, body)` 假 transport 与
  `LOGIN_HTML` 注入 401 / 登录页 / `code!=1` / `code==1`+空 rows），断言"状态未知不覆盖快照"。
- `tests/test_exporter_exams.py`：考试事件与课程事件共存时的**全局 UID 唯一性**、`SEQUENCE`
  基线行为（新增考试 → added；改时间 → 同 UID 且 SEQUENCE+1；**断言考试事件不是
  `VALUE=DATE`**，否则会被 `parse_baseline` 静默踢出基线）、`--no-exams` 产物不含考试。
- `tests/test_cli_exams.py`：`fetch/export/subscribe push/subscribe rotate` 的 `--no-exams` 透传。
- `tests/test_diff_exams.py`：新增/取消/时间变更/教室变更/座位变更（`ExamChange` 形状）。
- **固件改造点**：`subscribe_support.make_home`（:53-78）把 payload 写成 `{"kbList": rows}`，
  **不是** `datas.<模块>.rows` 的真实信封；照它造考试固件会让 `parse_exam_rows` 的取行逻辑
  测不到真形态。要么加 `make_exam_home`，要么给 `make_home` 增 kind 参数走真信封。
  `save_raw`/`load_raw` 的 `kind=` 要用默认值保持 `test_save_raw_rotates_previous_snapshot`
  （`test_fetcher.py:271-289`，逐字断言旧签名）不破。
- 回归护栏：门禁以 `.github/workflows/ci.yml` 的四条命令为准（含 `ruff format --check .`，
  且它会连带格式化本文档里的 ```python 代码块）。基线条数**以实现前重跑为准**：
  2026-10-08 本机实测 476 passed / 2 skipped，但 `tests/*.py` 里 `def test_` 只有 344 个，
  差值来自 parametrize——写计划时按"重跑得到的数"当基线，不要引用本文档给的数。

## 10. 文档交付

README 新增「考试安排」小节（默认包含、`--no-exams`、三态行为、座位号在哪看）、
命令参考表补旗标、CHANGELOG 走 `[Unreleased]`。

## 11. 发布与回滚 / 开放问题

作为 **0.5.0** 的 minor 功能发布。回滚 = `--no-exams`（不删数据，只是不并入），
或把 `exam_schedule` 端点从用户覆盖文件里删掉。

原先列的开放问题，2026-10-08 第二轮实测（五个学期、23 条真行）后的状态：

| # | 问题 | 结论 |
| --- | --- | --- |
| 1 | `code == 1` + 空 `rows` 的"确认无考试"是否存在 | **存在，但不在主接口上**：`cxwapdksrwtj.do` 空数据时返回 `code:1 / msg:操作成功` + `rows:[]`；`wdksap.do` 未排考时返回的是 `code:0 / 查询失败`。§7 表照旧把 `code:0` 归入状态未知（保守，见 §7 末段），"确认无考试"这一态在主接口上**仍未观测到**，实现时按"可能发生但没见过"处理即可 |
| 2 | 连场 / 跨午夜 | **同日多场已实测**（同课 09:00-11:30 与 19:00-21:30）→ 降级 UID 键必须含 `KSSJMS`。跨午夜未见（所有区间同日） |
| 3 | `cxwapdksrw.do` 是否有主接口缺的字段 | 捕获过一次真实请求（form 为 `KSDM` + `XNXQDM`，按批次查），返回行里 `YAPKSKC/WAPKSKC/ZPCDM/KSRWZT` 等在主接口没有，但**考试事件用不上**（批次级统计/排考状态）。维持 §3 的 non-goal，作为 v2 扩展点 |
| 4 | 缓考/补考走哪条、`KSMC` 文案 | 四个学期实测到的类型词只有「期中考试 / 结课考试 / 期末考试」。**补考、缓考仍未观测**——但 §6.4 的 `SUMMARY` 提取不做枚举白名单，真出现时原样带上即可，这条不再是设计风险 |

还剩的、必须等 12 月排考公布后回头验证的只有一件事：**`WID` 在"考试被重新编排"后是否稳定**
——已验证的是同一份数据两次查询逐字符一致（§4），还没验证的是改期/换考场后服务器是否
换 `WID`。若换，则 D5 会退化成"改期产生新事件"，届时靠 `diff_exams` 的取消小节兜底。

另记一笔取舍：**不引入 `cxwapdksrwtj.do` 作为第二个请求**。它能区分"本学期没有考试任务"
与"查询失败"，但代价是每次 `fetch` 多一个接口、多一个失败面；而 §7 的保守判定已经把
最坏结果限制成"多一条 warning"。若 12 月验证发现 warning 太吵，再考虑升级。
