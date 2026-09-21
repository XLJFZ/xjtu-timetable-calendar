# xjtu-timetable-calendar 技术设计 v0.1

| 项 | 内容 |
|---|---|
| 状态 | **草案；D5/D6/D7 已执行，D8/D9 待用户提供数据** |
| 日期 | 2026-09-20（第二轮：仓库卫生清理） |
| 基准提交 | 本地 `master`，0 commit（全部文件处于暂存状态） |
| 用途 | 把数据模型、eHall 适配层、ICS 导出语义、调课处理、目录结构一次定清楚，作为后续实现与验收的基准 |

> **文档定位说明**
>
> 本设计原计划作为「实现前的规格」。核查后发现 `D:\xjtu-timetable-calendar` 已存在**完整实现**（15 个源模块、8 个测试文件、263 项测试全绿），且其架构已与本文档拟定的路线高度一致。
>
> 因此本文档的定位调整为 **「追认 + 冻结」**：以现状实现为基准，把隐含契约显式化，并列出必须修正的漂移项（见 §6）。**不再需要「搭骨架」这一步**。

---

## 0. 结论摘要

三条，按重要性排序：

1. **骨架已存在，且领先于设计。** 三个业务难点（eHall 数据源、课表时间/周次建模、ICS 更新语义）都已有对应实现与守卫测试。原计划的第 4 步「搭仓库骨架」**应予取消**。

2. **真正的缺口不在代码，在交付通道。** 本地仓库 `git log` 为 0 提交，远端 `XLJFZ/xjtu-timetable-calendar` 只有 3 个文件（README / LICENSE / .gitignore）、**无任何代码**。也就是说：门户上「开发中」的标注是对的，但代码实际上从未离开本机。

3. **四项必须优先修正**（详见 §6）：
   - `.gitignore` 静默排除了 `examples/schedule.example.json` —— README 指定的作息表模板，一旦推送，用户按文档操作会找不到文件；
   - README 已与实际代码漂移（测试数、项目结构、路线图完成度）；
   - **README / 测试 / 源码中散布真实课程名与真实教室号**，将随公开仓库外泄（本轮已清洗，见 §6.0）；
   - `login → fetch` 已用真实数据跑通（18 行真实课表落盘），但 `export` **从未产出过一份真实 `.ics`** —— 校历与作息表未配置，导出层至今只经受过单元测试验证。

---

## 1. 分层与数据模型

### 1.1 四层职责划分

设计上刻意让「易变的部分」各自独立成层，任一层变化不影响其他层。

| 层 | 模块 | 输入 | 输出 | 唯一职责 |
|---|---|---|---|---|
| 取数层 | `auth.py` / `fetcher.py` | eHall 会话 | 原始 JSON | 拿到本人有权访问的课表 JSON |
| 适配层 | `parser.py` | 原始 JSON | `Course` / `CourseMeeting` | **接口变更的唯一适配点** |
| 换算层 | `academic_calendar.py` / `schedules.py` | 教学周 / 节次 / 日期 | 具体日期 / 具体钟点 | 把「规则」落到「时刻」 |
| 导出层 | `exporter.py` | `CourseMeeting` + 换算结果 | `.ics` | RFC 5545 序列化与 UID 生成 |

关键的**职责边界**在于换算层内部的两分：

- `AcademicCalendar` 只回答「**第 N 教学周的星期 X 是哪一天**」；
- `ScheduleTable` 只回答「**某日期、第 K 节是几点到几点**」。

两者互不知道对方的存在。这是本设计能容纳「学期中途切作息」的根本原因。

### 1.2 数据模型

模型的中心是 `CourseMeeting`（`src/xjtu_calendar/models.py`）：

```python
@dataclass(frozen=True)
class CourseMeeting:
    course_id: str | None
    course_name: str
    weekday: int          # 1 = 星期一 … 7 = 星期日
    periods: list[int]    # 节次「序号」，不是时间
    weeks: list[int]      # 教学周
    teacher: str | None
    location: str | None
    campus: str | None
    raw_week_text: str | None    # 原始文本留档，便于排查解析偏差
    raw_period_text: str | None
```

**原始课表事实只有三样：`weekday` + `periods` + `weeks`。** 钟点不属于课表事实，而属于「该日期当天学校执行哪套作息规则」。

配套模型：

| 模型 | 位置 | 职责 |
|---|---|---|
| `Semester` | `models.py` | 学期标识 + `first_week_monday`（第 1 教学周周一，唯一日期锚点）+ `total_weeks`（**可选**，仅作校验上限，见 §6.0.2） |
| `Course` | `models.py` | 课程元信息（与上课时间无关） |
| `ScheduleProfile` | `models.py` | 一套作息：节次序号 → `("HH:MM", "HH:MM")` |
| `SchedulePeriod` | `models.py` | 作息生效区间 `[start, end]` → `profile` |
| `CalendarEvent` | `models.py` | 已带具体日期与时刻的「一次实际上课」 |
| `DateOverride` | `academic_calendar.py` | 对特定日期的教学干预 |
| `ScheduleTable` | `schedules.py` | `profiles` + `periods` 的查询容器 |

### 1.3 不可动摇的不变量

以下五条属于设计红线，违反任一条即视为设计回归。前四条已有测试守卫。

| # | 不变量 | 理由 | 守卫 |
|---|---|---|---|
| 1 | `CourseMeeting` 不得出现任何钟点字段 | 作息切换不应污染数据模型 | `test_exporter.py::test_course_meeting_has_no_time_fields`、`test_parser_real.py::test_real_fixture_no_time_fields_leak` |
| 2 | 时区恒为 `Asia/Shanghai`，不读系统时区 | 中国无 DST；用户可能在任意时区运行 | `timeutil.TZ_XIAN` 单点定义 |
| 3 | 不使用 `RRULE` 表达整学期课程 | 作息切换会让 `RRULE` 展开出错误钟点 | `test_exporter.py::test_render_ics_does_not_use_rrule` |
| 4 | UID 稳定，且**不含时间与地点** | 换教室/作息调整应视为「同一事件的新版本」 | `test_exporter.py` UID 用例组 |
| 5 | 缺配置时明确报错，不猜测默认值 | 静默的错误默认值比明确报错危险得多 | `ScheduleNotConfigured` 异常路径 |

### 1.4 关键接口契约

| 函数 | 签名要点 | 前置条件 | 后置条件 / 异常 |
|---|---|---|---|
| `TimetableParser.parse` | `(payload) -> (list[Course], list[CourseMeeting])` | payload 已 `json.loads` | 跳过项记入 `self.report`，不静默丢弃 |
| `Semester.week_to_date` | `(week, weekday) -> date` | `week >= 1`，`1 <= weekday <= 7` | 越界抛 `ValueError` |
| `Semester.__post_init__` | — | `first_week_monday` 必须是周一 | 否则抛 `ValueError` |
| `ScheduleTable.resolve_schedule_profile` | `(day) -> ScheduleProfile` | — | 无覆盖区间或缺 profile 抛 `ScheduleNotConfigured` |
| `ScheduleTable.resolve_period_time` | `(day, periods) -> (datetime, datetime)` | `periods` 非空 | 缺节次定义抛 `ScheduleNotConfigured`；返回带时区 |
| `make_uid` | `(semester_key, meeting, day) -> str` | — | 同输入必得同输出（幂等） |
| `build_events` | `(meetings, calendar, schedules, *, with_override_notes=True) -> list[CalendarEvent]` | — | 按 `start` 升序；同日同 UID 去重 |
| `render_ics` | `(events, *, calendar_name, prodid, dtstamp=None) -> str` | — | CRLF 行尾；缺 `icalendar` 抛 `CalendarExportError` |

---

## 2. eHall 适配层

### 2.1 端点配置

端点定义放在 `config/ehall_endpoints.json`，随仓库分发（该文件自身声明「必须保持可公开」，不含凭据）。

