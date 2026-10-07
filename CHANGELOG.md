# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
**版本号单一来源为 `pyproject.toml`**，本文件只记录变更本身。

---

## [Unreleased]

### Added

- **日历标题自动带上年级与学期**：`export` 与 `subscribe push` 生成的 `X-WR-CALNAME`
  默认从课表数据推导为「西安交通大学课表 · 大三-上」。年级来自教务的入学年级字段
  （`NJDM`，按 `学年起始年 − 入学年 + 1` 换算，仅在大一…大五范围内显示），上/下来自
  学期代码 `YYYY-YYYY-N` 的尾号。凡不敢定的情况一律退回基础名「西安交通大学课表」：
  自定义学期 key（如 `2026-fall`）、缺少 `NJDM`、年级分布并列；跨年级选课取多数并在
  日志留提示。显式 `export --name` 完全照用、不追加后缀。
  **年级不参与 UID 计算、不写入 VEVENT**，因此已导入 / 已订阅的用户不会因标题变化而
  重收课程：以现有留底为基线重算，96 个事件的 UID、`SEQUENCE:0`、`LAST-MODIFIED`
  全部保持原值，产物与线上仅差 `X-WR-CALNAME` 一行。

---

## [0.4.1] - 2026-10-08

补丁版本：修掉一个在中文 Windows 上会让 `subscribe` 直接崩掉的输出编码问题，并把
真机冒烟（GitHub Pages + 安卓系统日历）得到的事实补进 README。**UID 与事件内容均
无变化**，已导入的用户无需重新导入。

### Fixed

- **CLI 在非 UTF-8 输出环境下不再崩溃**：中文 Windows 上把输出重定向或管道化
  （如 `subscribe rotate > log.txt`）时，stdout 回落到 locale 编码（cp936），
  打印含 `⚠️` 的警示行会抛 `UnicodeEncodeError`——`subscribe rotate` 因此可能
  中断在「token 已换、尚未补发」的中间态。现在 CLI 启动时统一加固输出流：
  重定向场景按 UTF-8 落盘；GBK 控制台保持原编码、个别不可编码字符降级为 `?`，
  警示行与退出码不受影响。

### Documentation

- README「URL 订阅」补三条真机结论：国产 ROM 系统日历多为**一次性导入**（不会回源
  自动更新）及其分辨方法、需要 DAVx⁵ 中转时的两个实操坑（电池优化/自启动、导入产生
  的日历要在**账户层**删除）；以及 `rotate` 之后 git 层即时生效但 Pages/CDN 仍可能
  有 1~2 分钟供旧内容，不要据此误判失败。

---

## [0.4.0] - 2026-10-07

订阅通道落地：`subscribe` 把同一份 .ics 发布到**用户自己的** GitHub Pages
分支（孤儿单提交 + 不可猜 token 文件名），日历客户端按 URL 订阅，课表更新后
无需再手动重导；导出获得**确定性**——`DTSTAMP` 取自课表快照的修改时刻而非
渲染时刻，重复发布真正幂等、产物可复现。**UID 算法与事件内容（日期/钟点/
周次）均无变化**，对已导入用户是安全的原地升级；仅日历级 `DTSTAMP` 的含义
从「何时渲染」改为「数据何时变更」（更贴 RFC 语义）。

### Added

- **URL 订阅（`subscribe` 子命令）**：
  - `subscribe init --repo <git-url>`：登记发布目标（专用分支，默认 `cal`），
    生成 32 位十六进制随机 token 作为文件名；GitHub 远端自动推导 Pages 订阅
    地址，非 GitHub 用 `--url-base` 显式给出；
  - `subscribe push`：进程内复用导出管线构建 .ics，以**无父孤儿提交**强推
    到发布分支——远端历史恒为单提交，旧版本课表不留档；内容一致时跳过；
  - `subscribe rotate`：一键换 token 使旧订阅链接失效（补发成功前旧 URL 仍
    可读取，README 写明窗口）；
  - `subscribe status [--verify]`：URL、上次发布、快照年龄、本地留底一致性；
    `--verify` 对订阅 URL 做匿名 GET 自检；
  - 安全设计：状态文件按凭据待遇原子私有写盘；日志不落 URL/token；
    强推前校验远端分支 tip 必须是「仅含一个根级 .ics」的本工具产物，否则
    fail-closed 拒绝；留底缺失时拒绝无基线重发布（防 SEQUENCE 归零）；
    发布走系统 git 子进程，**仓库与工具零凭据管理**。
  - 新增 `src/xjtu_calendar/subscribe.py`、`tests/subscribe_support.py` 与
    33 项测试（状态层 / `file://` 假远端集成 / CLI e2e / 日志守门）；
    `pytest` 口径 421 → **454**（452 passed + 2 skipped，含 doctest）。

