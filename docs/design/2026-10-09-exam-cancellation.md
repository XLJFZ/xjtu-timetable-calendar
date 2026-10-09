# 考试取消通路（`STATUS:CANCELLED`）— 设计规格

日期：2026-10-09　状态：待评审
作者：D1-D5 由用户逐条确认；D6-D17 由代理按用户授权自行拍定（每条附理由，见 §2 的"谁定的"列）
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

- 课程事件的字节输出一律不变（详见 §6.6 与 §9 的两支 golden）。
- 考试侧任何失败都必须降级成"没有撤销"，绝不让 `export` / `subscribe push` 非零退出。
- 不新增第二个订阅 URL、不新增第二个 token、不新增网络依赖。

## 2. 决策表

| # | 决策 | 结论 | 谁定的 | 理由 / 代价 |
| --- | --- | --- | --- | --- |
| D1 | 撤销信号怎么下发 | 同一份 `method:PUBLISH` 文件里给该事件加 `STATUS:CANCELLED` | 用户 | 保住"一份日历一个 URL"；`METHOD:CANCEL` 独立产物要用户手动多订一条链接，等于把负担还给用户 |
| D2 | 保留多久 | 到该事件**原定开始时刻过去**为止 | 用户 | 错过几次刷新的订阅者仍能在真正需要前收到；时刻一过条目无意义，个人信息不再占位 |
| D3 | 撤销条目在发布产物里带哪些字段 | **最小字段**：UID + DTSTART/DTEND + STATUS + SEQUENCE + DTSTAMP | 用户 | 座位号/考场/主考教师不在保留期内继续公开；代价是不支持"直接删除"的客户端会显示一个无标题的置灰条目 |
| D4 | `--no-exams` 要不要触发撤销 | **不触发，且完全不碰台账** | 用户 | 旗标语义定为"这次的渲染口径"，不是"这些考试不存在"；试完再打开不会出现"删了又重建" |
| D5 | 真机验证 | 要，在**隔离 token / 专用 URL** 上做，与手机端自动刷新演练合并 | 用户 | 不碰日常在用的真实订阅；把"客户端到底删不删"从推测变成证据 |
| D6 | 撤销候选从哪来 | 新增**考试台账**文件，撤销候选 = 台账 UID − 本次 live 考试 UID | 代理 | 考试的 UID（`exams.py:288`）与课程的 UID（`exporter.py:72`）同域同配方，从 UID 串分不出彼此；现算差集会在课表快照缺失时把整个学期课程误判成消失的考试并逐条撤销——不可接受的失败模式 |
| D7 | 台账是"上次 live 集合"还是"累积账本" | **累积**：本次 live ∪ 上次台账里时刻未过的条目 | 代理 | 若只存 live 集合，撤销信号只能活一次渲染，与 D2 直接矛盾 |
| D8 | 撤销的 SEQUENCE 怎么保证幂等 | **不新增任何序号机制**：靠 `resolve_sequence` 的既有指纹规则 | 代理 | 撤销条目字段每次完全相同 ⇒ 与上次发布产物指纹一致 ⇒ 序号保持；首次转撤销时指纹变 ⇒ 恰好 +1。于是 `sequence.EventBaseline` 一个字段都不用改（详见 §6.4） |
| D9 | 保留期用哪个时钟 | **墙钟**（可注入 `cancel_expiry_at`，默认 Asia/Shanghai 当前时间），不用渲染 stamp | 代理 | 渲染 stamp 走 `_stamp_from_snapshot`（课表快照 mtime，`exporter.py:538-550`），用户不 `fetch` 它就不前进；拿它判过期等于撤销信号永不消失 |
| D10 | 台账里存不存撤销标记与全字段 | 存**最后一次 live 形态的全字段**，不存 STATUS、不存序号 | 代理 | 台账是本地私有文件，留全字段才能复述与诊断（"上次发布的这场是什么"）；发布件按 D3 只取最小字段，两者刻意不对称 |
| D11 | 台账何时回写 | **产物确实落地/发布成功之后**才回写，与 `last-<学期>.ics` 同一时序（`cli.py:1228`） | 代理 | 否则会出现"我们以为自己撤销过了，但订阅端从没收到"这种最难查的错位 |
| D12 | `rotate` 换 URL 后旧订阅者的撤销 | 不补救，只**事前警告** | 代理 | 换 token 就是换 URL（`subscribe.py:297`），旧地址已成孤儿，任何补救都要写第二个文件；诚实告知比假动作好 |
| D13 | `diff` 那句"本工具不发布取消事件" | 必须改口，并新增"本次将撤销 N 条"摘要行 | 代理 | `cli.py:1094-1099` 在 v2 之后会变成假话；用户可见文案的错误比代码缺陷更难被测试抓到 |
| D14 | 升级后第一次渲染的产物 | 必须与 v0.5.0 **逐字节相同**（第二支 golden 钉住） | 代理 | 台账为空 ⇒ 无撤销 ⇒ 产物不变；这是"已订阅用户升级不被误伤"的唯一硬证据，比现有课程侧 golden（课程-only 输入）更贴这个特性 |
| D15 | 台账为空/损坏时的行为 | 降级成"不知道有东西要撤销"，记 warning，不报错 | 代理 | 沿用 §"增量出问题则产物等于没有增量"的既有原则（`exporter.py:679` 一带） |
| D16 | 版本号 | `0.6.0` | 代理 | 新能力、不破坏既有契约；`--no-exams` 与三态判定原样保留 |
| D17 | 课程侧要不要撤销通路 | **不做** | 代理 | 课程"消失"多半是选课/学期变化，撤销会造成整学期删除的误伤面；本文只处理考试 |