```json
{
  "endpoints": [
    { "name": "current_semester", "method": "POST",
      "path": "/jwapp/sys/wdkb/modules/jshkcb/dqxnxq.do",
      "required": false },
    { "name": "timetable", "method": "POST",
      "path": "/jwapp/sys/wdkb/modules/xskcb/xskcb.do",
      "required": true }
  ]
}
```

两个路径均来自 **2026-09-20 的真实网络观测**（`scripts/probe_ehall.py` 捕获，报告见 `_notes/ehall-probe.md`），非猜测。

**占位符防护**：`fetcher.PLACEHOLDER_MARKER = "REPLACE_WITH_OBSERVED_PATH"`。任何路径含该标记的端点会被 `load_endpoints` 忽略、被 `require_endpoint` 拒绝。这是防止「拿模板去打真实请求」的硬闸。

### 2.2 取数路径与回退

| 路径 | 实现 | 适用 | 依赖 |
|---|---|---|---|
| HTTP（首选） | `fetch_via_http` | 已知端点，轻量 | `httpx` |
| 浏览器（兜底） | `fetch_via_browser` | 不猜接口，只观察前端真实请求 | `playwright` + 持久化 profile |

浏览器路径使用 `launch_persistent_context(user_data_dir=...)` 指向系统 Edge/Chrome（`config.find_browser()` 自动探测：Edge → Chrome → None），**不下载浏览器内核**，且登录态跨进程保留。

### 2.3 错误分类

这一层最容易做错的地方：**会话失效时 eHall 以 `200 + 登录页 HTML` 响应接口请求**，而不是 401。若笼统报「响应不是合法 JSON」，用户会以为是程序 bug。

`classify_body()` 把响应体分成 `json` / `login-html` / `html` / `text` / `empty` 五类，据此分流：

| 响应形态 | 判定 | 异常 | 退出码 | 重试 |
|---|---|---|---|---|
| 401 | 会话过期 | `AuthenticationExpired` | 3 | **否** |
| 403 | 无权限 | `PermissionDenied` | 4 | **否** |
| 200 + 登录页 HTML | 会话过期（**非解析错误**） | `AuthenticationExpired` | 3 | **否** |
| 200 + 空体 | 异常 | `TimetableFetchError` | 1 | 否 |
| 200 + 非 JSON 非登录页 | schema 变化 | `TimetableFetchError` | 1 | 否 |
| 408/425/429/5xx | 可能自愈 | 有限次退避 | 1 | 是（默认 3 次，基数 1.5） |
| 网络层异常 | 可能自愈 | 有限次退避 | 1 | 是 |

**请求串行，不并发。** 全局 `--debug` 打开详细异常。

### 2.4 字段候选与「结构化优先」原则

`parser.py::FIELD_CANDIDATES` 是接口变更的唯一适配点。它用「候选键名列表 + 大小写不敏感匹配」应对教务系统命名不统一的问题。

| 字段 | 首选键（真实观测） | 兼容键 | 取值策略 |
|---|---|---|---|
| `course_id` | `KCH` | `courseId` / `courseCode` / `kch` | 单值 |
| `course_name` | `KCM` | `courseName` / `kcmc` | 单值 |
| `teacher` | `SKJS` | `teacher` / `teacherName` | 单值 |
| `location` | `JASMC` | `location` / `classroom` / `cdmc` | 单值 |
| `campus` | `XXXQDM_DISPLAY` | `campus` / `campusName` | 显式字段优先，缺失时从地点文本粗判 |
| `weekday` | `SKXQ` | `weekday` / `weekDay` / `xqj` | 支持中文/英文/数字多种写法 |
| `periods` | **`KSJC` + `JSJC`** | `periods` / `jcs` | **结构化区间优先**，文本解析仅作回退 |
| `weeks` | **`SKZC` 位掩码** | `ZCMC` / `weeks` / `zcd` | **结构化位掩码优先**，展示串仅作回退 |

**「结构化优先于文本」是本层的核心决策**，有两处体现：

1. 节次：真实响应同时提供 `KSJC`/`JSJC`（起止节次整数）与 `YPSJDD`（如 `"1-14周 星期一 1-8节 A-1008"` 的整串展示）。**取结构化整数，不解析展示串。**
2. 周次：真实响应提供 `SKZC`（如 `"1111111100000000"` 的 01 位掩码，第 i 位为 1 ⇔ 第 i+1 周上课）与 `ZCMC`（如 `"1-2周,5周"`）。位掩码是权威事实，`ZCMC` 仅供回显与排障。

文本解析器（`weeks.py` / `periods.py`）仍然保留并做了充分测试（78 + 40 例覆盖全角、单双周、混合区间、括号噪声），但它们的定位是**回退路径与旧系统适配**，不是主路径。

**候选列表刻意保持短。** 纯猜测的键名一律不收录 —— 候选越长误匹配风险越高。反例：`xm` 在西交大语义是**学生姓名**，绝不能当教师字段。

### 2.5 本层未决事项

| # | 未决项 | 风险 | 建议验证方式 |
|---|---|---|---|
| A1 | `XNXQDM` 参数是否必需、取值是否随会话变化 | 中 | 用真实会话分别带/不带该参数各请求一次 |
| A2 | 学期代码格式（`2026-2027-1`）是否为唯一形态 | 中 | 连续观察两个学期 |
| A3 | `xskcb` 是否可返回非当前学期数据 | 低 | 传一个历史学期代码试探（**注意：仅限本人有权访问的数据**） |
| A4 | `JXBID`（教学班）/ `BJDM`（班级）未参与去重 | 低 | 当前以 `KCH` 去重得 9 门课，与观测一致，暂无需变更 |

---

## 3. ICS 导出语义

### 3.1 UID 规则

```
UID = sha256( semester_key | stable_course_key | course_name | 实际上课日期 | 节次列表 ).hexdigest()[:32]
      + "@xjtu-timetable-calendar"
```

其中 `stable_course_key = course_id or "name:" + course_name`（接口缺 `course_id` 时的降级方案，保证稳定性）。

**刻意不纳入哈希的字段：时间、地点。**

| 变化场景 | UID | 日历客户端行为 | 是否符合预期 |
|---|---|---|---|
| 换教室 | 不变 | 原地更新地点 | ✅ |
| 作息调整（钟点变） | 不变 | 原地更新时间 | ✅ |
| 不同日期 | 变 | 新建事件 | ✅ |
| 不同节次 | 变 | 新建事件 | ✅ |
| 重复导出同一份课表 | 不变 | 无重复 | ✅ |

### 3.2 VEVENT 粒度与 RRULE 禁用

**每次实际上课生成一个独立 VEVENT**，不为整学期课程使用 `RRULE`。

反面论证：`FREQ=WEEKLY;COUNT=16` 只携带一个 `DTSTART`。若第 8 周后切换冬季作息，`RRULE` 展开出的第 9–16 周事件仍沿用第 1 周的钟点，与实际课表不符 —— **而这是西安交大每年都会发生的事**。单双周、不连续周、节假日停课、调课补课、换教室同样无法表达。

一学期约数百个事件，对现代日历应用不构成负担。

### 3.3 属性映射

| VEVENT 属性 | 来源 | 备注 |
|---|---|---|
| `UID` | `make_uid(...)` | §3.1 |
| `DTSTAMP` | `now_local()`，每次渲染取一次 | 全事件共享同一时间戳 |
| `DTSTART` / `DTEND` | `ScheduleTable.resolve_period_time(day, periods)` | 带 `Asia/Shanghai` |
| `SUMMARY` | `meeting.course_name.strip()` | 只放课程名，不塞信息 |
| `LOCATION` | `meeting.full_location`，可被 override 覆盖 | 校区与教室去重拼接 |
| `DESCRIPTION` | 教师 / 教学周 / 节次 / 本周序 / 课程编号（+ 覆盖备注） | 结构化多行 |

日历级属性：`PRODID` = `-//xjtu-timetable-calendar//XJTU Personal Timetable Export//CN`，`VERSION:2.0`，`CALSCALE:GREGORIAN`，`METHOD:PUBLISH`，`X-WR-CALNAME`，`X-WR-TIMEZONE:Asia/Shanghai`。

