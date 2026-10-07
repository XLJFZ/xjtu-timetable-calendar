# URL 订阅式 ICS 发布（`subscribe` 子命令）— 设计规格

- 日期：2026-10-07
- 状态：待用户审阅
- 前置决策来源：主会话头脑风暴（三节设计逐节获认可）

## 1. 背景与目标

v0.3.0 的交付方式是「本地生成 .ics → 用户手工导入日历」。课表变化后用户必须
重新导出、重新导入，且手机/电脑多端各来一遍。本功能新增 **URL 订阅通道**：
用户把自己的 .ics 发布到一个匿名可读、URL 不可猜测的静态地址，日历客户端
（Apple 日历、Google Calendar 等「通过 URL 添加订阅日历」）直接订阅，
课表更新后由客户端自动拉取，无需再导入。

**首要用户**：多人可用——任何装了这个包的同学都能给自己的日历建一条订阅。
**成功标准**：用户手机日历在课表调整后（自己跑过一次 `subscribe push`）
24 小时内自动呈现新状态；旧事件按 UID 原地更新而非重复堆积。

## 2. 已锁定的需求决策（用户逐条确认）

| 决策点 | 结论 |
|---|---|
| 服务谁 | 多人可用的功能，不是自用脚本 |
| 托管前提 | 每人的 GitHub Pages（公开仓库 + 不可猜 token 文件名） |
| 刷新机制 | **纯手动** `subscribe push`；不做定时任务、不做常驻进程 |
| 隐私口径 | 接受「URL 不可猜 = 访问能力」；支持一键 rotate 使旧链接失效 |

## 3. Non-goals（明确不做，留扩展点）

- 定时/自动刷新（Windows 计划任务、launchd、cron、常驻 serve）。
- 中心托管服务、账号体系、服务端鉴权。
- 多学期/多份课表合并进同一个订阅 URL（一 token 一学期，见 §6 状态文件）。
- 非 git 的发布通道（WebDAV、scp、Gist）。Gist 已评估并排除：
  历史版本不可删、需要用户交 PAT 给工具管理，隐私与凭据两头更差。
- CalDAV 直写（iCloud/Google）——另一种交付形态，独立立项再说。

## 4. 总体架构

```
fetch（已有）──> raw 快照 ──> subscribe push
                                 │  内存构建 .ics（复用 export 管线）
                                 ▼
                     ~/.xjtu-timetable-calendar/subscribe/
                       ├── subscribe-<semester>.json   （状态，private 写盘）
                       └── work-<semester>/            （git 工作区）
                                 │  孤儿提交，tree 仅含 <token>.ics
                                 ▼
                     git push --force  origin <branch:cal>
                                 │
                                 ▼
        https://<user>.github.io/<repo>/<token>.ics   （GitHub Pages 匿名 GET）
```

新增模块 `src/xjtu_calendar/subscribe.py` + CLI 子命令 `subscribe`；
对 `exporter` 做一次**行为不变**的函数提取。除此之外不动现有逻辑。

## 5. 组件与接口

### 5.1 `subscribe.py`

```python
TOKEN_BYTES = 16  # → 32 位十六进制

@dataclass
class SubscriptionState:
    semester: str
    repo_url: str          # git 远端，如 https://github.com/alice/cal-alice.git
    branch: str            # 默认 "cal"
    token: str             # secrets.token_hex(16)，文件名 = f"{token}.ics"
    url_base: str          # 订阅 URL 前缀，如 https://alice.github.io/cal-alice/
    last_push: PushRecord | None   # pushed_at(UTC iso) / content_sha256 / token / semester

def new_token() -> str: ...
def subscription_url(state) -> str:            # url_base.rstrip('/') + '/' + token + '.ics'
def derive_url_base(repo_url) -> str | None:   # 仅识别 github.com；识别不了返回 None
def load_state(cfg, semester) -> SubscriptionState | None
def save_state(cfg, state) -> None             # atomic_write_text(private=True)
def rotate_token(cfg, state) -> SubscriptionState
def publish(cfg, state, ics_bytes) -> PublishResult
```

`publish` 的 git 序列（全部 `subprocess.run`，参数列表形式，统一
`-c core.autocrlf=false -c commit.gpgsign=false`）：

1. 工作区 `subscribe/work-<semester>/`：无 `.git` 则 `git init -q` + `remote add origin`；
2. `git ls-remote --exit-code origin <branch>` 探测远端分支：
   - 存在 → `git fetch` + `git checkout -B <branch> origin/<branch>`，
     校验 tip 的 tree **只含一个 `*.ics` 文件**；否则 `SubscribeGuardError` 拒绝；
   - 不存在 → `git checkout --orphan <branch>`；
3. 写 `<token>.ics`（字节与 `export` 产物一致）。**跳过判定**：内容哈希与
   `last_push.content_sha256` 相同**且** token 与 `last_push.token` 相同
   → 返回 `NO_CHANGE`，不动远端（token 变化必须推，rotate 依赖这一点）；
4. 提交为**无父孤儿提交**（从已检出远端 tip 的状态先 `git reset --orphan`
   再 commit），保证 `cal` 分支恒为单提交、历史不增长；
5. `git push --force origin <branch>`；
6. **仅 push 成功后**更新 `last_push`（含本次 token 与内容哈希）并 `save_state`。

失败路径（网络/权限/护栏）：不落盘新状态，错误信息含「远端仍是上一版」。

### 5.2 CLI（沿用 `cmd_*` 模式）

```
xjtu-calendar subscribe init   --repo URL [--branch cal] [--url-base URL] [--semester KEY]
xjtu-calendar subscribe push   [--semester KEY] [--input FILE]     # 数据源与 export 同参数
xjtu-calendar subscribe rotate [--semester KEY]
xjtu-calendar subscribe status [--semester KEY] [--verify]
```

