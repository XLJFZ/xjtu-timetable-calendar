# 考试安排接入（`exams` 数据源）— 设计规格

日期：2026-10-08　状态：待评审（已按**代码复核 + 五个学期二轮实测**修订，修订点集中在
§4.1、§6.2、§6.4、§6.5–§6.7、§7、§9、§11）
作者：与用户逐条确认而成（决策见 §2）
关联：`docs/design/2026-10-07-url-subscribe-ics.md`（订阅通道）、`_notes/exam-endpoint-findings.md`
（第一轮真实观测）、`_notes/exam-open-round2.md`（第二轮五学期记录，转录 + 打码）

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
| D5 | UID 依据 | 用接口的行 ID `WID`。**实测到的只是**「同一份数据两次查询 `WID` 逐字符一致」；**改期/换考场后服务器是否换 `WID` 未经验证**（§11），若换则本决策退化为"改期产生新事件"，由 `diff_exams` 的取消小节兜底 |

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

> **打码口径**：本文样例的日期、课程名、课程号、教室、座位一律写成占位
> （`YYYY-MM-DD` / `<课程名>` / `ARCH000000` / `<楼号-房间>` / `NN`），保留形态与字符集特征，
> 不保留能定位到具体某个人的日程。观测记录见 gitignored 的 `_notes/exam-endpoint-findings.md`
> （第一轮）与 `_notes/exam-open-round2.md`（第二轮，转录 + 打码，非原始抓取）。
>
> **一处必须如实记录的历史**：本文档的初版（原 commit `9171570`）带着**真实**课程名、
> 课程号与两场考试的真实日期时间，并在 2026-10-08 短暂公开在 `origin/main` 上。
> 当天已重写 `main` 把那六笔提交（含顺带携带脏文档的网页端 README 提交）全部替换为
> 脱敏版，逐笔复核过**识别性字段**（课程名、课程号、考试日期时间、教室、座位号）
> 在新链每一笔里都为零，且**重写后的 tip tree 与重写前逐字节相同**（`3909a8da0e`）——
> 历史表述被洗干净，当前内容一个字节没动。学期标识（`2025-2026-2` 一类）按 §4 的写法
> 保留，它不指向具体某个人。
> 剩下的事实也要写清：GitHub 对脱离引用的旧对象有自己的回收节奏，在回收之前
> 旧 sha 仍可能被直接取回（2026-10-08 实测仍可 `GET /git/blobs/04e5ffcf…` 拿到原文）；
> 任何此前 clone 过的副本同样不受重写影响。所以这不是"信息从未公开过"，
> 而是"分支历史已干净，残余暴露窗口在 GitHub 服务端与既有副本里"。

