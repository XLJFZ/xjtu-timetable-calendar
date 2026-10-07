# xjtu-timetable-calendar

将西安交通大学 eHall「我的课表」中**当前登录账号本人有权访问的个人课表**，解析并导出为
标准 iCalendar（`.ics`）文件，可直接导入 Apple 日历、iPhone 日历、Google 日历、
Microsoft Outlook 等应用。

> **本项目为非官方工具**，与西安交通大学无隶属关系。
>
> 本项目**不绕过统一身份认证**、**不绕过权限控制**、**不访问其他学生的课表**、
> **不包含任何个人课表数据**、**不包含任何登录凭据**。
> 只处理当前登录用户正常能够访问的数据。

---

## 目录

- [这个工具解决什么问题](#这个工具解决什么问题)
- [核心设计：为什么不是简单的「课表转 ICS」](#核心设计为什么不是简单的课表转-ics)
- [安装](#安装)
- [快速开始](#快速开始)
- [URL 订阅（subscribe）](#url-订阅subscribe)
- [必须由你配置的内容](#必须由你配置的内容)
- [命令参考](#命令参考)
- [项目结构](#项目结构)
- [数据模型](#数据模型)
- [隐私与安全](#隐私与安全)
- [开发](#开发)
- [常见问题](#常见问题)
- [路线图](#路线图)
- [许可](#许可)

---

## 这个工具解决什么问题

教务系统里的课表，本质上是一组**规则**：

> 某门课，**星期五**、**第 1-2 节**、**第 1-8 教学周**、在 A-1001 上课。

而日历应用需要的是**事件**：

> 2026-09-11（周五）08:00–09:50，示例课程甲，创新港校区 A-1001。

从前者到后者的转换有两个**容易做错**的地方：

1. **教学周要换算成具体日期** —— 需要知道「第 1 教学周的星期一是哪天」。
2. **节次要换算成具体钟点** —— 需要知道「那一天学校执行的是哪套作息」。
   西安交大有冬/夏季作息调整，**同一节课在不同时期钟点不同**。

本项目把这两件事显式建模、分离处理，而不是把结果写死。

---

## 核心设计：为什么不是简单的「课表转 ICS」

### 一、课程节次不提前转成固定钟点

原始课表事实只有三样：

```
星期（weekday） + 节次（periods） + 教学周（weeks）
```

`08:00` / `09:50` / `14:00` 这些**不属于课表事实**，而属于
「该日期当天学校执行的是哪套作息规则」。

所以内部模型长这样：

```python
CourseMeeting(
    course_name="示例课程甲",
    weekday=5,  # 星期五
    periods=[1, 2],  # 第 1-2 节
    weeks=[1, 2, 3, 4, 5, 6, 7, 8],
    location="A-1001",
    teacher="…",
)
```

而**不是**：

```python
CourseMeeting(..., start_time="08:00", end_time="09:50")  # ✗ 错误设计
```

钟点在**导出阶段**由 `ScheduleTable` 现算。这样学期中途切换作息时，
数据模型完全不受影响。

> ### ⚠️ 澄清：这不是时区 DST
>
> 中国时区恒为 `Asia/Shanghai`，全年无夏令时。本项目**从不**读写系统时区
> （你可能在东京或纽约运行本程序）。
>
> 真正变化的是**「第 N 节课对应的实际钟点」**。这是学校的作息规则问题，
> 与时区无关。两者必须彻底解耦。
>
> 导出的 ICS 内嵌 `Asia/Shanghai` 的 `VTIMEZONE` 定义（RFC 5545 §3.2.19
> 要求每个被引用的唯一 TZID 都要有对应时区组件），而不只是给一个
> `X-WR-TIMEZONE` 提示。

### 二、不为整学期课程使用单个 RRULE

天真的做法是生成一条重复规则：

```
DTSTART:20260911T080000
RRULE:FREQ=WEEKLY;COUNT=16
```

**这在西安交大是错的。** 如果第 8 周后切换冬季作息，`RRULE` 展开出的后续事件
仍然沿用第一次的钟点，与实际课表不符。

学校还可能存在：单双周、不连续周、节假日停课、调课、补课、换教室、作息切换。

因此本项目采用**「每一次实际上课生成一个独立 VEVENT」**：

```
第 1 周 周五 -> VEVENT  (08:00-09:50)
第 2 周 周五 -> VEVENT  (08:00-09:50)
...
第 6 周 周五 -> VEVENT  (08:30-10:20)   ← 已切换冬季作息
```

一学期几百个事件对现代日历应用完全不是负担，换来的是逻辑可靠性。

### 三、UID 稳定，可做增量更新

UID 由课程标识 + 实际上课日期 + 节次派生（`sha256`），**不含时间和地点**：

- 同一门课的同一节课**反复导出得到相同 UID** → 重新导入时原地更新，不产生重复；
- 换教室或作息调整 → 视为同一事件的新版本，客户端自动覆盖；
- 不同日期的课 → UID 不同，不会互相覆盖。

> **已知边界**（为保持 v0.1.x 兼容，本版不改 UID 算法）：课程名也参与 UID，
> 因此同一 `course_id` 的课程若被改名（如「大学英语（2）」→「大学英语Ⅱ」），
> 其事件会被视为新事件而非原事件的新版本。将来若引入 UID v2，
> 会配套一次性显式迁移方案，而不是直接改历史 UID。

### 四、SEQUENCE / LAST-MODIFIED：让「重新导入」真正生效

部分日历客户端在重新导入 ICS 时，靠 `UID + SEQUENCE` 判断事件是「没变」还是
「变了」。本项目每次导出都会写入这两个属性：

- **默认行为**：自动把输出文件的旧版本当作基线——内容未变的事件保留原
  `SEQUENCE` / `LAST-MODIFIED`（客户端跳过，不产生噪音）；内容变了（换教室、
  调时间、改课）则 `SEQUENCE` 递增、`LAST-MODIFIED` 刷新，客户端原地更新；
- `--sequence-from PATH`：显式指定基线 ICS（比如导出到新文件名时）；
- `--no-sequence`：关闭基线比对，全部按新增处理；
- `LAST-MODIFIED` 恒为 UTC（RFC 5545 要求），`SEQUENCE` 基线解析失败时
  显式指定会报错终止、自动探测则降级为警告。

### 数据流水线

```
eHall
  ↓  登录 / 会话（用户本人在浏览器完成认证）
  ↓  获取原始课表 JSON
  ↓  Parser                    eHall 字段 → 标准化模型（接口变更的唯一适配点）
  ↓  Course / CourseMeeting    （星期 + 节次 + 教学周）
  ↓  AcademicCalendar          教学周 → 实际日期
  ↓  ScheduleTable             日期 → 冬/夏季作息 → 节次 → 实际 HH:MM
  ↓  CalendarEvent             逐次实际上课
  ↓  iCalendar (.ics)
```

---

## 安装

需要 **Python 3.11+**。

**方式一（推荐）：从 PyPI 安装**（v0.3.0 起已发布）：

```bash
pip install xjtu-timetable-calendar
```

**方式二：源码安装**（需要跟踪最新开发进度时）：

```bash
git clone https://github.com/XLJFZ/xjtu-timetable-calendar.git
cd xjtu-timetable-calendar

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .   # 开发用可编辑安装；只想使用则 `pip install .`
```

> **安装即自包含**：eHall 接口定义随包分发在
> `src/xjtu_calendar/data/ehall_endpoints.json`（通过
> `importlib.resources` 读取），`pip install .` 与 `pip install -e .`
> 之后都**无需手工复制任何配置文件**。CI 会在干净虚拟环境里实测这一点。
> 需要覆盖时，把同名文件放到 `~/.xjtu-timetable-calendar/ehall_endpoints.json` 即可。

可选依赖：

```bash
# 浏览器登录（login 子命令需要）
pip install playwright
# 若使用系统已装的 Edge / Chrome，无需再执行 playwright install

# HTTP 抓取路径（比浏览器路径轻量）
pip install httpx
```

> **提示**：本项目会自动探测本机已安装的 Edge / Chrome，不需要额外下载浏览器内核。
> 如需指定浏览器，设置环境变量 `XJTU_CALENDAR_BROWSER`。

> **PyPI**：包已正式发布（`pip install xjtu-timetable-calendar`），发布流程见「开发 → 发布」。

---

## 快速开始

### 1. 登录（只需一次）

```bash
python -m xjtu_calendar login
```

会弹出一个浏览器窗口。**请你自己完成统一身份认证**——本程序不读取、不记录、
不上传任何凭据。登录后进入「我的课表」页面，确认课表正常显示后回到终端按 Enter。

会话保存到 `~/.xjtu-timetable-calendar/session/`，该目录已在 `.gitignore` 中排除，
登录态会跨进程保留。

### 2. 配置校历与作息表

**这一步必须由你完成，本项目刻意不内置猜测值**——见下一节。

### 3. 获取课表

```bash
python -m xjtu_calendar fetch --semester 2026-fall
```

### 4. 导出

```bash
python -m xjtu_calendar export --semester 2026-fall --output timetable.ics
```

输出示例：

```
Semester:
  2026-2027 学年秋季学期

Courses:
  8

Meetings:
  126

Events:
  1248

Date range:
  2026-09-08 ~ 2026-12-22

Output:
  timetable.ics
```

把 `timetable.ics` 导入日历应用即可（Apple 日历可直接双击打开）。

---

## URL 订阅（subscribe）

「快速开始」的交付方式是「拿到文件 → 手工导入」：课表一变就要重新导出、
重新导入，多端各来一遍。`subscribe` 增加一条 **URL 订阅通道**——把同一份 .ics
发布到**你自己的 GitHub Pages**（公开仓库 + 不可猜的 token 文件名），
日历客户端按 URL 订阅；之后每次课表调整只需本地跑一次 `subscribe push`，
客户端自动拉取，不再需要导入。

> **口径先说清**：刷新是**纯手动**的——本工具不做定时任务、不常驻进程，
> 也不会替你 `fetch`。「手机 24 小时内自动呈现新状态」的前提是你自己 push 过。
> 一个学期一个 token、一个订阅 URL；多学期合并进同一 URL 是明确不做的事。

### 一次性设置

1. 在 GitHub 建一个**公开**仓库（例如 `cal-alice`）。它里面只会出现一个
   `<token>.ics` 文件。
2. 开启 Pages：仓库 Settings → Pages → **Deploy from branch**，
   分支选 `cal`、目录选 `/(root)`。
   （`cal` 分支由首次 `subscribe push` 创建；下拉里选不到它时，先完成下面
   两步再回来开启，顺序不影响结果。）
3. 登记发布目标，生成 token：

   ```bash
   python -m xjtu_calendar subscribe init --repo https://github.com/<用户名>/cal-alice.git --semester 2026-fall
   ```

   init 会打印订阅 URL 和上面那条 Pages 指引。GitHub 远端的 URL 前缀自动推导；
   非 GitHub 远端必须用 `--url-base` 显式给出公开访问前缀，否则 init 直接拒绝。
   每学期一个状态文件（`subscribe/subscribe-<学期>.json`），重复 init 会报错，
   重新登记需先删除该状态文件（换新 token）。
4. 发布第一版：

   ```bash
   python -m xjtu_calendar subscribe push --semester 2026-fall
   ```

5. 把订阅 URL 复制到日历客户端（入口见下）。Pages 首次生效有数分钟延迟，
   可运行 `subscribe status --verify` 对 URL 做一次匿名 GET 自检。

### 日常更新

课表有变动时（建议先用 `diff` 确认）：

```bash
python -m xjtu_calendar fetch --semester 2026-fall
python -m xjtu_calendar subscribe push --semester 2026-fall
```

- push 与 `export` 共用同一条构建管线，数据源参数也相同
  （`--input` 可直接指定课表 JSON，默认用 fetch 缓存）。
- 内容与上次发布完全一致时报「无变化，跳过推送」，不动远端。
- raw 快照超过 7 天会警告「建议先 fetch」，但不阻断。
- SEQUENCE 基线来自本地留底 `subscribe/last-<学期>.ics`：未变动的事件在客户端
  保持安静，变动的事件原地更新——与重新导入的行为同一口径。
- `subscribe status` 汇总 URL、上次发布时间、留底与远端是否一致、快照年龄。

### 换 token（rotate）

把 URL 转发给了不该给的人、换了课表环境、或有任何泄露疑虑时：

```bash
python -m xjtu_calendar subscribe rotate --semester 2026-fall
```

换新 token 后 rotate 会顺手重新发布（若之前从未 push 过，则提示你跑
`subscribe push`）。新文件挂上分支、旧文件名从 tip 消失，**旧 URL 自此 404**；
**所有日历客户端都要重新粘贴新 URL**——这是换 token 的固有成本。
极端情况 rotate 后发布失败（网络/权限）：token 已换、远端还挂着旧文件名，
按终端提示修复后跑 `subscribe push` 补发即可。

### 隐私口径

1. **URL 即能力**：token 是 32 位十六进制随机串，猜不到；但知道完整 URL 的
   人就能匿名读取你的课表——**转发 URL 等于授权**。
2. **历史零残留**：发布分支永远是「无父孤儿单提交 + 强推」，课表的旧版本
   不进 git 历史，仓库里翻不到你上周的课表。
3. **Pages 生效延迟数分钟**：push 成功 ≠ 订阅 URL 立刻是新内容；客户端要等
   Pages 构建完成后的下一次自动拉取才呈现新状态。`status --verify` 可确认。

token 状态文件按收紧的文件权限写盘（与 `storage_state.json` 同等待遇）；
日志只记分支名与内容哈希，**不记订阅 URL / token**。

### 已知边界

- **课程改名会表现为新事件**：UID 含课程名（见「核心设计」的 UID 冻结边界），
  改名前后是两个事件，订阅端与手工导入的表现一致；`diff` 能在 push 之前
  先看到改名条目。
- **换 token 后所有客户端要重贴 URL**（见上）。
- 发布通道只有静态 URL（以 GitHub Pages 为主）；不做 CalDAV 直写、
  不做中心托管服务。

> ⚠️ `~/.xjtu-timetable-calendar/subscribe/work-<学期>/` 是**工具专用**的 git
> 工作区：手工放进去的文件会被下一次 `subscribe push` 原样发布出去
> （记住仓库是公开的！），此后分支 tip 不再满足「根目录仅一个 .ics」的
> tree 护栏，后续发布被 fail-closed 拒绝，直到你清空该目录并自行清空
> （或换名重建）那个分支。**不要往这个目录放任何东西。**

### 常见客户端订阅入口

- Apple 日历：「通过订阅添加日历」（macOS 文件 → 添加日历 → 订阅；
  iPhone 设置 → 日历 → 添加日历）。
- Google Calendar：「通过 URL 创建日历」（左侧「其他日历」→「通过 URL 添加」）。

---

## 必须由你配置的内容

本项目**刻意不编造**任何学校信息。以下两项必须由你提供：

### 1. 教学日历（校历）

位置：`~/.xjtu-timetable-calendar/semesters/<学期标识>.json`

模板：`examples/academic_calendar.example.json`

```json
{
  "semester": {
    "key": "2026-fall",
    "name": "2026-2027 学年秋季学期",
    "first_week_monday": "2026-09-07",
    "total_weeks": 16
  },
  "excluded_dates": ["2026-10-01"],
  "overrides": {
    "2026-10-10": {
      "source_date": "2026-10-06",
      "note": "（示例）本日按 10-06 的课表上课"
    }
  }
}
```

> 📌 上面这段是**格式示例**，其中的日期与 `total_weeks` 都不是真实校历数据，
> 必须替换成你从官方校历抄录的值。

**关键字段是 `first_week_monday`** —— 第 1 教学周**星期一**的日期。

> ⚠️ 注意：第 1 教学周的星期一**不等于**开学报到日，也不一定是学期首日。
> 请从教务处发布的官方校历确认后填写。填错会导致**全部事件系统性偏移**。

**`total_weeks` 可以省略。** 它是可选的**校验上限**，不决定生成多少周 ——
导出的周次完全来自课表自身：

| 配置 | 行为 |
|---|---|
| 省略 / `null` | 按课表自身的周次原样展开，**不做上限过滤** |
| 填了数字，课表出现更大周次 | **直接报错终止**（fail-closed），并提示核对校历 |
| 填了数字，课表周次都在范围内 | 正常导出 |

> ⚠️ **没有官方依据时宁可留空。** 填一个猜来的数字，一旦真实课表超出它，
> 导出会报错；反过来若工具静默丢弃越界周次，你会拿到一份
> **看起来正常、实则缺课**的日历 —— 后者危险得多，所以本工具选择报错。

`excluded_dates` 是全校停课日（节假日），`overrides` 是调课日。
两者都可以从教务处的停课/调课通知**自动解析合并** —— 见下文
[3. 停课/调课通知自动获取](#3-停课调课通知自动获取notice-子命令可选)；
也可以手工从校历抄录。

**`overrides` 支持两类校历例外。** 工具不把节假日、调课混为一谈：

| 字段 | 含义 |
|---|---|
| `excluded_dates` | **停课日**：该日不生成任何事件。 |
| `overrides[date].source_date` | **调课日**：该日原课程停上，改为按 `source_date` 所在教学周 + 星期的课表上课。 |
| `overrides[date].cancel` | 仅停课（不调入其他课表）。 |
| `overrides[date].note` / `location` | 附加备注 / 覆盖教室。 |

调课日语义（对应学校通知里的「某日上原本某日的课」）：

```json
"overrides": {
  "2026-10-10": { "source_date": "2026-10-06" }
}
```

- 10-10 原有的星期六课程**停上**；
- 生成的事件来自 **10-06 所在教学周 + 星期二** 的课程安排
  （单双周 / 周次位掩码按 **source 教学周**解释，不按 10-10 自己的周次）；
- 事件的日期是 **10-10 本身**，钟点按 **10-10 当天适用作息**解析
  （例如冬春季作息切换后补课自动用新钟点）；
- UID 与普通事件同一规则，稳定可复现。

配置校验（fail-closed）：`source_date` 等于自身、落在学期范围外、
目标日又在 `excluded_dates` 中、或同一日期残留
`unsupported_adjustments` 旧声明 —— 加载时即报错。

**`unsupported_adjustments`（可选）：声明「已知但本工具无法表达」的调课。**

```json
"unsupported_adjustments": [
  { "date": "2026-11-14", "description": "运动会停课，补课安排未公布" }
]
```

能用 `overrides[source_date]` 表达的调课**不要**写在这里 ——
同一日期两种声明并存会在加载时报错。这个字段只用于声明真正表达不了的
安排（例如「某日按某周几课表上课，但具体哪周未知」），导出行为是：

- **默认直接报错**，逐条列出调课明细 —— 绝不静默产出缺课的日历；
- 你确认可以接受缺失后，加 `--allow-unsupported-adjustments`
  继续导出（每条都会以警告形式再次出现）。

`description` 必填：没有说明的「无法表达」只会让人困惑。

### 2. 作息表

位置：`~/.xjtu-timetable-calendar/schedules/schedule.json`

模板：`examples/schedule.example.json`

```json
{
  "profiles": {
    "xjtu-summer-autumn": {
      "name": "夏秋季作息（05-01 起）",
      "periods": {
        "1": ["08:00", "08:50"],
        "2": ["09:00", "09:50"],
        "3": ["10:10", "11:00"],
        "4": ["11:10", "12:00"],
        "5": ["14:30", "15:20"],
        "6": ["15:30", "16:20"],
        "7": ["16:40", "17:30"],
        "8": ["17:40", "18:30"],
        "9": ["19:40", "20:30"],
        "10": ["20:40", "21:30"]
      }
    },
    "xjtu-winter-spring": {
      "name": "冬春季作息（10-01 起）",
      "periods": {
        "1": ["08:00", "08:50"],
        "2": ["09:00", "09:50"],
        "3": ["10:10", "11:00"],
        "4": ["11:10", "12:00"],
        "5": ["14:00", "14:50"],
        "6": ["15:00", "15:50"],
        "7": ["16:10", "17:00"],
        "8": ["17:10", "18:00"],
        "9": ["19:10", "20:00"],
        "10": ["20:10", "21:00"]
      }
    }
  },
  "periods": [
    { "start": "2026-05-01", "end": "2026-09-30", "profile": "xjtu-summer-autumn" },
    { "start": "2026-10-01", "end": "2027-04-30", "profile": "xjtu-winter-spring" }
  ]
}
```

- `profiles`：每套作息定义「第 N 节 → 起止 `HH:MM`」
- `periods`：每套作息的生效日期区间（闭区间）

> 📌 上面这套节次时间按**西安交通大学当前公开作息时间**填写（夏秋季 05-01 起、
> 冬春季 10-01 起）。**学校如调整作息，请以最新官方通知为准。**
> 这张表本身可以由 `schedule` 子命令从教务处官方页自动解析并合并 ——
> 见下文 [4. 作息表自动获取](#4-作息表自动获取schedule-子命令可选)。

注意生效区间是按**切换日**划分，不是按学期：学校的作息切换不是时区夏令时
（`Asia/Shanghai` 全年不变），切换点会落在学期中间——一个学期完全可能跨越两套作息。
把 profile 绑死在学期上会导致学期中途的钟点整体错位。

**缺少配置时的行为**：明确报错并给出提示，**不会猜测或回退到某个默认值**。
这是刻意的设计——错误的静默默认值比明确的报错危险得多。

### 3. 停课/调课通知自动获取（`notice` 子命令，可选）

教务处会在假期前后发布结构化的停课/调课通知（日期 | 周次星期 | 调休及教学安排）。
`notice` 子命令可以解析这类通知页，并（在你确认后）把停课日与调课日
合并进学期配置 —— **只新增，绝不覆盖你已有的条目**：

```bash
# 预览：只打印解析结果，不写任何文件
python -m xjtu_calendar notice --url <通知页地址> --semester 2026-2027-1

# 也可以离线解析本地保存的 HTML
python -m xjtu_calendar notice --from-file notice.html --semester 2026-2027-1

# 确认无误后真正合并进学期配置
python -m xjtu_calendar notice --url <通知页地址> --semester 2026-2027-1 --apply
```

解析规则与安全边界：

- 通知里的「第 N 周星期 X」会与配置里的 `first_week_monday` **交叉校验**
  （日期对不上即报错），借此同时解决「10 月 1 日属于哪一年」这类年份推断；
- 「某日停课、上某日（第 N 周星期 X）的课」→ 识别为**调课**（`overrides[source_date]`）；
- 「停课 / 放假 / 法定节假日 / 调休」→ 识别为**停课日**（`excluded_dates`）；
- 识别不了的行**不会自动写入**，而是逐条列出请你人工确认；
- **fail-closed**：只要存在任何无法可靠解析的行，`--apply` 就**整体拒绝写入**
  （配置文件字节级不变），而不是「写进去能解析的那一半」；
  预览模式仍可运行，但会明确标注结果不完整、不可直接应用；
- 页面结构变化导致「找得到表头却一行都解析不出」时直接报错，
  绝不把「解析不到」伪装成「本次没有调课」；
- 写回前 / 写回后都会用正式领域模型重新校验配置，
  并以同目录临时文件 + 原子替换落盘，不会留下截断的 JSON；
- `--apply` 合并是**只新增**操作：配置中已有的值一律保留，解析结果与现有
  条目冲突时会给出警告。

> 📌 通知页是普通公开网页，`notice` 只发匿名 GET 请求，**不携带任何登录凭据**，
> 且 `--url` 只接受 `http/https` 地址（本地保存的 HTML 请走 `--from-file`）。

### 4. 作息表自动获取（`schedule` 子命令，可选）

教务处公开页「学生作息时间表」（`due.xjtu.edu.cn/xxfw/zxsj.htm`）以表格列出
夏/冬两套作息第 1~10 节课的起止钟点，切换点就写在列表头原文里
（「5月1日开始实行」「10月1日开始实行」）。`schedule` 自动解析并（在你确认后）
合并进 `schedules/schedule.json`：

```bash
# 预览：抓取官方页打印解析结果，不写任何文件
python -m xjtu_calendar schedule

# 离线解析本地保存的 HTML
python -m xjtu_calendar schedule --from-file zxsj.html

# 合并进作息配置（生效区间的覆盖范围来自学期校历）
python -m xjtu_calendar schedule --semester 2026-2027-1 --apply
```

解析规则与安全边界（与 `notice` 同口径）：

- 只把「第N节课」行提取为 profiles；早餐/午餐/午休/预备铃等行仅作说明、不写入；
  **新增的行类别会进 `unresolved` 并让 `--apply` 整体拒绝写入**——不做静默猜测；
- 切换点取自列表头原文（月-日），年份覆盖区间来自学期校历
  （`first_week_monday` ~ 学期末）；校历没有 `total_weeks` / `end_date` 时
  `--apply` 拒绝——**不猜**截止日期；
- 合并**只新增**：已有 profile 键 / 已有区间一律保留现值并警告；
  无任何新增时文件字节不变（幂等，可重复执行）；
- 写前 / 写后都过 `ScheduleTable` 正式校验（区间重叠、引用未定义 → 拒绝），
  并以原子替换落盘；
- 页面是普通公开网页，匿名 GET、无凭据，且 `--url` 只接受 `http/https`。

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `login [--force]` | 浏览器手动登录，保存本地会话 |
| `status` | 查看会话与配置状态（不发起网络请求） |
| `fetch [--semester S] [--source auto\|http\|browser] [--from-file F]` | 获取课表原始 JSON（覆盖前自动把上一份轮转为 `*.prev.json`，作为 `diff` 基线） |
| `diff [--semester S] [--old F] [--new F]` | 比对新旧课表快照：新增/删除课程、时段增减、周次/教室/教师变化（纯本地） |
| `export [--semester S] [-o OUT] [--input F] [--calendar-config F] [--schedule-config F] [--from-date D] [--to-date D] [--sequence-from ICS] [--no-sequence]` | 生成 `.ics` |
| `notice --url U \| --from-file F [--semester S] [--apply]` | 解析停课/调课通知，预览或合并进学期配置 |
| `schedule [--url U \| --from-file F] [--semester S] [--apply]` | 解析官方「学生作息时间表」页，预览或合并进作息表配置 |
| `subscribe init --repo U [--branch B] [--url-base U]` \| `subscribe push [--input F]` \| `subscribe rotate` \| `subscribe status [--verify]`（均可带 `--semester S`） | 把 .ics 发布到自己的 GitHub Pages，日历客户端按 URL 订阅（见上文「URL 订阅」） |
| `inspect [--input F] [-o OUT]` | 对原始 JSON 做**脱敏**结构分析 |

全局参数：`--debug`（详细异常）、`-q`（静默）、`--version`

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 一般错误（网络、解析、导出） |
| `2` | 未登录，需要先跑 `login` |
| `3` | 登录态已失效，需要重新 `login` |
| `4` | 当前账号无权限访问该资源 |
| `130` | 用户中断 |

### 环境变量

| 变量 | 作用 |
|---|---|
| `XJTU_EHALL_BASE` | eHall 域名，默认 `https://ehall.xjtu.edu.cn` |
| `XJTU_CALENDAR_HOME` | 数据目录，默认 `~/.xjtu-timetable-calendar` |
| `XJTU_CALENDAR_BROWSER` | 强制指定浏览器可执行文件 |
| `XJTU_SEMESTER` | 默认学期标识 |

---

## 项目结构

```
xjtu-timetable-calendar/
├── README.md
├── LICENSE
├── .gitignore
├── pyproject.toml
├── CHANGELOG.md                        # 版本变更记录
├── docs/
│   └── design-v0.1.md                  # 技术设计 v0.1（设计基准与漂移记录）
├── .github/workflows/
│   └── ci.yml                          # CI：pytest / mypy / ruff + wheel 自包含自检
├── config/
│   └── ehall_endpoints.example.json    # 接口定义模板（真实定义随包分发）
├── src/xjtu_calendar/
│   ├── __init__.py                     # __version__：importlib.metadata + pyproject 回退
│   ├── __main__.py                     # python -m xjtu_calendar
│   ├── cli.py                          # 命令行入口
│   ├── config.py                       # 集中配置（URL / appId / 目录）
│   ├── errors.py                       # 异常体系 + 退出码
│   ├── fileutil.py                     # 原子替换写入（校历配置 / 作息表 / 会话文件共用）
│   ├── logging_setup.py                # 日志与脱敏工具
│   ├── models.py                       # Semester / Course / CourseMeeting / CalendarEvent
│   ├── weeks.py                        # 教学周文本解析（含 SKZC 位掩码）
│   ├── periods.py                      # 节次文本解析
│   ├── academic_calendar.py            # 教学周 → 日期
│   ├── schedules.py                    # 作息表 / 节次 → 实际时间
│   ├── parser.py                       # eHall JSON → 标准化模型（接口变更唯一适配点）
│   ├── auth.py                         # 会话管理
│   ├── fetcher.py                      # 课表抓取（HTTP / 浏览器双路径）
│   ├── exporter.py                     # → CalendarEvent → .ics
│   ├── diff.py                         # 新旧课表快照比对（调课检测）
│   ├── notices.py                      # 停课/调课通知解析（HTML 表格 → 配置条目）
│   ├── schedule_notice.py              # 官方作息页解析（表格 → schedule.json 合并方案）
│   ├── sequence.py                     # SEQUENCE / LAST-MODIFIED 版本管理
│   ├── timeutil.py                     # 时区常量（Asia/Shanghai）
│   └── data/
│       └── ehall_endpoints.json        # 接口定义（真实观测，随 wheel 分发）
├── scripts/
│   └── probe_ehall.py                  # eHall 接口分析工具
├── tests/
│   ├── conftest.py
│   ├── test_weeks.py                   # 连续/离散/混合/单双周/全角/位掩码
│   ├── test_periods.py                 # 连排/逗号/单节/括号噪声
│   ├── test_calendar.py                # 日期换算 / 作息解析 / 停课覆盖
│   ├── test_parser.py                  # 字段映射 / 容错 / 脱敏
│   ├── test_parser_real.py             # 基于真实响应脱敏固件的校准测试
│   ├── test_fetcher.py                 # 响应分类 / 端点校验（MockTransport，不发真实请求）
│   ├── test_exporter.py                # UID / 时间 / ICS 合规
│   ├── test_makeup.py                  # 停课日 / 调课日（source_date 语义）
│   ├── test_packaging.py               # 版本号单一来源 + 包内资源随 wheel 分发
│   ├── test_sequence.py                # SEQUENCE / LAST-MODIFIED 版本管理
│   ├── test_notices.py                 # 通知解析 / 周次交叉校验 / 只新增合并
│   ├── test_schedule_notice.py         # 作息页解析 / 区间铺排 / 只新增合并 / 幂等
│   ├── test_cli_schedule.py            # schedule 子命令 CLI 级 e2e（fail-closed 契约）
│   ├── test_diff.py                    # 课表快照比对（课程/时段/字段三级口径）
│   ├── test_cli_diff.py                # diff 子命令 e2e + fetch 快照轮转联动
│   ├── test_cli_notice.py              # notice 子命令 CLI 级 e2e
│   └── fixtures/
│       ├── timetable_sample.json           # 手工构造的脱敏样例
│       ├── ehall_timetable_real_sanitized.json  # 真实结构脱敏固件
│       ├── notice_holiday_2026.html        # 真实停课/调课通知固件（样式已剥离）
│       └── schedule_zxsj.html              # 官方「学生作息时间表」页固件（2026-10-07 抓取）
└── examples/
    ├── academic_calendar.example.json
    └── schedule.example.json
```

---

## 数据模型

### `CourseMeeting` —— 项目的中心

```python
@dataclass(frozen=True)
class CourseMeeting:
    course_id: str | None
    course_name: str
    weekday: int  # 1 = 星期一 … 7 = 星期日
    periods: list[int]  # 节次**序号**，不是时间
    weeks: list[int]  # 教学周
    teacher: str | None
    location: str | None
    campus: str | None
    raw_week_text: str | None  # 原始文本留档，便于排查
    raw_period_text: str | None
```

**不存在任何钟点字段。** 这一点由测试 `test_course_meeting_has_no_time_fields` 守住。

### 支持的周次写法

| 输入 | 输出 |
|---|---|
| `1-16周` | `[1..16]` |
| `1,3,5,7周` | `[1,3,5,7]` |
| `1-4,7-12周` | `[1,2,3,4,7..12]` |
| `2,5-8周` | `[2,5,6,7,8]` |
| `1-16周（单）` | `[1,3,5,…,15]` |
| `1-16周（双）` | `[2,4,6,…,16]` |
| `单周` / `双周` | 全学期奇数周 / 偶数周 |
| `第1-8周` / `1-8` / `1～8周` | `[1..8]` |

### 支持的节次写法

`1-2节` · `3-4节` · `5-8节` · `1,2节` · `1-4节` · `1-2,5-6节` · `１－２节` · `1～2节`

---

## 隐私与安全

### 绝不提交的内容

`.gitignore` 已排除：

- 会话文件：`.cookies/`、`state_*.json`、`storage_state.json`、`session*.json`、`profile/`、`.env`
- 个人数据：`timetable*.json`、`schedule*.json`、`*.ics`
- 运行期目录：`.xjtu-timetable-calendar/`

> **`.ics` 文件也在排除列表里。** 它包含你的完整课程表、教师姓名和上课地点。

### 日志脱敏

日志**绝不输出** Cookie、Authorization、ticket、token、学号、姓名、
以及完整统一身份认证 URL 的参数。

`logging_setup.redact()` / `redact_url()` 提供递归脱敏，供需要记录 URL 或响应结构时使用。

### 网络行为准则

本项目的实现严格遵守：

1. 只访问当前账号**正常有权限**的接口
2. 不绕过统一身份认证
3. 不绕过权限控制
4. 不枚举学号
5. 不枚举课程
6. 不抓取其他用户数据
7. **不使用高并发**（所有请求串行）
8. **不暴力重试 401 / 403**
9. API 异常采用**有限次数**指数退避
10. 不把 Cookie 写入日志
11. 不把 token 打印到终端

遇到 `401` / `403` 会明确提示「登录状态失效」或「当前账号无权限」，
**而不是继续重试**。

### 订阅 URL 的风险口径（subscribe）

`subscribe` 是对上述准则的**例外通道**，风险模型也不同：它把你的课表发布到
一个**匿名可读的公开地址**上。安全边界只有一层——URL 里的随机 token 猜不到，
但**知道 URL 就能读**，转发即授权。为此实现上做了配套约束：发布分支恒为
孤儿单提交（课表旧版本不进 git 历史）、状态文件收紧权限写盘、日志不含
URL/token、强推前有 tree 护栏（拒绝覆盖非本工具产物）。
完整口径与操作方式见上文[「URL 订阅（subscribe）」](#url-订阅subscribe)。

### 测试数据

`tests/fixtures/` 中的全部数据均为**虚构的脱敏样例**，不含任何真实个人信息：

| 文件 | 来源 | 说明 |
|---|---|---|
| `timetable_sample.json` | 手工构造 | 英文兼容键路径的样例，课程名为 `示例课程甲/乙/丙/丁`，地点为 `A-1001…A-1004` |
| `ehall_timetable_real_sanitized.json` | 真实响应脱敏 | 层级/键名/类型/`SKZC` 周次位掩码为真实结构；姓名、学号、教师、教室、课程名、机构代码**全部替换为占位值** |

文档（README、docstring）中出现的课程名与地点同样使用上述同一套占位值，
**不引用任何真实课程或教室**。

---

## 开发

```bash
pip install -e ".[dev]"
pytest                      # 421 项测试（含 doctest）
pytest --cov=xjtu_calendar  # 带覆盖率
ruff check .                # 代码风格（含 scripts/ 与 tests/）
mypy src                    # 类型检查（strict）
```

上面四条在 `main` 上**全部为零输出**：`ruff` 无告警、`mypy` 无错误、测试全通过。
仓库已配置 GitHub Actions（`.github/workflows/ci.yml`）：每次 push / PR 在
Python 3.11 / 3.12 / 3.13 上跑上述三条；另有一个 `wheel` 任务会**真正构建 wheel
并在干净虚拟环境里安装**，验证 `pip install .` 得到的包是自包含的
（含包内默认接口定义、CLI 可运行）—— 避免「从源码目录碰巧找到配置」的假通过。

版本变更记录在 `CHANGELOG.md`；版本号**单一来源**为 `pyproject.toml`
（`importlib.metadata` 读取，未安装时回退解析同一份文件，代码里没有第二处常量）。

> **升版后请重装 editable 包。** `importlib.metadata` 读的是**安装时生成**的元数据，
> 只改 `pyproject.toml` 而不重装，`--version` 仍会报旧版本
> （回退分支只在「压根没装」时才会走）：
>
> ```bash
> python -m pip install -e . --no-deps
> ```
>
> 另有一个易踩的坑：`python -m build` 会在源码树里留下 `src/*.egg-info`，
> 而 pytest 配了 `pythonpath = ["src"]`，会让它**先于** site-packages 被
> `importlib.metadata` 命中。若它滞后于 `pyproject.toml`（升版后既没重装、
> 也没重建），就会出现「命令行 `--version` 说 0.2.0，`import xjtu_calendar`
> 说 0.1.1」这种**同一份代码两个答案**——`tests/test_packaging.py` 的版本守卫
> 会因此变红。它只是构建残留（`*.egg-info/` 已在 `.gitignore` 内，且 editable
> 安装实际靠 site-packages 的 `__editable__*.pth`，并不依赖它），
> 处置任选其一：重装 editable、`python -m build` 重建、或直接把该目录移走。
> 该目录**不要**提交，也**不需要**改测试去迁就它。

### 发布

- **GitHub Release 全自动挂资产**：Release 一旦发布（`published`），
  `.github/workflows/release.yml` 构建 sdist + wheel、过 `twine check`，
  并上传到该 Release。CI 的质量门禁仍由 `ci.yml` 独立负责，两者互不干扰。
- **PyPI 走手动可信发布**：`.github/workflows/publish-pypi.yml` 需在
  Actions 页手动触发（workflow_dispatch），使用 OIDC trusted publishing，
  **仓库不保存任何 token**。一次性前置**已完成**（2026-10-07）：已在
  pypi.org 账号级 Publishing 页登记待定发布者（Project name
  `xjtu-timetable-calendar`、Publisher type **GitHub Actions**、owner
  `XLJFZ`、repository `xjtu-timetable-calendar`、workflow name
  `publish-pypi.yml`、**Environment 留空**），v0.3.0 首传已自动建项目并绑定。
  之后每次发布只需：合入升版提交 → 打 tag → 发 GitHub Release →
  在 Actions 里点 Run workflow。PyPI 拒绝重复版本号，误发有保险。
- 版本号仍是 `pyproject.toml` 单一来源；升版后记得按上文重装 editable。

若你在自己的分支上看到大量 `RUF001/002/003`，那是规则的已知误报 ——
它把**中文全角标点**当成「易混淆 Unicode」，而本项目的 docstring、注释与
面向用户的文案本身就是中文。这些规则已在 `pyproject.toml` 里显式关闭，
请不要为了通过 lint 去改写全角标点。

### 测试覆盖重点

- **周次解析**：连续 / 离散 / 混合 / 单双周 / 全角 / 无「周」字 / 去重乱序 / 越界报错 / `SKZC` 位掩码
- **节次解析**：连排 / 逗号 / 单节 / 全角 / 括号噪声 / 结构化 `KSJC`+`JSJC` 优先
- **日期换算**：周一与周日边界、跨周、逆向换算
- **作息解析**：夏季→summer、冬季→winter、未配置时明确报错
- **UID**：重复导出不变、不同日期不同、不含地点（换教室不产生新事件）
- **SEQUENCE / LAST-MODIFIED**：无基线归零、内容未变保留、内容变更递增、UTC 规范、基线解析容错
- **通知解析**：表格网格展开（rowspan/colspan）、周次交叉校验、停课/调课分类、只新增合并、无法识别行不落盘
- **作息获取**：官方作息页网格解析（第 1~10 节课、切换点取自表头原文）、非教学行仅作说明、
  未知行类别进 unresolved 并拒写、生效区间跨切换点/跨年铺排、幂等（无新增时文件字节不变）
- **ICS**：必需属性齐全、**课程 VEVENT 不使用 RRULE**（时区组件内部的规则不受此约束）、
  CRLF、时区 `Asia/Shanghai` 且内嵌 `VTIMEZONE`（TZID 引用完整）、特殊字符转义
- **接口层**：响应分类（含「200 + 登录页 HTML」判为会话失效）、占位符端点拒绝、401/403 不重试

### eHall 接口分析

```bash
python scripts/probe_ehall.py
```

打开浏览器让你手动登录并进入课表页，脚本记录前端**自己调用的**结构化 JSON 接口，
输出脱敏结构报告到 `_notes/ehall-probe.md`（该目录已排除）。

包内默认接口定义（`src/xjtu_calendar/data/ehall_endpoints.json`，随 wheel 分发）
中已填入**经真实网络观测确认**的端点：

| 端点 | 方法 | 路径 | 作用 |
|---|---|---|---|
| `current_semester` | POST | `/jwapp/sys/wdkb/modules/jshkcb/dqxnxq.do` | 当前学年学期（无参数） |
| `timetable` | POST | `/jwapp/sys/wdkb/modules/xskcb/xskcb.do` | 学生课表（form 参数 `XNXQDM`） |

因此 `fetch --source http` 可直接走轻量路径。若端点配置缺失或仍是占位符，
`fetch` 会明确报 `EndpointNotConfigured` 而不是拿模板去发请求。

> **不要猜接口。** 以网页实际网络请求为准。学校改版后需重新运行 probe 校准。

---

## 常见问题

**Q：为什么我的课表里有些课没导出？**

运行 `export --debug` 查看解析报告，其中会列出跳过的记录及原因
（如「无法识别星期」「周次解析失败」）。再用 `inspect` 查看脱敏结构，
把实际字段名补进 `src/xjtu_calendar/parser.py` 的 `FIELD_CANDIDATES`。

**Q：可以自动处理节假日和调课吗？**

可以 —— 见[第 3 节：停课/调课通知自动获取](#3-停课调课通知自动获取notice-子命令可选)。
`notice` 子命令解析教务处发布的结构化通知页，把「停课日」与「按某日课表上课」
的调课整理进学期配置（周次与学期起始日交叉校验）；解析不了的行逐条列出且不落盘，
只要存在无法可靠解析的行，`--apply` 就整体拒绝写入（fail-closed）。
当然也可以随时手工录 `excluded_dates` 与 `overrides`。
**作息表**的自动获取同样已经实现 —— 见上文
[4. 作息表自动获取](#4-作息表自动获取schedule-子命令可选)（`schedule` 子命令，
官方作息页，切换点取自页面原文，fail-closed 同口径）。

**Q：怎么发现课表被调过（换教室、改时间、加减课）？**

重复 `fetch` 即可：每次 fetch 会把上一份快照轮转为 `timetable-<学期>.prev.json`，
然后 `python -m xjtu_calendar diff --semester <学期>` 列出新增/删除课程、
时段增减与周次/教室/教师变化（纯本地比对，不发网络请求）。
确认变化后重新 `export`，UID 稳定所以日历会原地更新。
用订阅通道的话，把「重新 export」换成 `subscribe push` 即可。
也可以用 `--old/--new` 显式指定任意两份 raw JSON 做比较。

**Q：为什么不用一个 RRULE 简化 ICS？**

见上文[核心设计](#二不为整学期课程使用单个-rrule)。简单说：作息切换会让
`RRULE` 展开出错误的时间，而这是西安交大每年都会发生的事。

**Q：上课钟点错了怎么办？**

检查 `schedule.json` 的 `profiles` 钟点，以及 `periods` 里
夏秋季 / 冬春季的切换日期是否正确。`export` 运行时会校验配置并把问题以警告形式列出。

**Q：会话过期了怎么办？**

```bash
python -m xjtu_calendar login --force
```

**Q：`timetable.ics` 会不会被误提交？**

`.gitignore` 已包含 `*.ics`。但仍建议把导出文件放在仓库外，
或放在 `_notes/` 等已排除目录。

---

## 路线图

**已完成（MVP）**

- [x] 标准化数据模型（星期 + 节次 + 教学周，零钟点）
- [x] 周次 / 节次文本解析（含单双周、混合区间）
- [x] 教学周 → 实际日期
- [x] 冬/夏季作息解耦，节次 → 实际钟点
- [x] 停课 / 调课覆盖机制（`source_date` 调课日：按来源教学日课表生成事件）
- [x] 逐次上课生成独立事件，UID 稳定
- [x] RFC 5545 `.ics` 导出（`Asia/Shanghai`，内嵌同 `TZID` 的 `VTIMEZONE`）
- [x] CLI、错误体系、日志脱敏、测试
- [x] 用真实网络请求确认 eHall 课表接口，固化到随包分发的接口定义
- [x] 包自包含（接口定义随 wheel 分发）+ 版本号单一来源 + CI 门禁
- [x] 按真实响应校准 `parser.py` 的字段候选（含 `SKZC` 位掩码、结构化节次优先）
- [x] `fetch` 以真实账号跑通：真实课表成功落盘并逐行校验
- [x] 产出首份真实 `.ics`
- [x] `SEQUENCE` / `LAST-MODIFIED` 版本管理（重新导入可正确更新）
- [x] 停课/调课通知自动解析合并（`notice` 子命令，含周次交叉校验）
- [x] 作息表自动获取（`schedule` 子命令，官方作息页；切换点取自表头原文，
  覆盖范围来自学期校历，fail-closed + 只新增 + 幂等）
- [x] 调课检测（`diff` 子命令 + `fetch` 快照轮转：新增/删除课程、时段增减、
  周次/教室/教师/课程名变化，纯本地比对）
- [x] URL 订阅发布（`subscribe` 子命令：.ics 以孤儿单提交强推到个人
  GitHub Pages 分支，客户端按不可猜的 token URL 订阅，`rotate` 一键换链接）

**待完成**

- （本条路线图的待办已全部完成；下方「未来扩展」为架构已预留的新一批方向）

**未来扩展（架构已预留）**

1. **URL 订阅式 ICS** —— 已交付 `subscribe` 手动发布通道（见上文
   「URL 订阅」：本地构建 + 强推 GitHub Pages，客户端按 URL 自动拉取）；
   **服务端定期重新生成**仍未实现（当前口径是纯手动 push）
2. **考试安排** —— eHall 考试信息 → 日历
3. **校历事件** —— 开学、放假、考试周、校庆、节假日
4. **多学期管理**

**第一版明确不做**：Web UI、手机 App、小程序、服务器账号系统、数据库、
Google / Apple 日历 API、CalDAV 双向同步、自动后台登录。

---

## 许可

[MIT](LICENSE)
