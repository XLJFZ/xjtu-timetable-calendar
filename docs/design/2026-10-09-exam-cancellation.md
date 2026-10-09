# 考试取消通路（`STATUS:CANCELLED`）— 设计规格

日期：2026-10-09　状态：已按**代码现实复核 + 对抗评审**修订（原稿有三处会被实现期才发现的错误：
D4 的理由、`sequence_stats` 的污染项、以及"降级等于没有增量"与撤销的相互作用；修订点集中在
新增的 D18-D24、§5、§6.3-§6.5、§7、§9，原稿问题记在文末 §12）
作者：D1-D5 由用户逐条确认；D6-D17 由代理按用户授权拍定；D18-D24 是复核后追加
关联：`docs/design/2026-10-08-exam-schedule.md`（v0.5.0 考试接入；其 §7.1 就是本文要消掉的限制）、
`docs/design/2026-10-07-url-subscribe-ics.md`（订阅通道与 token 轮转）

---

## 1. 背景与目标

v0.5.0 把考试事件并进了同一份 `.ics`，但**只能新增、不能撤销**：`render_ics` 全仓只发
`method:PUBLISH`（`exporter.py:469`），没有任何 `STATUS:CANCELLED` / `METHOD:CANCEL` 通路。
后果写在 v0.5 spec 的 §7.1 与 README `:472-484`：考试改期、取消，或用户"确认本学期无考试"
之后，**已经发布到订阅端的考试事件不会消失**，只能让用户手动删。

目标：**让"这次不该存在"的考试事件在下一份发布产物里带着撤销信号出现一次以上，
直到它的原定时刻自然过去**，从而把上面那条人工补救从主路径里去掉。

约束（不是目标，是红线）：

- 课程事件的字节输出一律不变（§6.6 与 §9 的两支 golden）。
- 考试侧任何失败都必须降级成"没有撤销"，绝不让 `export` / `subscribe push` 非零退出。
- **降级态绝不能变成"撤销一切"**：本特性最大的新风险就是把"我没看到考试"实现成
  "考试不存在"（D18，这是原稿没防住的那一条）。
- 不新增第二个订阅 URL、不新增第二个 token、不新增网络依赖。

## 2. 决策表

