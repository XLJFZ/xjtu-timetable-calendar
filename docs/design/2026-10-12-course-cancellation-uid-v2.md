# 课程侧撤销通路与 UID v2 迁移 — 设计规格

日期：2026-10-12　状态：**待评审**（CD1-CD6 由用户逐条确认；CD7-CD19 由代理按用户"你自己判断 /
继续推进"的授权拍定，其中 CD10、CD16、CD17 是设计期复核发现的坑，原方案里没有）
作者：同左
关联：`docs/design/2026-10-09-exam-cancellation.md`（v0.6 考试撤销通路；本文推翻其 **D17**
「课程侧不做撤销」，并复用它的 D2/D3/D8/D9/D10/D11/D15/D18/D19/D20/D22 全套机制）、
`docs/design/2026-10-11-subscription-followups.md` §2（本议题的原始备忘）、
`docs/design/2026-10-07-url-subscribe-ics.md`（订阅通道、留底、tree 护栏）

---

## 1. 背景与目标

v0.6 的撤销通路**只管考试**（D17）。课程侧于是留着这个洞：课程 UID 的配方含课程名
（`exporter.py:116`，注释在 `exporter.py:96-100` 里明写是 legacy UID contract），所以
**改课名 / 教学班换身份**时旧事件不会被任何机制收掉——订阅端的表现是"旧事件永存 + 新事件出现"，
而旧的那批是带真实课程名的**整学期日程**。学期中这比"考试取消"常见得多。

v0.6 当时判定"不做"的理由（课程消失多半是选课/学期变化，误伤面是整学期删除）依然成立——
所以本文不是"把考试那套搬到课程上"，而是**同时改掉产生这个问题的配方**：

- **根治**：UID v2 去掉 `course_name` ⇒ 改名不再产生新事件，走同 UID 原地更新。
  撤销通路于是只服务真退课、教学班 `course_id` 变化、以及升级那一次迁移——**量小**。
- **配齐防护**：课程侧的误伤面是整学期，所以门槛比考试侧硬一档（两道门 + 熔断拒绝发布）。

目标一句话：**让"这次不该存在"的课程事件带着撤销信号出现，同时保证"没抓到课表"
永远不会被实现成"课全没了"，并且让已导入用户平稳跨过 UID 配方这一变。**

红线（不是目标，是约束）：

- **降级态绝不能变成"撤销一切"**——这是 D18 那条教训在课程侧的放大版，也是本文最大的新风险。
- 撤销条目不含课程名/教师/考场，产物不新增任何暴露面（D3 口径不变）。
- 不新增第二个订阅 URL、不新增 token、不新增网络依赖、不新增配置项。
- 干净升级的第一手证据是测试，不是文档承诺。

---

## 2. 决策表

