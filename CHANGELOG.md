# 更新日志（CHANGELOG）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 格式，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。
**版本号单一来源为 `pyproject.toml`**，本文件只记录变更本身。

---

## [未发布]

### Fixed

- **`login` 按完 Enter 会先等教务侧（`/jwapp`）的 cookie，没等到不关窗口。**
  2026-10-14 两次真人登录都存下了半截会话（46 / 33 条 cookie，教务侧 0 条），
  症状是三个 `.do` 接口一律 403。第二次的现场重放证明流程本身是通的：点「学生」后
  课表还在跳转/渲染就按了 Enter，`context.close()` 一执行 cookie 就没机会发出来。
  现在 Enter 之后最多轮询 20 秒，仍没有则提示补点「学生」并再接着等 6 秒；
  超时**照旧保存**（门户/CAS 部分是好的）并打警告，收尾那句改成
  「N 个 cookie，其中教务侧 M 个」，让登录这一步自己就能看见成败。
  追问那一轮按 Ctrl-C ⇒ 会话文件保持原样，不会被写成半截。
- **测试默认隔离 `XJTU_CALENDAR_HOME`。** 本次加用例时实测踩到：一条只用了 `tmp_path`
  却忘了 setenv 的测试把 `login` 的落盘目标指到了真实用户目录，覆盖了那份可用会话。
  现在 conftest 的 autouse 用例把每条测试关进 tmp，需要真实 home 的得显式打标记。

---

## [0.8.3] - 2026-10-14

**补丁版：离线全新安装走查抓出的三处「绿灯撒谎」。** 走查环境 = 一次性 venv（Python 3.12.13）
+ 刚从 PyPI 装的 0.8.2 + 隔离 `XJTU_CALENDAR_HOME` + 本地裸仓库当发布目标，照 `SKILL.md`
八步逐字照抄。产物字节不变（同一份隔离 home 分别用 `v0.8.2` 工作树与本版跑 `export`，
两份 **sha256 逐字节相同**：44285 B / `43002fb66cc6…`；`src/` 自 0.8.2 起只动了 `doctor.py`
的一条判定与三处 `raise` 的提示参数）。

### Added

- **`doctor` 的「学期校历」从"文件在不在"升级成能看见数字**：解析该文件并把
  「已配置（<标识>）：第 1 教学周周一 …、共 … 周」打进 detail。原来它只判文件存在，
  而把 README 的**格式示例**（`2026-09-07` / 16 周）逐字抄进 `2026-2027-1.json` 也能通过，
  `export` 成功、产物看起来完全正常，真实官方值是 `2026-09-14` / 18 周
  ⇒ **整学期错一周、全程零提示**。现在数字出现在判定发生的那一刻，Agent 才有机会念给用户核对。
  文件读不动或缺 `semester` 对象时折成**警告**并继续跑完其余项（不再对坏文件亮绿灯）。
- **新增「校历内部 `key` ≠ 学期标识（文件名）」警告**：`semester.key` 会进入**每一条事件的
  UID**。走查实测：文件名与日期都不动、只改 `key` 再导出，**103 个 UID 零交集**，随后
  `subscribe push` 报出成批「无法安全下发撤销」——它把这批"旧 UID 凭空消失"当成退课了。
  README 的模板恰好写着示例 key，用户"顺手改成和文件名一致"就会踩中。
  **UID 的学期来源本次不动**（已拍板：只堵提示、不改身份算法）。

### Changed

- **缺依赖的提示点名 extras**（`login` 缺 playwright、`fetch` 缺 httpx、浏览器路径缺 playwright
  三处）：裸 `pip install xjtu-timetable-calendar` 只带来 `icalendar`，走查里新用户撞上的
  **第一个**硬阻塞就是「登录功能需要 playwright」，而 `--version` 那一步是绿的。
  提示现在给出 `pip install "xjtu-timetable-calendar[browser]"` 这条一次装全的写法。

### Documentation

- **推荐安装命令带上 extras**：README 方式一 / 方式二、站点第 1 步卡片、`SKILL.md` 第 1 步；
  原来 README 把它写成「可选依赖」放在安装之后——对"要跑通八步"的人这不叫可选。
  `SKILL.md` 第 1 步的证据加第二条（`python -c "import httpx, playwright"` 两个都要过），
  并写明 **`--version` 通过不等于能走完本向导**。
- **`SKILL.md` 三处「照抄就跑不通 / 照抄会误判」订正**：第 7 步补 `subscribe init` 的
  `--semester` 必填（缺它停在「[错误] 未指定学期」，退出码 1；上一轮走查补了 `--repo` 漏了这个）、
  第 6 步补「`diff` 首跑必报『旧课表快照不存在』，属正常状态」、第 3 步证据改成
  「doctor 通过 + 把打印出的日期回读给用户确认」。
- **站点第 1 步卡片补上「窗口里还会落在『请选择身份』，点『学生』等到课表显示再按 Enter」**
  ——今天那笔 403 的源头，此前只写在 README 与 skill 里。
- `references/troubleshooting.md` 新增两节：缺 extras、改校历 `key` 导致整学期重复。
- **「已知边界」订正**：从"没经过全新安装验证"改成"**只做过一轮离线走查**"，
  并列明真人从零跑第 2 步登录仍未覆盖（需要真凭据，离线环境造不出来）。