## 3. Non-goals（明确不做）

- **不发 `METHOD:CANCEL`**，不产出第二份 `.ics`，不让用户多订一条链接。
- **不做课程事件取消**（D17）。
- **不从发布仓/远端拉台账**：本特性零新增网络依赖，台账只在本地。
- **不试图覆盖"一次性导入型"客户端**（ColorOS 一类系统日历导入后不回源，见 README「URL 订阅」
  与 `_notes/review-handoff-2026-10-07.md`）。对它们本文只提供"手动删除时的辨认线索"。
- **不新增 CLI 旗标**（撤销是数据驱动的行为，不是用户选项），也不新增配置项。
- **不改 UID 配方、不改 `--no-exams` 语义、不改三态判定表**。

## 4. 现状机制事实（全部来自读代码，非推测）

| 事实 | 位置 | 对本设计的意义 |
| --- | --- | --- |
| 全仓唯一的 METHOD 出口，硬写 `PUBLISH` | `exporter.py:469` | 撤销必须在同一个 cal 对象里做，不动 method |
| SEQUENCE 基线读**本地留底** `subscribe/last-<学期>.ics` | `cli.py:1229/1245/1274/1287/1315` | 撤销条目首次转撤销时指纹必变 ⇒ 既有规则自然给 +1（D8 的根据） |
| `parse_baseline` 丢弃无 UID 与 DTSTART/DTEND 非 datetime 的事件 | `sequence.py:120-123,139-141` | 撤销条目仍是 DATE-TIME（v0.5 的红线继续成立），不会被基线漏掉 |
| `resolve_sequence`：指纹不变 → 保号并保留 LAST-MODIFIED；变了 → `+1`；新增 → 0 | `sequence.py:158,188-193` | 幂等的全部来源；本文不触碰它 |
| 渲染 stamp / DTSTAMP 由课表快照 mtime 推导，为的是"同快照同配置 ⇒ 字节一致" | `exporter.py:538-550` | D9 的根据；也意味着撤销条目在同一天内重复渲染是字节稳定的 ⇒ `publish` 的 NO_CHANGE 跳过仍成立 |
| 考试事件与课程事件在全局 UID 唯一性断言处汇合 | `exporter.py:730-744` | 撤销条目必须同样参与该断言（§6.5） |
| 考试并入失败一律降级，产物等于"没有增量" | `exporter.py:679` 一带 | D15 沿用同一原则，不另立规矩 |
| `CalendarEvent` 是 frozen dataclass，字段见 `models.py:280-296`，`__post_init__` 要求 `end > start` | `models.py:280` | 新增 `status` 必须**追加在末尾且带默认值**，否则位置参数调用点全变 |
| `subscribe_dir(cfg)` 目录字面量目前在 `subscribe.py:114` | — | 台账路径归属 `Settings`，`subscribe_dir` 改为委托，避免出现两处目录名 |
| `rotate_token` 换 token 即换 URL，旧地址成孤儿 | `subscribe.py:297,326-338` | D12 的根据 |
| `diff` 的考试取消只是"报告"，并附带一句"不会自动消失" | `cli.py:1089,1094-1099` | D13 的改口点 |