| # | 决策 | 结论 | 谁定的 | 理由 / 代价 |
|---|---|---|---|---|
| CD1 | 这一版修到哪一层 | **根治**：改 UID 配方 + 一次性迁移，撤销通路同时做但只服务小量场景 | 用户 | 单纯把考试那套搬到课程上，改名的表现会从"旧事件永存"变成"旧事件变成整学期几百条「已取消的活动」"（撤销条目按 D2 保留到原定时刻过去，而忽略 `STATUS:CANCELLED` 的 Outlook.com / ColorOS 都实测会当普通事件导入）。止血不解决问题，还把代价放大了一档 |
| CD2 | UID v2 配方 | `semester_key \| stable_course_key \| day \| periods_token`——**只减 `course_name` 一项**，其余四项一字不动 | 用户 | 时间和地点本来就不在配方里（换教室原地更新），年级从来没进过。少动一项就少一分不可知 |
| CD3 | 迁移撤销的下发面 | **只在 `subscribe push` 自动发一次全量撤销**；`export` 不参与 | 用户 | 双击导入的用户会把几百条「已取消的活动」当**新事件**导进去，而导入型永不回源、只能手删。手工导入的老用户改由 README + `doctor` 提示覆盖 |
| CD4 | 课程侧准入判据 | **两道门**：门 1 数据可信（照 D18）+ 门 2 比例熔断，**熔断即拒绝发布**、非零退出，要显式旗标才放行 | 用户 | 考试侧熔断触发时是"静默不撤销"（D18 的降级 = 没有增量），最坏是撤销晚一天。课程侧最坏是"我们以为撤销过了，订阅端从没收到"这种最难查的错位，所以选 fail-closed |
| CD5 | 撤销条目标题 | 沿用固定文案「已取消的活动」，**不带课程名** | 用户 | 孤儿单提交意味着改名前的旧课程名在新产物里已不再出现，带上去 = 把它重新公开一个保留期，与 D3 相左。辨认问题靠文档解决：迁移那一次本来就是整本旧事件，唯一正确动作是退订重加 |
| CD6 | 验收 | **先在隔离 token 的 URL 上做真机演练**（Google 网页 + ColorOS 各一条腿），再发版 | 用户 | 照 v0.6 的 D5 先例。这次风险面比那次大（一次产生几百条撤销），未验证就写进文档会违反"真机未验证的表现不许写成事实" |
| CD7 | 迁移怎么判定 | 订阅状态文件新增 `uid_recipe` 字段：**缺失 = 该 URL 最后一次发布出自 v1** ⇒ 本次 push 走迁移分支；成功后写 `2` | 代理 | UID 是 hash，反推不了配方；往公开产物里塞自定义属性（`X-WR-*`）既加暴露面又有严格客户端兼容风险。状态文件是本地私有、已有读写路径 |
| CD8 | 课程侧要不要独立台账 | **要**：新增 `subscribe/last-courses-<学期>.ics`，与考试台账同等待遇 | 代理 | 不能直接拿发布留底当"上次 live 集"：考试 UID 与课程 UID **同域同形状**（`sha256[:32]@domain`），从串上分不出彼此——这正是考试侧 D6 的理由。带 `--no-exams` 发布时留底里的考试 UID 会看起来"全消失了"，误撤整批考试 |
| CD9 | 通用撤销机制放哪 | **纯搬迁**到新模块 `cancellation.py`（`LedgerEntry` / `cancel_candidates` / `build_cancellation_events` / `CANCELLATION_SUMMARY` / 台账 render+load 的通用部分），`exams.py` 保留 re-export | 代理 | 既让"权威在哪"说得清，又不动 v0.6 已发布代码的调用点与测试 import——零回归面。参数化台账文件名（`last-exams-*` / `last-courses-*`），逻辑一份都不复制 |
| CD10 | 迁移候选要不要过 D2 过期筛 | **要**：留底里 `DTSTART` 已过的 UID 不进迁移候选 | 代理（复核新增） | 留底里也含**上一次已撤销**的条目，而它们不在任何台账里（台账只存 live 形态，D10），迁移分支若不过期筛会把上学期的旧事件反复撤销、无限续命。本学期课程的时刻都在未来 ⇒ 全量撤销不受影响 |
| CD11 | 迁移分支与门 2 的关系 | 迁移分支**豁免**熔断，但 push 前必须打印一行预告（N 条 + 原因） | 代理 | 迁移是配方切换，不是数据缺失，用整学期撤销的规模去判熔断会永远卡死升级。"自动但不静默"：不要求用户额外操作，但绝不让它悄悄发生 |
| CD12 | 熔断判据 | `候选数 ≥ 10` **且** `候选数 ÷ 未过期（原定时刻 ≥ now）课程台账条目数 ≥ 0.30` ⇒ 触发。两个常量写死在模块顶部（`MASS_CANCELLATION_MIN_EVENTS = 10`、`MASS_CANCELLATION_RATIO = 0.30`），**不新增配置项**；出口是旗标 `--allow-mass-cancellation` 与新退出码 `6` | 代理（分母经终审修订，见 `7cc0ae2`） | 双条件的分工：数量下限保证**候选不足 10 条时熔断永不介入**——不论比例多高，任何日历上的正常逐学期加减课调整都不会被拦（小台账"未过期 30 条里撤 12 条"因 12 ≥ 10 且 12/30 = 40% ≥ 30%，**是会触发的**，出口正是旗标 + 退出码 6，这符合 fail-closed 的本意）；比例条件则放行大日历的局部异常（未过期条目 500 条里撤 20 条 ≈ 4%，不触发）。阈值 0.30 拍的是"一学期最多几门课变动"的量级。**分母口径终审改为「未过期」条目**：分子 `deliverable` 本就只数未来条目（`cancel_candidates` 把 `start < now` 筛掉），分子分母必须同域。旧口径数整本台账会被过期条目稀释——学期中段"台账 500 条里 450 条已过去 + 只抓到 1 门课" ⇒ 候选 50、50/500 = 10% < 30% ⇒ 熔断**漏响、剩余学期 100% 被静默撤**；按未过期条目 50 算则 50/50 = 100% ≥ 30%，熔断如期触发（`courses.upcoming_ledger_count`）。**（2026-10-12 修复回写 `bb2a721`）** 门 2 数的是**本次新撤销**，不是「产物里带了几条撤销」：**分子与分母都排除基线（留底）里 `STATUS:CANCELLED` 的 UID**。原因：课程台账按 D10 不带 STATUS，「还活着的课」和「上次已撤、这次继续携带的墓碑」在台账里同形，`cancel_candidates` 会把墓碑重新算成候选，于是迁移跑完之后每一次普通 `push` 都被自己的保险丝误杀（真数据彩排实测：75 条墓碑 ÷ 未过期分母 150 = 50%，修复前第三次 push 退出码 6）。「上次已发布过这条撤销」的权威在留底（带 STATUS），不在台账。**分子分母必须同时排除**——只排分子会在学期末重新打开分母稀释的洞（剩余 live 被抹掉、分母还挂着墓碑 ⇒ 比例跌破阈值、熔断漏响）。两个常量（10 / 0.30）与「不新增配置项」口径照旧 |
| CD13 | 门 1 的判据 | `courses_trusted =` 快照读到了 **且** 解析没有任何 skipped/unresolved 行 **且** 课程门数 ≥ 1。不满足 ⇒ 不读台账、不撤销、`course_ledger_text=None`（照 D18/D21） | 代理 | `--from-file` 本身**不**取消信任——离线导入是常态操作，判据是"这份数据里有没有课、有没有读不懂的行"，不是它从哪来。课程侧现有行为已经 fail-closed（解析不了的行拒绝写盘），所以门 1 主要防的是"接口返回了半份课表" |
| CD14 | 日期裁剪与撤销 | live 集与台账回写都取**未经 `--from-date/--to-date` 过滤**的课程事件 | 代理 | D19 的对应物。裁剪是课程侧的**常态操作**（考试侧只是偶尔），若候选在过滤之后算，一次局部导出就会把窗口外的课当成"消失"并撤销、还从台账剪掉——局部导出动了全局订阅状态 |
| CD15 | v1 配方代码去留 | **删**。迁移从留底读旧 UID，不需要重算 v1；v1 golden 文件保留作历史证据，新增 v2 golden，并把 v1 payload 形状写进 docstring | 代理 | 不留死分支。但两年后必须有人能解释旧产物里那批 UID 是怎么来的——所以形状进文档不进代码 |
| CD16 | 改名免疫的适用范围 | **只对拿到 `course_id` 的行成立**；缺失时 `stable_course_key` 退回 `name:课程名`（`models.py:263-265`），改名仍会产生新 UID | 代理（复核新增） | 真实响应的脱敏固件里 `KCH`（`parser.py:105` 的第一候选）出现 18 次 ⇒ 课程号是常态，但回退分支是刻意保留的健壮性。规格必须写成"缺失时改名仍产生新事件，且 `diff` 会先报出来"，不许含糊成"改名问题已解决" |
| CD17 | 降级回 v0.7.x 的后果 | 状态文件会被重写并**丢掉** `uid_recipe`（`SubscriptionState.from_dict` 忽略未知键，`to_dict` 用 `asdict`）⇒ 下次 v0.8 push 再走一次迁移分支 | 代理（复核新增） | 自愈、无副作用：留底此刻已是 v2 产物，"留底 UID − 本次 live" 为空集。写成显式用例钉住，免得将来当成 bug 查 |
| CD18 | `doctor` 与 skill | `doctor` 新增一项「UID 配方与迁移状态」（显示 `uid_recipe`、课程台账条数、留底是否仍为 v1）；skill 只加一段升级说明，**`MIN_DOC_VERSION` 保持 `(0, 7, 0)`** | 代理 | 判据只存一处的纪律不变；skill 的新增内容是解释而非判据，所以版本下限不动（下限必须是判据能跑起来的版本，见 0.7.0 那条教训） |
| CD19 | 版本号与搭车项 | **0.8.0**；`.webcal` 分发**不进本版** | 代理 | v0.8.0 已经动了一次所有订阅用户的状态，再叠一个"新增扩展名要验 tree 护栏计数口径"的风险，回滚会变复杂。`.webcal` 排到 0.8.x 独立一笔 |
| CD20 | v2 配方的 UID 碰撞 | 同课程号 + 同日 + 同节次而**课程名不同** ⇒ 视为不可信：**本次课程撤销通路整体不工作**（零撤销、不迁移、不写课程台账文本），产物与考试侧照常，只发一条不含课程名的 warning | 控制器（终审 A） | v2 去掉 `course_name` 放宽了碰撞面，而 `build_events` 的 UID 去重是静默丢弃、不进 `report.skipped` ⇒ 门 1 看不见"真实的一门课消失了"。取舍：不改产物字节、只收回撤销权，符合不变量 1「任何降级只会让撤销变少」；不选整场抛错（那是拒绝发布，代价过大） |

