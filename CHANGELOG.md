# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
**版本号单一来源为 `pyproject.toml`**，本文件只记录变更本身。

---

## [Unreleased]

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

### Changed

- **运行依赖下限提升到 `icalendar>=6.1.0`**（原 `>=5.0`）：
  `add_missing_timezones()` 是 6.1.0 才引入的 API（实测 5.0.0 / 5.0.14 / 6.0.0
  均无）。未设置版本上限——未观测到 6.1+ 的不兼容行为。
- **RRULE 守卫语义收窄为「课程 VEVENT 不使用 RRULE」**：此前的全局文本断言
  `"RRULE" not in ics` 会在 VEVENT 仍然正确的前提下，把标准时区组件内部的
  规则一并误伤。红线本身未变，只是把断言落到正确的组件层级。
- **UID 算法保持不变（显式记录已知边界）**：不引入 UID v2、不做自动迁移。
  课程名仍参与 UID，因此同一 `course_id` 的课程被改名时，其事件会被视为
  新事件而非原事件的新版本。这是为兼容已发布的 v0.1.x 而**刻意保留**的选择；
  后续 UID v2 / 订阅机制再单独设计显式迁移。
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

### Added

- 新增 CLI 级端到端测试 `tests/test_cli_notice.py`：正式 schema + 真实通知
  fixture + 临时 `XJTU_CALENDAR_HOME`，覆盖预览、`--apply`、
  unresolved 拒写三条路径（旧的单元测试抓不到「CLI 自己重新实现配置解析」）。
- 新增 `Semester.export_date_range()`：显式给出学期导出边界，
  供 `unsupported_adjustments` 范围判定使用。
- 新增时区合规测试：`VTIMEZONE` 恰好一个且 `TZID=Asia/Shanghai`、
  解析后 `get_missing_tzids() == set()`、`DTSTART`/`DTEND` 本地钟点往返不变、
  空日历不生成 `VTIMEZONE`；RRULE 守卫改为按 VEVENT 结构化断言。

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