序列化交给 `icalendar` 库完成 —— ICS 的 75 字节折行与转义规则（逗号、分号、反斜杠、换行）极易手写出错。

### 3.4 本层未决事项（**本设计最薄弱处**）

| # | 未决项 | 说明 |
|---|---|---|
| B1 | **未写 `SEQUENCE` / `LAST-MODIFIED`** | 当前仅靠 `UID` 相同 + `DTSTAMP` 更新来让客户端覆盖。对「双击导入静态 .ics」这一当前主用法，实践上可行；但路线图中的 **URL 订阅式 ICS** 会依赖这个语义，届时不写 `SEQUENCE` 可能导致客户端不刷新。**建议在 v0.2 补上**（`SEQUENCE` 取课表数据版本号或导出行数派生值）。 |
| B2 | 同 UID 同日去重静默跳过 | `build_events` 中 `seen_uids` 命中时 `continue`，**不报告**。若真实数据出现同课程同日同节次的重复行，用户不会感知。建议计入解析报告。 |
| B3 | 未生成 `VTIMEZONE` | 依赖客户端对 IANA 时区的处理。主流客户端均可接受，但严格合规性上存疑。 |
| B4 | 未设置 `TRANSP` / `STATUS` / `VALARM` | 有意保持最小；是否加提醒属产品决策。 |

---

## 4. 调课与校历干预

这是三个业务难点中的第三个。当前机制**已就位但不完整**。

### 4.1 现有机制与优先级

`DateOverride` 提供三种用法，按优先级从高到低：

| 优先级 | 字段 | 效果 | 在 `build_events` 中的处理 |
|---|---|---|---|
| 1 | `cancel=True` | 该日整日停课 | `calendar.is_excluded(day)` → 丢弃 |
| 2 | `skip_meeting_keys` | 该日仅跳过指定课程 | `override.skips(meeting)` → 丢弃 |
| 3 | `location` | 覆盖上课地点（换教室） | 替换 `CalendarEvent.location` |
| 4 | `note` | 追加 DESCRIPTION 备注 | 拼接到 `description` |

另有一类全局机制：`excluded_dates`（全校停课日集合），与 `overrides.cancel` 等价但更简洁。

停课日**直接从事件流中丢弃，不生成「已取消」事件**。理由是：日历里出现一门不上的课，比不出现更糟。

### 4.2 能力缺口

| # | 缺口 | 影响 | 严重度 |
|---|---|---|---|
| C1 | **无法表达「新增事件」** | `DateOverride` 只能删（cancel / skip）或改（location / note），**不能加**。补课（把周一第 1-2 节补到周六）无法表达 —— 因为周六在 `weeks × weekday` 网格上不存在对应事件，没有任何事件可供"覆盖"。 | **高** |
| C2 | 调课需用户手写两条规则 | 「周一调至周三」= 周一 `skip` + 周三 需要 C1 的能力。当前两步都做不到第二步。 | 高（依赖 C1） |
| C3 | 无调课**检测** | 路线图列出的「新旧课表比对，输出新增/删除/时间变化/教室变化」未实现。用户需要自己比对才发现课表变了。 | 中 |
| C4 | 停课/调课数据全部手工录入 | `excluded_dates` 需从校历抄录，无自动获取。 | 中 |
| C5 | `overrides` 无冲突检测 | 两个 override 命中同一日期时的行为未定义（`dict` 后写覆盖前写）。 | 低 |

**C1 是需要在 v0.2 解决的实质设计问题。** 可选方案见 §7。

### 4.3 C1 的硬性要求：宁可失败，不可静默产出错误日期

2026 秋季学期**本身**就带两个真实边界案例（已由官方来源核实，见 §7 D8 / D9）：

| 日期 | 学校安排 | 需要的表达 |
|---|---|---|
| 2026-09-20（第 1 周星期日） | **停**原星期日课程，**改上** 10-06（第 4 周星期二）的课 | 在 09-20 **新增**一个「第 4 周星期二」的事件 |
| 2026-10-10（第 4 周星期六） | **停**原星期六课程，**改上** 10-07（第 4 周星期三）的课 | 在 10-10 **新增**一个「第 4 周星期三」的事件 |

两条都落到 C1：需要在原本**没有**事件的日期上凭空加一个事件。
v0.1 的 `DateOverride` 只能删（`cancel` / `skip`）或改（`location` / `note`），**表达不了**。

**硬要求（不是建议）**：`export` 遇到无法表达的调课时，必须 **fail 或明确 report**，
**禁止输出「表面成功、但日期错误」的 `.ics`。**

理由与 §4.1 那条「停课日直接丢弃、不生成已取消事件」同源：
**一个日期错误的日历比没有日历更糟** —— 用户会照着它去上课，而错误是静默的。
因此第 12 步（真实 `.ics` 穿透）的验收标准里，必须包含
「这两个调课日被**显式报出**而不是被忽略」。

### 4.4 已核实的校历与作息数据（2026–2027 学年第一学期）

**D8 —— 校历锚点（已核实）**

```yaml
academic_year: "2026-2027"
semester: 1
week_1_monday: "2026-09-14"
```

两条官方证据互证：学校暑期通知明确 9 月 14 日各校区同步开课；
教务处 9 月 15 日的调休通知把 9 月 20 日标为「第 1 周星期日」。
由此 `cxjcs.do` 的 `XQKSRQ=2026-09-14` 可以直接采信。

**D9 —— 作息表（已提供）**

现行规则：**5 月 1 日**切换夏秋季作息，**10 月 1 日**切换冬春季作息；
教学时间各校区保持同步。

| 节次 | 夏秋季（05-01 起） | 冬春季（10-01 起） |
|---|---|---|
| 1 | 08:00 – 08:50 | 08:00 – 08:50 |
| 2 | 09:00 – 09:50 | 09:00 – 09:50 |
| 3 | 10:10 – 11:00 | 10:10 – 11:00 |
| 4 | 11:10 – 12:00 | 11:10 – 12:00 |
| 5 | 14:30 – 15:20 | **14:00 – 14:50** |
| 6 | 15:30 – 16:20 | **15:00 – 15:50** |
| 7 | 16:40 – 17:30 | **16:10 – 17:00** |
| 8 | 17:40 – 18:30 | **17:10 – 18:00** |
| 9 | 19:40 – 20:30 | **19:10 – 20:00** |
| 10 | 20:40 – 21:30 | **20:10 – 21:00** |

**⚠️ 本学期的结构性事实：它跨越两套作息。**
9 月 14 日开学时执行夏秋季，**2026-10-01 起切换冬春季**。
因此 **profile 不能绑定在 semester 上**，必须支持「按日期选择生效的那套」：

```yaml
schedule_profiles:
  - effective_from: "2026-09-14"
    profile: xjtu-summer-autumn
  - effective_from: "2026-10-01"
    profile: xjtu-winter-spring
```

这与 §1 的数据模型红线是一致的另一半：钟点既不进 `CourseMeeting`，
也不该由「学期常量」决定，而是**由 `date` 落在哪个区间决定**
—— 正好落在 `schedules.py` 的 `SchedulePeriod` 职责上。

---

## 5. 目录结构