---

## [0.8.2] - 2026-10-14

**补丁版，两件事：把 pip 用户会照抄的四处提示换到真实存在的位置，和把「登录只走了一半」变成可判定的状态。**
产物字节、UID 配方、撤销与迁移判据一律不变 —— `src/` 自 0.8.1 起动的全是提示文案、只读会话摘要与
`doctor` 的一条判定，导出通路（解析 → 展开 → 渲染）零改动（`git diff v0.8.1..HEAD -- src/` 只有
`auth.py` / `doctor.py` / `fetcher.py` 三处新增判定与 `errors.py` / `exporter.py` 的 hint 字符串，
`exporter.py` 那 4 行全在 `raise` 的提示参数里）。**实测对照**：同一份隔离 home（真实
`semesters` / `schedules` / `raw` 三件套）分别用 `v0.8.1` 工作树与 main 跑 `export`，
两份产物 **sha256 逐字节相同**（44285 B，`b48686a85e10…`，UID 集合与 `SEQUENCE` 行一致）；
金标留底用例（`tests/fixtures/legacy_*`，0.8.0 时代提交、本版未改）同样通过。

### Added

- **「登录停在『请选择身份』」从此有判据**（`doctor` 的「会话」项 + `SessionInfo`）：新增
  `jwapp_cookie_count` / `has_app_session`，数的是会话文件里 `path` 落在 `/jwapp` 的 cookie 条数，
  `describe()` 在大于 0 时带上「教务侧 cookie N 个」（`status` 与 `doctor` 一起变可读）。
  导出存在而该计数为 0 ⇒ `doctor` 报**警告**（不是阻塞，理由见文件头原则 2），文案直接点名
  「这通常不是账号没权限」+ 处置。判据来自 2026-10-14 真人踩坑：`login` 打开的就是
  `select_role.html`，在那个页面上按 Enter 会得到一份**能过 CAS、取不到课表**的半截会话，
  三个教务接口一律回 403（金智平台的 `scenes_abnormalPage` HTML，不是登录页，所以 401 分支抓不到），
  而旧的两条判据（会话文件 mtime 变了、`fetch` 不再 401）**对它都是绿的**。
  只有 profile、没有导出文件时不据此判定。
- `login` 窗口说明的第 2 步补上「若页面让你『选择身份』，先点『学生』」，并预告停在原地的后果是
  403 而不是 401 —— 源头少一次踩坑比事后排障便宜。

### Changed

- **`fetch` 的 403 提示不再把人往死路上推**：原文「当前账号没有访问该接口的权限。本工具只导出本人
  有权限的课表。」改成点名"会话没走完登录"+ 判据指向 `doctor` + 出口 `login --force`，
  同时保留"确实无权限时只导出本人课表"的口径。当天的消融实验顺带钉住两条**别再猜**的结论并写进
  测试文档串：去掉 `_WEU` 立刻 403、而两份同名 `JSESSIONID`（门户 `/` 与教务 `/jwapp`）少任意一份
  都仍然 200 ⇒ 承重的是 `_WEU`，不是"压平字典留下了哪一份"，所以 `load_cookies` 的按域过滤与
  同名取舍**都不动**；换浏览器 `User-Agent`、补 `Referer`/`Origin` 均不改变 403 ⇒ 不是站点在拦客户端。
- **四处会打印给用户的提示不再指向 `examples/`**（`doctor` 的「作息表」阻塞项、
  `SemesterNotConfigured.hint`、`exporter` 缺教学日历 / 缺作息表两条 hint）：仓库根的 `examples/`
  **不在 wheel 里**（`package-data` 只有 `data/*.json`，解包 0.8.1 核过），pip 用户照提示去
  `ls examples/` 只会一无所获。改指向 README「必须由你配置的内容 → 1. 教学日历（校历）/ 2. 作息表」
  ——那里内联着可直接抄的完整模板 JSON，所以这是换到一个对任何人都成立的位置，不是把话术改软。
  仓库 `examples/` 仍是权威拷贝（`tests/subscribe_support.py` 继续从那里读，避免 schema 漂移）。

### Documentation

- **skill 走查（对着 0.8.1 真 CLI 逐字跑）修掉四处「照抄就跑不通」**：`SKILL.md` 第 1 步版本下限
  0.7.0 → 0.8.0（并解释两层的区别）、第 4 步补 `schedule --semester <标识> --apply`（原写法只预览不落盘）、
  第 7 步补 `subscribe init` 的必填 `--repo`（原命令直接 argparse 报错）、第 4 步不再让用户去填
  pip 安装里不存在的 `examples/`；同时删掉一条**虚构**的 `subscribe init --no-exams` 旗标
  （`--no-exams` 只在 `fetch` / `export` / `subscribe push` / `subscribe rotate` 上，
  且它既不发布考试、也不发布撤销）。
- **`?browser=no` 从灰页判据里摘掉**：它是 eHall 服务端在 CAS 回跳时给落地页拼的固定参数，
  2026-10-08 一次**成功登录**的抓包里同样在，其后接口全部正常 ⇒ 带它不代表被拒绝。
  灰页本身的成因**仍未证实**（最大嫌疑是后台执行器弄坏子进程 stdio）。README / `SKILL.md` /
  `troubleshooting.md` 三处同改。