### Changed

- **`DTSTAMP` / 新事件 `LAST-MODIFIED` 改为快照时刻**：同一份课表数据重复
  导出字节可复现，`subscribe push` 的「无变化跳过」自此真实生效；显式
  `dtstamp` 参数保留给测试。既有事件的 SEQUENCE / LAST-MODIFIED 基线语义不变。
- 文档结构：设计规格与实现计划迁入 `docs/design/`、`docs/plans/`；
  README 新增「URL 订阅（subscribe）」章节并同步项目结构树。

---

## [0.3.0] - 2026-10-07

能力与安全的收口版本：`schedule` 打通作息表自动获取（v0.2 路线图待办清零）、
`diff` 提供课表变化检测；一批「承诺与代码的缝隙」类缺陷修复（端点指引、日志脱敏、
凭据落盘、cookie 域隔离）；发布通道（Release 资产自动挂载 + PyPI 可信发布）就绪。
**UID 算法与导出事件内容均无变化**，对已导入用户是安全的原地升级。

### Added

- **作息表自动获取（`schedule` 子命令）**：解析教务处公开「学生作息时间表」页
  （`due.xjtu.edu.cn/xxfw/zxsj.htm`），第 1~10 节课两季钟点取自页面网格，
  切换点（5月1日 / 10月1日开始实行）取自列表头原文；生效区间的年份覆盖来自学期校历
  （校历给不出学期末时 `--apply` 拒绝，不猜截止日）。与 `notice` 同口径：
  默认预览、`--apply` 只新增不覆盖、未知行类别进 unresolved 并整体拒写、
  写前写后 `ScheduleTable` 校验、原子替换、幂等（无新增时文件字节不变）。
  新增 `src/xjtu_calendar/schedule_notice.py`、官方页脱敏固件
  `tests/fixtures/schedule_zxsj.html` 与 23 项测试（含 CLI 级 e2e）。
  **v0.2 路线图的唯一待办就此完成。**
- **调课检测（`diff` 子命令 + `fetch` 快照轮转）**：`fetch` 覆盖缓存前把上一份 raw
  快照原子轮转为 `timetable-<semester>.prev.json`（单代基线，无需手工留底）；
  `diff` 纯本地比对新旧快照，按「课程级（整门新增/删除）→ 时段级（星期+节次配对，
  增减时段）→ 字段级（周次 / 教室 / 教师 / 课程名）」三层输出变化清单。
  课程改名按时段变化呈现——正是 UID 已知边界会被日历当成新事件的情形，提前可见。
  无基线时明确说明补救方式（`--old` 显式指定），不假装成功。
  新增 `src/xjtu_calendar/diff.py`、`tests/test_diff.py`、`tests/test_cli_diff.py`。
- **发布通道**：
  - `.github/workflows/release.yml`——GitHub Release 发布时自动构建 sdist + wheel、
    `twine check` 后挂载到该 Release；
  - `.github/workflows/publish-pypi.yml`——手动触发的 PyPI 可信发布（OIDC trusted
    publishing，仓库不存 token；需在 PyPI 侧一次性登记 publisher，步骤见文件注释与
    README「开发 → 发布」）。
- **GitHub Pages 落地页**（`index.html`）：替换默认 Jekyll 的 README 硬渲染，
  手写单文件响应式设计（内联 CSS、零外部依赖）。