## 5. 总体架构

```
fetch（不变）
  └─ raw/exams-<学期>.json（三态判定决定要不要覆盖）

export / subscribe push / rotate
  ├─ 读 raw/exams-<学期>.json        → live 考试集（现有路径，不改）
  ├─ 读 subscribe/last-exams-<学期>.ics → 台账（**新增**，可能不存在）
  ├─ 撤销候选 = 台账 UID − live UID
  ├─ 其中 DTSTART >= cancel_expiry_at(墙钟) 者 → 渲染成最小字段 + STATUS:CANCELLED
  ├─ live 考试事件 + 课程事件 + 撤销事件 → 全局 UID 断言 → render_ics（METHOD 仍 PUBLISH）
  └─ 新台账文本随 ExportResult 返回；调用方在写完产物/发布成功后才回写
```

台账的生命周期一句话：**它是"曾经被当作 live 考试发布过、且原定时刻还没过去"的事件账本**。
它不是快照的副本，也不是上次产物的切片；它只回答两件事——"发布过哪些 UID"和"它们原定何时"。

## 6. 组件与接口

### 6.1 `config.Settings`

```python
def subscribe_dir_path(self) -> Path: ...  # home/"subscribe"，目录字面量的唯一归属地
def exam_ledger_path(
    self, semester_key: str
) -> Path: ...  # subscribe_dir_path/f"last-exams-{学期}.ics"
```

`subscribe.subscribe_dir(cfg)` 改为委托 `Settings.subscribe_dir_path()`（保留原函数与签名，
现有测试与调用点不动）。目录由 `ensure_dirs()` 一并物化。**不新增配置项**。

### 6.2 `models.CalendarEvent`

追加一个末尾字段：

```python
status: str | None = None  # 只在撤销事件上非空；课程与 live 考试构造点一律不传
```

`render_ics` 仅在 `item.status` 非空时 `component.add("status", item.status)`。
课程侧构造点与 live 考试构造点都不传该字段 ⇒ 事件模型对既有路径完全无感。
`__post_init__` 的 `end > start` 约束对撤销事件同样成立（起止取自台账原值）。

### 6.3 `exams` 模块（新增四个函数，全部纯函数）

`LedgerEntry` 是一个 frozen dataclass，**定义在 `exams.py`、不进 `models.py`**：它只为台账这
一个用途存在，放进公共模型等于把考试概念漏给课程侧。

```python
LEDGER_STATUS_CANCELLED = "CANCELLED"


def load_exam_ledger(text: str) -> dict[str, LedgerEntry]:
    """台账文本 → {UID: LedgerEntry}。用 icalendar 解析，不手写行解析器。

    LedgerEntry 只带渲染撤销事件与写回台账所需的东西：uid / start / end /
    summary / location / description（最后一次 live 形态）。
    解析不出 UID 或起止非 datetime 的条目直接丢弃（与 parse_baseline 同一口径）。
    """


def cancel_candidates(
    ledger: Mapping[str, LedgerEntry], live_uids: Collection[str], *, now: datetime
) -> list[LedgerEntry]:
    """台账里有、本次没有、且原定开始时刻还没过 → 待撤销（按 start 升序，保证产物字节稳定）。"""


def build_cancellation_events(
    entries: Collection[LedgerEntry], *, semester: str
) -> list[CalendarEvent]:
    """撤销事件：UID/DTSTART/DTEND 照抄，status=CANCELLED，
    summary=""、location=None、description=None（D3 的最小字段）。
    UID 不再重算——撤销的整个前提就是"复用已发布的 UID"。
    """


def render_exam_ledger(live: Collection[CalendarEvent], pending: Collection[LedgerEntry]) -> str:
    """新台账 = 本次 live 的全字段 + 仍待撤销条目的原全字段（D7 累积、D10 存全字段）。
    刻意不含 STATUS：台账记录"发布过什么"，不记录"现在是不是撤销状态"。
    """
```

### 6.4 幂等为什么不需要新机制（D8 的展开）