- **README 新增「`fetch` 报 403 而不是 401」一条**，并给原有的「同名 `JSESSIONID` 警告不用管」
  补上**为什么**不用管（当天的消融证据）；「安全与网络行为」里那句「401/403 会提示登录失效或无权限」
  已不成立，改成实际口径。
- **README 日历名那条「退订再重新添加一定有效」降级为「通常会」**：它与本仓库 10-12 之后的统一口径
  （`client-matrix.md` 写「通常会」，且 ColorOS 实测「删除后重加仍保留旧事件」）冲突，
  按红线「真机未验证的表现不许写成事实」订正。
- **站点 `index.html`**：① 窄屏两张表改「一行一张卡片」（矩阵已 6 列，390px 视口下横向最小宽度实测
  566px > 可用 350px，被压成一字一行）；② 跟随系统明暗切换，纯 CSS 零 JS 零存储不闪白
  （`:root` 补 8 个语义变量、拆出 `--brand-solid`、`color-scheme` + 第二条带 `media` 的 `theme-color`），
  浅色回归 = 65 选择器 × 13 项颜色属性与改前基线零差异，深色 = 39 个文字/背景配对全过 WCAG AA。
  负收益记录：`overflow-wrap:anywhere` 能把命令表压矮 43%，但会把 `subscribe` 折成 `subscri/be`，
  命令表不能这么改。

---

## [0.8.1] - 2026-10-12

**补丁版，两件事：`subscribe` 多打一行 iOS/macOS 的 `webcal://` 链接、迁移预告文案改成实测口径。**
产物字节、UID 配方、撤销与迁移判据一律不变 —— `src/` 自 0.8.0 起只动了 `cli.py` 的打印与
`subscribe.py` 上一个只读属性，发布通路的行为面零变化。

### Added

- **`subscribe` 的四处输出各补一行 iOS/macOS 的 `webcal://` 订阅链接**：`init`、`push`
  （含「无变化，跳过推送」那一支）、`rotate`（打的是**新** token 的那条）、`status`，
  文案为「iOS/macOS：通常可直接打开 <链接> 完成订阅（真机未验证，失败就走 README 的手动路径）」。
  链接来自新增的只读属性 `SubscriptionState.webcal_url`——只把同一行里那条 `https://` 订阅 URL
  换个 scheme，指向**同一份** `<token>.ics`；`url_base` 非 https（明文 `http`、彩排用的 `file://`）
  时属性为 `None`、整行不打印，宁可不提示也不给一条看起来能点其实无效的链接。
  **发布物形态不变**：`webcal://` 是 URL scheme 而不是文件，所以不在 `.ics` 旁边多写一个
  `<token>.webcal`——那会撞 `subscribe` 的 tree 护栏（发布分支根目录恰好一个 `*.ics`）与 rotate
  只清 `*.ics` 的清理逻辑，让下一次发布被自家护栏判成「分支被污染」。
  「点开就弹订阅确认」在本项目**没有做过真机验证**（AGENTS 红线 3），措辞停在「通常」；
  rotate 后旧链接随之作废、必须用新那条重订，iOS/macOS 的手动路径与完整口径见 README
  「iOS / macOS：`webcal://` 订阅链接」。

### Changed

- **迁移预告的 CLI 文案同步到实测口径**（`subscribe push` 首次迁移时打印的那段）：
  原来写「整学期规模的残条在真机上尚未演练，退订再重新添加是唯一确定有效的清理手段」，
  2026-10-12 整学期演练实测证明后半句是错的——占位由产物自身携带，重加会再导入一遍。
  现在改为「占位与新课程 1:1 成对，且占位由产物自身携带，退订再重新添加也清不掉，
  只能等各自原定时刻过去后由后续发布收掉」。这是用户在做迁移那一刻唯一会读到的说明，
  口径必须与文档一致。

### Documentation

- **v1→v2 迁移的用户口径按 2026-10-12 整学期真机演练订正**（README、站点 `index.html`、
  `skills/xjtu-timetable-setup` 第 7 步与 `references/client-matrix.md`、`references/troubleshooting.md`、
  本文档 §11）：此前多处写着「忽略 `STATUS:CANCELLED` 的客户端留下整学期「已取消的活动」残条，
  **退订再重新添加是唯一确定有效的清理手段**」——这条**判错并改掉**。演练实测（合成身份字段 + 真实时间骨架、
  整学期规模 96 live + 75 墓碑）：ColorOS 上迁移表现为 v1 条目被原地改写成「已取消的活动」+ v2 另起新日程、
  同时段 1:1 成对；这批占位是产物自身携带的 `STATUS:CANCELLED` 事件，忽略 `STATUS` 的客户端**重加时会再导入
  一遍**（实测重加后仍在、仍 1:1）。新口径：占位只能等各自原定时刻过去后由下一次发布收掉，**退订重加清不掉
  占位**、只能清本地重复项。那 21 条已过原定时刻的旧条目：本次按**先删除日历、再重新添加**的顺序操作，
  它们**仍在**手机上 ⇒ 对"从产物里彻底消失"的条目退订重加同样无效（未记录删除时是否勾选「同时删除事件」，
  故不对 ColorOS 删除语义下断言，但作为补救手段该做法已被证伪）；Google 那条腿**未在整学期规模复验**
  （演练 URL 已下线），继续写「通常会」。这一项是 0.8.0 发布**之后**的文档订正，随本版（0.8.1）上线；
  站点与 README 的隐私口径两条（原记在本小节、实际随 0.8.0 的树一起发布）已归位到下方 `[0.8.0]` 小节。