- 新增 54 项测试（CLI 端点指引 2、日志脱敏 2、原子写入 6、会话落盘回归 1、
  URL 白名单 1、作息获取 23、cookie 域过滤 2、登录页长页识别 1、VTIMEZONE 守卫 1、
  指纹一致性守卫 1、diff 14）；`pytest` 口径 367 → **421**（含 doctest）。

### Changed

- **VTIMEZONE 改为全量定义（不再按事件范围裁剪 ±1 天）**：`Asia/Shanghai` 1991 年起恒为
  +08:00，组件本就只有一个 `STANDARD`，裁剪省不下体积；而个别日历客户端按 `TZID`
  全局缓存时区定义，一份「只在 X~Y 有效」的裁剪定义可能污染其他日历。事件钟点零变化
  （有结构回归守卫）。0.2.0 条目中「按本次事件范围生成」的描述以本条为准。

### Fixed

- **`status` / `fetch` 的端点指引指向了不存在的查找位置**：`load_endpoints` 的查找链是
  「显式路径 > 用户覆盖（`~/.xjtu-timetable-calendar/ehall_endpoints.json`）> 包内默认」，
  仓库的 `config/` 目录从不被读取；旧文案却引导用户去写 `config/ehall_endpoints.json`，
  照做会写出一个程序永远不会读的文件。现改用 `EndpointNotConfigured` 的默认指引，
  并新增文案守门测试（`tests/test_cli_endpoints.py`）。
- **日志脱敏未覆盖教师姓名键**：真实响应中教师姓名挂在 `SKJS` / `teacher` / `teacherName`
  下，`redact()` 此前不按键打码，`--debug` 日志会原样输出教师姓名。补入 `_SENSITIVE_KEYS`，
  使 README「日志绝不输出姓名」的承诺与代码一致。
- **`storage_state.json`（自注「等价于登录凭据」）落盘非原子、无属主权限**：把 `notices`
  的原子替换实现提取为 `xjtu_calendar.fileutil.atomic_write_text`，`ensure_login` 经其
  `private=True` 路径写入（POSIX 0600，Windows 尽力而为）；`notices` 改用同一实现，行为不变。
- **`notice --url` 缺少 scheme 白名单**：`urlopen` 事实上接受 `file://` 等协议，
  「匿名 GET 公开页」的承诺没有代码保证。现在只接受 http/https，其余拒绝并引导用 `--from-file`。
- **HTTP 路径的 cookie 不再全量外发**：`load_cookies` 过去把 storage_state 里**所有域**的
  cookie 压平发给 eHall 接口——跨域同名（如 CAS 域与 eHall 域的会话键）会互相覆盖，
  其他站点的 cookie 也被无谓携带。现按 RFC 6265 域匹配过滤，只发送覆盖目标主机的条目；
  缺 `domain` 字段的旧快照保持宽容（宁多不漏，升级不打断现有会话）。
- **登录页识别窗口过窄**：会话失效时 eHall 返回「200 + 登录页 HTML」，但特征词若出现在
  前 4000 字符之外（长 `<head>`/样式脚本）会被误判成普通的「响应不是 JSON」。特征扫描
  放宽到 64 KiB，让用户看到「该重新 login」而不是含糊的获取失败。

### Removed

- 清理零引用死代码（git 历史可随时找回）：`weeks._ODD_KEYWORDS/_EVEN_KEYWORDS`、
  `parser.iter_meeting_groups` / `parser.parse_date_range`、`auth.clear_session` /
  `auth.assert_not_expired`、`errors.WeekError` / `errors.PeriodError`、
  `ScheduleProfile.covers`、`timeutil.ensure_tz`。

---

## [0.2.0] - 2026-09-29

校历自动化与导出合规性收口：`notice` 子命令打通停课 / 调课通知，ICS 补齐
RFC 5545 要求的时区定义，并修复一批「静默丢数据」与「范围判定错误」的缺陷。
**UID 算法保持不变**（见 Changed），因此对已导入用户是安全的原地升级。

### Added