| # | 决策 | 结论 | 谁定的 | 理由 / 代价 |
| --- | --- | --- | --- | --- |
| D1 | 撤销信号怎么下发 | 同一份 `method:PUBLISH` 文件里给该事件加 `STATUS:CANCELLED` | 用户 | 保住"一份日历一个 URL"；`METHOD:CANCEL` 独立产物要用户手动多订一条链接，等于把负担还给用户 |
| D2 | 保留多久 | 到该事件**原定开始时刻过去**为止 | 用户 | 错过几次刷新的订阅者仍能在真正需要前收到；时刻一过条目无意义，个人信息不再占位 |
| D3 | 撤销条目在发布产物里带哪些字段 | **最小字段**：UID + DTSTART/DTEND + STATUS + SEQUENCE + DTSTAMP，且**不写 SUMMARY 行** | 用户 | 座位号/考场/主考教师不在保留期内继续公开。实测（icalendar 7.3.0）`add("summary","")` 会写出字面 `SUMMARY:` 空值行，所以"没有标题"必须靠**不调用 `add`** 实现，见 §6.2 |
| D4 | `--no-exams` 要不要触发撤销 | **不触发，且完全不碰台账** | 用户 | 旗标语义定为"这次的渲染口径"，不是"这些考试不存在"。原稿写的理由（"不会出现删了又重建"）**不成立并已更正**：关开关时 live 考试本来就从产物里缺席（v0.5 既有行为），重新打开会以同 UID 回来 —— 那是 v0.5 就有的语义，本文不改变它，只承诺**撤销分支也不被触发**（D18、D21） |
| D5 | 真机验证 | 要，在**隔离 token / 专用 URL** 上做，与手机端自动刷新演练合并 | 用户 | 不碰日常在用的真实订阅；把"客户端到底删不删"从推测变成证据 |
| D6 | 撤销候选从哪来 | 新增**考试台账**文件，撤销候选 = 台账 UID − 本次 live 考试 UID | 代理 | 考试的 UID（`exams.py:288`）与课程的 UID（`exporter.py:72`）同域同配方，从 UID 串分不出彼此；现算差集会在课表快照缺失时把整个学期课程误判成消失的考试并逐条撤销——不可接受的失败模式 |
| D7 | 台账是"上次 live 集合"还是"累积账本" | **累积**：本次 live ∪ 上次台账里时刻未过的条目 | 代理 | 若只存 live 集合，撤销信号只能活一次渲染，与 D2 直接矛盾 |
| D8 | 撤销的 SEQUENCE 怎么保证幂等 | **不新增任何序号机制**：靠 `resolve_sequence` 的既有指纹规则 | 代理 | 撤销条目字段每次完全相同 ⇒ 与上次发布产物指纹一致 ⇒ 序号保持；首次转撤销时指纹变 ⇒ 恰好 +1。`sequence.EventBaseline` 一个字段都不用改（§6.4）。**前提由 D20 兜住**：没有基线就没有幂等可言 |
| D9 | 保留期用哪个时钟 | **墙钟**（可注入 `cancel_expiry_at`，默认 `datetime.now(TZ_XIAN)`），不用渲染 stamp | 代理 | 渲染 stamp 走 `_stamp_from_snapshot`（课表快照 mtime，`exporter.py:538-551`），用户不 `fetch` 它就不前进；拿它判过期等于撤销信号永不消失 |
| D10 | 台账里存不存撤销标记与全字段 | 存**最后一次 live 形态的全字段**，不存 STATUS、不存序号 | 代理 | 台账是本地私有文件，留全字段才能复述与诊断；发布件按 D3 只取最小字段，两者刻意不对称 |
| D11 | 台账何时回写 | **产物确实落地/发布成功之后**才回写，与 `last-<学期>.ics` 同一时序（`cli.py:1249-1254`） | 代理 | 否则会出现"我们以为自己撤销过了，但订阅端从没收到"这种最难查的错位 |
| D12 | `rotate` 换 URL 后旧订阅者的撤销 | 不补救，只**事前警告** | 代理 | 换 token 就是换 URL（`subscribe.py:297`），旧地址已成孤儿；诚实告知比假动作好。落地方式见 D24 |
| D13 | `diff` 那句"本工具不发布取消事件" | 必须改口，并新增"本次将撤销 N 条"摘要行 | 代理 | `cli.py:1094-1099` 在本文实现后会变成假话；同类陈述散在四处，全清单见 §10 |
| D14 | 升级后第一次渲染的产物 | 必须与 v0.5.0 **逐字节相同**（第二支 golden 钉住） | 代理 | 台账为空 ⇒ 无撤销 ⇒ 产物不变；这是"已订阅用户升级不被误伤"的唯一硬证据，比现有课程侧 golden（课程-only 输入）更贴这个特性 |
| D15 | 台账为空/损坏时的行为 | 降级成"不知道有东西要撤销"，记 warning，不报错 | 代理 | 沿用"增量出问题则产物等于没有增量"的既有原则（`exporter.py:690-717`） |
| D16 | 版本号 | `0.6.0` | 代理 | 新能力、不破坏既有契约；`--no-exams` 与三态判定原样保留 |
| D17 | 课程侧要不要撤销通路 | **不做** | 代理 | 课程"消失"多半是选课/学期变化，撤销会造成整学期删除的误伤面 |
| **D18** | **撤销的准入门槛** | **只有"考试数据可信"时才计算候选**：快照读到了、且解析没炸（`NO_EXAMS` 的空快照算可信）。任何降级路径（无快照、非 UTF-8/JSON 炸、`--from-file` 从未抓过考试）⇒ **不撤销、不读台账、`exam_ledger_text=None`** | 复核 | 现有降级把 `exam_events` 置成 `[]`（`exporter.py:690-696` 与 `:712-717`），而台账里全是 UID ⇒ `台账 − ∅` = **撤销整学期**。`tests/test_exporter_exams.py:204-215` 那条"GBK 坏字节"用例正是这个形状，今天它断言 `result.ics == courses.ics`，没有门槛时它在新设计下会变成"整学期取消" |
| **D19** | 候选用哪个 live 集 | **未经日期过滤**的 live 集 | 复核 | `exporter.py:726-727` 用同一组边界把 `exam_events` 一起裁掉。若候选在过滤**之后**算，一次 `export --from-date/--to-date` 就会把窗口外的考试当成"消失"而撤销并从台账剪掉 —— 局部导出动了全局订阅状态 |
| **D20** | UID 不在 SEQUENCE 基线里的候选 | **跳过并 warning**，不下发 | 复核 | `sequence.py:188-189`：UID 不在基线 ⇒ 序号给 **0**。客户端若已握着序号 5，收到序号 0 的 CANCELLED 依规范应当忽略 ⇒ 幽灵永存，正好是本特性要杀的东西。发生条件都是真实的：`--no-sequence`、`export -o <新路径>`（`cli.py:507` 的自动基线探测拿不到旧文件）、上一次发布来自 `--no-exams`（产物里没有考试） |
| **D21** | `--no-exams` 期间撤销条目怎么办 | 与 live 考试**同等缺席**：关开关时产物里既没有 live 考试也没有撤销条目 | 复核 | 撤销条目属于"考试侧"，单独留着它会让"关掉考试"的产物里冒出一堆无标题条目，语义更难解释。代价：撤销的下发时机顺延到开关重新打开之后，台账不受影响（D4），因此不会丢信号 |
| **D22** | 台账用什么格式 | **自己的极简 VCALENDAR writer**，不复用 `render_ics`；读取时**丢弃 naive 起止** | 复核 | `render_ics` 无条件写 DTSTAMP/SEQUENCE/LAST-MODIFIED 与 `X-WR-CALNAME`（`exporter.py:476-487`），拿它写台账等于把 D10 的"不存序号"立刻推翻。naive 那条是硬伤：`sequence.py:81-85` 的 `norm_dt` 对 naive 值不加 tz，而 D9 的 `now` 是 aware ⇒ 比较直接 `TypeError` |
| **D23** | `sequence_stats` 要不要含撤销条目 | **不含**；且明确被污染的是 `updated` 不是 `added` | 复核 | `sequence_stats` 返回 `{"preserved","updated","added"}`（`sequence.py:219`）。撤销条目的 UID **在**基线里（live 形态），指纹变了 ⇒ 计入 `updated`。原稿说它污染 `added == 1` 是错的；照原稿写的测试（"有撤销条目时 added 仍为 1"）在任何实现下都恒真，证伪不了东西 |
| **D24** | 两处工程细节 | ① 台账写盘前 `mkdir(parents=True, exist_ok=True)`，写失败只 warning 不改退出码；② rotate 的警告走**探针渲染**（算出 N 后丢弃 `exam_ledger_text`），再换 token | 复核 | `ensure_dirs` 今天**不创建** `subscribe/`（`config.py:118-127` 只列了 home/session/semesters/schedules/raw），而 `atomic_write_text` 要求父目录存在 ⇒ CLI 里一处 `OSError` 会让导出非零退出，违反 §1 红线。rotate 在 `cli.py:1271` **先换 token** 才渲染，"换之前警告"按原稿写法无法实现 |