---

## [0.8.0] - 2026-10-12

**这一版改了课程 UID 的配方（v1 → v2）——这是每个老用户都会遇到的一次性身份切换，先看这条。**

- 课程 UID 不再把**课程名**纳入身份：同一 `course_id` 的课改名后走**原地更新**，不再是
  「旧事件永存 + 新事件出现」。例外：`course_id` 缺失、退回用课程名做标识的行，改名仍会产生新事件，
  `diff` 会在发布前先报出来。
- 为跨过这次配方变更，**首次 `subscribe push` 会自动做一次迁移**：把整学期旧 UID 以
  `STATUS:CANCELLED` 一次性下发、并用 v2 重建事件；发布前打印一行预告，迁移标记只在发布成功后回写。
  `export` 不参与迁移——手工 `export` 导入者请删旧日历重导一次。
- **回滚不是免费的**：退回 0.7.x 会重新发一批 v1 UID、订阅端出现双份，唯一补救是退订重加；
  真出问题应停在 0.8.0 往前修，不要手改状态文件里的 `uid_recipe`。

### Added

- 课程侧撤销通路：真退课、教学班 `course_id` 变化会以 `STATUS:CANCELLED` 自动下发，
  与考试侧共用撤销机制（新模块 `cancellation.py`）；课程台账落在本地私有的
  `subscribe/last-courses-<学期>.ics`，只在发布成功后回写、永不发布。
- 两道门保护课程撤销：门 1 数据可信（有记录、零跳过行、至少一门课），门 2 熔断——
  候选 ≥ 10 条**且** ≥ **未过期**（原定时刻还没过去的）课程台账条目 30% 时**拒绝发布**。
- 新旗标 `--allow-mass-cancellation`（`export` / `subscribe push` / `subscribe rotate` 三处接受），
  确认无误后放行熔断。
- 新退出码 `6`：疑似数据不完整的整批课程撤销被拒绝发布。
- `doctor` 新增「UID 配方与迁移状态」检查项（`uid_recipe`、课程台账条数、留底是否仍为 v1），
  警告级、不阻塞。

### Changed

- `exporter.make_uid` 采用配方 v2（去掉 `course_name`），v1 形状仅存于文档；
  新增模块常量 `UID_RECIPE_VERSION = 2`。
- 订阅状态文件 `subscribe/subscribe-<学期>.json` 新增 `uid_recipe` 字段，作为迁移判定的唯一依据。
- `--from-date` / `--to-date` 日期裁剪对课程撤销与台账零影响（live 集在过滤前捕获）。
- 门 2 比例熔断的分母改为**未过期**课程台账条目（堵住学期中段比例被过期条目稀释而漏响的洞）；
  熔断提示文案随之改为「占尚未过去的已发布事件的」。
- v2 配方下「同课程号 + 同日 + 同节次、课程名不同」的 UID 碰撞不再静默：本次放弃课程撤销
  并给出只报数量的提示，课表产物与既有「保留首个」的字节行为不变。

### Fixed

- 修复「升级到 v0.8 完成 UID 迁移后，下一次 `subscribe push` 被课程侧比例熔断误拦（退出码 `6`）」：
  根因是门 2 把已经在产物里携带过的撤销条目（迁移/退课留下的墓碑）重新算成**本次新撤销**——课程台账按
  D10 不带 STATUS，墓碑与活课在台账里同形、`cancel_candidates` 会把它们再次选成候选，于是升级后的每一次
  普通发布都被自己的保险丝误杀（真数据彩排实测 75 条墓碑 ÷ 未过期分母 150 = 50%）。判据改为**分子分母
  同时排除**基线（留底）里 `STATUS:CANCELLED` 的 UID（`EventBaseline` 新增 `status` 字段、
  `upcoming_ledger_count` 新增 `exclude_uids`），撤销墓碑照旧持续携带、产物形态与文案不变；迁移跑完后
  同一份课表再次发布走 `无变化，跳过推送`、退出码 `0`。

### Documentation

以下两条在发版时记在 `[未发布]` 小节里，实际内容已随本版的树一起发布（`8b6e51c` 与 `72421cf`
都在 `v0.8.0` 的祖先链上），本版把它们归位到 `[0.8.0]`。

- 站点落地页 `index.html` 补齐到 v0.7.0 的实际能力（原内容停在 v0.4 之前）：新增「考试与撤销」
  与「订阅 · 客户端」两节（考试并入同一份 .ics、三态判定、`STATUS:CANCELLED` 撤销下发与
  「已取消的活动」固定标题、2026-10 真机演练的客户端行为矩阵、手机侧该订哪个客户端），
  快速开始扩出可选的订阅第四步与 `doctor` 出口，命令表补 `subscribe` / `doctor`，
  自动化一节加 `doctor` 与 `skills/`，隐私一节把订阅通路的公开面口径写实
  （发布仓库公开 ⇒ 匿名浏览分支即可拿到 `<token>.ics` 文件名，转发 URL 等于授权）。
  仍是手写单文件 + 内联 CSS，未引入构建工具。