- **`SEQUENCE` / `LAST-MODIFIED` 版本管理**：每次导出自动写入这两个属性，
  日历客户端据此区分「事件没变」（保留原 `SEQUENCE`，跳过更新）与「事件变了」
  （`SEQUENCE` 递增、`LAST-MODIFIED` 刷新），修复重新导入时更新可能被忽略的问题。
  - 默认把输出文件的旧版本当作基线；`--sequence-from PATH` 显式指定，
    `--no-sequence` 关闭；
  - 内容指纹 = 摘要 / 起止时间 / 地点 / 描述（时间统一折算 UTC 比较）；
  - `LAST-MODIFIED` 恒为 UTC（RFC 5545 要求）；
  - 基线解析失败：显式指定报错终止，自动探测降级为警告（全部按新增）。
- 新增 `src/xjtu_calendar/sequence.py` 与 `tests/test_sequence.py`（15 项）。
- **停课/调课通知自动获取（`notice` 子命令）**：解析教务处发布的结构化
  停课/调课通知页（HTML 表格），把「停课日」与「按某日课表上课」的调课
  自动整理成学期配置条目。
  - 通知里的「第 N 周星期 X」与 `first_week_monday` **交叉校验**，
    同时解决年份推断（对不上即报错，不盲信解析结果）；
  - 默认只打印预览，`--apply` 才合并进学期配置，且**只新增、不覆盖**已有条目，
    与现有值冲突时给出警告；
  - 无法识别的行逐条列出、不落盘；支持 `--from-file` 离线解析本地 HTML；
    通知抓取为匿名 GET，不携带登录凭据；
  - 新增 `src/xjtu_calendar/notices.py`、`tests/test_notices.py`（11 项）
    与真实通知脱敏固件 `tests/fixtures/notice_holiday_2026.html`
    （解析输出与手工维护的真实配置完全一致）。
- **ICS 内嵌 `VTIMEZONE`（RFC 5545 §3.2.19 合规）**：事件引用
  `TZID=Asia/Shanghai`，此前只有 `X-WR-TIMEZONE` 提示、没有对应的 `VTIMEZONE`
  组件，属已知合规偏差。现在在 `to_ical()` 之前由官方
  `Calendar.add_missing_timezones()` 补齐，按**本次事件范围**生成（两侧各留
  一天余量），不手写时区定义。空日历不引用 TZID，因此不生成 `VTIMEZONE`。
  **事件自身的 UID / `DTSTART` / `DTEND` / `SEQUENCE` 均不受影响。**
- 新增 CLI 级端到端测试 `tests/test_cli_notice.py`：正式 schema + 真实通知
  fixture + 临时 `XJTU_CALENDAR_HOME`，覆盖预览、`--apply`、
  unresolved 拒写三条路径（旧的单元测试抓不到「CLI 自己重新实现配置解析」）。
- 新增 `Semester.export_date_range()`：显式给出学期导出边界，
  供 `unsupported_adjustments` 范围判定使用。
- 新增时区合规测试：`VTIMEZONE` 恰好一个且 `TZID=Asia/Shanghai`、
  解析后 `get_missing_tzids() == set()`、`DTSTART`/`DTEND` 本地钟点往返不变、
  空日历不生成 `VTIMEZONE`；RRULE 守卫改为按 VEVENT 结构化断言。

### Changed

- **运行依赖下限提升到 `icalendar>=6.1.0`**（原 `>=5.0`）：
  `add_missing_timezones()` 是 6.1.0 才引入的 API（实测 5.0.0 / 5.0.14 / 6.0.0
  均无）。未设置版本上限——未观测到 6.1+ 的不兼容行为。
- **RRULE 守卫语义收窄为「课程 VEVENT 不使用 RRULE」**：此前的全局文本断言
  `"RRULE" not in ics` 会在 VEVENT 仍然正确的前提下，把标准时区组件内部的
  规则一并误伤。红线本身未变，只是把断言落到正确的组件层级。
- **UID 算法保持不变（显式记录已知边界）**：**本版不引入 UID v2、不做自动迁移、
  不引入兼容开关**。课程名仍参与 UID 身份，因此同一 `course_id` 的课程被改名时，
  其事件会被视为新事件而非原事件的新版本。这是为兼容已发布的 v0.1.x 而
  **刻意保留**的选择；将来若要做 UID v2，必须配一次性显式迁移方案，
  不在普通 minor release 里直接改历史 UID。