## 3. Non-goals（明确不做）

- **不发 `METHOD:CANCEL`**，不产出第二份 `.ics`，不让用户多订一条链接。
- **不做课程事件取消**（D17）。
- **不从发布仓/远端拉台账**：本特性零新增网络依赖，台账只在本地。
- **不试图覆盖"一次性导入型"客户端**（ColorOS 一类系统日历导入后不回源，见 README「URL 订阅」）。
  对它们本文只提供"手动删除时的辨认线索"。
- **不新增 CLI 旗标**（撤销是数据驱动的行为，不是用户选项），也不新增配置项。
- **不改 UID 配方、不改 `--no-exams` 语义、不改三态判定表**（D18 是在三态**之后**再加一道
  渲染侧门槛，不是改判定）。
- **不处理"台账文件的自动过期清理"**（换过学期 key 之后遗留的旧台账文件；见 §8 的残余说明）。

## 4. 现状机制事实（全部来自读代码，非推测）

| 事实 | 位置 | 对本设计的意义 |
| --- | --- | --- |
| 全仓唯一的 METHOD 出口，硬写 `PUBLISH` | `exporter.py:469` | 撤销在同一个 cal 对象里做，不动 method |
| SEQUENCE 基线读**本地留底** `subscribe/last-<学期>.ics`；`export` 的自动基线是输出文件本身 | `cli.py:1229/1245/1274/1287`、`cli.py:506-507` | D8 的根据；也是 D20 那个"基线里没有 ⇒ 给 0"的入口 |
| `parse_baseline` 丢弃无 UID 与 DTSTART/DTEND 非 datetime 的事件 | `sequence.py:120-123,139-141` | 撤销条目仍是 DATE-TIME（v0.5 红线继续成立），不会被基线漏掉 |
| `resolve_sequence`：新增 → 0（`:188-189`）；指纹不变 → 保号并保留 LAST-MODIFIED（`:190-192`）；变了 → `+1`（`:193`） | `sequence.py:158-193` | 幂等的全部来源；D20 处理的正是"新增 → 0"这一支 |
| `event_fingerprint` 的输入只有 summary/location/description/start/end，且起止归一到 UTC；DTSTAMP、SEQUENCE、LAST-MODIFIED **刻意不参与** | `sequence.py:74-94` | D3 的"字段每次相同"才等于指纹相同；`norm_dt` 对 naive 值不加 tz ⇒ D22 必须丢 naive |
| `sequence_stats` 返回 `{"preserved", "updated", "added"}`，`updated` 即 bumped | `sequence.py:196-219` | D23：被污染的是 `updated` |
| 考试侧**两处**降级都把 `exam_events` 置空：无快照（`TimetableFetchError`）与任何 `Exception` | `exporter.py:690-696`、`exporter.py:712-717` | D18 的门槛必须覆盖这两处，缺一不可 |
| 日期过滤用同一组边界**同时**裁课程与考试 | `exporter.py:726-727` | D19 |
| 全局 UID 断言只吃 `exam_events`，处置是"丢弃后来者 + warning，不中止导出"；随后 `render_events = [*events, *kept_exam]` | `exporter.py:730-744` | 撤销条目插在断言之前，与 D19 的"过滤之后"不冲突 |
| `render_ics` 与 `sequence_stats` 目前吃**同一个**合并集合 | `exporter.py:794`、`exporter.py:808` | D23 要求两者口径分叉，必须显式写出来 |
| 渲染 stamp / DTSTAMP 由**课表**快照 mtime 推导 | `exporter.py:538-551`、`:789` | D9 的根据。注意：字节稳定只到"某条目跨过自己的 DTSTART"为止，跨过那天产物就少一条（不是"同一天绝对稳定"） |
| `ExportResult` 是**非 frozen** 三字段 dataclass，构造点全用关键字 | `exporter.py:524-535`、`:810` | 新字段追加在 `sequence_stats` 之后即可，测试构造点不破（m2） |
| `CalendarEvent` 是 frozen dataclass，最后一个字段 `meeting` 已带默认值；四个构造点全用关键字 | `models.py:280-296`；`exporter.py:274,337`、`exams.py:530`、`tests/test_sequence.py:46` | `status` 追加在末尾且带默认值是安全的；无 `dataclasses.replace/asdict/astuple` 用在 `CalendarEvent` 上 |
| `render_ics` 无条件写 DTSTAMP/SEQUENCE/LAST-MODIFIED 与 `X-WR-CALNAME` | `exporter.py:476-487` | D22：台账不能由它生成 |
| 实测：icalendar 7.3.0 下 `add("summary","")` 输出 `SUMMARY:` 空值行；不调用 `add` 才没有该行 | 本地脚本实测（v0.6 实现时由 §9 的用例钉住） | D3 的"无标题"必须靠不写该行 |
| `ensure_dirs()` **不创建** `subscribe/`；`subscribe_dir` 的字面量只在 `subscribe.py:114` | `config.py:118-127` | D24①；也说明 §6.1 的新 helper 是收敛而非重复 |
| 默认 home 在用户主目录下（`Path.home()/DEFAULT_HOME_NAME`），`.gitignore:40` 另有同名条目 | `config.py:62` | §8 的口径要写准：台账默认**在仓库之外**，不是"靠 .gitignore 排除" |
| `rotate_token` 在 `cli.py:1271` **先于**渲染执行 | 同左 | D24② |
| `publish` 命中 NO_CHANGE 时 `return 0` 在写留底**之前** | `cli.py:1249-1254` | D11 的时序基线；台账跟随同一个写入点 |

