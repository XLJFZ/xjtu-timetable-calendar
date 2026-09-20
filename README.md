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
    weekday=5,              # 星期五
    periods=[1, 2],         # 第 1-2 节
    weeks=[1, 2, 3, 4, 5, 6, 7, 8],
    location="A-1001",
    teacher="…",
)
```

而**不是**：

```python
CourseMeeting(..., start_time="08:00", end_time="09:50")   # ✗ 错误设计
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

```bash
git clone https://github.com/XLJFZ/xjtu-timetable-calendar.git
cd xjtu-timetable-calendar

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -e .
```

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
    "2026-10-10": { "note": "国庆调休补课" }
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

`excluded_dates` 是全校停课日（节假日）。第一版不自动推断，
需要你从校历抄录；后续版本会考虑自动获取。

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

注意生效区间是按**切换日**划分，不是按学期：学校的作息切换不是时区夏令时
（`Asia/Shanghai` 全年不变），切换点会落在学期中间——一个学期完全可能跨越两套作息。
把 profile 绑死在学期上会导致学期中途的钟点整体错位。

**缺少配置时的行为**：明确报错并给出提示，**不会猜测或回退到某个默认值**。
这是刻意的设计——错误的静默默认值比明确的报错危险得多。

---

## 命令参考

| 命令 | 作用 |
|---|---|
| `login [--force]` | 浏览器手动登录，保存本地会话 |
| `status` | 查看会话与配置状态（不发起网络请求） |
| `fetch [--semester S] [--source auto\|http\|browser] [--from-file F]` | 获取课表原始 JSON |
| `export [--semester S] [-o OUT] [--input F] [--calendar-config F] [--schedule-config F] [--from-date D] [--to-date D]` | 生成 `.ics` |
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
├── docs/
│   └── design-v0.1.md                  # 技术设计 v0.1（设计基准与漂移记录）
├── config/
│   ├── ehall_endpoints.json            # 接口定义（真实观测，随仓库分发）
│   └── ehall_endpoints.example.json    # 接口定义模板
├── src/xjtu_calendar/
│   ├── __init__.py
│   ├── __main__.py                     # python -m xjtu_calendar
│   ├── cli.py                          # 命令行入口
│   ├── config.py                       # 集中配置（URL / appId / 目录）
│   ├── errors.py                       # 异常体系 + 退出码
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
│   └── timeutil.py                     # 时区常量（Asia/Shanghai）
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
│   └── fixtures/
│       ├── timetable_sample.json           # 手工构造的脱敏样例
│       └── ehall_timetable_real_sanitized.json  # 真实结构脱敏固件
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
    weekday: int              # 1 = 星期一 … 7 = 星期日
    periods: list[int]        # 节次**序号**，不是时间
    weeks: list[int]          # 教学周
    teacher: str | None
    location: str | None
    campus: str | None
    raw_week_text: str | None     # 原始文本留档，便于排查
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
pytest                      # 270 项测试（含 doctest）
pytest --cov=xjtu_calendar  # 带覆盖率
ruff check .                # 代码风格（含 scripts/ 与 tests/）
mypy src                    # 类型检查（strict）
```

上面四条在 `main` 上**全部为零输出**：`ruff` 无告警、`mypy` 无错误、测试全通过。
若你在自己的分支上看到大量 `RUF001/002/003`，那是规则的已知误报 ——
它把**中文全角标点**当成「易混淆 Unicode」，而本项目的 docstring、注释与
面向用户的文案本身就是中文。这些规则已在 `pyproject.toml` 里显式关闭，
请不要为了通过 lint 去改写全角标点。

### 测试覆盖重点

- **周次解析**：连续 / 离散 / 混合 / 单双周 / 全角 / 无「周」字 / 去重乱序 / 越界裁剪 / `SKZC` 位掩码
- **节次解析**：连排 / 逗号 / 单节 / 全角 / 括号噪声 / 结构化 `KSJC`+`JSJC` 优先
- **日期换算**：周一与周日边界、跨周、逆向换算
- **作息解析**：夏季→summer、冬季→winter、未配置时明确报错
- **UID**：重复导出不变、不同日期不同、不含地点（换教室不产生新事件）
- **ICS**：必需属性齐全、无 RRULE、CRLF、时区 `Asia/Shanghai`、特殊字符转义
- **接口层**：响应分类（含「200 + 登录页 HTML」判为会话失效）、占位符端点拒绝、401/403 不重试

### eHall 接口分析

```bash
python scripts/probe_ehall.py
```

打开浏览器让你手动登录并进入课表页，脚本记录前端**自己调用的**结构化 JSON 接口，
输出脱敏结构报告到 `_notes/ehall-probe.md`（该目录已排除）。

`config/ehall_endpoints.json` 中已填入**经真实网络观测确认**的端点：

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

机制已经就位（`excluded_dates` 与 `overrides`），但数据需要你从校历录入。
自动获取属于路线图内容。

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
- [x] 停课 / 调课覆盖机制
- [x] 逐次上课生成独立事件，UID 稳定
- [x] RFC 5545 `.ics` 导出（`Asia/Shanghai`）
- [x] CLI、错误体系、日志脱敏、测试
- [x] 用真实网络请求确认 eHall 课表接口，固化到 `config/ehall_endpoints.json`
- [x] 按真实响应校准 `parser.py` 的字段候选（含 `SKZC` 位掩码、结构化节次优先）
- [x] `fetch` 以真实账号跑通：真实课表成功落盘并逐行校验

**待完成**

- [ ] 产出首份真实 `.ics`（需先由用户配置校历与作息表）
- [ ] 从学校官方来源自动获取校历与作息表（替代手工配置）
- [ ] 补 `SEQUENCE` / `LAST-MODIFIED`，为 URL 订阅式 ICS 做准备
- [ ] 补课 / 调课表达：当前 `DateOverride` 只能删改、不能新增事件

**未来扩展（架构已预留）**

1. **URL 订阅式 ICS** —— 服务端定期重新生成，日历自动同步课表变化
2. **调课检测** —— 新旧课表比对，输出新增 / 删除 / 时间变化 / 教室变化
3. **考试安排** —— eHall 考试信息 → 日历
4. **校历事件** —— 开学、放假、考试周、校庆、节假日
5. **多学期管理**

**第一版明确不做**：Web UI、手机 App、小程序、服务器账号系统、数据库、
Google / Apple 日历 API、CalDAV 双向同步、自动后台登录。

---

## 许可

[MIT](LICENSE)