```
xjtu-timetable-calendar/
├── README.md
├── LICENSE
├── .gitignore
├── pyproject.toml
├── docs/
│   └── design-v0.1.md                  # 本文档
├── config/
│   ├── ehall_endpoints.json            # 端点定义（真实观测，随仓库分发）
│   └── ehall_endpoints.example.json    # 端点定义模板
├── src/xjtu_calendar/
│   ├── __init__.py
│   ├── __main__.py                     # python -m xjtu_calendar
│   ├── cli.py                          # 命令行入口
│   ├── config.py                       # 集中配置（URL / appId / 目录 / 网络约束）
│   ├── errors.py                       # 异常体系 + 退出码
│   ├── logging_setup.py                # 日志与递归脱敏
│   ├── models.py                       # ★ 数据模型（红线 1）
│   ├── weeks.py                        # 教学周文本解析（回退路径）
│   ├── periods.py                      # 节次文本解析（回退路径）
│   ├── academic_calendar.py            # 教学周 → 日期 + 干预规则
│   ├── schedules.py                    # ★ 作息表 / 节次 → 钟点
│   ├── parser.py                       # ★ eHall JSON → 标准化模型
│   ├── auth.py                         # 会话管理
│   ├── fetcher.py                      # 课表抓取（HTTP / 浏览器）
│   ├── exporter.py                     # ★ → CalendarEvent → .ics
│   └── timeutil.py                     # 时区常量（Asia/Shanghai）
├── scripts/
│   └── probe_ehall.py                  # eHall 接口分析工具
├── tests/
│   ├── conftest.py
│   ├── test_weeks.py                   # 78 例
│   ├── test_periods.py                 # 40 例
│   ├── test_calendar.py
│   ├── test_parser.py
│   ├── test_parser_real.py             # 基于真实脱敏固件
│   ├── test_fetcher.py
│   ├── test_exporter.py
│   └── fixtures/
│       ├── timetable_sample.json           # 手工构造样例
│       └── ehall_timetable_real_sanitized.json  # 真实结构脱敏固件
└── examples/
    ├── academic_calendar.example.json
    └── schedule.example.json           # ⚠️ 当前被 .gitignore 误排除，见 §6-1
```

**用户数据目录**（`~/.xjtu-timetable-calendar/`，可用 `XJTU_CALENDAR_HOME` 覆盖）：

```
session/     storage_state.json + profile/   ← 等同于凭据，绝不提交
semesters/   <semester-key>.json             ← 校历，用户填
schedules/   schedule.json                   ← 作息表，用户填
raw/         timetable-<key>.json            ← 原始课表，含个人信息，绝不提交
```

**提交边界**：源码、测试固件（脱敏）、端点定义、示例配置 → 提交；会话、原始课表、导出 .ics、`_notes/` → 排除。

---

## 6. 现状核查（漂移清单）

以下十二项为**本轮实测**结果，非推断。每项均附验证方式。
（6-1 至 6-10 为设计评审时发现；6-11 与 6-12 为清理完成后的回归验证中新发现。）

### 6.0 修复记录（2026-09-20 第二轮：仓库卫生）

按 D5 / D6 / D7 一次性处理。**本轮范围严格限定为发布卫生，零业务行为变化** ——
未改动 `parser.py` / `exporter.py` / `fetcher.py` 的任何逻辑，未顺手修 §4.2 的补课缺口
（那是数据模型变更，需单独立设计决策，不能与清理混在同一提交里）。

| 条目 | 处理 |
|---|---|
| 6-1 | `.gitignore` 追加 `!examples/*.example.json`；`examples/schedule.example.json` 已进入暂存 |
| 6-4 | 真实课程名 / 教室号 / 暗示专业方向的课程编号全部替换为统一虚构值（`示例课程甲/乙/丙/丁` + `A-1001…A-1004`）；涉及 README、4 个测试文件、3 个源文件 docstring、2 个固件 |
| 6-5 | README 测试数改为实测 263；`.workbuddy/memory/MEMORY.md` 的 218 亦为过时值 |
| 6-6 | README 项目结构树补全 `docs/`、`config/ehall_endpoints.json`、`test_parser_real.py`、`test_fetcher.py`、真实脱敏固件 |
| 6-7 | 路线图前两项勾选，并新增三条「待完成」（首份真实 `.ics`、`SEQUENCE`、补课能力） |
| 6-8 | `_notes/structure.md` 重写为真实 `datas.xskcb.rows` / `KCH` / `KCM` 结构（47 字段、`SKZC` 掩码语义、旧英文键结构作废声明） |
| 6-9 | `.gitignore` 排除 `.workbuddy/`；`git rm --cached` 移出暂存区（本地文件保留） |
| 6-10 | `pyproject.toml` 删除指向不存在目录的 `package-data` 死配置 |

**保留不动的三处**（属功能代码或公开信息，不是个人数据）：

1. `parser.py::_guess_campus` 的校区标记（`创新港` / `兴庆` / `雁塔` / `曲江`）——
   校区名是公开信息，且该函数是功能性回退逻辑，改动即属业务行为变更。
2. 示例中的 `创新港校区` / `兴庆校区` —— 同上，且用于验证校区字段与去重拼接。
3. `models.py` 中关于「避免『创新港校区』与『创新港』重复」的注释 —— 解释去重逻辑。

**验收**：`pytest -q` 仍为 **263 passed**（清理前后同值），证明测试语义未被改动。

**回归中新发现、本轮不动**：6-11（`ruff check` 961 处来自误用的 `RUF001-003` 规则，
属 lint 策略决策）与 6-12（lint / 类型检查工具未声明为依赖，且 `mypy` 3 处不通过）。
两者都不是「泄露」类问题，且 6-12 的修复点位于 `exporter.py`，与本轮约定冲突，故记录待定。

**未处理（留给下一轮）**：6-2（本地零提交 / 远端无代码）与 6-3（真实 `.ics` 未产出）。
前者需要推送通道，后者需要 D8/D9 的校历与作息数据 —— 两者都已从「仓库卫生」进入「业务验证」。

### 6.0.1 修复记录（2026-09-20 第三轮：开发工具链一致性 + 行尾契约）

**动机**：首个公开代码提交之前，必须保证「README 宣称能跑的命令在干净环境下真的能跑」。
第二轮结束时 README 让用户跑 `ruff` 与 `mypy`，但两者既未声明为依赖，跑起来也各有 1141 / 3 个错误 ——
照 README 搭环境第一步就失败。这比任何文档漂移都更值得在首次提交前收掉。

| 条目 | 处理 |
|---|---|
| 6-11 | `pyproject.toml` 的 `ignore` 显式加入 `RUF001`/`RUF002`/`RUF003`（附理由注释）。剩余 44 处真实问题全部修掉：36 处由 `ruff --fix` 自动修正（`RUF022` / `UP035` / `UP037` / `I001` / `F401` / `RUF100` / `UP017`），8 处手工处理（`UP031` 改用 f-string、`B007` 改为只遍历键、`SIM112` 环境变量名改大写、`B905`+`RUF007` 改用 `itertools.pairwise`、`C416` 改用 `list()`、`RUF059` 未用解包变量加下划线前缀） |
| 6-12 | `dev` 依赖补上 `ruff>=0.16` 与 `mypy>=2.0`；3 处 `mypy --strict` 错误全部修掉（`models.py::_timedelta` 补 `-> timedelta` 返回标注并上提 `timedelta` 导入；`exporter.py` 的 `cal.to_ical()` 用 `raw: bytes` 显式收窄，运行时行为不变） |
| 新增 | `.gitattributes`：`* text=auto eol=lf` + 图片类二进制排除。本仓库无 `.bat`/`.cmd`，故不引入 `eol=crlf` 例外 |
| 新增 | 删除 `_notes/demo.ics`（清理前生成的演示导出，含真实课表痕迹，无任何引用） |
| 6-8 补充 | `_notes/structure.md` 所在目录已整体被 `.gitignore` 排除，该笔记属本地开发笔记而非交付物 |

**为什么 `SIM112` 可以放心改大写**：Windows 环境变量名大小写不敏感，CPython 在 `nt` 平台上
把 `os.environ` 的键统一转成大写存储、键查找同样转大写，故 `PROGRAMFILES` 与 `ProgramFiles`
取到同一值（已实测两种拼写返回一致）。这是**命名规范化**，不是行为变更。

**为什么 `RUF001/002/003` 应当关闭而不是改写标点**：这三条规则的设计目标是识别
「形似 ASCII 的非 ASCII 字符」这类钓鱼式混淆，但它的字符表把中文全角标点
（`（）`、`「」`、`：`、`、` 等）一并算了进去。本仓库的 docstring、注释与用户可见文案
以中文写作，全角标点是**正确写法**。实测 1097 处命中占全部 RUF 命中的 96%，
全部为误报 —— 去改 1097 处标点只会降低可读性。