## 5. 总体架构

```
fetch（不变）
  └─ raw/exams-<学期>.json（三态判定决定要不要覆盖）

export / subscribe push / rotate
  ├─ 读 raw/exams-<学期>.json → live 考试集（现有路径，不改）
  ├─ 门槛（D18）：只有"快照读到且解析没炸"才继续，否则整条撤销分支不执行、台账不动
  ├─ 读 subscribe/last-exams-<学期>.ics → 台账（**新增**，可能不存在）
  ├─ 撤销候选 = 台账 UID − **未经日期过滤的** live UID（D19）
  ├─ 剪掉：原定时刻已过的（D2/D9 墙钟）、UID 不在 SEQUENCE 基线里的（D20，记 warning）
  ├─ 剩下的渲染成最小字段 + STATUS:CANCELLED
  ├─ 课程事件 + live 考试 + 撤销事件 → 全局 UID 断言 → render_ics（METHOD 仍 PUBLISH）
  │     └─ sequence_stats 只吃「课程 + live 考试」，不含撤销（D23）
  └─ 新台账文本随 ExportResult 返回；调用方在写完产物/发布成功后才回写（D11）
```

台账的生命周期一句话：**它是"曾经被当作 live 考试发布过、且原定时刻还没过去"的事件账本**。
它不是快照的副本，也不是上次产物的切片；它只回答两件事——"发布过哪些 UID"和"它们原定何时"。

`include_exams=False`（`--no-exams`）时，上面从"门槛"往下的整块**一行都不执行**（D4、D21）。

## 6. 组件与接口

### 6.1 `config.Settings`

```python
def subscribe_dir_path(self) -> Path: ...  # home/"subscribe"，目录字面量的唯一归属地
def exam_ledger_path(
    self, semester_key: str
) -> Path: ...  # subscribe_dir_path/last-exams-<学期>.ics
```

`subscribe.subscribe_dir(cfg)` 改为委托 `Settings.subscribe_dir_path()`（保留原函数与签名，
现有测试与调用点不动）。**不改 `ensure_dirs()`**：目录由台账写入点自己 `mkdir(parents=True,
exist_ok=True)`（D24①），避免给一个可选特性拓宽全局初始化面。**不新增配置项**。

### 6.2 `models.CalendarEvent`

追加一个末尾字段：

```python
status: str | None = None  # 只在撤销事件上非空；课程与 live 考试构造点一律不传
```

`render_ics` 仅在 `item.status` 非空时 `component.add("status", item.status)`；
`SUMMARY` 改为**仅当 `item.summary` 非空才写**（否则 `SUMMARY:` 空值行照旧出现，见 §4 的实测行）。
这两处条件对课程与 live 考试都不成立（它们的 summary/status 一直是非空/空），故字节不变。
`__post_init__` 的 `end > start` 约束对撤销事件同样成立（起止取自台账原值）。

### 6.3 `exams` 模块（新增四个函数，全部纯函数）

`LedgerEntry` 是 frozen dataclass，**定义在 `exams.py`、不进 `models.py`**：它只为台账这一个
用途存在，放进公共模型等于把考试概念漏给课程侧。字段：`uid / start / end / summary /
location / description`（最后一次 live 形态的全字段，D10）。