- **license 迁移到 PEP 639**：`license = "MIT"`（SPDX 表达式）+ `license-files`，
  移除 License 分类器（PEP 639 禁止两者并存）；`build-system` 提升到
  `setuptools>=77`。wheel `METADATA` 现在携带 `License-Expression: MIT`，
  构建期不再出现弃用告警。
- CI 增加 `ruff format --check`：全仓已统一为 ruff 格式，从此防止格式漂移。

### Fixed

- **`notice` 子命令读不了正式学期配置（发布阻断项）**：CLI 之前在配置顶层取
  `first_week_monday`，而正式 schema 把它放在 `semester` 里，导致完全合法的
  配置被误报「学期配置缺少 first_week_monday」。现在统一改用
  `AcademicCalendar.from_file()`，**正式领域模型成为学期配置 schema 的唯一真源**。
- **`notice` 全链路 fail-closed**：
  - 通知解析不再因日期写法不熟悉（`10 月 1 日` / `2026年10月1日` /
    `10月1日（星期四）`）就静默丢掉整行；这类行现在进入 `unresolved`；
  - `--apply` 在 `unresolved` 非空时**整体拒绝写盘**，配置文件字节级不变，
    不再产出「半完整校历」（预览模式仍可用，但会标注结果不可直接应用）；
  - 找到调课表头却没有任何业务行时直接报错，不再把「解析不到」
    伪装成「本次没有调课」。
- **`unsupported_adjustments` 范围判定**：改按「用户请求的导出范围」
  （`--from-date` / `--to-date`，缺省回退学期边界）判断，而不是按最终生成出
  的事件跨度 —— 后者会漏报「首次上课之前」的特殊安排。学期边界无法确定时
  返回全部（宁可多报，不可漏报）。
- **调课日的 `location` 覆盖被忽略**：`overrides[target].location` 现在无条件
  生效；`with_override_notes` 只控制是否把备注写进 `DESCRIPTION`，
  不再连带关掉地点覆盖。
- **`notice --apply` 写回安全**：合并前 / 合并后都用 `AcademicCalendar` 复检，
  并以「同目录临时文件 + `os.replace`」原子替换（异常路径清理临时文件），
  绝不会把 `export` 读不懂的配置写回用户目录。

### 验证

- 三道 gate：pytest 367（全通过）/ `ruff format --check` 与 `ruff check` 无告警 /
  `mypy --strict` 无错误。
- 最低依赖实证：在 `icalendar==6.1.0`（本版下限）下，全量测试与本版 wheel 的
  导出探针均通过。
- 真实账号端到端导出：96 个 `VEVENT`；UID 集合、`DTSTART`/`DTEND`、`SEQUENCE`
  与补齐时区**之前**的产物逐项一致，唯一差异是新增 `VTIMEZONE`
  （`TZID=Asia/Shanghai`）——验证「补齐时区不影响事件本身」。

---

## [0.1.1] - 2026-09-22

打包、安装可靠性与工程化收口。没有新增面向用户的课表能力，也没有破坏性的
schema / API 变化，因此是补丁版本（`v0.2.0` 留给后续真正的课表能力扩展）。

### Changed

- **默认 eHall 接口定义随安装包分发**：移入
  `src/xjtu_calendar/data/ehall_endpoints.json` 并声明为 package data，
  不再依赖源码仓库的 `config/` 目录。
- **接口定义通过 `importlib.resources` 加载**（对 wheel / zip 导入同样可用），
  不再使用 `Path(__file__).parent/...` 这种只在源码仓库成立的写法。
  查找优先级：**显式路径 > 用户数据目录
  `~/.xjtu-timetable-calendar/ehall_endpoints.json` > 包内默认值**；
  前一级解析不出有效端点时回退下一级。
- **运行期版本改为读取包元数据**：`xjtu_calendar.__version__` 由
  `importlib.metadata` 提供，未安装时回退解析 `pyproject.toml`。
  版本号唯一来源仍是 `pyproject.toml`，代码中不再有第二处版本常量
  （**未引入** `setuptools-scm`）。