**验收（三条 gate 全绿）**：

```
pytest -q     → 263 passed
ruff check .  → All checks passed!
mypy src      → Success: no issues found in 16 source files
```

### 6.0.2 修复记录（2026-09-21 P0：越界周次 fail-closed）

**动机**：`build_events` 原先对「超出 `total_weeks` 的周次」执行 `continue` 静默跳过。
这会造成**最危险的一类故障**：程序退出码 0、`.ics` 正常生成、日历应用正常导入，
但**少了几周的课**，且全程无任何提示。用户会拿着一份看起来完整的日历去上课。
按风险排序，「静默生成错误结果」优先于「功能不够强」，故排在 C1 之前。

**契约（已冻结）**

| `total_weeks` | 导出行为 |
|---|---|
| `None`（省略 / `null`） | 完全依据 `CourseMeeting.weeks` 展开，**不做上限过滤** |
| 有值，且出现 `week > total_weeks` | 抛 `CalendarExportError` **终止导出**（fail-closed） |
| 有值，且出现 `week < 1` | 抛 `CalendarExportError`（非法数据） |
| 任意情况 | **绝不 `continue` 静默吞掉** |

报错信息必须包含：课程名、越界周次、当前 `total_weeks`、以及
「核对校历 / 留空 `total_weeks`」的处理指引 —— 否则用户只知道失败，不知道改哪里。

**连带变更**

| 位置 | 变更 |
|---|---|
| `models.py` | `Semester.total_weeks` 改为 `int \| None`，默认 `None`；`<= 0` 校验仅在有值时生效；`last_week_sunday` 在 `None` 时抛 `ValueError`（学期末日无定义，不给默认值） |
| `academic_calendar.py` | 新增 `_optional_int()`；`total_weeks` 缺省（`None` / 空串）为 `None`，非数字报 `ParseError` |
| `parser.py` | 新增 `DEFAULT_EXPANSION_LIMIT = 30` —— 解析边界**只**用于裸「单周/双周」展开，与导出校验解耦 |
| `cli.py` | `expansion_limit=total_weeks or DEFAULT_EXPANSION_LIMIT`，不再把 `None` 直接传给解析器 |
| `README.md` / 示例配置 | 说明 `total_weeks` 可选及其三态行为，强调「没有官方依据时宁可留空」 |

**⚠️ 顺带发现、本轮未处理**：`weeks.py` 的 `parse_weeks` / `parse_week_mask`
末尾也有 `if 1 <= w <= max_week` 的**静默截断**，属同一类问题，但作用在解析阶段。
→ **已于同日处理，见 §6.0.3。**

**验收**

```
pytest -q     → 270 passed（新增 5 项：越界报错 / None 全量展开 / week<1 报错 /
                无静默丢弃守卫 / 配置省略与非法值）
ruff check .  → All checks passed!
mypy src      → Success: no issues found in 16 source files
```

### 6.0.3 修复记录（2026-09-21 P0-2：周次解析的静默截断）

**动机**：§6.0.2 修掉导出侧的静默丢周后，同一类问题在**解析侧**仍然存在 ——
`parse_weeks` 末尾的 `if 1 <= w <= max_week` 与 `parse_week_mask` 末尾的
`if week <= max_week` 会把超界周次悄悄砍掉，然后返回一个「解析成功」的结果。
两者是同一原则的两端，必须一起封死。

**契约（已冻结）：显式输入不得被静默篡改；内部安全上限不得伪装成业务事实。**

| 输入类型 | 越界处理 |
|---|---|
| 显式周次文本（`1-18周`、`第20周`、`1,3,5,31`） | `< 1` 或 `> expansion_limit` → 抛 `WeekOutOfRangeError`，**绝不裁剪** |
| 位掩码（`SKZC`） | 第 `expansion_limit` 位之后仍有置位 → 抛 `WeekOutOfRangeError`，**绝不只取低位** |
| 裸「单周 / 双周」 | 按 `expansion_limit` 展开，**不报错** —— 它是 shorthand，上限只是展开边界 |

第三种与前两种性质不同：前两者是**用户/教务系统显式声明的**周次，越界说明
数据或配置有错；第三种是我们自己生成的展开，上限只是「不知道学期长度时的
安全边界」，**不代表学期真有 30 周**。因此 `max_week` 这一命名被废弃，
统一改为 `expansion_limit`（`weeks.py` 的公开函数与 `TimetableParser` 构造参数），
避免被误读成业务事实。原文自己声明超界范围（`1-40周（单）`）时仍按第一种报错。

**异常分流**：新增 `WeekOutOfRangeError(WeekParseError)`。两者的区别正是
**调用方该如何反应**：

- 普通 `WeekParseError` = 「这段文本我看不懂」→ 位掩码场景可回退展示串，
  文本场景记入 `report.skipped`（cli 打 warning）。这是既有行为，保留。
- `WeekOutOfRangeError` = 「看得懂，但内容越界」→ **硬失败**（parser 转成
  顶层 `ParseError`）。既不回退字段也不记为跳过 —— 静默跳过一条 meeting
  等于这门课少上课，用户看不到任何提示。

**连带变更**

| 位置 | 变更 |
|---|---|
| `weeks.py` | `max_week` → `expansion_limit`；`_Range.expand()`（内含 `min()` 裁剪）→ `_Range.weeks()`（原样展开）+ `_require_within()`（越界报错）；`parse_week_mask` 越界报错；新增 `WeekOutOfRangeError` 与 `DEFAULT_EXPANSION_LIMIT` |
| `parser.py` | `TimetableParser(max_week=…)` → `(expansion_limit=…)`；位掩码与文本两条路径都增加 `except WeekOutOfRangeError → raise ParseError` 分支 |
| `cli.py` | `expansion_limit=total_weeks or DEFAULT_EXPANSION_LIMIT` |
| 测试 | 全量替换 `max_week=` 调用；删除断言「裁剪」行为的用例，改为断言「报错」 |

**验收**

```
pytest -q     → 277 passed（新增 8 项：显式文本越界 / 位掩码越界 /
                越界异常可区分性 / 裸单双周不报错 / 单双周+显式范围仍校验 /
                week 0 / parser 层硬失败 / parser 层「看不懂」仍走跳过）
ruff check .  → All checks passed!
mypy src      → Success: no issues found in 16 source files
```

### 6.0.4 修复记录（2026-09-21：unsupported_adjustments + 首次真实 ICS 穿透）

**动机**：§4.3 的硬性要求是「调课日必须显式报出，而非静默忽略」。但工具此前没有
承载「已知却无法表达」的机制 —— 配置里写不进这件事，导出自然也就无从报起。
首次真实导出因此被卡住：直接导会**静默产出错误日历**（假期日照常排课、
借课日无事件），违反红线。

**新机制：`unsupported_adjustments`（教学日历配置的可选字段）**

```json
"unsupported_adjustments": [
  { "date": "2026-09-20", "description": "按 2026-10-06（第 4 周星期二）的课表上课" }
]
```

- 定位：**显式声明的能力边界**，不是待办标记，更不是业务事实。
  绝不伪装成 `DateOverride`，也绝不生成占位事件。
- `description` 必填（空白报 `ParseError`）—— 没有说明的「无法表达」只会让人困惑。
- 解析进 `AcademicCalendar.unsupported_adjustments`；
  导出侧新增 `collect_unsupported(calendar, events)`：找出日期落在本次事件
  跨度内的声明（事件为空时视为全部命中）。
- **处置默认 fail-closed**：抛 `UnsupportedAdjustmentError`，逐条列出明细，
  拒绝生成；`--allow-unsupported-adjustments` 显式放行后，每条转为
  `logger.warning`（⚠️ 前缀 + 「该时段事件缺失」），并继续导出。

**连带修复：ICS 落盘的二次行尾翻译（真 bug）**

