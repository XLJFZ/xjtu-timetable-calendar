# 项目结构

这份文档讲使用层面的细节，从 README 拆出来以保持主入口简短。

> 从 README 拆出，内容一致；主入口见 [../../README.md](../../README.md)。

## 项目结构

```
xjtu-timetable-calendar/
├── README.md
├── LICENSE
├── .gitignore
├── pyproject.toml
├── CHANGELOG.md                        # 版本变更记录
├── docs/
│   ├── design-v0.1.md                  # 技术设计 v0.1（设计基准与漂移记录）
│   ├── design/                         # 功能设计规格（spec，评审后的权威口径）
│   └── plans/                          # 对应实现计划（plan，含任务拆分与验收口径）
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
│   ├── exams.py                        # 考试安排：解析 / 三态判定 / 事件构建 / 考试变更比对
│   ├── auth.py                         # 会话管理
│   ├── fetcher.py                      # 课表抓取（HTTP / 浏览器双路径）
│   ├── exporter.py                     # → CalendarEvent → .ics
│   ├── naming.py                       # 日历标题推导（大X-上/下；不确定就退回基础名）
│   ├── diff.py                         # 新旧课表快照比对（调课检测）
│   ├── notices.py                      # 停课/调课通知解析（HTML 表格 → 配置条目）
│   ├── schedule_notice.py              # 官方作息页解析（表格 → schedule.json 合并方案）
│   ├── sequence.py                     # SEQUENCE / LAST-MODIFIED 版本管理
│   ├── subscribe.py                    # URL 订阅：状态文件 + git 孤儿提交发布
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
│   ├── test_exams.py                   # 考试解析（四种实测时刻形态）/ UID 降级 / 红线
│   ├── test_exams_fetch.py             # 三态落盘：状态未知绝不覆盖已有快照
│   ├── test_exporter_exams.py          # 课程+考试合并 / 全局 UID 唯一性 / SEQUENCE 基线
│   ├── test_diff_exams.py              # 考试变更五类（新增/取消/时间/教室/座位）
│   ├── test_cli_exams.py               # --no-exams 在 fetch/export/push/rotate 的透传
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


---

## 开发

```bash
pip install -e ".[dev]"
pytest                      # 全量测试（含 doctest；条数以本地运行为准）
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
- **发版之后回门户同步文案**：`XLJFZ/xjtu-student-tools` 是独立仓库、纯静态，
  工具的版本号与通路描述在那里各写了一份（`index.html` 的工具卡片 + `README.md` 的
  工具表与对应小节，两处都要改）。**版本号与发布日期那一格已由门户自己的定时任务刷**
  （PyPI 一出版本就跟上，跳 minor 及以上还会开一条 issue 提醒人），所以这条只管句子：
  凡是**动了用户可见的通路或默认值**（新增一类事件、
  旗标默认翻转、撤销语义变化）就必须回门户补，只升版本号不算同步完。句子可以由门户重写，
  但事实要回源头取：版本号对 PyPI `info.version`，默认值与限定语对本文与 `README.md` 的
  对应小节，别凭印象概括。实测教训：v0.1.1（2026-09-28）之后的 8 个 minor 版本门户一字未动，
  直到 2026-10-23 才跟上 v0.9.0；改的时候也差点写出「日历里的旧日程不必手动删除」——
  ColorOS 忽略 `STATUS:CANCELLED` 且单条删不掉，这类限定只有回实测记录才对得住。
  推送门户要显式带代理：`git -c http.proxy=http://127.0.0.1:7897 push origin main`，
  裸推超时失败时照样打印 `Everything up-to-date`，判收到要看远端 sha 与线上产物。

- **发布订阅产物（`subscribe push`）之前的三条顺序规矩**：① 先跑 `doctor` 看「订阅」那一格，
  报「本地发布状态不相容」就先 `subscribe repair`；② `repair` 与 `push` **之间不要再跑 `export`**
  （`export` 会回写课程台账，而台账正是那道门的输入，机制见 `subscription.md` 的「五个边界」），
  否则门会再拦一次 —— **0.9.3 起这一条只对 0.9.2 及更早的自证有意义**：那道门现在能看穿
  "差异只是时间把台账剪短了"并自行放行（`reason=ledger_pruned_by_time`），见 followups §15；③ 带过期退场的发布，**彩排必须贴着 push 当场重跑** —— 哪些占位算"已过原定
  时刻"是输入，隔几小时的旧预测会静默失效（10-17 实测过两次只差一条）。
  这三条不是理论：2026-10-24 一次例行发布前的彩排就是照 ① 发现门拦人，再顺着查到根因在 ②——
  上一次发版验证期间跑过一次 `export`，课程台账被剪新，而发布凭据还停在上一版。因果链的隔离
  复现脚本在 `_notes/pre-push-1024/rehearsal.py`（该目录 gitignored，不进仓库）。

若你在自己的分支上看到大量 `RUF001/002/003`，那是规则的已知误报 ——
它把**中文全角标点**当成「易混淆 Unicode」，而本项目的 docstring、注释与
面向用户的文案本身就是中文。这些规则已在 `pyproject.toml` 里显式关闭，
请不要为了通过 lint 去改写全角标点。

### 测试覆盖重点

- **周次解析**：连续 / 离散 / 混合 / 单双周 / 全角 / 无「周」字 / 去重乱序 / 越界报错 / `SKZC` 位掩码
  / **空格分段**（`1-4周 7-12周` 是并集不是 1..12；`1 - 8 周` 仍是一段——收紧与分词的先后顺序）
- **节次解析**：连排 / 逗号 / 单节 / 全角 / 括号噪声 / 结构化 `KSJC`+`JSJC` 优先
  / 空格分段 / **显式越界报错不裁剪**（`1-30节` ⇒ `PeriodOutOfRangeError`，
  与周次侧 `WeekOutOfRangeError` 同构；`parser.py` 的文本回退据此硬失败，不记为跳过）
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
- **考试安排**：四种实测时刻形态解析 / 三态判定与「状态未知不覆盖快照」/
  UID 降级链与同日两场不撞 / 考试事件必须带 `TZID` 的 DATE-TIME（红线）/
  课程+考试全局 UID 唯一性 / `--no-exams` 四路径透传 / 考试变更五类

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
| `exam_schedule` | POST | `/jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do` | 我的考试安排（form 参数 `XNXQDM`；成功标志在 `extParams.code == 1`） |

因此 `fetch --source http` 可直接走轻量路径。若端点配置缺失或仍是占位符，
`fetch` 会明确报 `EndpointNotConfigured` 而不是拿模板去发请求。

> **不要猜接口。** 以网页实际网络请求为准。学校改版后需重新运行 probe 校准。