- **README 的订阅隐私口径同步为诚实版本**（与站点、与 `skills/` 第 7 步同一口径）：
  「URL 订阅 → 隐私口径」新增两条——公开仓库的分支文件列表页匿名可读、会直接列出
  `<token>.ics` 的文件名，所以「知道 URL 才能读」这层保护在公开仓库上并不成立；
  「产物里有什么就是公开什么」，默认并进的考试事件会把考试名、时间、考场与
  `DESCRIPTION` 里的座位号放进那份公开产物（`--no-exams` 可关）。
  「隐私与安全 → 订阅 URL 的风险口径」把原来的「安全边界只有一层」改成「暴露面有两处」。
  措辞只是不再高估保护强度，**暴露面本身没有修**——转私有受免费账户限制、换托管暂缓，
  见 `docs/design/2026-10-11-subscription-followups.md` §4 / §7。

### Notes

- 撤销条目标题沿用固定文案「已取消的活动」，不含课程名 / 教师 / 教室。
- 迁移那一次产生的整学期「已取消的活动」残条，在忽略 `STATUS:CANCELLED` 的客户端
  （Outlook.com / ColorOS）上的真机表现**尚未演练**（发版前在隔离 URL 单独补验），
  文案一律写「通常会」；退订重加是唯一确定有效的清理。
  **【发布后订正，2026-10-12 同日】** 隔离 URL 演练已做完：残条表现拿到 ColorOS 实测
  （96 条新日程与 75 条占位 1:1 成对），且**「退订重加是唯一有效的清理」这句被判错**——占位由产物携带、
  重加会再导入一遍。原文照留以忠实反映当时随包发布的说明，新口径见 `[0.8.1]` 小节的 Documentation。
- `.webcal` 分发**不在本版**（CD19，排到 0.8.x 独立一笔）。
- 设计规格见 `docs/design/2026-10-12-course-cancellation-uid-v2.md`；用户口径见 README
  「升级到 v0.8（UID 配方变更）」。

---

## [0.7.0] - 2026-10-11

新增一个只读的判据子命令与两个 Agent skill。**版本号取舍**：加子命令是 feature，按语义化
版本该进 0.7.0，于是原本预留给「课程侧撤销」的 0.7.0 让位到 0.8.0（这是 2026-10-11 由维护者
拍板的取舍，不是自动推论）。数据通路、产物结构与课程侧字节均无变化。

### Added

- `xjtu-calendar doctor`：**只读体检**子命令，把"部署到位没有"变成结构化检查清单
  （版本 / 会话 / 学期校历 / 作息表 / 课表快照 / 考试快照 / 渲染 / 订阅留底 / URL 自检）。
  不写任何文件、不触发 `fetch`、默认一次 HTTP 都不发（只有 `--verify` 且已配置订阅时
  才对订阅 URL 做一次匿名 GET）；任何判据内部抛错都降级成一条阻塞项而不是 traceback。
  退出码 **0 = 无阻塞**（警告不阻塞）、**5 = 有阻塞项**，供脚本与 Agent 判定成败。
  真机首跑修掉两处只有对着真实数据才会暴露的误报：留底按 `read_text` 读会把 CRLF 折成
  LF 导致永久误报"有未发布变更"；渲染项曾把**按周次展开后**的事件数说成课程门数。
  判据的版本下限是 `MIN_DOC_VERSION = (0, 7, 0)`——**必须是 `doctor` 问世的那个版本**，
  下限写成 0.6.x 会给一份跑不出这套检查的旧安装发绿灯。
- 仓库根 `skills/` 两个技能：**`xjtu-timetable-setup`**（低自由度部署向导，八步各有
  动作 / 证据 / 不满足时，登录一步显式禁止后台执行器与管道，收尾以 `doctor` 退出码定成败；
  `references/` 收有症状式排查与客户端矩阵的决策版副本，后者带 README 同步 sha）与
  **`timetable-api-archaeology`**（高自由度接口考古方法论：第 0 步是合规与授权，
  观测四问里"没有数据 vs 查询失败能否区分"是 go/no-go 而非打分项，身份字段决定"改期"
  会不会变成"删了重建"，每个待判信号必须配一个必然可见的对照变化；`references/` 收
  观测清单与红线）。判据只存一处：skill 不重复实现任何检查，全部调 `doctor`。
  两者都写明了验不到的部分——**新用户旅程未经验证**。
- 设计文档 `docs/design/2026-10-11-agent-skills.md`：把这套流程交付成两个 Agent skill 的
  设计（已批准）——`xjtu-timetable-setup`（低自由度的部署向导，八步各带"完成的证据"）与
  `timetable-api-archaeology`（高自由度的接口考古方法论，以"没有数据 vs 查询失败能否区分"
  为 go/no-go）。体检逻辑不做成 skill 内脚本，而是升级为 CLI 子命令 `xjtu-calendar doctor`
  （只读、退出码可判定、输出过隐私 grep），判据因此只存一处。含版本偏斜的处理、矩阵的
  单一权威与同步标记纪律、验收的诚实边界（**新用户旅程在本机验证不到**），
  以及 `doctor` 是 feature、牵动下一个版本号这一显式提醒。