| 字段 | 含义 | 观测值 |
| --- | --- | --- |
| `KCM` / `KCH` | 课程名 / 课程号 | `<课程名>` / `ARCH000000`（形态：学院代码 + 6 位数字） |
| `KSMC` | 考试名称 | `YYYY-YYYY学年 第二学期 结课考试` |
| `KSRQ` | 考试日期 | `YYYY-MM-DD 00:00:00`（**时间部分恒 00:00:00**） |
| `KSSJMS` | 时间描述 | `YYYY-MM-DD 15:00-17:30(星期二)` |
| `JASMC` | 教室 | `<楼号-房间>`（见 §4.1：也有 `随堂考试-` 这类非房间号形态） |
| `ZWH` | 座位号 | `NN` |
| `XXXQDM` | 校区代码 | `5`（创新港） |
| `XF` / `ZJJSXM` | 学分 / 主考教师 | 有值；两者都只进 `DESCRIPTION`（§6.4），且 `ZJJSXM` 属人名，日志必须打码 |
| `WID` / `KSDM` / `KSRWID` | 行 ID / 批次 / 任务 ID | 两次独立查询逐字符一致 |
| `pageSize` | 分页大小 | 捕获到的是 `999`，`totalSize` 最大实测 7 → 一学期不会翻页，但实现要护栏（§6.4） |

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
2024-2025-2 / 2024-2025-1`，共 23 条考试行。行级记录（转录 + 打码，**不是原始抓取**）见
`_notes/exam-open-round2.md`；原始未打码输出只存在于当时进程 stdout 与控制台，未落盘，
且会话已过期无法重跑（2026-10-08 实测：`wdksap` 请求返回 `Exceeded maximum allowed redirects`）。

新增事实：

| 观测 | 对设计的影响 |
| --- | --- |
| `KSSJMS` 有 **4 种写法**：`15:00-17:30`、`9：00-11：30`（全角 + 个位小时）、`15:00—17:30`（**em dash `—` 连接**）、`考试时间为：9.30-12.00`（**自由文本前缀 + 用点代替冒号**） | §6.4 的解析器必须按"提取两个 `H[:：.．]MM` 时刻"来写，不能 `strptime` 死格式；见 §6.4 形态表 |
| 同一 `KSRQ` 出现两场（同一天 `09:00-11:30` 与 `19:00-21:30`，同课） | 一天多场是常态 → UID **绝不能含日期**（D5 用 `WID` 正好规避），且降级键要能区分同日两场 |
| 一个学期可有 **多个 `KSDM` 批次**（`2024-2025-1` = 期中 1 门 + 结课 6 门） | 批次不是唯一键；`KSMC` 才是给人看的类型词 |
| `KSMC` 实测取值：`期中考试` / `结课考试` / `期末考试`（未见「补考」「缓考」） | §6.4 的类型词提取只做过剥「X 学年 第 X 学期」前缀，**不要**按这三种枚举白名单过滤；补考/缓考仍未观测，见 §11 |
| `JASMC` 有非房间号形态：`随堂考试-`（带尾随连字符的标记串） | `LOCATION` 只做拼接，不做格式校验/正则清洗 |
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
不把已经很重的 `parser.py` 与 `exporter.py` 继续撑大（**故意不写行数**，这数字每次提交都会腐）。**考试侧不借用
`TimetableParser`**：`_iter_records`/`_get` 是它的私有方法，模块级的 `FIELD_CANDIDATES`
（`parser.py:104`）又按课程字段写（`teacher` 认的是 `SKJS`，考试行是 `ZJJSXM`），
`_iter_records` 还会取 `datas` 的第一个 module 并把报错文案写死成「课程」。
`exams.py` 自带候选表与取行函数。

## 6. 组件与接口

### 6.1 `config.Settings`

```python
def raw_exams_path(self, semester_key: str) -> Path:      # raw/exams-<学期>.json
def raw_exams_prev_path(self, semester_key: str) -> Path: # raw/exams-<学期>.prev.json
```

### 6.2 `data/ehall_endpoints.json` 与 fetcher 落盘

新增端点 `exam_schedule`（`method: POST`，`path` 来自 §4 的实测）。

**`Endpoint` 没有请求体字段**（字段只有 `name/method/path/description/required`，
`fetcher.py:99-103`；`_parse_endpoints` 会**静默忽略**多余键，`fetcher.py:165-175`）。
`XNXQDM` 由**调用方**以 `params=` 传入，与课表完全同形（`cli.py:313`
`fetch_via_http(endpoint, cfg=cfg, params={"XNXQDM": semester})` → `fetcher.py:376`
`client.post(url, data=params or {})`）；想写的参数说明只能放进 `description`。
写成 `"body": {...}` 不会报错，只会发出一个**空表单**的 POST——服务器多半回
`code:0/查询失败`，于是 §7 判成"状态未知"、永不覆盖快照，**这个 bug 能藏一整年**。

`required: false` **当前只是文档性标注**：`Endpoint.required` 在生产代码里没有任何读取点
（`fetcher.py:103/172` 声明并解析后即被丢弃），所以「端点缺失只影响考试」这句话必须由
`cmd_fetch` 自己实现——**捕获 `EndpointNotConfigured` 并降级为「本次不抓考试」**，不能指望
`required` 字段。

`fetcher.save_raw` 泛化为 `save_raw(payload, cfg, semester_key, kind="timetable")`，
`kind="exams"` 时写考试路径并做同样的单代轮转。**`load_raw` 必须同步加 `kind`**，否则
export 侧根本取不到考试快照。另外现状 `save_raw`（`fetcher.py:571`）调
`atomic_write_text` 时**没有传 `private=True`**，与 §8 的要求不符——本次一并修，不只考试侧。

改动面比「加个旗标」大。`cmd_fetch`（`cli.py:261-333`）里有**三条取数分支**，
`save_raw` 也真有**两处调用点**（`:275` 与 `:329`），逐条表态：

- **`--from-file`**：无会话、无网络，且 `:273-277` 有自己的早退路径 → **不抓考试**，
  日志说明一句；export 侧因此没有考试快照时按 §7 的"无快照"分支走。
- **浏览器抓取**：`fetch_via_browser` 用 `hint_keywords =
  ("timetable","schedule","course","kcb","semester","term")` 过滤捕获到的 URL
  （`fetcher.py:455-462`），而实测路径 `/…/wdksap/wdksap.do` **一个都不匹配**，
  且导航的是 `cfg.select_role_url`（课表应用，`fetcher.py:496`）→ v1 明确
  **浏览器分支不抓考试**（要抓就得扩关键词 + 另导航一次考试页，属于独立改动，别顺手做）。
- **HTTP 抓取**：唯一会抓考试的分支，`params={"XNXQDM": semester}` 同形多发一次。

三条分支都不抓时，`--no-exams` 的语义才称得上"与路径无关"；README 也要按这个口径写，
否则用户会以为浏览器登录路径也能拿到考试。

### 6.3 `models.ExamSchedule`（frozen dataclass）

```python
course_id: str | None  # KCH
course_name: str  # KCM
exam_name: str | None  # KSMC
date_str: str  # ISO "YYYY-MM-DD"，来自 KSRQ（时间部分恒 00:00:00，已丢弃）
start_time: str  # "HH:MM"，来自 KSSJMS
end_time: str  # "HH:MM"，来自 KSSJMS
location: str | None  # JASMC
campus: str | None  # 见下：考试行只有 XXXQDM 代码，没有 *_DISPLAY
seat: str | None  # ZWH
credits: float | None  # XF
teacher: str | None  # ZJJSXM
row_id: str | None  # WID（UID 首选依据）
task_id: str | None  # KSRWID（WID 缺失时的退路）
exam_code: str | None  # KSDM
```

**日期与时刻一律持有为字符串**（`date_str` + 零补齐 `"HH:MM"`）：本小节下文的
`combine` 拼装与 `_validate_hhmm` 校验吃的都是字符串，`dataclass` 里不放
`date`/`time` 对象（初版草图写 `date: date` / `start_time: time`，与 shipped
`models.ExamSchedule` 不符，已按实现更正）。

**校区名不靠猜**：实测考试行只有 `XXXQDM='5'`，没有课表里那个 `XXXQDM_DISPLAY='创新港校区'`。
解析方式是取**同学期课表快照**里出现过的 `XXXQDM → XXXQDM_DISPLAY` 对照（同一数据源、
同一学期，属于已观测事实）；对照不到该代码时，`LOCATION` 只写 `JASMC`，不硬编码代码表。
同学期同时出现 `1` 和 `5` 两个代码（§4.1 实测），所以对照表按**多校区**建，不能假设只有一个。
另外 `parser.FIELD_CANDIDATES["campus"]` 现在是 `("XXXQDM_DISPLAY", "campus", "campusName")`
（`parser.py:109`），**没有取代码的 `XXXQDM`**，要拿到校区代码得新增条目——实现者不要以为现成可用。

### 6.4 `exams` 模块

```python
def parse_exam_rows(payload) -> list[ExamSchedule]
# 自带候选表与取行逻辑；字段缺失/时间解析失败 → 跳过 + report.skip
def parse_exam_time_text(text: str) -> tuple[str, str] | None
# 返回零补齐的 ("HH:MM", "HH:MM") 字符串对（交 schedules.combine 落地）；
# 解析不出 → None（调用方跳过该条，绝不造 00:00 假事件）
def classify_exam_payload(payload) -> ExamState
# 三态判定的**唯一归属点**：把 §7 那张表变成一个纯函数，cli 只负责"要不要落盘"
def build_exam_events(exams, semester_key) -> list[CalendarEvent]
def make_exam_uid(semester_key: str, exam: ExamSchedule) -> str
# sha256("<学期>|WID=<wid>")[:32]@xjtu-timetable-calendar
# WID 缺失 → KSRWID → 再缺失 → "KCH|KSDM|KSRQ|KSSJMS" 组合，并在日志标注降级
def diff_exams(old_exams, new_exams) -> ExamDiff
# 返回聚合对象 ExamDiff（.changes 为排好序的 list[ExamChange]、.is_empty）；
# **不是**裸 list，也不带 added/removed 两个列表（初版草图写 `-> list[ExamChange]`，
# 与 shipped 实现不符，已更正）
```

`classify_exam_payload(payload) -> ExamState`（`ExamState` 是 `HAS_EXAMS` / `NO_EXAMS` /
`UNKNOWN` 的枚举，带上 `rows`、`msg`、`code` 供调用方记日志）是 §7 那张表的**代码归属点**。
不定义务的话，实现者只能把判定内联进 `cmd_fetch`，于是 §9「用假 transport 测三态」根本无从下手：
`transport()` 喂的是 `fetch_via_http`，而它按 §7 的分析在异常态**直接抛出**、正常态**返回整个
payload**，三态分类活在它**上面**那一层。规则写死：
**只有 `HAS_EXAMS` / `NO_EXAMS` 才允许 `save_raw(kind="exams")`，`UNKNOWN` 一律不落盘**；
`fetch_via_http` 抛出的任何异常（含 `AuthenticationExpired`）由 `cmd_fetch` 映射成 `UNKNOWN`，
`EndpointNotConfigured` 同样映射成 `UNKNOWN`（§6.2）。

取 module 时**不要**照 `fetcher.fetch_current_semester`（`fetcher.py:306-309`）抄——它写的是
`if rows and ...`，恰好把三态里最需要分辨的**空 rows** 跳过；用 `parser.py:248` 那种
`isinstance(module.get("rows"), list)` 的结构判定。`extParams` 整个缺失、或 `code` 是字符串
`"1"` 而不是整数 1，都归入 `UNKNOWN`（宁可不写快照）。

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
返回 `None`。

**拼装必须走 `schedules.combine(day, hhmm)`（`schedules.py:251-262`），不要手搓时区。**
它已经做了三件这件事最容易出错的事：强制 `Asia/Shanghai`（`timeutil.TZ_XIAN`）、
拒绝 naive datetime（docstring 里写着 `.. important::`）、并用 `models._validate_hhmm`
校验 `"HH:MM"`（那个正则 `\d{1,2}:\d{2}` 本来就容忍个位数小时）。全仓**没有**的是
"从脏字符串里抓时刻"这一步（`academic_calendar._parse_date` 只吃 `date.fromisoformat`，
喂 `KSRQ='YYYY-MM-DD 00:00:00'` 会直接 `ValueError`），所以 `parse_exam_time_text` 的
输出必须是两个 `"HH:MM"` 字符串，交给 `combine` 落地——这样 §6.4 的红线（带 TZID 的
DATE-TIME）在**构造点**就被保证，而不是靠事后测试兜。

> 注意 `combine` 里 `int(x) for x in hhmm.split(":")` 只认**半角**冒号，
> 所以"归一全角 `：` / 点分 `.` → `HH:MM`"是 `parse_exam_time_text` 的责任，别指望 `combine` 兼容。

**翻页护栏**：实测 `pageSize=999` 而 `totalSize` 最大 7，一页够用；但 §4 的响应里
`totalSize` 是现成的，实现时加一条 `len(rows) != totalSize → warning`，
将来某学期超过一页时不会静默丢考试。

**红线：考试事件必须是带 `TZID` 的 `DATE-TIME`，绝不能用 `VALUE=DATE` 的全天事件。**
理由写死在这里，免得实现时被"考试标个全天更直观"的直觉推翻：`sequence.parse_baseline`
（`sequence.py:139-141`）会**静默丢弃** DTSTART/DTEND 不是 datetime 的事件（现有测试
`test_sequence.py:201` 把这条当规范锁死）。全天考试事件因此永远进不了留底基线 →
每次重发布 SEQUENCE 恒为 0 → 客户端**永不更新**这条考试。这是本特性最可能翻车的一处。

事件形态：`SUMMARY` = `<课程名>（结课考试）`（括号内取 `KSMC` 去掉"第X学期"后的类型词）；
`LOCATION` = `<校区名> <楼号-房间>`；`DESCRIPTION` = 考试全称 / 课程号 / 座位号 / 学分 / 主考教师；
`DTSTART`/`DTEND` 用 `Asia/Shanghai` 的绝对时刻；**无 `RRULE`、无 `VALARM`**。
`KSSJMS` 括号里的星期与 `KSRQ` 推出的星期不一致时，以 `KSRQ` 为准并记 warning。
**这条是防御性的**：§4.1 转录的 **22 行**样本逐行核对过，括号内星期与 `KSRQ` **全部一致**
（总计 23 行，其中 1 行当时滚出输出窗口、未纳入核对，见 `_notes/exam-open-round2.md`），
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

1. **合并点要钉死，且要区分"渲染用"与"统计用"两份事件列表。** 现状
   `exporter.py:645` 出 `events` → `:651-659` 按日期过滤 → `:707` 标题 → `:712`
   `summarize(meetings, events)` → `:717` `sequence_stats(events, baseline)`。
   考试事件必须在**过滤之前**并进 `events`（否则 `--from-date/--to-date` 拦不住它们），
   但 `summarize` 与 `cli.py:376-387` 打印的 `Events: N` / `Date range` **继续只吃课程事件**，
   考试数量单独走 `info["exam_events"]`。理由：`date_range` 现在是
   `min(e.start)..max(e.end)`（`exporter.py:507-519`），考试一并进就会把导出摘要的日期区间
   拉长到考试周，既有测试与用户看到的"课表覆盖范围"都会莫名变样。
   `sequence_stats` 则**必须**看到考试事件（否则考试的 SEQUENCE/added 统计失真）。
2. **合并后做一次全局 UID 唯一性断言**。现有 `seen_uids` 去重只在 `build_events` 内部生效
   （`exporter.py:213`，去重在 `:256` 与 `:319`），考试事件在外面并进**不会被查重**；而 `render_ics` 直接
   `add_component`（`exporter.py:471-485`），重复 UID 会原样写进 .ics，违反 RFC 5545 并让
   客户端行为不可预期。断言失败时丢弃后来者 + warning，不整轮中止。
3. **`--input` 只喂课表 payload**（`exporter.py:580-589`），考试 payload 仍然从
   `raw/exams-<学期>.json` 读。**注意 `--input` 不支持 `-`/stdin**：现状 `Path(input_path)`
   然后 `if not source.is_file(): raise`，`-` 会被当成文件名报错。离线且不含考试的写法是
   显式 `--input <文件> --no-exams`。
4. **`--from-date/--to-date` 会连考试一起裁**（过滤在 `exporter.py:651-659`）。
   v1 采用"一并裁剪"并在 README 说明，不特殊放行考试事件。

### 6.6 `exams.diff_exams`

按 `WID` 配对，报告**新增 / 取消 / 时间变更 / 教室变更 / 座位变更**。

**`diff` 子命令有个必须先绕掉的短路**：`cli.py:782-785` 在打印任何小节之前就先判
`if result.is_empty: print("无变化：…完全一致。"); return 0`。只改考试（座位重排、换考场
正是学期中最常见的事件）时课程侧为空 → 直接打出"无变化"并退出，考试变更**一个字都不会出现**。
所以 §6.6 的实现必须把它改成
`if course_diff.is_empty and not exam_changes:` 才走这条捷径。

同时把考试侧的取数口径写清（否则又是回来问一轮）：默认读
`cfg.raw_exams_path(semester)` 与 `cfg.raw_exams_prev_path(semester)`（§6.1），
`--old/--new`（`cli.py:167-171`）**只对课表生效**，考试侧暂不提供显式指定；
**首次**拿到考试快照（无 `.prev`）时全部行按「新增」报告，不要静默跳过。

**不能复用 `diff.SlotChange`**：该形状强制 `weekday: int` 与 `periods: tuple[int, ...]`
（`diff.py:52-61`），CLI 打印侧硬编码「星期X」「第N节」（`cli.py:810-814`），`field` 标签
集合还写死在 `("周次","教室","教师")`（`diff.py:129-133`）。考试既没有周次也没有节次：
`weekday=0` 会让 `'一二三四五六日'[change.weekday - 1]` **静默印成「日」**，越界值直接
`IndexError`（`diff` 崩给你看）。改为 `exams.py` 里独立的 `ExamChange` dataclass +
`diff` 子命令新增一节「考试变更」，与课程变更小节并列。

### 6.7 CLI

```
fetch            [--semester S] [--no-exams]
export           [--semester S] [-o OUT] [--name N] [--no-exams] [--from-date D] [--to-date D]
subscribe push   [--semester S] [--input F] [--no-exams]
subscribe rotate [--semester S] [--no-exams]        # 现状连 --input 都没透传，见下
diff             [--semester S]                     # 自动包含考试小节
```

（**只列与考试相关的旗标**；`export` 还有 `--calendar-config` / `--schedule-config` /
`--allow-unsupported-adjustments` / `--sequence-from` / `--no-sequence` 等既有旗标，别以为它们不存在。）

`--no-exams` 在 `fetch` 侧是"不抓"，在 export/push 侧是"不并入"（本地快照不动）。

**`rotate` 也必须接 `--no-exams`**：`_subscribe_rotate`（`cli.py:973`）走的同样是
`build_ics_for_semester`，但当前既不接 `--input` 也不接该旗标——用户在 `push` 上加了
`--no-exams` 之后随手 rotate 一次，就会把考试悄悄塞回订阅 URL，属于"用户明确关掉的开关被
后台重新打开"，必须一起补。**注意这不是加个 argparse 旗标就完事**：
`_subscribe_rotate(cfg, state, semester)` 目前根本不接收 `args`，这是一次签名改动，
调用点（`cli.py` 的 dispatch 处）要一起改。

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
- 考试快照的陈旧提示：现有 `snapshot_age_days`（`subscribe.py:304-311`）算的是**快照 vs 现在**，
  而本特性想要的是**考试快照 vs 课表快照**。两种口径不一样，v1 明确采用后者
  （考试排期是跟着课表一起变的，跟"今天"关系弱），文案写成「考试数据来自 X 日的课表同期快照」，
  并把它实现成 `snapshot_age_days(path)` 这种接受任意路径的形式（`cli.py:940-942` 现有调用不变）。
  **不要**沿用 `subscribe.py:306` 里硬编码 `raw_timetable_path` 的那一版。
- **绝不因为考试失败而让 `fetch`/`export` 非零退出**：课程是主功能，考试是增量。
- **"消失的考试"不会从客户端消失**：`render_ics` 只发 `method: PUBLISH` 的普通 VEVENT
  （`exporter.py:467`、`:471-485`），全仓没有 `STATUS:CANCELLED` / `METHOD:CANCEL` 通路。
  所以 `--no-exams` 回滚、以及"确认无考试 → 写空快照覆盖旧的"这两条，都只是**不再发布**，
  已订阅的日历里那些考试事件仍会留着（Google/Outlook 对"URL 内容里少了一条事件"的处理不一致，
  多半是保留）。D4 的"宁缺毋滥"因此只保证不新增、不保证撤销——这条限制必须写进 README（§10），
  不能让用户以为关掉开关就干净了。真正的取消通路（保留一段 `STATUS:CANCELLED` 的发布期）
  留到 v2 单独立项。

**网络层可行性已核对（结论：三态判得住）**：`fetch_via_http` 返回的是**完整 payload**
（`fetcher.py:440-443`），`extParams` 原样可取；非 200 / 401 / 403 / 登录页 HTML / 空体 /
非 JSON 全部**先抛异常、绝不返回 payload**，且 401 与"200 + 登录页"都归为
`AuthenticationExpired`（`fetcher.py:390,424-429`）。所以登录态失效**不会伪装成
`code == 0`**，只会走异常分支——"状态未知"这一态是可区分的。

`code == 0 / msg: 查询失败` 按 §4.1 的旁证（同一学期统计接口返回 `code:1` 且 `totalSize:0`）
更可能是「本学期没有考试任务」而非"出错"。本表仍把它归入**状态未知**是刻意的保守选择：
误判成"确认无考试"会**覆盖掉已有快照**，真遇到一次网络抖动就把考试信息抹掉；
而归入未知只多留一条 warning，最坏结果是"考试晚一天更新"。方向上不可逆的操作要选后者。

### 7.1 被移除的考试不会从客户端消失（v1 的已知限制）

`render_ics` 只发 `method: PUBLISH` 和普通 `VEVENT`（`exporter.py:467,471-485`），
全仓**没有** `STATUS:CANCELLED` 或 `METHOD:CANCEL` 通路。后果有两个，都必须在 README 里说清：

- `--no-exams` 回滚、或"确认无考试"写空快照之后，**订阅端仍会留着已经发布的考试事件**
  （Google 按 URL 重拉时是按新内容覆盖，但"少掉的事件"各家客户端处理不一致）。
- 所以 D4 的"宁缺毋滥"只保证**不再新增**，不保证**撤销已发布的**。

v1 决定：**不实现取消通路**，只在 §10 的 README 小节写一句"关掉考试后客户端可能需要手动删除
旧考试事件"。真要做取消（`STATUS:CANCELLED` 保留一段发布期）留到 v2，作为独立设计。

## 8. 安全与隐私

- `raw/exams-*.json` 与 `.prev.json` 含学号、姓名、教师姓名 → 只落在
  `~/.xjtu-timetable-calendar/raw/`（已被 `.gitignore` 排除）。
- **权限位要本次一并修**：现状 `save_raw`（`fetcher.py:571`）调 `atomic_write_text` 时
  **没有传 `private=True`**，考试快照落盘会继承 umask。修的时候课程快照同样受益，
  不要写成"沿用现有私有化"——它并不存在。
- 测试固件一律脱敏（学号/姓名替换为占位值），但**保留真实字段形态**（含全角冒号样本）。
- 日志不落完整响应体。打码名单**大部分已经有了**：`logging_setup._SENSITIVE_KEYS` 现含
  `xh`/`xm`/`name`/`skjs`/`teacher`（`logging_setup.py:31-60`），本次真正要加的只有
  `sjbh` 与 `zjjsxm`（主考教师是考试侧才出现的键）。别在计划里写成"新建打码机制"。
- 订阅 URL 的风险口径不变（一个学期一个 token；转发即授权）。
- **本文档自身的公开历史**：初版 spec（原 commit `9171570`）带着真实课程名、课程号与两场
  考试的真实日期时间，2026-10-08 短暂公开在 `origin/main`。当天已重写 `main`（见 §4 的
  历史说明）：脏版本连同其后的五笔提交被替换为脱敏版，重写后的 tip tree 与重写前逐字节
  相同。**仍未清零的残余**：GitHub 对脱离引用的旧对象有自己的回收节奏，回收之前旧
  blob 仍可按 sha 取到；页面缓存与 Fork 不受重写影响。因此本文档的口径是
  "分支历史已干净，残余窗口在服务端与既有副本"，而不是"真实数据从未进过公开仓库"。

## 9. 测试策略

- `tests/test_exams_parsing.py`：**§4.1 那四种 `KSSJMS` 形态各一条**（半角、全角+个位小时、
  em dash、`考试时间为：9.30-12.00` 点分式）、缺 `KSRQ`、解析不出、星期与日期不一致
  （**构造用例**，§6.4 说明实测未见）、
  `WID` 缺失的 UID 降级、同日两场不撞 UID、起止相等。
- `tests/test_exams_fetch.py`：三态判定（**注意 `transport(status, body)`（:78）与
  `LOGIN_HTML`（:43）、`cfg`/`no_session_cfg` 固件（:51-71）现在都定义在
  `tests/test_fetcher.py` 内部**，不是共享 support 模块。新测试文件要么把它们提到
  `tests/fetcher_support.py`（仓库已有 `subscribe_support.py` 这个先例），要么显式
  `from tests.test_fetcher import transport` —— 计划里必须写清选哪条，否则会出现"复制一份
  假 transport"的第三种结局）。断言"状态未知不覆盖快照"。
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
  差值来自 13 处 `parametrize`（约 +124）**以及 `--doctest-modules`**：`pyproject.toml:72-78`
  的 `testpaths = ["tests", "src"]` + `addopts` 里的 `--doctest-modules` 会把 `src/` 的
  doctest 也当测试跑。→ **新模块 `exams.py` 的 docstring 里别放 `>>>` 例子**，除非它真的能跑通；
  写了就跑不绿，这是本仓库特有的坑。
- §9 上面五个文件之外，还有三条行为也必须配测试（原稿漏列）：
  `cmd_fetch` 对 `EndpointNotConfigured` 的降级（§6.2）、D3「不写 VALARM」（断言产物里
  没有 `BEGIN:VALARM`）、§8 的 `private=True`（`tests/test_fileutil.py:55-66` 已有
  monkeypatch `os.chmod` 的先例可照抄）。

## 10. 文档交付

README 新增「考试安排」小节（默认包含、`--no-exams`、三态行为、座位号在哪看），
并且必须写清两条限制：**浏览器抓取与 `--from-file` 路径拿不到考试**（§6.2），
以及**关掉开关不会撤销已经发布到客户端的考试事件**（§7.1）。
命令参考表补旗标、CHANGELOG 走 `[Unreleased]`。

## 11. 发布与回滚 / 开放问题

作为 **0.5.0** 的 minor 功能发布。回滚**只有 `--no-exams` 这一条**（不删数据，只是不并入）。
原稿写的"把 `exam_schedule` 端点从用户覆盖文件里删掉"**是无效的**：
`load_endpoints` 按**整档**优先级解析（显式 → 用户覆盖 → 包内默认，`fetcher.py:225-280`），
命中任何一个非空文件就**直接返回**，没有逐端点合并。既然 0.5.0 会把 `exam_schedule` 发进
**包内默认文件**，覆盖文件里删不删都影响不到它；真要靠端点禁用，用户得自备一份**完整**端点清单。
这条不要写成回滚手段，至多在 README 里作为"高级用户自求多福"提一句。

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