`cli.cmd_export` 原先用 `write_text(ics, encoding="utf-8")` 写文件 ——
Windows 下默认 `newline=None` 会把 `render_ics` 产出的 `\r\n` 再翻译成
`os.linesep`，得到 **`\r\r\n`**，违反 RFC 5545。改为 `newline=""`（写什么是什么），
并加测试锁定「文件字节里 `\r\r\n` 必须为 0」。

**首次真实 ICS 穿透结果（2026-2027-1，9 门课 / 18 条安排）**

| 检查 | 结果 |
|---|---|
| 未声明机制时默认导出 | ✅ 拒绝并逐条列出 2 条调课（fail-closed 生效） |
| 事件数 | 100 → 补 `overrides`（10-10 全天停）后 **98** |
| 国庆周 10-01 ~ 10-07 | ✅ 0 事件 |
| 2026-09-20（借周二课表） | ✅ 0 原生事件（无周日课），缺失部分已声明 |
| 2026-10-10（借周三课表） | ✅ 原生 2 条周六事件已按通知删除，缺失部分已声明 |
| 作息切换 | ✅ 10-01 前第 5 节全部 14:30，之后全部 14:00（跨切换无错位） |
| UID | ✅ 98 个互不相同；两次导出 UID 集合完全一致（仅 DTSTAMP 变化） |
| 行尾 | ✅ 1027 个 CRLF / 0 个 `\r\r\n` |
| 时区 | ✅ 全部 `Asia/Shanghai` |

**配置层面的两个裁定（用户数据目录）**：`excluded_dates` 补入 10-01 ~ 10-07；
`overrides` 补入 10-10 全天停课（「停原周六课程」是 `DateOverride` **能**表达的
部分，不能因为新增部分表达不了就放弃能做的删除）。⚠️ 遗留待确认：
2026-09-25（周五，中秋）是否放假 —— 未核实前不写入。

**验收**

```
pytest -q     → 287 passed（新增 10 项：解析 / 范围判定 / 空事件全命中 /
                配置形状校验 / ICS 落盘行尾锁定）
ruff check .  → All checks passed!
mypy src      → Success: no issues found in 16 source files
```

### 6-1 `.gitignore` 静默排除作息表模板 【严重度：高】

`examples/schedule.example.json` 命中 `.gitignore:17` 的 `schedule*.json` 规则而被排除。排除规则中的否定项 `!tests/fixtures/*.json` 只覆盖 `tests/fixtures/`，**不覆盖 `examples/`**。

**影响**：README「必须由你配置的内容 → 2. 作息表」明确指示用户参考 `examples/schedule.example.json`。推送后该文件不存在，用户按文档操作会卡住 —— 而作息表是导出成功的必要条件。

**验证**：`git check-ignore -v examples/schedule.example.json` → 命中 `schedule*.json`；`git status` 中确实无此文件。

**建议修法**：在排除规则后追加 `!examples/*.example.json`（否定规则必须在对应排除规则**之后**）。

### 6-2 本地仓库零提交、远端无代码 【严重度：高】

| 位置 | 实际状态 |
|---|---|
| 本地 `D:\xjtu-timetable-calendar` | 分支 `master`，`git log` 报「无任何提交」，全部文件处于暂存（`A`）状态；`git remote -v` 为空 |
| 远端 `XLJFZ/xjtu-timetable-calendar` | 默认分支 `main`，仅 `.gitignore` / `LICENSE` / `README.md`，**无 `src/` / `tests/` / `config/`**；仓库大小 5KB |

**影响**：完整实现从未离开本机。门户上「开发中 / 尚无可用版本」的标注在事实上成立，但项目进展与外部可见度完全脱节。

**附带问题**：本地默认分支名 `master` 与远端 `main` **不一致**，首次推送需显式处理分支映射。

### 6-3 真实 `.ics` 从未产出 【严重度：高】

分两段看，结论不同：

| 环节 | 状态 | 证据 |
|---|---|---|
| `login` | ✅ 已跑通 | 真实登录成功，eHall 域 cookie 落盘（2026-09-20 03:26） |
| `fetch` | ✅ 已跑通 | 自动学期发现 → 18 行真实课表写入 `raw/timetable-2026-2027-1.json`；解析 18/18 与原始字段逐行全等，9 门课、4 门多时段、1 门跨教室、单双周与非连续周均正确 |
| `export` | ⛔ 未跑通 | 校历（`semesters/*.json`）与作息表（`schedules/schedule.json`）未配置 |

`export` 在此情况下抛出 `ScheduleNotConfigured` 并给出明确提示 —— **这是设计内的正确行为（红线 5），不是 bug**。

**但它意味着一个实质缺口：导出层至今只经受过单元测试验证。** 真实课表的 12 项边界情况（跨教室、非连续周、单双周、节次区间形态）只在 parse 层被证实，尚未穿过 `AcademicCalendar × ScheduleTable × exporter` 走完整条链路。日历客户端能否正确导入、UID 更新语义是否符合预期，均未验证。

**解除条件**（两项数据，来源不同）：

1. **校历** —— 可从接口自取。`POST /jwapp/sys/wdkb/modules/jshkcb/cxjcs.do` 返回 `XQKSRQ`（学期开始日期，观测值 `2026-09-14` 恰为周一）与 `ZJXZC=16` / `ZZC=18`。**注意 `XQKSRQ` 是否等于第 1 教学周周一，需与官方校历核对**（设计明确警示二者可能不同，填错会导致全部事件系统性偏移）。
2. **作息表** —— 接口不含此数据，**必须由人工提供**冬/夏季各节次钟点。这是无法自动化的部分。

### 6-4 真实课程名与教室号散布在将公开的文件中 【严重度：中】

一组**真实课程名 + 真实教室号 + 具体上课日期钟点**曾同时出现在**将随仓库公开**的文件中。
（基于最小暴露原则，此处不再复录已清洗的原值；受影响文件清单如下。）

| 文件 | 出现位置（**行号为清洗前的状态**） |
|---|---|
| `README.md` | 第 37、41、70、74 行（问题陈述、导出示例、数据模型示例） |
| `tests/test_parser.py` | 第 63、74、87、89、314、318 行 |
| `tests/test_exporter.py` | 第 93、95、242、250、380 行 |
| `tests/fixtures/timetable_sample.json` | 第 12、14 行 |
| `src/xjtu_calendar/models.py` | 第 150、189 行（docstring） |
| `src/xjtu_calendar/exporter.py` | 第 72 行（doctest） |
| `src/xjtu_calendar/academic_calendar.py` | 第 147 行（docstring） |
| `tests/test_calendar.py` | 第 304、320 行 |

（`_notes/demo.ics` 曾同样含此信息。它位于被 `.gitignore` 排除的 `_notes/`，本不随仓库分发；
但经引用检查确认没有任何测试或文档依赖它，且需要演示产物时可随时重新生成虚构版本，
故已于 2026-09-20 **删除**，不再保留这份真实数据残留。）

**背景**：这些值最初是构造出来的示例。但随着真实课表落盘，其中课程名恰好是西安交大的**真实课程**，且是**本项目作者本人选修的课程**；README 还给出了该课程的具体日期与钟点。

**影响**：公开仓库中同时出现「真实课程名 + 真实教室 + 具体上课日期时间」，结合仓库作者身份可推断出个人课表片段。这与项目自己声明的「不包含任何个人课表数据」不一致。

**对照**：`tests/fixtures/ehall_timetable_real_sanitized.json` 的处理是**正确范例** —— 课程名一律为 `示例课程甲/乙/…`，教室为 `A-1000…`，学生为 `示例学生`，学号 `3200000000`。

**处置**：已统一替换为同一套占位值（`示例课程甲/乙/丙/丁` / `A-1001…A-1004`），
并把原示例中暗示所学专业方向的课程编号改为中性编号，测试夹具变量亦一并改为中性名。详见 §6.0。

### 6-5 测试数量漂移 【严重度：中】

| 来源 | 声称 |
|---|---|
| `README.md` 「开发」节 | 227 项 |
| `.workbuddy/memory/MEMORY.md` 「命令」节 | 218 项 |
| **实测 `pytest -q`** | **263 passed in 1.82s** |