---

## 3. Non-goals（明确不做）

- **不做多学期合并 URL**（v0.4 起就是明确不做）。台账与状态文件都按学期 key 分文件，
  换学期 = 换 token = 换 URL，所以**不存在跨学期撤销**。
- 不改考试 UID 配方（`exams.py:288`，走 `WID` 降级链），不改考试侧任何行为。
- 不引入 `METHOD:CANCEL` 独立产物、不引入第二个订阅入口（D1 口径不变）。
- 不做"自动判断学校改了课表结构"——门 1 只判数据完整性，接口考古是另一个 skill 的事。
- 不抽 `SourceAdapter`、不为多校可移植性改任何结构（结论见
  `docs/design/2026-10-11-multi-school-portability.md`）。
- 不做定时发布、不常驻进程（订阅通路的"纯手动"口径不变）。

---

## 4. 现状机制事实（全部来自读代码，非推测）

**考试侧那套撤销机制（可复用的部分）**

- 三态判定：`ExamState`（`exams.py:100-105`）由 `classify_exam_payload`（`exams.py:132-148`）
  给出，唯一消费点在 `cli.py:497-522`。**它不是撤销的门槛**——门槛是 `exporter.py:726`
  那个 `exams_trusted` 布尔（`exporter.py:764` 算，`:790` 任何异常重置，`:884` 消费）。
- 台账条目：`LedgerEntry(uid, start, end, summary, location, description)`，起止必须 tz-aware
  （`exams.py:515-528`）。写：`render_exam_ledger`（`exams.py:531-579`），刻意不含
  STATUS/SEQUENCE/LAST-MODIFIED（D10）。读：`load_exam_ledger`（`exams.py:590-660`），
  **永不抛错**（D15），丢弃 naive/重复/无 UID 条目，日志只记 `type(exc).__name__`。
- 集合运算：`cancel_candidates(*, ledger, live_uids, baseline_uids, now)`
  （`exams.py:663-696`）→ `(deliverable, unresolvable)`，全序 `(start, uid)`。三道筛：
  live 里有 ⇒ 跳过；`start < now` ⇒ 条目从台账退场（D2/D9）；不在基线 ⇒ `unresolvable`
  且**留在台账里排队**（D20/F3）。
- 撤销事件：`build_cancellation_events`（`exams.py:713-733`）UID 逐字复制、
  `summary=CANCELLATION_SUMMARY`（`exams.py:710`「已取消的活动」）、location/description 置空、
  `status=CANCELLED`（`exams.py:700`）。序号来自 `resolve_sequence`（`sequence.py:158-193`）
  在 `render_ics`（`exporter.py:480`）里给的既有指纹规则，**没有新增任何序号机制**（D8）。
- 基线 UID 集：`_cancellation_baseline_uids`（`exporter.py:585-601`），基线为 None 时返回
  **空集**（D20/F2）。

**课程侧现状**

- `make_uid(semester_key, meeting, day)`（`exporter.py:75-122`）：payload 五项，
  `course_name` 在 `:116`，legacy 契约注释在 `:96-100`。
- 课程事件是**按天展开**的：一门课 16 周 = 16 个 UID，一学期总量数百 ⇒ "消失"的粒度是整学期。
- 裁剪在 `exporter.py:792-806`，而考试侧的 `pre_filter_exam_events` / `live_exam_uids`
  在过滤**之前**捕获（`exporter.py:796-797`）⇒ 裁剪对考试撤销零影响（D19 的实现形状）。
  **课程侧现在没有任何"过滤前快照"**。
- 撤销块在 `exporter.py:880-976`（`pre_cancellation_render_events` 快照 + 异常回滚），
  与 live 撞 UID 的撤销条目会被丢弃（`exporter.py:934-946`），台账文本在 `:959-964`。
- `subscribe.py` **完全没有台账概念**；留底 `last-<学期>.ics` 与状态文件都在 CLI 层
  （`cli.py:1338`、`1396`、`1480`）。