```python
LEDGER_STATUS_CANCELLED = "CANCELLED"


def load_exam_ledger(text: str) -> dict[str, LedgerEntry]:
    """台账文本 → {UID: LedgerEntry}，用 icalendar 解析，不手写行解析器。

    丢弃：无 UID、起止缺失、**起止为 naive datetime**（D22：与 aware 的 now 比较会
    TypeError）、同一条 UID 重复出现时保留第一条。丢弃只记 warning，不抛异常。
    """


def cancel_candidates(
    ledger: Mapping[str, LedgerEntry],
    live_uids: Collection[str],
    baseline_uids: Collection[str],
    *,
    now: datetime,
) -> list[LedgerEntry]:
    """台账有、本次 live 无、原定时刻未过、且 UID 在 SEQUENCE 基线里 → 待撤销。
    排序必须全序：按 (start, uid) 升序（同日两场考试是实测见过的，只按 start 不是全序）。
    """


def build_cancellation_events(entries: Collection[LedgerEntry]) -> list[CalendarEvent]:
    """撤销事件：UID/DTSTART/DTEND 照抄，status=CANCELLED，summary=""（配合 §6.2 即不写
    SUMMARY 行）、location=None、description=None。UID 一律不重算——撤销的前提就是
    "复用已经发布出去的那个 UID"。
    """


def render_exam_ledger(live: Collection[CalendarEvent], pending: Collection[LedgerEntry]) -> str:
    """新台账 = 本次 live 的全字段 + 仍待撤销条目的原全字段（D7 累积、D10 全字段）。
    刻意不含 STATUS、序号、DTSTAMP 之外的任何渲染期产物（D22）：台账记录"发布过什么"，
    不记录"现在是不是撤销状态"，也不参与 SEQUENCE。
    """
```

`live` 参数收的是**未经日期过滤**的考试集合（D19）。

### 6.4 台账格式（D22 的展开）

台账不是发布产物，但必须是合法 iCalendar 文档，因为读取端就是 icalendar。写死的口径：

- 顶层 `VCALENDAR`：`PRODID`（复用 `render_ics` 用的同一个常量）、`VERSION:2.0`、
  `CALSCALE:GREGORIAN`。**不写** `METHOD`、`X-WR-CALNAME`、`X-WR-TIMEZONE`。
- 每条 `VEVENT`：`UID`、`DTSTART;TZID=Asia/Shanghai`、`DTEND;TZID=Asia/Shanghai`、`SUMMARY`、
  `LOCATION`、`DESCRIPTION`、`DTSTAMP`（RFC 5545 对 VEVENT 是必需属性，取渲染 stamp）。
  **不写** `STATUS`、`SEQUENCE`、`LAST-MODIFIED`。
- 行尾与产物同为 CRLF，写入用 `newline=""`（与 `cli.py:1252-1254` 留底同口径）。

### 6.5 幂等为什么不需要新机制（D8 的展开，含 D20 的边界）

第一次撤销：基线 `last-<学期>.ics` 里该 UID 是**全字段 live 形态**，本次渲染的是**最小字段**
形态，`event_fingerprint` 变 ⇒ `resolve_sequence` 给 `previous.sequence + 1`。
第二次及以后：基线里已是上次发布的最小字段形态，本次字段完全相同 ⇒ 指纹不变 ⇒ 保号并保留
LAST-MODIFIED。**撤销条目不会每次发布都涨一个序号**，这是既有规则的推论，不是新增逻辑。

边界（D20）：以上只在"该 UID 确实在基线里"时成立。基线缺失或被 `--no-sequence` 关掉时
`resolve_sequence` 会把它当新增给 0，而序号 0 低于客户端已握有的值 ⇒ 撤销极可能被忽略。
这类候选**直接跳过并 warning**，宁可不撤销也不发一个注定被忽略的信号。

`sequence_stats` 口径（D23）：撤销条目**不进** stats。它们在基线里以 live 形态存在，指纹变了，
若混进去会凭空抬高 `updated` —— 而不是原稿错判的 `added`。stats 继续吃"课程 + live 考试"
的合并集（保持 `tests/test_exporter_exams.py:218-244` 那条 R1 断言的语义不变），撤销数量走
`info["exam_cancellations"]` 单独报。

### 6.6 `exporter.build_ics_for_semester`

- 新关键字参数 `cancel_expiry_at: datetime | None = None`；`None` 取 `datetime.now(TZ_XIAN)`
  （`TZ_XIAN` 已在 `exporter.py`，不新造时区常量）。只有测试注入，**不暴露成 CLI 旗标**。
- 注入点：门槛（D18）通过之后；候选用**过滤前**的 live 集（D19）；撤销事件在
  `exporter.py:730` 的全局 UID 断言**之前**并入，因此仍受该断言管辖，不豁免。
- `include_exams=False`：不读台账、不算撤销、不回写（D4、D21）。
- 门槛未通过（无快照 / 解析炸）：`exam_ledger_text=None`、`exam_cancellations=0`，与
  `--no-exams` 同形（D18）。台账缺失 → `logger.info`；台账不可解析 → `logger.warning` + 当作
  无台账（D15）。考试侧任何异常一律走既有降级，不改退出码。