**验证**：`python -m pytest -q`（环境 `cv-project0`，icalendar 7.3.0 / pytest 9.1.1）。

### 6-6 README 项目结构树过时 【严重度：中】

README 结构树中 `config/` 只列了 `ehall_endpoints.example.json`，注释为「接口定义模板（需由接口分析填充）」。实际上 `config/ehall_endpoints.json` 已存在**并已填入真实观测到的端点路径**，且 `tests/` 也未列出 `test_parser_real.py` 与真实脱敏固件。

### 6-7 路线图完成度未更新 【严重度：中】

README「待完成」列出的前两项，**实际均已完成**：

| 路线图条目 | 实际状态 |
|---|---|
| 「用真实网络请求确认 eHall 课表接口，固化到 `config/ehall_endpoints.json`」 | ✅ 已完成（`_notes/ehall-probe.md` 163KB 观测记录 + 已填入的端点配置） |
| 「按真实响应校准 `parser.py` 的字段候选」 | ✅ 已完成（`tests/test_parser_real.py` + 26KB 真实结构脱敏固件，校准基准在文件头注明） |

仅第三项「从学校官方来源自动获取校历与作息表」确实未完成。

### 6-8 `_notes/structure.md` 描述的是虚构结构 【严重度：中】

该文件记录的字段是 `data.rows[]` 下的 `courseId / courseName / teacher / location / campus / weekday / periodText / weekText / credits`，并注明「来源：`tests/fixtures/timetable_sample.json`」。

而真实 eHall 响应是 `datas.xskcb.rows[]`，字段为 `KCH / KCM / SKJS / JASMC / SKXQ / KSJC / JSJC / SKZC / ZCMC / XXXQDM_DISPLAY / YPSJDD`（47 个字段）。

**结论**：该文档描述的是早期**手工构造的样例固件**，与真实接口无关。当前若有人照它排查问题会被严重误导。建议重命名为 `structure-sample.md` 或直接由真实固件的结构报告取代。

### 6-9 `.workbuddy/memory/` 已进入暂存区 【严重度：中】

三个文件（`2026-09-19.md` / `2026-09-20.md` / `MEMORY.md`，合计约 15KB）已 staged，将随首次提交进入**公开仓库**。内容为项目设计约定与本机环境记录，含本机绝对路径与运行环境细节。

**不含凭据**，但属工作区工具元数据，非项目交付物。姊妹仓库 `xjtu-student-tools` 的 `.gitignore` 已排除 `.workbuddy/`，此处应对齐。

### 6-10 `pyproject.toml` 存在死配置 【严重度：低】

`[tool.setuptools.package-data]` 声明 `xjtu_calendar = ["data/*.json"]`，但 `src/xjtu_calendar/data/` 目录**不存在**。

**影响**：无功能影响（setuptools 容忍不匹配的通配），属噪音。✅ 已删除该配置块（见 §6.0）。

### 6-11 `ruff check` 在自身代码库上失败 1003 处 【严重度：中】

**验证**：`ruff check src tests`（ruff 0.16.8）→ `Found 1003 errors`。

按规则分布：

| 规则 | 数量 | 性质 |
|---|---|---|
| `RUF002` | 516 | docstring 中的全角标点 |
| `RUF001` | 287 | 字符串中的全角标点 |
| `RUF003` | 158 | 注释中的全角标点 |
| 其余 13 条规则 | 42 | `RUF022` 未排序 `__all__`、`UP035` 弃用导入、`F401` 未用导入等 |

**根因是配置，不是代码。** 前 961 处（95.8%）全部来自 `RUF001/002/003`
（ambiguous unicode character）—— 该规则族把**中文全角标点视为易混淆字符**。
对一个以中文写文档与注释的项目，这属于规则误用：
`[tool.ruff.lint].select` 引入的 `RUF` 族顺带打开了这三条，
而它们的适用场景是「代码库混入易混淆 Unicode 以达到视觉欺骗」，与中文注释无关。

**建议**：在 `ignore` 中显式排除，而非修改 961 处标点：

```toml
ignore = ["E501", "RUF001", "RUF002", "RUF003"]
```

之后剩余约 42 处，其中 34 处可 `--fix` 自动修复。

**状态**：✅ 已处理（2026-09-20 第三轮，见 §6.0.1）。按建议在 `ignore` 中显式排除这三条规则；
其余 44 处真实问题全部修掉，`ruff check .` 现为 `All checks passed!`。

> **数字修正**：本节标题与正文的 1003 / 961 来自更早一次 `ruff check src tests` 的观测，
> 与第三轮 `ruff check .`（范围额外包含 `scripts/`）并非同一口径。以第三轮实测为准：
> 全仓 **1141** 处，其中 `RUF001/002/003` 合计 **1097** 处（96.1%），其余 **44** 处才是真实问题。
> 结论不变 —— 仍然「根因是配置，不是代码」。

### 6-12 lint / 类型检查工具未声明为依赖，且 `mypy` 不通过 【严重度：中】

两件事，相关但独立。

**(a) 声明缺失。** README「开发」节指示运行 `ruff check src tests` 与 `mypy src`，
但 `[project.optional-dependencies].dev` 只有
`pytest` / `pytest-cov` / `httpx` / `playwright` —— **既无 ruff 也无 mypy**。
按 README 执行 `pip install -e ".[dev]"` 后这两条命令会直接 command not found。

**(b) `mypy src` 不通过**（mypy 2.3.1，`strict = true`）：

| 位置 | 错误 |
|---|---|
| `models.py:110` | `Returning Any from function declared to return "date"` |
| `models.py:113` | `Function is missing a return type annotation`（`_timedelta` 辅助函数） |
| `exporter.py:258` | `Returning Any from function declared to return "str"`（`cal.to_ical()` 返回 `Any`） |

三处均为**既有问题**，非本轮清理引入（`git diff` 未触及这些行）。
前两处同一根因：`models.py` 内部用了一个延迟导入的 `_timedelta` 辅助函数且未标注返回类型；
第三处需要给 `icalendar` 的返回值加类型收窄。

**状态**：✅ 已处理（2026-09-20 第三轮，见 §6.0.1）。两项都已收掉：
(a) `dev` 依赖补上 `ruff>=0.16` 与 `mypy>=2.0`；
(b) 三处 `mypy` 错误全部修掉（`models.py` 的 `_timedelta` 补返回标注并上提导入，
`exporter.py` 的 `to_ical()` 用 `raw: bytes` 收窄）—— 均为**类型标注层面**的改动，
运行时行为不变。`mypy src` 现为 `Success: no issues found in 16 source files`。

> 第二轮的约定是「不碰 `exporter.py` **的逻辑**」，而非「不碰这个文件」。
> 第三轮只在该文件加了一行变量类型标注，未触碰任何控制流，与本约定不冲突。

---

## 7. 待裁定事项