---

## [0.6.1] - 2026-10-11

补丁版，两处用户可见的行为修正，都来自 2026-10-10/11 那次真机演练与排障：撤销条目
不再是一条「无主题」残条，`login` 不再"看起来成功、其实什么都没做"。数据通路、
产物结构与课程侧字节均无变化。

### Added

- 设计文档 `docs/design/2026-10-11-subscription-followups.md`：v0.6 演练过程中撞见的
  客户端差异与跟进议题，各自只写现状证据 / 改动面 / 前置条件，待排期展开——
  撤销条目的固定标题、课程侧撤销通路、`.webcal` 分发、订阅 token 在公开仓库上的暴露面、
  行为矩阵的维护纪律（含两格欠账）、`login` 的三处静默失败、免费账户上的 Pages 托管门槛。
- 设计文档 `docs/design/2026-10-11-multi-school-portability.md`：多系统 / 多校可移植性的
  议题备忘（结论是"现在不动"）。含耦合面实测（8998 行里学校专属约两千五、两张字段映射表
  为何刻意不共用）、三个卡点（接口考古的永久成本、登录差异、**合规与隐私的风险量级跳变**）、
  三档方案的代价与选型判据、`--from-file` 为什么**不是**交换格式，以及真要动手时第一步的
  只读观测配方与"能推翻本结论的证据"。

### Changed

- **`login` 不再"看起来成功、其实什么都没做"**（本机体验，不动数据通路）。沿用旧会话时
  改为打印「沿用本地会话（未验证有效性）」并给出 `login --force` 出口；`cmd_login` 收尾
  那句「会话就绪」改成中性的「本地会话」；401 与"一个 cookie 都没抓到"两条修复建议
  一律点名 `--force`（此前只说"重新运行 login"，而不带 `--force` 的那条路正是死路）；
  登录前的说明新增两段——即将打开的是**独立的浏览器实例**（平时浏览器里的登录态对它
  无效）、这一步要真人完成（放进后台任务或管道时那个 Enter 永远等不到）；用户中止登录时
  明确提示「会话未保存」。**刻意不做**廉价的网络探活：「探测不通」与「会话过期」必须可区分，
  风险大于收益。
- **撤销条目现在带一个固定标题「已取消的活动」**（`exams.CANCELLATION_SUMMARY`）。
  v0.6.0 的撤销条目刻意不写 `SUMMARY`，于是**忽略 `STATUS:CANCELLED` 的客户端**
  （Outlook.com、ColorOS 系统日历，2026-10 真机实测）把它当普通事件导入后，用户看到的
  只是一条「无主题」日程——认不出是废弃条目，也不知道该不该删。改成一个不含任何个人信息
  的固定文案后，这类残条一眼可辨。标题恒定 ⇒ 内容指纹恒定 ⇒ 同一份产物重渲染时
  `SEQUENCE` 不漂移（D8 不破）；`LOCATION` / `DESCRIPTION` / 课程名仍然一律不进产物，
  有用例逐条比对。课程侧与 live 考试事件的字节输出完全不变。
  规格侧 D3 已就地修订（`docs/design/2026-10-09-exam-cancellation.md` §2 / §6.2 / §9）。

### Documentation

- **会话与登录的可操作性写成判读阶梯**（10-11 一次真实排障的产物）：README「快速开始」
  与「常见问题」补明——`login` 是交互式流程，必须由真人在前台终端完成；会话过期后
  不带 `--force` 会打印「已检测到本地会话」直接退出；平时浏览器里的 eHall 登录态与工具的
  独立会话互不相通；判定成功只看会话文件的修改时间与 `fetch` 是否仍 401。另新增两条
  FAQ：`JSESSIONID` 同名 cookie 的 warning 不影响取数（干净会话实测仍出现），
  以及产物改名后已订阅客户端**未必**跟着改显示名（重加一定有效，不重加的行为未验证）。
- `AGENTS.md` 派发边界新增两条：**交互式命令不许放进后台执行器或管道**（判据是源码里的
  `input()`，回收判断只看产物证据），以及**给用户的命令要按他的 shell 写**（cmd 与 bash
  的环境变量、路径写法差异）。
- v0.5 考试规格 §7 补一条真实账户复验：未排考学期返回 `code:0 / 查询失败` 时，
  三态判定的**状态未知**分支在日常 home 上按设计降级（不含考试、`fetch` 零退出、课程不受影响）。
- **v0.6 撤销通路的真机演练结论落地**（纯文档，无代码变更）：README 新增
  「客户端行为矩阵」一节，并把三处基于推测的措辞改成实测结论——
  Google 日历（网页版）会移除 `STATUS:CANCELLED` 的日程；Outlook.com 与 ColorOS 系统日历
  **忽略该字段**，撤销条目会被当普通事件导入、显示成一条**无主题**日程；一次性导入型客户端
  不受影响（本就不回源）。自动回源周期同步写入实测值（Google 两次 ≤3h31m / ≤10h05m、
  ColorOS ≤2h35m、Outlook.com 14 小时以上未回源）。