- push 时序（`cli._subscribe_push`，`cli.py:1338-1370`）：校验分支 → 快照陈旧警告 →
  有 `last_push` 则必须有留底否则 fail-closed → `build_ics_for_semester(baseline_probe=…)` →
  `publish` → 写留底 → 写考试台账 → 汇总。**回写一律在发布成功之后**（D11）。
- tree 护栏：`publish` 内 `subscribe.py:256-266`，`git ls-tree -r --name-only HEAD`
  要求恰好 1 个条目、路径不含 `/`、以 `.ics` 结尾，否则 fail-closed 拒绝。
- 状态文件：`SubscriptionState`（`subscribe.py:79-111`），`from_dict` 逐键取名、**忽略未知键**，
  `to_dict` 用 `asdict` ⇒ 新字段对旧版本是"可读但会被它抹掉"（CD17 的依据）。
- 目录：`Settings.exam_ledger_path`（`config.py:170-176`）；`ensure_dirs`（`config.py:118-127`）
  **不创建** `subscribe/` ⇒ 台账写盘前必须自己 `mkdir`（D24 ① 的教训，课程侧同样适用）。

---

## 5. 总体架构

```text
raw 快照 ──▶ 解析课程 ──▶ v2 UID 展开 live 事件（过滤前快照，CD14）
                              │
             ┌────────────────┴─────────────────┐
             │                                  │
      门 1 courses_trusted?                门 2 熔断（仅常态分支）
      否：不读台账、不撤销、              超阈值且无旗标：
          ledger_text=None                  拒绝发布，退出码 6
             │ 是
   状态文件有 uid_recipe?
     ├─ 否（升级后第一次）──▶ 迁移分支：候选 = 留底 UID − live − 本次常态撤销
     │                        （留底 = 上份发布产物**整体**，课程 ∪ 考试）
     │                        再过 D2 过期筛（CD10）；豁免门 2；打印预告
     └─ 是 ────────────────▶ 常态分支：候选 = 课程台账 UID − live UID
                              → cancel_candidates 三道筛
                              ↓
产物 = live 课程 + live 考试 + 撤销条目（考试 ∪ 课程）
                              ↓
publish 成功 → 写 last-<学期>.ics + last-courses-<学期>.ics
             + last-exams-<学期>.ics + 状态文件 uid_recipe=2
```

三条不变量：

1. **任何降级路径都只会让撤销变少，不会让撤销变多**（门 1 不满足 ⇒ 整块跳过；
   台账损坏 ⇒ 当作空；基线缺失 ⇒ 候选判 unresolvable 而不是硬发 `SEQUENCE:0`）。
2. **撤销条目永远不进台账**（台账只存 live 形态，D10），所以不会有"撤销的撤销"。
3. **回写一律在发布成功之后**（D11），迁移标记也一样——publish 失败时 `uid_recipe`
   保持缺失，下次 push 仍走迁移分支。

迁移候选的覆盖面要如实写明：留底是**上份发布产物的整体**，所以清扫范围覆盖其中每一条
留底 UID（课程 ∪ 考试），**包括考试台账没接住的过期考试 UID**——带 `--no-exams` 发布过、
或考试在 v0.6 通路之外残留的旧事件，迁移那一次也会被一并撤销。这是刻意的设计而非泄漏：
过期考试 UID 通常由考试台账负责，多撤一次的代价可证明无害——撤销条目标题固定
（「已取消的活动」）、起止逐字复制，同一 UID 的两条撤销内容指纹相同 ⇒ `SEQUENCE` 不漂移、
产物形状不变（D8 的幂等论证在这里同样成立），客户端收到的仍是同一条 CANCELLED。

---

## 6. 组件与接口

### 6.1 新模块 `src/xjtu_calendar/cancellation.py`（纯搬迁，CD9）

搬入并参数化，`exams.py` 用 `from .cancellation import ...` 再导出，**既有调用点与测试
的 import 路径一律不改**：

- `LedgerEntry`、`CANCELLATION_SUMMARY`、`STATUS_CANCELLED`（原 `EXAM_STATUS_CANCELLED` 保留别名）
- `render_ledger(live, pending, *, dtstamp)`、`load_ledger(text)`（原 `render_exam_ledger` /
  `load_exam_ledger`，逻辑不变，只是不再叫考试）
- `cancel_candidates(...)`、`build_cancellation_events(...)`

搬迁的判据是"课程与考试的这部分**逐字节同构**"；一旦课程侧需要不同行为，就留在
`courses.py` 里，不许往公共模块里塞分支。

### 6.2 新模块 `src/xjtu_calendar/courses.py`（课程侧独有）

- `CoursesTrust(trusted: bool, reason: str | None)` /
  `assess_courses(*, total_candidates: int, skipped: Sequence[str], meetings: Sequence[CourseMeeting]) -> CoursesTrust`：
  门 1（CD13）。输入是解析报告的行数与跳过项、加上解析出的课程安排——不接收报告对象本身，
  保持纯函数。`reason` 只允许是"无快照 / 解析有跳过行 / 零门课"这类**类别词**，
  进日志时不得携带课程名。
- `mass_cancellation_check(*, candidates: int, ledger_total: int) -> MassCancellation | None`：
  门 2（CD12），常量 `MASS_CANCELLATION_MIN_EVENTS = 10`、`MASS_CANCELLATION_RATIO = 0.30`。
- `upcoming_ledger_count(ledger: Mapping[str, LedgerEntry], now: datetime, *, exclude_uids: Collection[str] = ()) -> int`：
  门 2 的分母（CD12）——台账里**原定时刻还没过去**、且**上次还没被撤过**的条目数。关键字默认参
  `exclude_uids`（修复回写 `bb2a721`）传「基线里已经是 `STATUS:CANCELLED` 的那批 UID」，把墓碑从分母一并
  抹掉；**分子分母必须同时排除**（只排分子会在学期末重新打开分母稀释的洞，见 CD12）。默认空集保持既有
  调用形状、不破坏兼容。exporter 侧对应的分子排除是 `candidates=len([c for c in deliverable if c.uid not in already_cancelled])`。