- `init`：校验/推导 `url_base`（非 GitHub 远端必须显式给）；生成 token；
  打印订阅 URL 与**一次性 Pages 开启指引**（Settings → Pages →
  Deploy from branch → `cal` /(root)）；已存在状态时报错，`--rotate` 才换 token。
- `push`：前置检查（已 init、raw 快照存在、git 可用）→ 内存构建 .ics →
  `publish`；快照超过 7 天 → 警告「数据可能过期，建议先 fetch」但不阻断；
  有未发布变化时正常，无变化报「无变化，跳过」。
- `rotate`：新 token → 走一次 `publish`（新 tree 只含新文件，旧 URL 立即 404）→
  打印新 URL 并提醒去各客户端更新订阅。
- `status`：URL、上次推送时间、快照年龄、`diff` 未发布变化摘要；
  `--verify` 对 URL 做一次匿名 GET，报告 200/404/超时（Pages 未开启或缓存中）。
- 多学期：每个学期一个状态文件与工作区（`subscribe-<semester>.json`），
  各学期订阅 URL 独立；`--semester` 解析沿用现有全局约定。

### 5.3 `exporter` 提取（纯重构）

`cmd_export` 的核心提取为：

```python
def build_ics_for_semester(cfg, semester, *, input_path=None) -> bytes
```

`export` 与 `subscribe push` 共用。守卫测试断言提取前后 export 产物**字节一致**
（沿用 v0.3 的指纹一致性守卫手法）。

## 6. 安全与隐私设计

- **token 即能力**：`subscribe-<semester>.json` 经 `fileutil.atomic_write_text(private=True)`
  写盘（POSIX 0600，Windows 尽力而为，同 `storage_state.json` 待遇）。
- **日志红线**：INFO 日志只记分支名与内容哈希，**不记订阅 URL / token**；
  完整 URL 仅出现在 `init/rotate/status` 的终端输出（用户主动索取）。
  新增守门测试断言日志流不含 token。
- **force-push 护栏**：只推状态文件记录的分支；远端 tip tree 非「单 .ics」即拒绝，
  防止误指向含代码的仓库/分支被覆盖。
- **历史零残留**：`cal` 分支永远单提交（孤儿 + 强推），课表旧版本不进 git 历史。
- **UID/SEQUENCE**：订阅内容就是现有 export 产物，UID 冻结策略不变，
  客户端按 UID 原地更新；SEQUENCE 基线沿用本地 .ics 文件。
  已知边界：`diff` 提示的课程改名会表现为新事件，订阅端同样适用（文档写明）。
- 风险告知（README 必写）：URL 转发给谁谁就能看；换课表环境/泄露疑虑时 `rotate`。

## 7. 错误处理汇总

| 场景 | 行为 |
|---|---|
| 未 init 就 push/rotate/status | 报错 + 给出 init 命令示例 |
| 无 raw 快照 | 复用 export 的既有错误口径 |
| git 不在 PATH | 明确提示安装 git（探测风格同 `find_browser`） |
| 非 GitHub 远端且未给 --url-base | init 即拒绝，解释推导规则 |
| 远端分支不是本工具产物 | `SubscribeGuardError`，拒绝并提示换分支 |
| push 网络/权限失败 | 状态不落盘；报「远端仍是上一版」；下次 push 自动重建工作区 |
| 内容无变化 | 跳过推送，退出码 0 |
| 快照 > 7 天 | 警告不阻断 |
| rotate 中途失败 | 旧 token 继续有效（状态未变），重试安全 |

## 8. 测试策略

- **单元 `tests/test_subscribe.py`**：token 格式/随机性；状态读写往返与
  private 写盘；`derive_url_base`（https/ssh 形式、非 GitHub 返回 None）；
  URL 拼接；内容哈希判等。
- **git 集成 `tests/test_subscribe_git.py`**（`file://` 临时裸仓库，无网络）：
  push 后 tip 为单提交、tree 仅 `<token>.ics`、字节与 export 一致；
  重复 push 跳过；**rotate 后内容未变也必须推送**（旧文件从 tip 消失、新文件出现）；护栏拒绝脏分支；
  失败注入（chmod 裸仓库）→ 状态不落盘 + 提示保留上一版。
- **CLI e2e `tests/test_cli_subscribe.py`**：`XJTU_CALENDAR_HOME` 隔离，
  init→push→status→rotate 全链路；`--verify` 对 file:// 场景优雅跳过；
  日志不含 token 的守门断言。
- **重构守卫**：`build_ics_for_semester` 提取前后 export 字节一致。
- **验收口径**：pytest 421 → 约 +30 全绿；mypy strict / ruff / CI 3.11-3.13 通过；
  真实冒烟：作者仓库开 `cal` 分支跑 `init/push/status --verify`，
  手机日历订阅确认事件与更新行为，随后 rotate 清理。

## 9. 文档交付

- README 新章节「URL 订阅（subscribe）」：用户侧一次性步骤
  （建公开仓库 → 开 Pages → `subscribe init/push` → 客户端订阅 URL）
  + 隐私口径说明 + 常见日历客户端订阅入口。
- 落地页快速开始区补一行「或订阅 URL 自动刷新」。
- CHANGELOG 按发布节奏记入（本功能合入时不预写，发布预备时统一整理）。

## 10. 发布与回滚

- 功能合入 main 后随下一个版本（v0.4.0）发布；无数据迁移、无既有行为变更，
  回滚 = 不再使用 `subscribe` 命令（远端 `cal` 分支由用户自管）。
- 本功能不触碰 PyPI/GitHub 发布通道，与 release.yml / publish-pypi.yml 正交。