- 顺带修掉两条**已被推翻的旧指引**：Google 日历当前版本**没有**「自动移除旧事件」开关；
  Android 侧的 URL 订阅工具是 **ICSx⁵**，不是 DAVx⁵（后者只做 CalDAV，官方 FAQ 明确把
  URL 订阅指向 ICSx⁵）。同时新增一条实测事实：ColorOS 系统日历自带「URL 日历」订阅入口，
  会定期回源并原地更新——国内用户此前被认为只能一次性导入。
- 0.6.0 条目里「真机删除表现尚未演练」的限定语已就地更正为指向 §11 的实测结论
  （改的是历史条目中一句前瞻性 hedge，不是发布内容本身）。

---

## [0.6.0] - 2026-10-10

考试撤销通路：把 v0.5 只能新增、不能撤销的缺口补上——已发布到订阅端的考试若在后续数据里
消失，下一份发布产物会带着撤销信号再出现一次，直到它的原定时刻自然过去。课程侧产物逐字节
不变，`--no-exams` 与三态判定原样保留。

### Added

- **考试撤销通路（`STATUS:CANCELLED`）**：已发布的考试事件在本次数据里消失（改期、取消，
  或用户「确认本学期无考试」）时，下一份 `export` / `subscribe push` 产物把那条事件标上
  `STATUS:CANCELLED` 再发一次，**保留到它原定开始时刻自然过去**；仍走同一份
  `method:PUBLISH` 文件、同一个订阅 URL，不新增第二个 token、不产出第二份 .ics、不发
  `METHOD:CANCEL`。会定期回源拉取的订阅客户端收到后随之移除（真机表现已于 2026-10-10/11
  演练并回写设计文档 §11：Google 网页版移除，Outlook.com 与 ColorOS 忽略该字段），一次性导入后不再回源的客户端
  仍需手动删除。撤销由数据驱动、**没有独立旗标**，也不新增配置项。
- **考试台账**（`subscribe/last-exams-<学期>.ics`）：记录「本机曾以 live 形态发布过、且
  原定时刻还没过去」的考试账本，是撤销候选的来源（台账 UID − 未经日期过滤的本次 live UID）。
  台账含考试原名/考场/座位，属个人数据：只落本地、以收紧权限（`private=True`）原子写盘、
  **永不发布**、日志不打印其内容；发布件里的撤销条目与之刻意不对称。
- **数据可信门槛**：只有「考试快照读到、且解析没炸」（确认无考试的空快照算可信）时才计算撤销
  候选；任何降级路径（无快照、非 UTF-8/JSON 坏、`--from-file` 从未抓过考试、整批解析失败）
  一律**不撤销、不读台账**，避免把「我没看到考试」实现成「撤销整学期」。考试侧任何失败都
  不让 `export` / `subscribe push` 非零退出。
- **撤销的最小字段**：撤销事件只带 UID + DTSTART/DTEND + `STATUS` + `SEQUENCE` + `DTSTAMP`，
  **不写 SUMMARY 行**、无考场、无座位、无教师——个人信息不在保留期内继续公开；全字段记录
  只留在本地台账里。
- **保留期用墙钟**：过期判定取 `datetime.now`（可注入 `cancel_expiry_at` 供测试），不用课表
  快照推导的渲染 stamp——后者用户不 `fetch` 就不前进，拿它判过期等于撤销信号永不消失。
- 新增 `src/xjtu_calendar/exams.py` 的四个纯函数（台账 reader/writer、撤销候选、撤销事件
  构建）、`config.Settings.exam_ledger_path()` 与撤销注入/回写管线；`ExportResult` 追加
  `exam_ledger_text`，`info` 增加 `exam_cancellations`。台账只在产物落地/发布成功之后回写，
  写盘失败只 warning、不改退出码。

### Changed

- **`diff` 文案改口**：v0.5 那句「被取消的考试不会从已订阅的日历里自动消失」不再成立——
  取消现在会以 `STATUS:CANCELLED` 自动下发；报告末尾据此改写说明，并**保留**「一次性导入后
  不再回源的客户端仍需手动删除」这半句（那部分事实没变）。
- **`diff` 新增「本次将撤销：最多 N 条」摘要行**：仅在本次报出取消项时打印，措辞用「将」
  与「最多」（diff 是发布前预览，此刻什么都没发）。这里的 N 数的是 diff 报出的取消条数，只是**实际
  下发量的上界**：真正随产物发出的还要过渲染侧门槛（数据可信）、D20 基线可发性、D2 时刻未过
  三道关卡，可能更少甚至为 0，所以补一句「以下次实际发布为准」。
- **`subscribe rotate` 换 token 前的事先警告**：轮转会换新订阅 URL，旧地址此后永远收不到
  撤销。故在 `rotate_token` **之前**跑一次只读探针渲染量出待撤销条数，若非零则提示
  「本次有 N 条考试事件尚未从旧订阅地址撤销，rotate 后旧地址将永远收不到撤销」；探针返回的
  台账文本一律丢弃（此刻产物还没发布出去）。
