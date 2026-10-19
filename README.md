# xjtu-timetable-calendar

将西安交通大学 eHall「我的课表」中**当前登录账号本人有权访问的个人课表**，解析并导出为
标准 iCalendar（`.ics`）文件，可直接导入 Apple 日历、Google 日历、
Microsoft Outlook 等应用。

> **本项目为非官方工具**，与西安交通大学无隶属关系。
>
> 本项目**不绕过统一身份认证**、**不绕过权限控制**、**不访问其他学生的课表**、
> **不包含任何个人课表数据**、**不包含任何登录凭据**。
> 只处理当前登录用户正常能够访问的数据。

---

## 目录

- [这个工具解决什么问题](#这个工具解决什么问题)
- [核心设计](#核心设计)
- [安装](#安装)
- [快速开始](#快速开始)
- [URL 订阅（subscribe）](#url-订阅subscribe)
- [考试安排](#考试安排)
- [必须由你配置的内容](#必须由你配置的内容)
- [命令参考](#命令参考)
- [项目结构 / 数据模型 / 开发](#项目结构--数据模型--开发)
- [隐私与安全](#隐私与安全)
- [路线图](#路线图)
- [许可](#许可)

深度文档（按需查）：

| 文档 | 讲什么 |
| --- | --- |
| [design-notes](docs/user-guide/design-notes.md) | 四条设计取舍 + 数据流水线 |
| [subscription](docs/user-guide/subscription.md) | URL 订阅机制、rotate、webcal、客户端入口、v0.8 迁移 |
| [exams](docs/user-guide/exams.md) | 考试接入、三态判定、撤销与改期、座位号 |
| [config-templates](docs/user-guide/config-templates.md) | 校历与作息表模板、notice / schedule |
| [cli-reference](docs/user-guide/cli-reference.md) | 全部子命令、退出码、环境变量 |
| [troubleshooting](docs/user-guide/troubleshooting.md) | 常见问题与判据 |
| [contributing](docs/user-guide/contributing.md) | 模块划分、数据模型、开发、发布、接口分析 |

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

## 核心设计

四条决定产物形态的设计取舍（为什么不用单条 RRULE、UID 为什么稳定、
SEQUENCE 怎么让重新导入生效、节次为什么不存钟点），连同数据流水线图，
见 [docs/user-guide/design-notes.md](docs/user-guide/design-notes.md)。

一句话版：**课表事实只存「星期 + 节次 + 教学周」，日期与钟点在导出时按
当天生效的校历和作息表算**；每条事件有稳定 UID，因此可以增量更新而不是重导。
## 安装

需要 **Python 3.11+**。

**方式一（推荐）：从 PyPI 安装**（v0.3.0 起已发布）：

```bash
pip install "xjtu-timetable-calendar[http,browser]"
```

> ⚠️ **别只装裸包。** 基础依赖只有 `icalendar`（解析与渲染够用），
> `login` 要 `playwright`、默认抓取路径要 `httpx`，两者都在 extras 里。
> 2026-10-14 用干净虚拟环境实测：裸 `pip install` 之后跑 `login` 直接报
> 「登录功能需要 playwright」，而 `--version` 那一步是绿的——也就是说
> 只装裸包会在流程中段才撞上，而不是当场。只想手工补：

```bash
pip install playwright   # login 子命令（使用系统已装的 Edge/Chrome 时无需再跑 playwright install）
pip install httpx        # 默认 HTTP 抓取路径（比浏览器路径轻量）
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

pip install -e ".[http,browser]"   # 开发用可编辑安装；只想使用则 `pip install ".[http,browser]"`
```

> **安装即自包含**：eHall 接口定义随包分发在
> `src/xjtu_calendar/data/ehall_endpoints.json`（通过
> `importlib.resources` 读取），`pip install .` 与 `pip install -e .`
> 之后都**无需手工复制任何配置文件**。CI 会在干净虚拟环境里实测这一点。
> 需要覆盖时，把同名文件放到 `~/.xjtu-timetable-calendar/ehall_endpoints.json` 即可。

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

两条容易踩的操作约束（2026-10-11 实测）：

- **这一步要由真人完成**。它停在终端等一个 Enter，放进后台任务、管道或自动化脚本里
  会永远等不到，表现就是"卡住"，而且浏览器窗口会一直开着占住会话目录。
- **会话过期后必须带 `--force`**。不带时，只要 home 里存在旧的会话文件，它就打印
  「已检测到本地会话」直接退出——哪怕那份文件是一个多月前的、`fetch` 正在返回 401。
  看起来成功，其实什么都没做。
- **弹出来的是独立的浏览器实例**（专用用户数据目录）。你在平时的浏览器里登录过
  eHall 与它毫无关系，必须**在那个窗口里**再认证一次，否则工具抓不到会话。

### 2. 配置校历与作息表

**这一步必须由你完成，本项目刻意不内置猜测值**——见下一节。

### 3. 获取课表

```bash
python -m xjtu_calendar fetch --semester 2026-fall
```

HTTP 路径会同时抓考试安排（默认行为，见下文[「考试安排」](#考试安排)）；
只想抓课表加 `--no-exams`。

### 4. 导出

```bash
python -m xjtu_calendar export --semester 2026-fall --output timetable.ics
```

产物默认**同时含本学期的考试事件**（单场绝对时刻、无提醒；座位号在 `DESCRIPTION` 里），
`--no-exams` 可关闭——细节与已知限制见[「考试安排」](#考试安排)。

日历标题（`X-WR-CALNAME`，也就是客户端里显示的名字）默认**按课表自动推导**为
「西安交通大学课表 · 大三-上」这样的形式：年级来自课表的入学年级字段（`NJDM`，
按 `学年起始年 − 入学年 + 1` 换算，只在大一…大五范围内显示），上/下来自学期代码
`YYYY-YYYY-N` 的尾号。凡是**不敢定**的情况一律退回基础名「西安交通大学课表」——
自定义的学期 key（如 `2026-fall`）、课表里没有 `NJDM`、年级分布出现并列，都不追加
后缀，宁可少信息也不给一个错的年级。跨年级（插班、跨年级选课、留级）时取多数，并在
日志里留一句提示。用 `--name` 显式命名则完全照用、不追加任何后缀。
**年级只影响标题，不参与 UID 计算、也不写进事件**，所以订阅端不会因为换了标题而重收课程。
至于已建立的订阅会不会跟着改显示名，各家客户端行为不同（有的会一直保留首次订阅时的名字，
需要重新添加才更新）——这一点本仓库未在真机上验证；可以确定的是事件的 UID 与 SEQUENCE
都不变，课程不会被重收一遍。

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

> **想让编码 Agent 代你走这套流程**：仓库根 `skills/` 下有两个技能——
> `xjtu-timetable-setup`（部署向导：登录交回真人、校历必须由你提供、每步带完成证据，
> 收尾用 `doctor` 的退出码判定成败）与 `timetable-api-archaeology`（给陌生学校的
> 接口考古方法论，含"什么时候该停下来"）。把整个目录复制进你所用 Agent 的
> skills 目录即可，内容不假设任何特定产品。

---

## URL 订阅（subscribe）

把课表发布到一个可通过 URL 访问的 `.ics`，日历客户端定期回源，
**改课不用重新导入**。机制、日常更新、换 token、iOS/macOS 的 `webcal://`
链接、已知边界、v0.8 的 UID 配方迁移、各客户端订阅入口，
见 [docs/user-guide/subscription.md](docs/user-guide/subscription.md)。

最要紧的几条口径：

- **公开仓库上「知道 URL 才能读」并不成立**：分支列表就能看到文件名，
  产物里有什么就是公开什么（含考试时是考试名/时间/考场/座位号，`--no-exams` 可关）。
- 升级到 v0.8 的那一次是**一次性 UID 迁移**：旧事件整学期撤销、原地重建，
  只在 `subscribe push` 时发生，忽略 `STATUS` 的客户端会看到一次「已取消的活动」。
- **安卓不分 ROM 直接用 ICSx⁵**（`at.bitfire.icsdroid`，免费获取地址
  <https://f-droid.org/packages/at.bitfire.icsdroid/>；商店版按我方口径收费），
  别在系统日历里找订阅入口——
  MIUI / 鸿蒙常常只给「导入日历」，那是一次性快照、之后任何变更与撤销都收不到。
  注意**不是 DAVx⁵**：它只做 CalDAV 双向同步，遇到 Webcal 是把 URL 转交给 ICSx⁵，
  自己不拉纯 HTTP 的 `.ics`。各家自带入口的能力参差，统一走一条通路比按机型摸索可靠。
  `f-droid.org` 在国内网络下实测不可达，下载环节通常要挂代理。
  它对撤销语义的两条行为都实测过了（2026-10-19，界面级判据）：**从产物里消失的条目会被清掉**、
  **手删的条目同步后不会再加回来** ⇒ 那条「已取消的活动」占位你在 ICSx⁵ 上可以自己删掉。
- 装不了 ICSx⁵ 时才退回系统日历自带的订阅入口，且要知道它们的脾气各不相同：
  **ColorOS 系统日历会回源并原地更新**（实测 ≤2h35m），它缺的是撤销语义——忽略
  `STATUS:CANCELLED`、消失项不删、单条删不掉；**Outlook.com** 则是添加时立刻拉一次、
  此后实测 14 小时以上未再回源（官方口径约每 3 小时），更新能力不可靠。
  双击导入那类一次性快照才是"完全不回源"。逐条读数与各家入口的具体走法见
  [订阅指南](docs/user-guide/subscription.md)的客户端矩阵。
- 发布这一步要同时推进四样东西（远端 `.ics`、本地留底、两份台账、迁移标记），它们不在一个
  原子域里。所以 0.8.5 起：**推送之前**先落可恢复的凭据，本地与线上对不上时 `subscribe push`
  / `subscribe rotate` 会拒绝（退出码 `7`）并让你跑 `subscribe repair`——它按**线上真实字节**
  裁决、不产生新发布。换 token 的那一次与 `push` 走同一条事务，不会再出现"rotate 之后
  再也发不出去"。

---

## 考试安排

考试自 v0.5 起进产物，默认包含、`--no-exams` 关闭。只有 HTTP 接口这条通路
拿得到考试；接口给的是**三态**（有数据 / 明确为空 / 无法判定），
**判定不了时绝不覆盖已有快照**。撤销下发、改期与 UID、座位号在哪看、
陈旧提示，见 [docs/user-guide/exams.md](docs/user-guide/exams.md)。

---

## 校历事件（v0.9 起，默认不带）

学期配置里可以声明学校**发布过**的全天事件——起课、报到注册、考试周、寒暑假、放假调休，
`export` / `subscribe push` 带上 `--include-calendar-events` 才并进产物（**默认关**，
关掉时既有订阅者的产物字节一字不变）。`source_id` 是这条事件的身份：改标题、改日期都走
原地更新（UID 不变、`SEQUENCE` +1），所以它必填、同学期内唯一，没有降级链。

三条口径：

- **配置写闭区间，产物写半开区间**（RFC 5545 的 `DTEND` 不含最后一天）：`"end": "2027-02-28"`
  导出成 `DTEND;VALUE=DATE:20270301`，而 `DESCRIPTION` 里仍是抄进去的那两天，逐字对得上原文。
- **正式发布比预览严**：`export` 允许带着其余好事件出（缺什么会说），而 `subscribe push` /
  `subscribe rotate` 只要 `events` 有错、或有未裁决的 `conflicts`，就**整体拒绝发布**
  （退出码 `8`，远端与本地状态一字未动）。理由：校历事件不接撤销通路，配置写错一条
  就是线上少一条且恢复不回来。**处置是改配置**——既不是 `subscribe repair`，也不是任何
  `--allow-*` 旗标。
- **默认关之外还防"漏写一个旗标"**：上一版产物里发布过校历事件、这次没带那个旗标 ⇒ 同样拒绝
  （`8`）。漏写旗标不等于"你要把校历事件全删掉"。反过来，从没启用过这特性的人，配置里躺着
  一段写坏的 `events` **也不会**被这道门拦住——配置坏着由 `doctor` 喊（它新增「校历事件」一格，
  与门共用判据，因此也接同名旗标）。

字段写法、来源域名清单与冲突记录见
[配置模板](docs/user-guide/config-templates.md)；删除后再加回来的代价与"客户端历史状态修不动"
那三条边界，见[订阅指南](docs/user-guide/subscription.md)的「已知边界」。

---

## 必须由你配置的内容

本项目**刻意不编造**任何学校信息。以下两项必须由你提供：

- **学期校历** —— 第 1 教学周的星期一是哪天、本学期共几周；
- **作息表** —— 每一节次在哪些日期区间对应什么钟点（本校有冬/夏季两套）。

两份模板的完整 JSON、字段口径、以及两个可选的自动获取子命令（`notice` / `schedule`），
见 [docs/user-guide/config-templates.md](docs/user-guide/config-templates.md)。

---

## 命令参考

全部子命令、参数、退出码语义与环境变量清单，
见 [docs/user-guide/cli-reference.md](docs/user-guide/cli-reference.md)。
日常只需三条：`login` → `fetch` → `export`（要 URL 订阅再加 `subscribe`）。

---

## 项目结构 / 数据模型 / 开发

模块划分、`CourseMeeting` 数据模型、支持的周次与节次写法、开发环境与测试、
发布流程、测试覆盖重点、eHall 接口分析，
见 [docs/user-guide/contributing.md](docs/user-guide/contributing.md)。
跑测试只要记住四条门禁：`pytest -q`、`mypy`、`ruff check .`、`ruff format --check .`。

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

遇到 `401` 会提示「登录状态失效」；遇到 `403` 会提示「先确认登录有没有走到课表页」
（实测最常见的原因是会话停在「请选择身份」，见[常见问题](#常见问题)），
**而不是继续重试**。

### 订阅 URL 的风险口径（subscribe）

`subscribe` 是对上述准则的**例外通道**，风险模型也不同：它把你的课表发布到
一个**匿名可读的公开地址**上，而承载它的仓库也必须是公开的。所以这条通路的
暴露面有**两处**，不是一处——URL 里的随机 token 猜不到，但「知道 URL 就能读」
这层保护在公开仓库上**并不成立**：分支的文件列表页匿名可读，会直接列出
`<token>.ics` 的文件名。转发即授权，而任何会浏览该仓库分支的人同样能读到你的课表。
为此实现上做了配套约束：发布分支恒为
孤儿单提交（课表旧版本不进 git 历史）、状态文件收紧权限写盘、日志不含
URL/token、强推前有 tree 护栏（拒绝覆盖非本工具产物）、**提交前还要核对索引里只有
那一个 `<token>.ics`**（发布工作目录里就算残留了别的文件也不会被推上公开分支）。
完整口径（含「产物里有什么就是公开什么」与零公网暴露的退路）见上文
[「URL 订阅（subscribe）」的隐私口径](#隐私口径)。

### 测试数据

`tests/fixtures/` 中的全部数据均为**虚构的脱敏样例**，不含任何真实个人信息：

| 文件 | 来源 | 说明 |
|---|---|---|
| `timetable_sample.json` | 手工构造 | 英文兼容键路径的样例，课程名为 `示例课程甲/乙/丙/丁`，地点为 `A-1001…A-1004` |
| `ehall_timetable_real_sanitized.json` | 真实响应脱敏 | 层级/键名/类型/`SKZC` 周次位掩码为真实结构；姓名、学号、教师、教室、课程名、机构代码**全部替换为占位值** |

文档（README、docstring）中出现的课程名与地点同样使用上述同一套占位值，
**不引用任何真实课程或教室**。

---

## 常见问题

排障条目按真实踩坑逐条写在各处判据里，见
[docs/user-guide/troubleshooting.md](docs/user-guide/troubleshooting.md)。
最常撞的四条：

- **`fetch` 报 403（不是 401）**：登录只走了一半，会话里没有教务侧
  （`/jwapp`）的 cookie。`login` 现在会自己等（0.8.4 起），
  判据看 `doctor` 的「会话」一项。
- **`subscribe push` 报成批「无法安全下发撤销」**：多半是校历配错或
  换了学期标识，先跑 `doctor` 再看那条熔断提示。
- **`subscribe push` / `subscribe rotate` 报退出码 `8`**：校历事件那道发布准入没过
  （`events` 有错 / 有未裁决的 `conflicts` / 上一版发布过校历事件而这次没带
  `--include-calendar-events`）。**改配置**，别去跑 `subscribe repair`、也别找 `--allow-*` 旗标。
- **`diff` 首跑报「旧课表快照不存在」**：正常状态，第二次 `fetch` 起才有基线。
- **改了校历内部 `key`**：它会进每一条事件的 UID，等于整学期换身份，
  `doctor` 会警告「内部 key ≠ 文件名标识」。

---

## 路线图

**已完成（MVP）**

- [x] 标准化数据模型（星期 + 节次 + 教学周，零钟点）
- [x] 周次 / 节次文本解析（含单双周、混合区间、空格分段；显式越界一律报错不裁剪）
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
- [x] 考试安排接入（`fetch` HTTP 路径同时抓考试快照，考试事件并入同一份 .ics /
  同一个订阅 URL；三态判定「状态未知绝不覆盖快照」、`--no-exams` 四路径开关、
  `diff`「考试变更」小节，见上文「考试安排」）
- [x] 课程侧撤销 + UID v2（0.8.0：课程名不再参与 UID，改名走原地更新；真退课以
  `STATUS:CANCELLED` 下发；两道门 + 熔断拒绝发布（退出码 `6`、`--allow-mass-cancellation` 放行）；
  `subscribe push` 一次性 v1→v2 迁移，见上文「升级到 v0.8」）
- [x] 订阅通路稳定性（0.8.1–0.8.5：`webcal://` 链接、登录自检、新用户走查抓出的提示层、
  **0.8.5 = 发布事务可恢复**：`subscribe repair` + 构建前的相容性那道门（退出码 `7`）+
  两份发布凭据 + `rotate` 与 `push` 同一条事务，见 `CHANGELOG.md`）
- [x] 校历事件（0.9.0：学期配置里声明**学校发布过**的全天事件——起课 / 报到注册 / 考试周 /
  寒暑假 / 放假调休，带 `--include-calendar-events` 才进产物，**默认关 ⇒ 既有订阅者的产物
  逐字节不变**；`source_id` 是身份，改标题改日期走原地更新；退出码 `8` 是它那道发布准入，
  处置只有改配置，见上文「校历事件」）

**待完成**

- （本条路线图的待办已全部完成；下方「未来扩展」为架构已预留的新一批方向）

**未来扩展（架构已预留）**

1. **URL 订阅式 ICS** —— 已交付 `subscribe` 手动发布通道（见上文
   「URL 订阅」：本地构建 + 强推 GitHub Pages，客户端按 URL 自动拉取）；
   **服务端定期重新生成**仍未实现（当前口径是纯手动 push）
2. **考试安排** —— 已交付（见上文「考试安排」：`fetch` → 同一份日历内含考试事件）；
   取消通路已于 v0.6.0 交付（见上文「考试撤销」：消失的已发布考试以 `STATUS:CANCELLED`
   自动下发、保留到原定时刻过去）；剩余边界只在一次性导入后不再回源的客户端与换设备后的
   空台账，仍需手动删除
3. **校历事件** —— 开学、放假、考试周、校庆、节假日
4. **多学期管理**

**第一版明确不做**：Web UI、手机 App、小程序、服务器账号系统、数据库、
Google / Apple 日历 API、CalDAV 双向同步、自动后台登录。

---

## 许可

[MIT](LICENSE)