- `migration_candidates(published: Mapping[str, LedgerEntry], live_uids: Collection[str], normal_cancel_uids: Collection[str], now: datetime) -> list[LedgerEntry]`：
  CD10 的集合运算（留底解析出的 `{UID: LedgerEntry}` − 本次 live − 本次常态撤销，再过期筛），
  输出按 `(start, uid)` 全序。
- 台账格式与考试侧共用 `cancellation.render_ledger` / `load_ledger`。

### 6.3 `config.Settings`

新增 `course_ledger_path`（`<subscribe_dir>/last-courses-<学期>.ics`），与
`exam_ledger_path` 同一段、同等待遇（`private=True` 原子写、永不发布、日志不打印内容）。

### 6.4 `exporter.make_uid`

payload 去掉 `course_name`（CD2），docstring 改写：删掉"刻意兼容"那段，换成
"v2 配方 + v1 形状存档 + 改名免疫的适用范围（CD16 的回退分支）"。
新增模块常量 `UID_RECIPE_VERSION = 2`（`doctor` 与状态文件都读它，别写第二处字面量）。

### 6.5 `exporter.build_ics_for_semester`

- 捕获**过滤前**的课程 live 集（照 `exporter.py:796-797` 的考试形状，CD14）。
- 撤销块扩成两支：课程 + 考试，各自算候选、各自出 `unresolvable`，合并后进 `render_ics`。
- 迁移分支：`uid_recipe` 缺失且留底存在时算一次全量候选，结果通过
  `ExportResult.migration_summary: MigrationSummary | None`（含候选数与占比）交给 CLI 打印预告
  （CD11）。构建层**只算不写**状态文件——标记的回写只发生在发布成功之后（CD7）。
- `ExportResult` 新增 `course_ledger_text`；`stats` 新增 `course_cancellations` 计数，
  **不计进** `sequence_stats` 的既有口径（D23 的教训：污染的是 `updated`）。

### 6.6 CLI

- `subscribe push` / `subscribe rotate`：迁移分支触发前打印一行
  「检测到上次发布使用 v1 UID 配方，本次将一次性撤销 N 条旧事件并原地重建——这是升级的预期行为」。
- 门 2 触发 ⇒ 抛 `MassCancellationBlocked` ⇒ 退出码 **6**。用户可见文案为「本次将撤销 N 条课程事件，
  占尚未过去的已发布事件的 P%——疑似课表没抓全」：这里的**分母是未过期（原定时刻 ≥ now）课程台账
  条目数**（判据 `候选数 ÷ 未过期台账条目数`，见 CD12 与 `courses.upcoming_ledger_count`），文案取
  「尚未过去的已发布事件」这个面向用户的通俗口径——台账只记本机上次成功发布过、且时刻还没过去的
  live 课程事件，与分子 `deliverable` 同域。现有用例只钉退出码 6 与不写盘，**不逐字断言**这句文案，
  措辞以本节为准。**不写盘的范围**：`export` 与 `subscribe push` 路径上熔断触发时，产物、留底、
  课程台账、考试台账、状态文件**字节全部不变**（回写一律排在渲染成功之后）。`subscribe rotate`
  是既有取舍的例外——换 token（`subscribe.rotate_token`）排在真实渲染**之前**（v0.6 D24
  「先警告再换 token」探针设计），所以 rotate 上熔断触发时新 token 与状态文件**已经换掉**，
  只有产物与两份台账不写。v0.8 不改这个行为，只把口径写准；能否让熔断早于换 token 判定见 §11。
- 新旗标 `--allow-mass-cancellation`：`export` / `subscribe push` / `subscribe rotate` 三处接受，
  放行时日志里给出候选数与占比。
- `doctor` 新增检查项「UID 配方与迁移状态」：`uid_recipe` 有无、课程台账条数、
  留底是否仍属迁移前。警告级，不阻塞（退出码规则不变）。

### 6.7 `sequence.py`：基线携带 STATUS 供门 2 排除（修复回写 `bb2a721`）

`EventBaseline` 新增末尾字段 `status: str | None = None`（frozen dataclass，默认 `None`）。
`parse_baseline` 经新增私有 helper `_read_status(component: Any) -> str | None` 从留底读出：归一化为
大写去空白，缺失/空串 ⇒ `None`（普通 live 事件即是 `None`，`STATUS:CANCELLED` ⇒ `"CANCELLED"`，与
`STATUS_CANCELLED` 同形）。**`status` 不参与 `fingerprint`**——塞进指纹会改动既有 SEQUENCE/幂等判定与
golden 用例。它是门 2 判定「这条撤销上次是否已发布过」的权威来源：课程台账按 D10 不带 STATUS，发布凭据
只在留底里。exporter 消费：`already_cancelled = {uid for uid, item in (baseline or {}).items() if item.status == STATUS_CANCELLED}`，
门 2 的分子与分母同时按它排除（见 CD12、§6.2），**墓碑照旧继续携带、产物形态与文案不变**。

> 幂等说明（实测校正）：固定标题让墓碑内容逐次恒定，重复携带本身**不** bump SEQUENCE——SEQUENCE 只在
> 迁移那一次从 0 抬到 1，此后每次发布内容未变 ⇒ `resolve_sequence` 走「未变」分支、SEQUENCE 稳定在 1，
> 产物逐字节相同、`NO_CHANGE` 幂等成立。**「每次发布 SEQUENCE 都会递增、幂等在撤销存续期内不成立」是
> 错误说法**，代码注释已按实测改掉，文档一律不写。

---

## 7. 错误处理与降级

