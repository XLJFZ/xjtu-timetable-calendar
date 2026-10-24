# 命令参考

这份文档讲使用层面的细节，从 README 拆出来以保持主入口简短。

> 从 README 拆出，内容一致；主入口见 [../../README.md](../../README.md)。

## 命令参考

| 命令 | 作用 |
|---|---|
| `login [--force]` | 浏览器手动登录，保存本地会话 |
| `status` | 查看会话与配置状态（不发起网络请求） |
| `fetch [--semester S] [--source auto\|http\|browser] [--from-file F] [--no-exams]` | 获取课表原始 JSON（HTTP 路径同时抓考试安排；覆盖前把上一份**复制**到 `*.prev.json` 作为 `diff` 基线 —— 复制式轮转，新快照写失败时当前那份仍在原位） |
| `diff [--semester S] [--old F] [--new F]` | 比对新旧课表快照：新增/删除课程、时段增减、周次/教室/教师变化；并列「考试变更」小节（新增/取消/时间/教室/座位；纯本地；`--old/--new` 只对课表生效） |
| `export [--semester S] [-o OUT] [--input F] [--name N] [--calendar-config F] [--schedule-config F] [--from-date D] [--to-date D] [--sequence-from ICS] [--no-sequence] [--no-exams] [--allow-mass-cancellation] [--include-calendar-events]` | 生成 `.ics`（默认并入考试事件；`--name` 不给时标题按课表自动推导，见上文「导出」；`--allow-mass-cancellation` 放行课程侧熔断，见「升级到 v0.8」；`--include-calendar-events` 把学期配置 `events` 段的校历事件并进来，默认**不带**） |
| `notice --url U \| --from-file F [--semester S] [--apply]` | 解析停课/调课通知，预览或合并进学期配置 |
| `schedule [--url U \| --from-file F] [--semester S] [--apply]` | 解析官方「学生作息时间表」页，预览或合并进作息表配置 |
| `subscribe init --repo U [--branch B] [--url-base U]` \| `subscribe push [--input F] [--no-exams] [--allow-mass-cancellation] [--allow-unverified-baseline] [--include-calendar-events]` \| `subscribe rotate [--no-exams] [--allow-mass-cancellation] [--allow-unverified-baseline] [--include-calendar-events]` \| `subscribe status [--verify]` \| `subscribe repair`（均可带 `--semester S`） | 把 .ics 发布到自己的订阅 URL（**推荐**私有仓 + Cloudflare Pages，这种托管 init 必须带 `--url-base`；也可用公开仓 + GitHub Pages），日历客户端按 URL 订阅（见上文「URL 订阅」；push/rotate 默认并入考试，`--no-exams` 同样生效；push 首次会自动做 v1→v2 UID 迁移，`--allow-mass-cancellation` 放行课程侧熔断；rotate 的补发与 push 共用同一条发布事务与同一道相容性检查，门在换 token **之前**；`repair` 按发布恢复快照把本地留底/台账对齐到线上那一版，不产生新发布；`--include-calendar-events` 两侧同口径——**rotate 也必须带**，不带时上一次发布过的校历事件会被这次补发丢掉，那道门会直接拒绝并返回 `8`） |
| `inspect [--input F] [-o OUT]` | 对原始 JSON 做**脱敏**结构分析 |
| `doctor [--semester S] [--verify] [--include-calendar-events]` | **只读体检**：版本 / 会话 / 校历 / 作息 / 快照 / 渲染 / **校历事件** / 订阅留底一次看全。不写任何文件、默认不发请求（`--verify` 才对订阅 URL 做一次匿名 GET），也**不代跑修复**。有阻塞项时退出码为 `5`；`--include-calendar-events` 决定这一格按哪条发布路径判定（不带 = 按普通 push 判，带了 = 按带校历事件的 push 判），因为体检与门必须对同一状态给同一结论 |

全局参数：`--debug`（详细异常）、`-q`（静默）、`--version`

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 一般错误（网络、解析、导出） |
| `2` | 未登录，需要先跑 `login` |
| `3` | 登录态已失效，需要重新 `login --force`（不带 `--force` 会直接沿用这份已失效的会话） |
| `4` | 当前账号无权限访问该资源 |
| `5` | `doctor` 检出阻塞项（警告不算，仍 `0`）。含「本地发布状态与已发布版本不相容」——同一状态 `subscribe push` 会返回 `7`，处置都是先跑 `subscribe repair` |
| `6` | 课程侧疑似数据不完整的整批撤销被拒绝发布（熔断，见「升级到 v0.8」；确认无误后用 `--allow-mass-cancellation` 放行） |
| `7` | 本地发布状态与"最后一次已发布的版本"不相容，`subscribe push` / `subscribe rotate` 拒绝发布（先跑 `subscribe repair`；确认远端就是那一版后可用 `--allow-unverified-baseline` 一次性放行——但「上一次发布的结果还没裁决」与「凭据读不懂」这两格不在旗标范围内，只能 `repair`；rotate 返回这一码时是"崩在推送之后、结果待裁决"，同样先 `repair`） |
| `8` | **校历事件**的发布准入未过，`subscribe push` / `subscribe rotate` 在构建之前整体拒绝（远端与本地状态一字未动）。两种触发：带了 `--include-calendar-events` 而学期配置的 `events` 段有错、或有未裁决的 `conflicts`；以及上一版产物里发布过校历事件而本次没带这个旗标（漏写旗标不等于"你要删光它们"）。**处置是改配置，不是 `repair`、也不是任何 `--allow-*` 旗标** |
| `130` | 用户中断 |

### 环境变量

| 变量 | 作用 |
|---|---|
| `XJTU_EHALL_BASE` | eHall 域名，默认 `https://ehall.xjtu.edu.cn` |
| `XJTU_CALENDAR_HOME` | 数据目录，默认 `~/.xjtu-timetable-calendar` |
| `XJTU_CALENDAR_BROWSER` | 强制指定浏览器可执行文件 |
| `XJTU_SEMESTER` | 默认学期标识 |

---