- `ExportResult` 追加 `exam_ledger_text: str | None`（在 `sequence_stats` **之后**，非 frozen、
  构造点用关键字，故安全）；`info` 增加 `exam_cancellations: int`。
- `exam_ledger_text` 的**空值口径**：`None` ⟺ 门槛没过、或 `--no-exams`、或既没读到台账也没
  什么可记 —— 此时调用方**不得创建文件**。只要门槛过了且读到过台账，就一律返回文本（可能只含
  仍待撤销的条目），好让过期条目被真正剪掉。**不存在"算出空台账但不回写"的中间状态**。

### 6.7 课程侧字节不变的保证

改动全部落在"门槛通过且（有考试或有台账）"的分支里：`status` 默认不出现、`SUMMARY` 条件化
只影响空值、撤销事件在门槛未过或无台账时集合为空、台账回写在 `--no-exams` 下完全不发生。
因此现有课程侧 golden（`tests/fixtures/legacy_course_export.ics`，课程-only 输入）天然继续
成立——但 §9 要求实测，不靠推理。

### 6.8 CLI

- `export` 写完输出文件之后写台账；`subscribe push`/`rotate` 与 `last-<学期>.ics` 同一时刻
  写台账（D11）。写盘走 `atomic_write_text(..., private=True)`，前先 `mkdir(parents=True,
  exist_ok=True)`，**写失败只 warning，不改退出码**（D24①）。
- 摘要新增一行：`撤销：N 条（已发布考试事件在本次产物中标记为取消）`，`N == 0` 时不打印。
- `subscribe rotate`：在 `subscribe.rotate_token` **之前**跑一次**探针渲染**，取
  `exam_cancellations` 作为 N；若 N > 0 打印「注意：本次有 N 条考试事件尚未从旧订阅地址撤销，
  rotate 后旧地址将永远收不到撤销」。探针渲染返回的 `exam_ledger_text` **必须丢弃**，
  因为产物此刻还没发布出去（D24②、D11）。
- `diff` 文案改口（`cli.py:1094-1099`）：取消现在会自动下发；但**保留**"一次性导入型客户端
  仍需手动删除"这半句，那部分事实没变。

## 7. 错误处理与降级

| 情形 | 行为 | 为什么 |
| --- | --- | --- |
| 三态判定 UNKNOWN | 不覆盖快照 ⇒ live 集不变 ⇒ 不产生任何撤销 | v0.5 安全阀自动继承 |
| 本地没有考试快照（`TimetableFetchError`） | **门槛不过**：不撤销、台账不动 | D18。「没抓过」不是「没有考试」 |
| 考试快照解析炸（非 UTF-8 / JSON 坏 / 结构走样） | 同上，warning 照旧 | D18；对应 `tests/test_exporter_exams.py:204-215` 那条真实形状 |
| `--from-file` / 浏览器抓取路径（从不抓考试） | 门槛不过 ⇒ 不撤销 | D18；v0.5 spec §6.2 已声明这两条路径拿不到考试 |
| 三态判定 NO_EXAMS（确认本学期无考试） | 全部已发布考试进入撤销 | 「确认没有」是该撤销的 |
| `export --from-date/--to-date` 缩窄窗口 | 候选照旧用过滤前的 live 集 ⇒ 窗口外考试**不**被撤销 | D19 |
| 台账不存在（升级后第一次渲染） | 无撤销；写回台账 | D14：升级本身不改动任何已发布事件 |
| 台账损坏 / 不可解析 | warning + 当作无台账，产物正常 | 绝不让增量把主功能拖崩 |
| 候选的 UID 不在 SEQUENCE 基线里 | **跳过该条**并 warning，不下发序号 0 的撤销 | D20 |
| 撤销条目原定时刻已过 | 从产物与台账同时消失（缺席） | D2；此后靠客户端全量替换语义收敛，不再承诺 |
| 被撤销的考试后来又出现在数据里 | 同一 UID 以 live 形态回来，指纹变 ⇒ 序号 +1，STATUS 消失 | 客户端若已删除则重新出现；行为正确，但真机表现未验证（§11） |
| `--no-exams` 期间存在待撤销条目 | 撤销条目与 live 考试一同缺席；台账不动 | D21：信号顺延，不丢失 |
| 台账所在目录 `subscribe/` 不存在 | 写入点自己 `mkdir`；不拓宽 `ensure_dirs` | D24① |
| 台账写盘 `OSError` | warning + 退出码不变 | 否则违反 §1 红线：考试侧问题不许拖崩导出 |
| 换机器 / 删 home | 台账为空 ⇒ 无从撤销，退回"缺席收敛" | 不做远端拉回；README 要写"换设备后如需撤销请手动删除" |
| `publish` 内容无变化被跳过 | 台账同样不回写（与 `last-*.ics` 同点，`cli.py:1249-1254`） | 没发出去就不算发布过；此时台账字节本来也未变（产物少一条撤销 ⇒ 字节必变 ⇒ 不会走这个分支） |