| 情形 | 行为 | 依据 |
|---|---|---|
| 快照缺失 / JSON 炸 / 非 UTF-8 / 有 skipped 行 / 0 门课 | 不撤销、不读台账、`course_ledger_text=None`，产物等于没有增量 | CD13（D18 同构） |
| 台账文件不存在 | 视作空台账 ⇒ 无候选（新用户/换设备），**不报错** | D15 |
| 台账文本损坏 | 逐条丢弃坏条目、保留可解析的，warning 只记异常类型 | D15/D22 |
| 台账里起止为 naive | 丢弃该条（`now` 是 aware，比较会 `TypeError`） | D22 |
| 候选 UID 不在 SEQUENCE 基线 | 判 `unresolvable`，**留在台账里排队**，不下发 | D20/F3 |
| 常态候选数触发熔断 | 拒绝发布、退出码 6、零写盘；提示旗标 | CD4/CD12 |
| `export` 触发熔断 | **不写产物、不写任何台账**、退出码 6（与订阅路径同一判据、同一心智） | CD4 |
| live 内 UID 碰撞（同课程号 + 同日 + 同节次而**课程名不同**，撞出同一 UID） | 本次课程撤销通路整体不工作：零撤销、不迁移、课程台账文本不产出，课表产物与考试侧照常；只发一条**只报数量**的 warning（不含课程名/教师/教室，§8） | CD20 |
| `export` 路径（未触发熔断） | 写课程台账（与考试台账同时机：产物落地后），但**不读也不写** `uid_recipe`、不发迁移撤销 | CD3 |
| 缺 `uid_recipe` 但**没有留底**（v0.7 时代 init 过、从未成功发布） | 迁移候选空集 ⇒ 直接写 `uid_recipe: 2`，无撤销 | CD7 |
| 升级后第一次 `push`（课程台账尚不存在） | 走迁移分支；常态分支因台账空集自然无候选 | CD8 |
| `publish` 失败 | **`push` 与 `export` 路径**：留底、两份台账、状态文件**全部不动** ⇒ 下次 push 重算。**`rotate` 例外**：换 token 排在真实渲染/publish **之前**（D24），失败时新 token 与状态文件**已换**，仅产物与两份台账不写 | D11/CD7 |
| 迁移分支 | 豁免熔断，但必须打印预告；清扫覆盖留底整体（课程 ∪ 考试，含过期考试 UID），重复撤销幂等无害（§5 末注） | CD11 |
| `NO_CHANGE`（内容未变） | 跳过推送 ⇒ 同样不回写 | 现状 |
| 带 `--no-exams` | 考试侧维持 D4/D21；**课程侧撤销照常**（不受该开关影响） | 本文 |
| 带 `--from-date/--to-date` | 对课程撤销与台账**零影响** | CD14 |
| 学期 key 换写法 | 旧 `last-courses-*.ics` 永远读不到 ⇒ README 给出手动删除指引 | 照考试侧 |
| 降级回 v0.7.x 再升回来 | 字段被抹掉 ⇒ 重走迁移分支 ⇒ 候选空集、无副作用 | CD17 |
| 留底不可读（`baseline is None`） | `already_cancelled` 取空集（与 `_cancellation_baseline_uids` 同口径），门 2 分子分母都无墓碑可排除——而此时候选全部判 `unresolvable`、`deliverable` 为空，熔断本就不可达，故**没打开新洞** | CD12 |

---

## 8. 安全与隐私

- 撤销条目字段 = UID + 起止 + STATUS + SEQUENCE + DTSTAMP + 固定标题，**不含课程名、
  教师、教室**（CD5）。用例逐条比对产物，防止"顺手带个 SUMMARY 原文"。
- 课程台账含全字段（课程名/教师/教室），与考试台账同等待遇：只落本地、`private=True`
  原子写、**永不发布**、日志与 warning 文案都不引用其内容。
- 熔断与迁移的日志只报**数量与占比**，不列课程名。
- UID 碰撞短路的 warning 只报**碰撞的 UID 数量**（「本次有 N 个课程 UID 对应了不同课程名」），
  不含课程名、教师、教室（CD20、§7）。
- 迁移那一次会把整学期旧事件的 UID + 起止时刻公开一次（撤销条目）。这些时刻本来就在
  同一份公开产物里出现过，UID 是 hash，**不新增可读信息**；规格里如实记为"公开一次占位"。
- `doctor` 新项的输出要过隐私 grep（无姓名/学号/课程名/考场/座位/token）。

---

## 9. 测试策略

**UID v2 与迁移**

1. v2 golden 与 v1 golden 并存：同一输入下事件数相同、`SUMMARY`/`DTSTART` 相同、UID 不同。
2. 改名免疫：同一 `course_id` 连续改名两次 ⇒ 三份产物的 UID 集合**完全相同**（CD2 的核心卖点）。
3. CD16 的反向用例：`course_id` 缺失时改名**仍产生新 UID**（把限制钉成事实，不许含糊）。
4. 迁移：状态文件缺 `uid_recipe` ⇒ 产物含留底全部课程 UID 的 `CANCELLED` 条目 + v2 live；
   发布成功后状态文件写入 `2`；再次 push **不重复发**。
5. 迁移 + publish 失败 ⇒ 字段仍缺失、留底与台账字节不变。
6. 迁移候选与常态候选不重复产出同一 UID（`subscribe push` 端到端）。
7. CD10：留底里 `DTSTART` 已过的 UID 不进迁移候选。
8. CD17：写出带 `uid_recipe` 的状态文件后，**按旧二进制会留下的样子改写它**（从 JSON 里
   移除 `uid_recipe` 键，等价于旧版 `from_dict` 忽略未知键再 `to_dict` 回写）⇒ 载入后字段
   缺失 ⇒ 下次 push 重走迁移分支且候选为空集（留底此刻已是 v2 产物）。

**两道门**

9. 门 1 五态：无快照 / JSON 炸 / 非 UTF-8 / 有 skipped 行 / 0 门课 ⇒ 一律不撤销、
   `course_ledger_text is None`、退出码不变。
10. 门 2：台账 9 门课 144 事件、本次只解析出 2 门 ⇒ 退出码 6，且留底/台账/状态文件
    **字节级不变**（用 sha 断言，不是"看起来没写"）。
