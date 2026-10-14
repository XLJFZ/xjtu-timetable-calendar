# 命令参考

这份文档讲使用层面的细节，从 README 拆出来以保持主入口简短。

> 从 README 拆出，内容一致；主入口见 [../../README.md](../../README.md)。

## 命令参考

| 命令 | 作用 |
|---|---|
| `login [--force]` | 浏览器手动登录，保存本地会话 |
| `status` | 查看会话与配置状态（不发起网络请求） |
| `fetch [--semester S] [--source auto\|http\|browser] [--from-file F] [--no-exams]` | 获取课表原始 JSON（HTTP 路径同时抓考试安排；覆盖前自动把上一份轮转为 `*.prev.json`，作为 `diff` 基线） |
| `diff [--semester S] [--old F] [--new F]` | 比对新旧课表快照：新增/删除课程、时段增减、周次/教室/教师变化；并列「考试变更」小节（新增/取消/时间/教室/座位；纯本地；`--old/--new` 只对课表生效） |
| `export [--semester S] [-o OUT] [--input F] [--name N] [--calendar-config F] [--schedule-config F] [--from-date D] [--to-date D] [--sequence-from ICS] [--no-sequence] [--no-exams] [--allow-mass-cancellation]` | 生成 `.ics`（默认并入考试事件；`--name` 不给时标题按课表自动推导，见上文「导出」；`--allow-mass-cancellation` 放行课程侧熔断，见「升级到 v0.8」） |
| `notice --url U \| --from-file F [--semester S] [--apply]` | 解析停课/调课通知，预览或合并进学期配置 |
| `schedule [--url U \| --from-file F] [--semester S] [--apply]` | 解析官方「学生作息时间表」页，预览或合并进作息表配置 |
| `subscribe init --repo U [--branch B] [--url-base U]` \| `subscribe push [--input F] [--no-exams] [--allow-mass-cancellation]` \| `subscribe rotate [--no-exams] [--allow-mass-cancellation]` \| `subscribe status [--verify]`（均可带 `--semester S`） | 把 .ics 发布到自己的 GitHub Pages，日历客户端按 URL 订阅（见上文「URL 订阅」；push/rotate 默认并入考试，`--no-exams` 同样生效；push 首次会自动做 v1→v2 UID 迁移，`--allow-mass-cancellation` 放行课程侧熔断） |
| `inspect [--input F] [-o OUT]` | 对原始 JSON 做**脱敏**结构分析 |
| `doctor [--semester S] [--verify]` | **只读体检**：版本 / 会话 / 校历 / 作息 / 快照 / 渲染 / 订阅留底一次看全。不写任何文件、默认不发请求（`--verify` 才对订阅 URL 做一次匿名 GET）。有阻塞项时退出码为 `5` |

全局参数：`--debug`（详细异常）、`-q`（静默）、`--version`

### 退出码

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `1` | 一般错误（网络、解析、导出） |
| `2` | 未登录，需要先跑 `login` |
| `3` | 登录态已失效，需要重新 `login --force`（不带 `--force` 会直接沿用这份已失效的会话） |
| `4` | 当前账号无权限访问该资源 |
| `5` | `doctor` 检出阻塞项（警告不算，仍 `0`） |
| `6` | 课程侧疑似数据不完整的整批撤销被拒绝发布（熔断，见「升级到 v0.8」；确认无误后用 `--allow-mass-cancellation` 放行） |
| `130` | 用户中断 |

### 环境变量

| 变量 | 作用 |
|---|---|
| `XJTU_EHALL_BASE` | eHall 域名，默认 `https://ehall.xjtu.edu.cn` |
| `XJTU_CALENDAR_HOME` | 数据目录，默认 `~/.xjtu-timetable-calendar` |
| `XJTU_CALENDAR_BROWSER` | 强制指定浏览器可执行文件 |
| `XJTU_SEMESTER` | 默认学期标识 |

---