第一次撤销：基线 `last-<学期>.ics` 里该 UID 是**全字段 live 形态**，本次渲染的是**最小字段**
形态，`event_fingerprint` 变 ⇒ `resolve_sequence` 给 `previous.sequence + 1`。
第二次及以后：基线里已是上次发布的**最小字段**形态，本次字段完全相同 ⇒ 指纹不变 ⇒
保号并保留 LAST-MODIFIED。**撤销条目不会每次发布都涨一个序号**，这是既有规则的推论，
不是新增逻辑。

推论：`sequence_stats` 的口径必须**不含**撤销事件，否则"新增考试 → `added == 1`"那条既有
断言（v0.5 Task 8 立下的）会被撤销条目污染。撤销数量走 `info["exam_cancellations"]` 单独报。

### 6.5 `exporter.build_ics_for_semester`

- 新关键字参数 `cancel_expiry_at: datetime | None = None`；`None` 时取
  ``datetime.now(TZ_XIAN)``（`TZ_XIAN` 已在 `exporter.py` 里，不新造时区常量）。只有测试注入，
  **不暴露成 CLI 旗标**。
- 注入点：在考试事件并入之后、**课程侧日期过滤之后**（撤销事件不受该过滤管辖——日期过滤的
  口径是"课表覆盖范围"，v0.5 spec §6.5 第 1 条已把考试数量与 `date_range` 分开，撤销事件同样
  不该被它管辖）、**全局 UID 唯一性断言之前**（撤销条目必须参与该断言，不豁免）。
- `include_exams=False` 时：不读台账、不算撤销、**不回写台账**（`ExportResult.exam_ledger_text`
  为 `None`），产物字节与 v0.5.0 相同（D4、D14）。
- 台账缺失 → `logger.info`（从没发布过不是错误）；台账不可解析 → `logger.warning` 并按"无撤销"
  继续（D15）。考试侧任何异常一律走既有降级，不改 `export` 的退出码。
- `ExportResult` 增加 `exam_ledger_text: str | None`，`info` 增加 `exam_cancellations: int`。
- `exam_ledger_text` 的**空值口径**必须定死，否则"要不要动那个文件"会散落在调用方各处的
  if 里：`None` ⟺ 本次既没读到台账、也没什么可记（从无考试、从未发布）——此时调用方**不得创建
  文件**。只要读到过台账，就一律返回文本（可能只含仍待撤销的条目，也可能是空台账的合法
  VCALENDAR），好让过期条目被真正剪掉。**不存在"算出空台账但不回写"这种中间状态**。

### 6.6 课程侧字节不变的保证

改动全部落在"有考试或有台账"的分支里：`status` 字段默认不出现、撤销事件在无台账时
集合为空、台账回写在 `--no-exams` 下完全不发生。因此现有的课程侧 golden
（`tests/fixtures/legacy_course_export.ics`，输入是课程-only）天然继续成立，不需要为它
做任何兼容处理——但 §9 要求实测，不靠推理。

### 6.7 CLI

- `export` 写完输出文件之后写台账；`subscribe push`/`rotate` 与 `last-<学期>.ics` 同一时刻
  写台账（D11）。写盘走 `atomic_write_text(..., private=True)`。
- 摘要新增一行：`撤销：N 条（已发布的考试事件在本次发布中标记为取消）`，`N==0` 时不打印。
- `subscribe rotate` 在换 token **之前**检查：若本次渲染存在待撤销条目，打印
  「注意：本次有 N 条考试事件尚未从旧订阅地址撤销，rotate 后旧地址将永远收不到撤销」。
- `diff` 的 `cli.py:1094-1099` 文案改口：取消现在会自动下发；但**保留**"一次性导入型客户端
  仍需手动删除"这半句，因为那部分事实没变。

## 7. 错误处理与降级