11. 双条件的两个边界：候选 9 条（不触发）、候选 10 条且占比 0.31（触发）、
    候选 20 条但台账 500 条（不触发）。
12. `--allow-mass-cancellation` 放行后能发出去，日志含候选数与占比、不含课程名。

**复用与不回归**

13. 搬迁后考试侧全部既有用例**一字不改**地通过（CD9 的判据）。
14. `--from-date/--to-date` 前后课程撤销集相同（CD14）。
15. 撤销条目与 live 课程撞 UID ⇒ 丢弃撤销条目（沿用考试侧 `test_cancellation_colliding_with_course_uid_is_dropped` 的形状）。
16. 幂等：同一输入重渲染，课程撤销条目 SEQUENCE 不漂移（D8）。
17. `stats` 的 `updated` 污染口径（D23）在课程侧同样钉住。
18. 台账 round-trip、naive 丢弃、垃圾文本降级（照 `tests/test_exam_ledger.py` 那组）。
19. 隐私：产物与日志均不含课程名/教师/教室；`doctor` 新项输出过一遍黑名单。
20. 门 2 分母只数**未过期**条目（CD12 终审修订）：台账 50 条 = 40 已过期 + 10 未过期且 10 条全是候选时，
    旧分母 10/50 = 20% 会**漏响**、新分母 10/10 = 100% ⇒ 必须抛 `MassCancellationBlocked`
    （`test_mass_cancellation_fuse_counts_only_upcoming_ledger_entries`）；反向对照「未过期里只有 1 条候选」
    不触发（`test_gate2_narrower_denominator_does_not_flag_small_edits`）。等号/邻界另由
    `test_gate2_boundaries` 参数化钉死。
21. UID 碰撞短路（CD20）：同课程号 + 同日 + 同节次、异名 ⇒ `build_events` 的 `collision_sink` 记下该 UID
    且产物仍"保留首个"字节不变（`test_uid_collision_between_different_course_names_is_detected`）；通路级 ⇒
    零撤销（产物无 `STATUS:CANCELLED`）、`course_ledger_text is None`、`migration_summary is None`、课表产物照常、
    只发一条不含课程名的 warning（`test_uid_collision_disables_the_course_cancellation_path`）。

**熔断只数「本次新撤销」（修复回写 `bb2a721`）**

22. 通路级（必须走 `build_ics_for_semester`）：迁移产出墓碑后，把该次产物当基线、**同一份课表**再渲染一次
    ⇒ 不得抛 `MassCancellationBlocked`（旧口径 12/30 = 40% 会拦下，升级后每次普通 push 都被自己的保险丝
    误杀），且那批墓碑仍须**原样携带**、一条不少，重复携带不 bump SEQUENCE
    （`test_recarried_tombstones_do_not_refuse_later_pushes`）。分母排除的单元保证：`upcoming_ledger_count`
    带 `exclude_uids` 把墓碑抹掉、默认参数不破坏既有调用形状
    （`test_gate2_denominator_excludes_already_cancelled_uids`）。
23. 排除墓碑之后门 2 **仍是保险丝**：基线里大量 `CANCELLED` 墓碑 + 少量以 live 形态发布过的旧事件、
    本次课表一条没抓到 ⇒ 新撤销 = 那一小撮 live、分母 = 未过期且非墓碑 ⇒ 仍须抛 `MassCancellationBlocked`
    （`test_tombstone_exclusion_leaves_the_fuse_armed`）。配套字段用例：`EventBaseline.status` 能从留底区分
    live/撤销、大写归一、且不进指纹（`test_parse_baseline_reads_status`、`test_status_is_not_part_of_the_fingerprint`）。

**跑法纪律**（AGENTS.md 第 3 条）：红绿阶段只跑定向测试（`-k` 或单文件），
四条全量门禁在收尾跑一次。

---

## 10. 文档交付

- `README.md`：「URL 订阅 → 已知边界」把"课程改名会表现为新事件"改成"v0.8.0 起改名走
  原地更新（`course_id` 缺失时例外）；真退课会以 `STATUS:CANCELLED` 下发"；命令表加
  `--allow-mass-cancellation`；退出码表加 `6`；新增一节「升级到 v0.8（UID 配方变更）」，
  写明订阅端自动迁移、手工导入者需删除旧日历重导一次、以及忽略取消字段的客户端
  **通常会**看到一次整学期「已取消的活动」（整学期迁移撤销从未在真机演练过，Task 13 才做，真机表现尚未验证）。
- 站点 `index.html`：UID 那张卡的措辞（"课程名参与 UID"的已知边界）同步。
- `skills/xjtu-timetable-setup`：第 7 步加一段升级说明（**不新增判据** ⇒
  `MIN_DOC_VERSION` 保持 `(0, 7, 0)`，CD18）；`references/client-matrix.md` 若矩阵有实测
  新读数则一并更新同步 sha。
- `docs/design/2026-10-09-exam-cancellation.md`：D17 就地标注"已被 v0.8 推翻，见本文"，
  原文保留（不改写历史决策，只加指针）。
- `docs/design/2026-10-11-subscription-followups.md`：§2 标注落地形态。
- `CHANGELOG.md`：0.8.0 条目（UID 配方变更必须放在最显眼位置——它是唯一会让老用户
  第一眼困惑的东西）。

---

## 11. 发布、演练与回滚

**发版前必做：隔离真机演练（CD6）**

1. 用**当前 main（v1 配方）**向一次性 token 的隔离 URL 发布一次；手机侧
   Google 网页 + ColorOS 各订阅一条腿，确认 v1 事件到位（这一步同时补上矩阵欠的
   "改名会不会跟着改显示名"那格的机会性观测）。
2. 实现完成后 `subscribe push`：状态文件天然缺 `uid_recipe` ⇒ 走迁移分支。
3. 记录读数：Google 侧旧事件是否消失、新事件是否到位；ColorOS 侧整学期「已取消的活动」
   长什么样、多久退场、用户能否辨认。**演练前必须先看"要公开什么"**——隔离 URL 里
   是真实课表，口径同 12 月那次考试首发。