| # | 事项 | 选项 | 我的建议 |
|---|---|---|---|
| **D1** | 本文档定位 | (a) 追认现状为 v0.1 基准，只修漂移；(b) 推翻现状重做 | **(a)**。现有实现质量高、红线有测试守卫，重做没有收益 |
| **D2** | 首次推送的分支 | (a) 本地改名 `master` → `main` 后推送；(b) 推送 `master` 并改远端默认分支 | **(a)** ✅ 已执行，与姊妹仓库 `xjtu-student-tools` 的 `main` 保持一致 |
| **D3** | 门户「开发中」标注 | (a) 代码推送后即改为「可用」；(b) 保持「开发中」直到 v0.1.0 release | **(b)**。真实 `.ics` 尚未产出（§6-3），此时标「可用」会引导用户走进已知断点 |
| **D4** | 补课/调课能力（缺口 C1） | (a) 扩展 `DateOverride` 增加 `add_meetings` 字段；(b) 新增独立的 `extra_events` 配置段；(c) 暂不实现 | **(a)**。语义最贴近现有结构，`overrides` 已是「按日期查规则」的形状 |
| **D5** | 推送前清理 | (a) 移除 `.workbuddy/` 出暂存区 + 修 `.gitignore` 后推送；(b) 直接推送 | **(a)** ✅ 已执行（见 §6.0） |
| **D6** | 示例数据清洗 | (a) 全部替换为统一占位值（`示例课程甲/乙/丙/丁` / `A-100x`）；(b) 只改 README 保留测试；(c) 保持不动 | **(a)** ✅ 已执行。理由：§6-4 的真实课程名+教室+具体日期组合会外泄个人课表片段，且与项目声明的「不含个人课表数据」冲突 |
| **D7** | README 与 `_notes/structure.md` 漂移 + `pyproject.toml` 死配置 | (a) 本轮一并修正；(b) 留待后续 | **(a)** ✅ 已执行 |
| **D8** | 校历数据来源 | (a) 用 `cxjcs.do` 的 `XQKSRQ` 起草，与官方校历核对后确定；(b) 用户直接人工录入 | **(a)** ✅ **已核实**。两条官方证据互证：暑期通知「9 月 14 日各校区同步开课」+ 教务处 9-15 调休通知把 9-20 标为「第 1 周星期日」。**第 1 教学周周一 = 2026-09-14**，`XQKSRQ` 是真值，可采信。数据见 §4.4 |
| **D9** | 作息表钟点 | 接口不含此数据，**无法自动化** | ✅ **已提供**：夏秋 / 冬春两套各 10 节，切换点 05-01 / 10-01，见 §4.4。**注意本学期跨两套作息**，profile 必须按 `effective_from` 生效，不能绑定在 semester 上 |
| **D10** | `.gitattributes` 行尾契约 | (a) 首次提交前加 `* text=auto eol=lf` + 二进制排除；(b) 暂不加，留待有需要时 | **(a)** ✅ 已执行。首次公开提交正是定契约的最佳时机；本仓库无 `.bat`/`.cmd`，故不引入 `eol=crlf` 例外 |
| **D11** | README 宣称的 ruff / mypy 不可跑 | (a) 首次提交前把依赖与告警一并收掉；(b) 先提交、以后再修 | **(a)** ✅ 已执行（§6.0.1）。首个公开快照必须是「测试 / lint / 类型检查 / 示例文件 / 文档」五者自洽的版本，否则用户照 README 搭环境第一步就失败 |
| **D12** | 本地真实数据残留与重复 skill | (a) 就地清掉；(b) 保留 | **(a)** ✅ 已执行。`_notes/demo.ics`（无引用的清理前演示产物）删除；用户级 skills 目录里的旧副本移出扫描范围 |

---

## 8. 下一步（按依赖顺序）

| 序 | 动作 | 依赖 | 状态 |
|---|---|---|---|
| 1 | 裁定 §7 的 D1–D12 | — | ✅ D5–D7 第二轮执行；D10–D12 第三轮执行 |
| 2 | 修 `.gitignore`（补 `!examples/*.example.json`、排除 `.workbuddy/`），`git rm --cached` 清理暂存区 | D5 | ✅ 完成 |
| 3 | 示例数据清洗：README / tests / src docstring 中的真实课程名与教室号 → 占位值 | D6 | ✅ 完成 |
| 4 | 修正 README 的测试数 / 项目结构 / 路线图；重写 `_notes/structure.md`；清理 `pyproject.toml` 死配置 | D7 | ✅ 完成 |
| 5 | 全量回归：`pytest` | 2–4 | ✅ 263 passed（P0 后为 270，见 §6.0.2） |
| 6 | 新增 `.gitattributes`（`eol=lf` + 二进制排除） | D10 | ✅ 完成 |
| 7 | 开发工具链一致性：`dev` 补 ruff / mypy；配置 `RUF001-003`；修掉 44 处 lint 与 3 处 mypy | D11 | ✅ 三条 gate 全绿 |
| 8 | 删 `_notes/demo.ics`；用户级 skills 目录的旧副本移出扫描范围 | D12 | ✅ 完成 |
| 9 | **首次公开代码提交**（本地 `bbb8a20`，分支 `main`；主题已 amend 为 `feat:`） | 2–8 | ✅ 已完成 |
| 10 | 起草校历配置（`cxjcs.do` 的 `XQKSRQ`）+ 人工核对第 1 教学周周一 | D8 | ✅ 已核实：`week_1_monday = 2026-09-14`（§4.4） |
| 11 | 补齐作息表钟点 | D9 | ✅ 数据已提供：夏秋 / 冬春两套 profile + `effective_from`（§4.4） |
| 12 | 跑通真实 `export`，用日历客户端导入验证（UID 更新语义、时区、跨作息切换） | 9–11 | ⬜ **下一步**。验收必须包含：① 10-01 前后同一节次钟点正确切换；② 09-20 / 10-10 两个调课日被**显式报出**而非静默忽略（§4.3） |
| 13 | 推送：以远端 `main` **现有 HEAD**（`af6bfa3`）为 parent 做原子提交，`force:false`（git 协议在本机不通）。**绝不 force 覆盖远端已有的 Initial commit** | 9 | ✅ 已完成：远端 `b81fa79`，parent = `af6bfa3`，推送后四项验证全绿 |
| 14 | **P0：越界周次 fail-closed**（`total_weeks` 改可选 + 删除静默 `continue`） | — | ✅ 已完成（§6.0.2），提交 `fix(export): fail closed on out-of-range course weeks` |
| 15 | **P0-2：周次解析的静默截断**（`max_week` → `expansion_limit` + 显式越界报错） | 14 | ✅ 已完成（§6.0.3），提交 `fix(parser): reject silently truncated week specifications` |
| 16 | 首次真实 `export` 穿透（含 `unsupported_adjustments` fail-closed 机制 + ICS 落盘行尾修复） | 12 | ✅ 已完成（§6.0.4）：98 事件，八项验收全过 |
| 17 | v0.2 设计：`CalendarAdjustment`（`CancelDate` / `ReplaceTeachingDay` / `AddMeeting`）取代「给 `DateOverride` 打补丁」的思路 | 16 | ⬜ **下一步**。落地后 09-20 / 10-10 两条 `unsupported_adjustments` 应转为正式表达 |
| 18 | 门户按 D3 处理「开发中」标注 | 13–16 | ⬜ |

> **第 2–8 步已于 2026-09-20 第二、三轮完成**，见 §6.0 与 §6.0.1。本批提交信息建议：
> `chore(repo): clean public fixtures and documentation`
> 第 10–12 步性质不同（业务验证），应单独提交：
> `feat(export): validate first real-world ICS generation`
> 两批**不要混在同一提交**里 —— 前一批是仓库卫生（配方），后一批是业务验证（功能），
> 混在一起既难 review 也难回滚。

---

## 附：本文档的验证方式

| 结论 | 验证命令 / 方式 |
|---|---|
| 287 项测试通过（P0 / P0-2 / §6.0.4 后） | `python -m pytest -q`（`cv-project0` 环境） |
| `ruff` 全绿 | `python -m ruff check .` → `All checks passed!` |
| `mypy` 全绿 | `python -m mypy src` → `Success: no issues found in 16 source files` |
| 远端无代码 | `GET https://api.github.com/repos/XLJFZ/xjtu-timetable-calendar/contents/` → 3 项 |
| 本地零提交 | `git log` → `fatal: your current branch 'master' does not have any commits yet` |
| 作息模板被排除（修复前） | `git check-ignore -v examples/schedule.example.json` → `.gitignore:17:schedule*.json` |
| 作息模板已恢复纳入（修复后） | `git diff --cached --name-only \| grep examples` → 两个 `.example.json` 均在内 |
| `.workbuddy/` 已被忽略 | `git check-ignore -v .workbuddy/memory/2026-09-20.md` → `.gitignore:39:.workbuddy/` |
| 固件已脱敏 | 正则扫描学号/手机号/身份证/姓名/凭据/邮箱；命中的「学号」「身份证」均为全零占位（`SKZC` 掩码与嵌入的日期串） |
| 真实字段名 | 解析固件 `datas.xskcb.rows[0]` 的 47 个键 |