- **`export` / `subscribe push` / `subscribe rotate` 发布后摘要**：产物真正带出撤销时新增一行
  撤销摘要（`N == 0` 时整行不打印），push 与 rotate 共用同一份措辞与口径。
- 升级安全：v0.5.0 已订阅用户升级后第一次渲染（台账尚不存在）产物与 v0.5.0 **逐字节相同**，
  由新增的考试侧 golden（`tests/fixtures/legacy_exam_export_v050.ics`）钉住。

---

## [0.5.0] - 2026-10-09

次要版本：把 eHall「我的考试安排」接进现有管线，考试事件与课表共用**同一份 .ics、同一个
订阅 URL**（不新增第二个 token）；`X-WR-CALNAME` 自动带上年级与学期。

兼容性：考试接入**没有改动课程侧产物**——`--no-exams` 口径的输出与接入前逐字节相同，这条
承诺由 `tests/test_legacy_course_export_golden.py` 对 `tests/fixtures/legacy_course_export.ics`
的字节比对钉住。课程事件的 UID / `SEQUENCE` / `LAST-MODIFIED` 一律不变，已导入的用户无需
重新导入；会变只有标题一行（新增年级与学期后缀），它不参与事件配对。

已知限制：v1 只发 `method:PUBLISH`，**没有取消通路**——关掉开关或考试被取消，已发布的考试
事件仍留在客户端，需手动删除（v2 立项）。`--no-exams` 可一键回到接入前的行为。

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
- **考试安排接入**：`fetch` 的 HTTP 路径在抓课表的同时抓「我的考试安排」
  （`studentWdksapApp`，form 参数 `XNXQDM` 与课表同形），快照落
  `raw/exams-<学期>.json` 并同样单代轮转；`export` / `subscribe push` /
  `subscribe rotate` 把考试事件并入**同一份 .ics、同一个订阅 URL**（不新增第二份
  日历）。考试事件为单场绝对时刻的 `VEVENT`——带 `TZID=Asia/Shanghai` 的
  DATE-TIME（**刻意非全天事件**，否则进不了 SEQUENCE 留底基线、客户端永不更新）、
  无 RRULE、无 VALARM（提醒交客户端）；`SUMMARY` =「课程名（类型词）」，
  校区+教室进 `LOCATION`，考试全称 / 课程号 / **座位号** / 学分 / 主考教师进
  `DESCRIPTION`。UID 优先依据接口行 ID `WID`（缺失依次降级 `KSRWID`、内容组合，
  降级有日志；**改期后服务器是否换 `WID` 发布时未验证**，若换则表现为
  「取消 + 新增」而非原地更新）。**默认包含**，`fetch/export/subscribe push/
  subscribe rotate` 均可 `--no-exams` 关闭（fetch 侧不抓、其余不并入，本地快照
  一律不删）。响应按 `extParams` **三态判定**：有考试 / 确认无考试 / 状态未知；
  状态未知**绝不覆盖已有快照**（沿用旧数据并在 warning 里报出快照日期，无快照则
  明说不含考试），且考试侧任何失败都不把 `fetch`/`export` 拖成非零退出。
  已知限制：**已发布的考试事件不会因关闭开关或考试取消而从客户端消失**（v1 无
  `STATUS:CANCELLED` 取消通路，需客户端手动删除，v2 立项）。新增
  `src/xjtu_calendar/exams.py` 与 README「考试安排」小节。
- **`diff` 新增「考试变更」小节**：与课程小节并列，按行 ID 配对，报告
  **新增 / 取消 / 时间变更 / 教室变更 / 座位变更** 五类（**考试改名不在其列**——
  课程侧会报改名，这一不对称与「UID 稳定，原地更新仅对携带 `WID` 的行成立」
  一并写进 README）；首次拿到考试快照时全部按「新增」报告，不假装「无变化」；
  报告含取消项时额外提示客户端不会自动删除。
- **考试快照陈旧提示**：`export` 并入考试时，比较**考试快照 vs 同学期课表快照**
  （相对口径，不是「距今多少天」；考试排期跟着课表一起变），考试侧落后超过 7 天
  给出 warning「考试数据来自 X 日的课表同期快照，建议重新 fetch（本次仍按现有
  快照导出）」；`--no-exams` 不并入考试，该提示保持安静。
- **翻页护栏**：考试响应行数与 `totalSize` 不一致时记 warning 但仍照常落盘
  （实测一页够用，此为将来超过一页时「静默丢考试」留警痕）。

### Changed

- **`save_raw`/`load_raw` 增加 `kind=`**（考试快照走 `kind="exams"`；默认值不变，
  课表路径零行为变化）；**原始快照一律以 `private=True` 落盘**——此前 `save_raw`
  写盘继承 umask，与 spec §8 的凭据待遇不符，本次连同**课表快照**一并收紧
  （POSIX 0600，Windows 尽力而为）。
- 日志按键打码名单补 `sjbh` 与 `zjjsxm`（主考教师姓名）；机制不变，
  既有 `xh`/`xm`/`skjs`/`teacher` 覆盖其余敏感键。
- `subscribe.snapshot_age_days` 泛化为 `(..., kind=)`（默认 `"timetable"`，
  既有调用点行为逐字节不变），供考试快照相对陈旧度比较复用天数算式的唯一归属地。

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