## 8. 安全与隐私

- 台账含考试原名/考场/座位，属个人数据：默认落在 `~/.xjtu-timetable-calendar/subscribe/`，
  **在仓库之外**（`config.py:62` 的默认 home 是用户主目录）。写盘 `private=True`。
  `.gitignore:40` 的 `.xjtu-timetable-calendar/` 只在用户把 `--home` 指进仓库时才起作用，
  原稿"目录已被 .gitignore 排除"的说法把默认情形说成了仓库内，已更正。
- **发布件里的个人信息净变化**：新增的只有"撤销条目的 UID + 起止时刻"（无考场、无座位、
  无教师、无课程名，D3）。这些时刻此前已随 live 形态公开过，本特性不引入新的可识别字段，
  只把它们的存活期延长到原定时刻过去。§9 有用例钉住"撤销条目不含 `LOCATION`/`DESCRIPTION`
  且没有 `SUMMARY` 行"。
- **一条新出现的残余**：台账按学期 key 分文件；若用户改过学期写法（`2026-fall` → `2026-2027-1`）
  或弃用某个 key，那个旧台账文件再也不会被读到，也就永远不会被剪掉，直到用户自己删除。
  本文**不做自动清理**（Non-goals），README 的换设备/换学期段落里给出手动删除指引。
- 日志不打印台账内容；`sjbh` / `zjjsxm` 等既有打码键不变。
- 订阅 URL 的风险口径不变（一学期一 token，转发即授权）。

## 9. 测试策略