4. 产物用后即删（从分支移除、URL 404），结论回写本文 §11 与 README 矩阵，
   未验证的表现一律写"通常会"。

**演练读数（2026-10-12，整学期规模，合成身份字段 + 真实时间骨架）**

隔离 URL 走 v1→v2 迁移后，state B 产物 = live 96 + 撤销墓碑 75（=96 − 21 条已过原定时刻被过期筛放掉）。
ColorOS 已拉到 state B，机制与推导完全一致：

- v1 那条 UID 被**原地改写**成标题「已取消的活动」、`LOCATION` 变空（产物里墓碑不带该字段，
  ColorOS 整体替换字段，与 2026-10-10 既有实测吻合）；v2 的新 UID 作为**新日程**进来 ⇒ 同一时段
  **1:1 成对**，规模 = **96 条新日程 + 75 条占位**（占位数与产物里的 75 条墓碑严格一致）。
- **退订重加清不掉占位**：占位是产物自身携带的 `STATUS:CANCELLED` 事件，忽略 `STATUS` 的客户端重加时
  会**再导入一遍**（实测重加后「已取消的活动」仍在、仍 1:1）。⇒ 本节此前/README/skill 里
  「退订再重新添加是唯一确定有效的清理手段」那句**判错并订正**：占位只能等各自原定时刻过去后由下一次
  发布收掉；退订重加能清掉的只有本地重复项。
- 那 **21 条已过原定时刻**的旧条目不进 state B 的任何一半（既非 live 也非墓碑）。本次操作序列是
  **先删除该 URL 日历、再重新添加**，它们**仍在**手机上 ⇒ 对"从产物里彻底消失"的条目，退订重加同样
  清不掉。未记录删除时是否勾选「同时删除事件」，故**不对 ColorOS 的删除语义下断言**；但作为补救手段，
  「退订重加」在本次演练中已被证伪——占位靠原定时刻过去后由后续发布收掉，~~消失项只能手动删~~。
  **后半句已被 2026-10-15 正式订阅的真机读数订正：ColorOS 上手动删也不行**（见下一条补记）。
- **补记（2026-10-15，正式订阅真机，非演练）**：上面留白的那格「删除语义」现在有结论——迁移发布后
  露出的那条「已取消的活动」（2026-10-15 16:10–18:00，与照常要上的一门课 1:1 成对）**单条删不掉**，
  而移除订阅只有**「删除订阅」**一个动作、删完本地事件并不随之消失。⇒ ColorOS 本机**没有任何可用
  的清理入口**（手删、退订重加、删除订阅三条全部否证），上面那句「直到学期过去」就是唯一结局。
  剩下的杠杆只有"改写那条条目、把它搬到过去时段"，议题、判据与未验点见
  `docs/design/2026-10-11-subscription-followups.md` §12。**限定：这条结论只属于 ColorOS 原生订阅这条
  通路**——同日已决定 Android 首选 ICSx⁵，而它每次成功同步都会清理源里已不存在的 UID（上游源码、
  真机未验），见同文 §13。
- **Google 那条腿未在整学期规模复验**（演练 URL 已下线，截图时刻尚未拉到 state B）：沿用 v0.6 的小规模
  实测"会移除、不留残条"，文档继续写「通常会」并显式标注未在整学期规模复验。换算给真实用户的可见规模：
  整学期迁移后，忽略 `STATUS` 的客户端会看到 96 新 + 75 占位（1:1）+ 若干删不掉的旧条目，直到学期过去。

**版本号**：`0.8.0`（CD19）。`pyproject.toml` 单一来源，升版后按 README 那条提醒重装 editable。

**回滚不是免费的**：v2 事件一旦发布，退回 v0.7.x 会重新发一批 v1 UID ⇒ 订阅端**双份**。
唯一补救是退订重加。所以回滚方案 = "停在 0.8.0 并前修"，而不是"回退配方"。
这一条要写进发布说明，免得真出事时有人去动 `uid_recipe`。

**开放问题（实现期才能定，但不影响本文结论）**

- 门 2 的 0.30 阈值是拍的量级。演练与真实学期数据出来后，若"正常退课"就能撞线，
  则调高常量并回写 CD12——**改数值不改判据形状**。
- `unresolvable` 队列在课程侧的规模可能远大于考试侧（一次裁剪失误就积压数百条）。
  是否需要"队列超过 N 条时提示用户查台账"，留到实现期看真机数据再定。
- 留底本身要不要加"配方版本"标记（现在靠状态文件推断）。若将来支持多 URL，
  状态文件与留底的对应关系会变成问题——本版明确不管。
- `rotate` 上的熔断能否**早于换 token** 判定？现在换 token 排在真实渲染之前（D24 探针设计），
  熔断在真实渲染里抛出时新 token 与状态文件已换（§6）。要做到"熔断先于换 token"，得让那条
  只读探针与真实渲染**同判据**（同一道门 2、同一未过期分母）并据探针结果决定是否换 token——
  这既动 v0.6 的既有取舍又有探针/真实两次渲染的一致性风险，留待后续决定，v0.8 不改行为。
- `doctor` 的渲染**不传基线**（沿用只读探针路径），所以迁移跑完之后的用户跑 `doctor` 会看到**一整屏**
  「课程 … 无法安全下发撤销…留在课程台账里」的 D20 逐条警告（彩排实测 75 行；每行只打印 UID 这一 hash，
  不含课程名）。**退出码不受影响、语义无害**——`doctor` 只是把「留底读不到发布凭据 ⇒ 判 unresolvable」这件事
  逐条念出来。要不要把它聚合成一行（「本次有 N 条撤销因无发布凭据不下发」），留给后续决定。**这是既有的
  构造性行为，不是 v0.8 引入的缺陷**，措辞不许写成 bug。