| 情形 | 行为 | 为什么 |
| --- | --- | --- |
| 三态判定 UNKNOWN | 不覆盖快照 ⇒ live 集不变 ⇒ 不产生任何撤销 | v0.5 的安全阀自动继承，撤销通路不可能在"教务还没公布"时误删 |
| 三态判定 NO_EXAMS（确认本学期无考试） | 全部已发布考试进入撤销 | "确认没有"就该撤销，这是 D2 的直接后果 |
| 台账不存在（升级后第一次渲染） | 无撤销；写回台账 | D14：升级本身不改动任何已发布事件 |
| 台账损坏 / 不可解析 | warning + 当作无台账，产物仍正常 | 绝不让增量把主功能拖崩 |
| 撤销条目原定时刻已过 | 从产物与台账同时消失（缺席） | D2；此后靠客户端的"全量替换"语义收敛，不再承诺 |
| 被撤销的考试后来又出现在数据里 | 同一 UID 以 live 形态回来，指纹变 ⇒ 序号 +1，STATUS 消失 | 客户端可能已删除，则重新出现；这是正确行为，但必须在 §11 记为"未在真机验证" |
| `--no-sequence` | 不影响是否撤销，只影响 LAST-MODIFIED/序号口径 | 两个开关职责不同，别把它们绑在一起 |
| 换机器 / 删 home | 台账为空 ⇒ 无从撤销，退回"缺席收敛" | 不做远端拉回（Non-goals）；README 要写"换设备后如需撤销请手动删除" |
| `publish` 内容无变化被跳过 | 台账同样不回写（与 `last-*.ics` 一致） | 没发出去就不算发布过 |

## 8. 安全与隐私

- 台账含考试原名/考场/座位，属个人数据：落在 `home/subscribe/last-exams-*.ics`，
  与 raw 快照同等待遇 —— `private=True` 写盘、目录已被 `.gitignore` 排除。
- **发布件里的个人信息净变化**：新增的是"撤销条目的 UID + 起止时刻"（无考场、无座位、
  无教师、无课程名）。原本这些时刻已经公开过（live 形态发布过），本特性不引入新的
  可识别字段，只把它们的存活期延长到原定时刻过去。§9 必须有用例钉住"发布件里的撤销事件
  不含 `LOCATION` / `DESCRIPTION`"，这条承诺不能只靠人读代码。
- 日志不打印台账内容；`sjbh` / `zjjsxm` 等既有打码键不变。
- 订阅 URL 的风险口径不变（一学期一 token，转发即授权）。

## 9. 测试策略