- **新增 GitHub Actions CI**（`.github/workflows/ci.yml`）：Python 3.11 / 3.12 / 3.13
  上跑 `pytest -q`、`mypy --strict`、`ruff check .`；另有一个 wheel 任务
  在干净虚拟环境安装后自检自包含性，避免「从源码目录碰巧找到配置文件」的假通过。
- `config/ehall_endpoints.example.json` 的定位调整为「用户覆盖模板」，
  并在文件内说明真实定义已随包分发。

### Fixed

- **常规 wheel 安装不再依赖源码仓库目录布局**：此前 wheel 不自包含，
  `pip install .` 之后必须手工复制接口配置才能首次使用；现在安装即可用。
- 移除 `load_endpoints` 中一处不可达的返回语句（上一轮改写遗留）。

### 验证

- 真实构建 `xjtu_timetable_calendar-0.1.1-py3-none-any.whl`，
  内含 `xjtu_calendar/data/ehall_endpoints.json`。
- 干净安装目录（非源码树）下：`__version__ = 0.1.1`、
  `xjtu-calendar --version` 输出 `0.1.1`、`METADATA` 中 `Version: 0.1.1`，端点可加载。
- 三道 gate：pytest 319 / `ruff` 无告警 / `mypy --strict` 无错误。

---

## [0.1.0] - 2026-09-21

首个可用版本（对应 commit `d78c93a`）。覆盖
「eHall 个人课表 → 标准 iCalendar」的完整链路，并在真实账号上跑通。

### Added

- **数据模型**：`Course / CourseMeeting / Semester` —— 课程安排只保存
  「星期 + 节次 + 教学周」，**不含任何钟点**；时区恒为 `Asia/Shanghai`。
- **周次解析**：连续 / 离散 / 混合区间、单双周、全角数字、无「周」字、
  去重乱序、越界报错，以及教务真实字段 `SKZC` 的 18 位周次位掩码。
- **节次解析**：连排 / 逗号分隔 / 单节 / 全角 / 括号噪声；
  结构化 `KSJC`+`JSJC` 优先于文本。
- **教学周 → 日期**：`AcademicCalendar` 完成换算；支持停课日（`excluded_dates`）
  与调课日（`overrides` + `source_date`）。
- **作息表**：`ScheduleTable` 按日期选择夏秋 / 冬春作息，再把节次换算为钟点；
  未配置时明确报错，**不填默认值**。
- **调课日建模**：`source_date` 语义 —— 事件日期 = 调课目标日，
  钟点 = 目标日作息，教学周（含单双周 / 位掩码）= 来源日的周次。
- **导出**：每次上课生成独立 `VEVENT`（**不用 RRULE**），
  UID = `sha256(semester+course_key+date+periods)`，稳定可复现；
  输出符合 RFC 5545（CRLF、时区、转义）。
- **抓取**：HTTP 路径（复用本地会话 cookie，轻量）+ 浏览器路径（Playwright，兜底）；
  端点来自真实网络观测，缺失或仍是占位符时明确报 `EndpointNotConfigured`。
- **CLI**：`login / status / fetch / export / inspect`，带错误体系与退出码。
- **隐私**：日志脱敏按「键上下文」匹配（覆盖教务敏感字段），
  用户数据全部放在 `~/.xjtu-timetable-calendar/`，不进仓库。

### Fixed

- 周次文本被静默截断时不再误判为合法（明确报错）。
- 教学周越界的课程不再静默跳过（fail-closed，列出明细）。
- 无法识别的日历调整项默认中断并列出，需显式 `--allow-unsupported-adjustments` 才放行。

### 验证

- 真实账号端到端导出：96 个 `VEVENT`；两次导出 UID 集合一致（仅 `DTSTAMP` 变化）。
- 真实校历事实已建模：中秋 09-25~09-27、国庆 10-01~10-07 停课；
  09-20 借 10-06（周二）、10-10 借 10-07（周三）课表。
- 三道本地 gate：测试全通过、`ruff` 无告警、`mypy --strict` 无错误。