`--doctest-modules` 会把 `src/` 的 doctest 当测试跑（`pyproject.toml:72-78`）⇒ **新写的
docstring 里不要放 `>>>` 例子**。`ruff format --check .` 会连带格式化本文档的 ```python 块。

**两支 golden，缺一不可**：

1. 现有课程侧 golden 不动，跑一遍确认仍绿。
2. **新增 v0.5.0 考试侧 golden**：输入 `tests/fixtures/legacy_exam_inputs.json`（全合成：
   示例课程 + 示例考试，占位名、`2030-*` 日期、钉住的快照 mtime，形状照
   `legacy_course_inputs.json` 再加一份真信封的考试 payload），产物
   `tests/fixtures/legacy_exam_export_v050.ics`，由 **tag `v0.5.0` 的 detached worktree**
   （`git worktree add --detach ... v0.5.0`，`PYTHONPATH=src`）渲染而成；新代码在**门槛通过、
   台账不存在**且 `include_exams=True` 时必须逐字节复现它（D14）。生成脚本照
   `_notes/regen_legacy_golden.py` 的惯例：先打印 `xjtu_calendar.__file__` 与
   `git rev-parse HEAD` 再动手；脚本绑定本机路径故不进仓库，重生成步骤写进测试文件 docstring。
   固件必须**留在 `tests/fixtures/` 目录下**：`.gitattributes` 的例外是按 glob
   `tests/fixtures/*.ics -text` 匹配的，挪进子目录就失去保护、会被全局 `* text=auto eol=lf`
   压平（本项目已因此被坑过一次）。写固件用 `newline=""`，否则 Windows 上得到 `\r\r\n`。

**行为用例**（`tests/test_exams_cancellation.py` 与既有文件增补），每条都要能指出"改坏哪一处它先红"：

- 台账往返：写 → 读 → 条目与起止一致；台账文本不含 `STATUS`、不含 `SEQUENCE`（D10/D22）。
- **D18 门槛**：无快照、坏字节两种降级下，**即使台账非空也不产生任何撤销**，且
  `exam_ledger_text is None`、台账文件字节未变。（这条就是原稿的漏洞。）
- D19：带 `--from-date/--to-date` 渲染一次，窗口外考试不出现在撤销集合里、也不从台账剪掉。
- D20：候选 UID 不在基线 ⇒ 该条被跳过且有 warning，产物里**没有** `SEQUENCE:0` 的撤销事件。
- D21：`--no-exams` 时撤销条目与 live 考试一同缺席；再打开后撤销恢复。
- 撤销候选：台账有 live 无 → 进候选；live 有 → 不进；时刻已过 → 不进且从新台账消失。
- 幂等：同一输入连续渲染两次（带同一份基线），撤销事件字节与 SEQUENCE 完全相同。
- 首次转撤销的序号 = 上次发布 + 1（用真实基线文件，不许手工拼 `EventBaseline`）。
- 最小字段：撤销事件含 `STATUS:CANCELLED`，不含 `LOCATION` / `DESCRIPTION`，
  **且产物里不出现 `SUMMARY:` 空值行**（§4 实测：`add("summary","")` 会写出该行）。
- 全序：同日两场考试都进入撤销时，产物字节稳定（`(start, uid)` 排序；只按 start 会抖）。
- naive 台账条目：读取时丢弃并 warning，不 `TypeError`（D22）。
- `--no-exams` 与 golden：产物等于 v0.5.0 golden；台账文件未被创建或修改（mtime 断言）。
- 学期隔离：A 学期台账对 B 学期渲染零影响。
- 全局 UID：伪造与课程 UID 撞车的撤销条目 ⇒ 走既有断言丢弃路径。
- 回写时序：让 `publish` 抛错，断 `exam_ledger_path` 仍是旧字节（D11）；再断
  `subscribe/` 目录不存在时导出仍成功且退出码 0（D24①）。
- rotate：有待撤销条目时警告出现、无时不出现；并断探针渲染**没有**写下台账（D24②）。
- D23：`sequence_stats` 在有撤销条目时 `updated` 不抬高、`added` 仍为 1 —— 同时**必须有一条
  反向用例**：把撤销条目混进 stats 时 `updated` 会变，否则这条断言证伪不了任何东西
  （原稿写的"added 仍为 1"就是这种恒真测试）。
- 既有回归护栏不许改：`tests/test_exporter_exams.py:204-215`（坏字节 ⇒ 产物逐字等于无考试）
  与 `:218-244`（`added == 1`）必须原样通过。

## 10. 文档交付

改口的全清单（复核时逐条 grep 出来的，凡是断言"没有取消通路"的陈述都要处理）：

- README `:472-484`（"全仓没有 `STATUS:CANCELLED`"整段）、`:1102`、`:1168` —— 改为"v0.6.0 起
  自动下发撤销，保留到原定时刻过去；一次性导入型客户端仍需手动删；换设备/换学期后本地台账为
  空或遗留，前者无从撤销、后者请手动删除旧文件"。
- `src/xjtu_calendar/cli.py:1095` 的注释与 `:1094-1099` 的用户可见文案。
- `src/xjtu_calendar/exams.py:398-399`（`_exam_key` docstring 里"v1 没有通路 ⇒ 旧事件永久
  留在每个订阅者日历里"）。
- `tests/test_diff_exams.py:192-193` 与 `tests/test_exporter_exams.py:250` 的同款陈述。
- `docs/design/2026-10-08-exam-schedule.md` §7.1：**保留原文**（已发布的历史叙述），末尾加一句
  "该限制由 `docs/design/2026-10-09-exam-cancellation.md` 消掉"。
- CHANGELOG：`[Unreleased]` 加撤销通路、`diff`/`rotate` 文案、门槛行为。
- 本文档不写"真实数据从未进入公开仓库"一类断言。

## 11. 发布与回滚 / 开放问题

- 版本 `0.6.0`（D16）。发布链路照旧：bump → CHANGELOG → CI 绿 → annotated tag → GitHub Release
  → PyPI。
- 回滚：删除 `home/subscribe/last-exams-*.ics` 即回到"不知道有东西要撤销"的状态。已下发的
  `STATUS:CANCELLED` 无法靠回滚撤回。
- **必须由真机演练回答的三个问题**（D5，验收门槛，不是"以后再说"）：
  1. 手机端 Google 日历（隔离 URL）收到 `STATUS:CANCELLED` 后是**删除条目**、**置灰保留**、
     还是**完全忽略**？三种结果对应 README 三种写法，本文不预判。
  2. 网页端与手机端是否一致（v0.4.1 的结论：网页端加了 URL 而手机端未同步属客户端侧）。
  3. 自动刷新周期到底多久一次（v0.4 诊断：ColorOS 侧不会自动刷新）。
  演练脚本沿用 `_notes/parallel-checks-2026-10-08.md` §D 骨架，把"注入一条合成考试 → 让它
  消失"作为其中一步；结论必须回写本文档与 README，**不达标就照实写成"未验证的期望"**。
- 与 12 月真实数据的交互仍未验证：若服务器改期时换 `WID`（v0.5 spec §11 第一条），本通路把
  旧 UID 撤销、新 UID 新增 —— 顺带**缓解**了那条风险，但叠加后的客户端表现同样只有演练能证明。
- 台账不会无限增长，但**不是**"学期结束自然清空"（原稿这句话不准确，已更正）：只有被渲染过
  的学期台账会被剪枝；不再被渲染的学期 key 对应的文件会一直留在本地（§8 的残余）。

## 12. 原稿的三处错（留档，免得复审时又当成新发现）

1. **门槛缺失（→ D18）**：原稿把"考试侧失败降级成没有增量"当成安全阀照抄，却没注意撤销的
   失败模式与新增相反——`live = ∅` 在撤销语义下是"全部取消"，而现有代码里"没抓到/解析炸"
   也返回 `∅`。这一条会让一次 GBK 坏字节直接撤销用户整学期考试。
2. **`sequence_stats` 污染项写错（→ D23）**：原稿说撤销会污染 `added`，实际 `sequence.py:219`
   把指纹变化的条目计进 `updated`；照原稿写的测试恒真，起不到看守作用。
3. **两处不可能落地的写法**：`--no-exams` 的理由（"不会出现删了又重建"）与 D8 在"基线里没有
   该 UID"时的成立性（→ D4 更正、D20 新增）；以及 rotate 想在换 token 之前拿到 N，而
   `cli.py:1271` 是先换 token（→ D24②）。