`--doctest-modules` 会把 `src/` 的 doctest 当测试跑（`pyproject.toml:72-78`）⇒ **新写的
docstring 里不要放 `>>>` 例子**。`ruff format --check .` 会连带格式化本文档的 ```python 块。

**两支 golden，缺一不可**：

1. 现有课程侧 golden 不动，跑一遍确认仍绿（课程侧回归的看守）。
2. **新增 v0.5.0 考试侧 golden**：输入 `tests/fixtures/legacy_exam_inputs.json`（全合成：
   示例课程 + 示例考试，占位名、`2030-*` 日期、钉住的快照 mtime，形状照
   `legacy_course_inputs.json` 再加一份真信封的考试 payload），产物
   `tests/fixtures/legacy_exam_export_v050.ics`，由**tag `v0.5.0` 的 detached worktree**
   （`git worktree add --detach ... v0.5.0`，`PYTHONPATH=src`）渲染而成；新代码在
   **台账不存在**且 `include_exams=True` 时必须逐字节复现它（D14）。生成脚本照
   `_notes/regen_legacy_golden.py` 的惯例：先打印 `xjtu_calendar.__file__` 与
   `git rev-parse HEAD` 再动手，**基线来历必须留在输出里**；脚本本身绑定本机路径，因此不进
   仓库，重生成步骤写在测试文件 docstring 里。
   固件行尾靠既有的 `tests/fixtures/*.ics -text` 规则保住 CRLF（`-text` 是按目录匹配的，
   新固件自动覆盖），并复用现有那条"固件必须仍是 CRLF"的契约用例。

**行为用例**（`tests/test_exams_cancellation.py` 与既有文件的增补）：

- 台账往返：写 → 读 → 条目集合与起止时刻一致；`render_exam_ledger` 不含 `STATUS`。
- 撤销候选：台账有 live 无 → 进候选；live 有 → 不进；时刻已过 → 不进且从新台账消失。
- **幂等**：同一输入连续渲染两次，撤销事件字节与 SEQUENCE 完全相同（`resolve_sequence` 保号）。
- 首次转撤销的序号 = 上次发布 + 1（用真实基线文件，不许手工拼 `EventBaseline`）。
- 最小字段：产物中撤销事件含 `STATUS:CANCELLED`，**不含** `LOCATION` / `DESCRIPTION`；
  `SUMMARY` 的实际字节由断言钉住（空值会被 icalendar 写成 `SUMMARY:` 还是整个省略，
  **以实测为准，不许在文档里猜**）。
- `--no-exams`：产物与不传该参数时的 v0.5.0 golden 相同、台账文件未被创建或修改（用 mtime 断言）。
- UNKNOWN：不写快照 ⇒ 渲染两次都不产生撤销。NO_EXAMS：全部进入撤销。
- 台账损坏（截断/非 ICS 文本）：warning + 产物等于无撤销，退出码 0。
- 学期隔离：A 学期台账对 B 学期渲染零影响。
- 全局 UID：伪造一条与课程 UID 撞车的撤销条目 ⇒ 走既有断言路径，不静默写进产物。
- 回写时序：`publish` 失败时台账未变（在测试里让 publish 抛错，再断 `exam_ledger_path` 仍是旧字节）。
- `rotate` 警告：存在待撤销条目时文案出现；无待撤销条目时不出现。
- `sequence_stats` 未被撤销条目污染：沿用 v0.5 Task 8 的 `added == 1` 断言并加一条"存在撤销条目时仍为 1"。

**可伪性要求**（每条都要说明"改坏哪一处它会先红"）：D4 的守卫靠"删掉 `include_exams` 判断"
变红；D7 的累积靠"台账只写 live"变红；D9 的墙钟靠"改用渲染 stamp"使过期用例不再退出候选而变红；
D11 的时序靠"渲染时就回写"使 publish 失败用例变红；D3 的最小字段靠"顺手把全字段渲染进产物"变红。

## 10. 文档交付

- README：`:472-484`（"全仓没有取消通路"整段）、`:1102`、`:1168` 三处改口为"v0.6.0 起会自动
  下发撤销，保留到原定时刻过去；一次性导入型客户端仍需手动删，换设备后本地台账为空则无从撤销"。
- `docs/design/2026-10-08-exam-schedule.md` §7.1：**保留原文**（它是已发布的历史叙述），
  只在末尾加一句"该限制由 `docs/design/2026-10-09-exam-cancellation.md` 消掉"。
- CHANGELOG：新增 `[Unreleased]` 条目（撤销通路、`diff` 文案改口、`rotate` 警告）。
- 本文档不写"真实数据从未进入公开仓库"一类断言。

## 11. 发布与回滚 / 开放问题

- 版本 `0.6.0`（D16）。发布链路照旧：bump → CHANGELOG → CI 绿 → annotated tag → GitHub Release
  → PyPI。
- 回滚：删除 `home/subscribe/last-exams-*.ics` 即回到"不知道有东西要撤销"的状态；产物侧
  无需清库。已下发的 `STATUS:CANCELLED` 无法靠回滚撤回（与 v0.5 的 ghost event 同类问题）。
- **必须由真机演练回答的三个问题**（D5，验收门槛，不是"以后再说"）：
  1. 手机端 Google 日历订阅（隔离 URL）在收到 `STATUS:CANCELLED` 后是**删除条目**、
     **置灰保留**、还是**完全忽略**？三种结果对应 README 的三种写法，本文不预判。
  2. 网页端 Google 日历与手机端是否一致（v0.4.1 的结论是网页端加了 URL 而手机端未同步属客户端侧）。
  3. 自动刷新周期到底多久一次（v0.4 的诊断结论：ColorOS 侧不会自动刷新）。
  演练脚本用 `_notes/parallel-checks-2026-10-08.md` §D 的骨架，把"注入一条合成考试 → 让它消失"
  作为其中一步；演练结论必须回写本文档与 README，**不达标就照实写成"未验证的期望"**。
- 与 12 月真实数据的交互仍未验证：若服务器改期时换 `WID`（v0.5 spec §11 的第一条开放问题），
  本通路把旧 UID 撤销、新 UID 新增 —— 也就是说本文档顺带**缓解**了那条风险，但两者叠加的
  客户端表现同样只有演练能证明。
- 台账会随学期累积吗？不会：路径按学期隔离，条目按"时刻未过"淘汰，学期结束后自然清空。
