"""`xjtu-calendar doctor` 的判据核心：把"部署到位没有"变成结构化、只读的检查清单。

设计出处见 `docs/design/2026-10-11-agent-skills.md` §3.1。四条硬约束：

1. **只读** —— 不写文件、不 fetch、不碰 git；HTTP 仅在 ``verify=True`` 时发一次匿名 GET。
2. **警告不等于失败** —— "会话很旧"、"没有考试快照"、"还没配订阅"都是正常状态。
   判成失败会把用户推向错误的修复动作（尤其"重登"这种有成本、且需要真人配合的操作）。
   **唯一的例外**是「本地发布状态与已发布版本不相容」（2026-10-15 审阅裁决）：那不是
   "用户还没做某件事"，而是发布路径的凭据已经互相矛盾，`subscribe push` 对同一状态
   会直接拒绝（退出码 7）——体检若只留一行警告，用户要等到真正发布才发现自己需要
   ``subscribe repair``。仍不新增退出码：`fail` ⇒ ``5``。
3. **不抛异常** —— 任何一条检查内部炸了都折成一条 ``fail`` 继续跑完其余项。
   体检仪自己炸等于没有报告，而它常常是用户卡住时最后能跑的东西。
4. **只读到底** —— 判成阻塞也只**提示** `subscribe repair`，绝不代跑 fetch、也绝不
   改本地凭据/留底/台账；否则体检仪会与修复命令抢同一批文件。

输出不得含可识别个人信息（姓名 / 学号 / 课程名 / 考场 / 座位 / token）。
本机路径允许打印，与 ``status`` 口径一致——那是用户自己机器上的目录，且输出不落盘。
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import __version__
from .auth import inspect_session
from .cancellation import load_ledger
from .errors import XjtuCalendarError
from .exporter import UID_RECIPE_VERSION, build_ics_for_semester
from .sequence import parse_baseline
from .subscribe import (
    check_local_compatibility,
    has_unpublished_changes,
    load_state,
    needs_uid_migration,
    snapshot_age_days,
    verify_url,
)

if TYPE_CHECKING:  # pragma: no cover
    from pathlib import Path

    from .config import Settings

__all__ = [
    "DOCTOR_EXIT_BLOCKED",
    "MIN_DOC_VERSION",
    "STATUS_FAIL",
    "STATUS_OK",
    "STATUS_WARN",
    "Check",
    "doctor_exit_code",
    "run_checks",
    "worst_status",
]

STATUS_OK = "ok"
STATUS_WARN = "warn"
STATUS_FAIL = "fail"

#: 有阻塞项时的退出码。1 留给未预期异常，2/3/4 已被认证与权限类占用。
DOCTOR_EXIT_BLOCKED = 5

#: 本模块与 skill 的判据按哪个版本的行为编写；低于它就警告"文档可能不适用"。
#:
#: 下限必须是 **0.7.0**：`doctor` 子命令自 0.7.0 才存在。写 0.6.x 的话，一份没刷新的
#: 旧安装记录（可编辑安装在本项目真踩过）会一路绿灯通过版本检查，而它其实跑不出
#: 这份判据所描述的那套检查。
MIN_DOC_VERSION: tuple[int, int, int] = (0, 7, 0)

#: 会话保存多久只触发警告。**旧 ≠ 失效**——真正的判据是 fetch 是否返回 401。
SESSION_AGE_WARN_DAYS = 14

#: 课表快照超过多少天算陈旧（与考试快照陈旧提示同一量级）。
SNAPSHOT_AGE_WARN_DAYS = 7.0


@dataclass(frozen=True)
class Check:
    """一条检查结果。``detail`` 面向用户，不得含可识别个人信息。"""

    name: str
    status: str
    detail: str


def _parse_version(raw: str) -> tuple[int, int, int] | None:
    """取 ``X.Y.Z`` 前缀；``0.7.0.dev1`` 这类后缀忽略，解析不出来返回 None。"""
    parts: list[int] = []
    for chunk in raw.split(".")[:3]:
        digits = ""
        for char in chunk:
            if not char.isdigit():
                break
            digits += char
        if not digits:
            return None
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def _check_version() -> Check:
    parsed = _parse_version(__version__)
    if parsed is None:
        return Check("version", STATUS_WARN, f"版本号无法解析：{__version__}")
    if parsed < MIN_DOC_VERSION:
        want = ".".join(str(p) for p in MIN_DOC_VERSION)
        return Check(
            "version",
            STATUS_WARN,
            f"{__version__} 低于本判据编写时的版本 {want}，部分检查可能不适用",
        )
    return Check("version", STATUS_OK, __version__)


def _check_session(cfg: Settings) -> Check:
    info = inspect_session(cfg)
    if not info.usable:
        return Check(
            "session",
            STATUS_FAIL,
            "本地无会话。请在**自己的终端**运行：python -m xjtu_calendar login"
            "（交互式，需在弹出的窗口里完成认证）",
        )
    if info.saved_at is None:
        return Check("session", STATUS_WARN, "会话缺少保存时间，无法判断新鲜度")
    if info.has_storage_state and not info.has_app_session:
        return Check(
            "session",
            STATUS_WARN,
            "会话里没有教务侧（/jwapp）的 cookie，fetch 会返回 403 而不是 401。"
            "这通常不是账号没权限，而是登录停在了「请选择身份」那一步："
            "重跑 python -m xjtu_calendar login --force，在弹出的窗口里点「学生」"
            "并等到课表真的显示出来，再回终端按 Enter。",
        )
    if info.has_app_session and not info.weu_present:
        # 「有 /jwapp 的条数」与「承重的票据在不在」不是一回事：2026-10-14 的消融实验
        # 只去掉 `_WEU`，三个 .do 接口立刻回金智异常页（403）；而两份同名 JSESSIONID
        # 少掉任意一份都仍然 200 ⇒ 别把这一格归因到同名 cookie 的取舍上。
        return Check(
            "session",
            STATUS_WARN,
            f"教务侧有 {info.jwapp_cookie_count} 个 cookie，却没有承重的 `_WEU` 票据："
            "fetch 仍会返回 403（实测去掉这一条接口立刻回金智的异常页，"
            "而同名 JSESSIONID 少任一份都还能取到数，所以问题不在同名 cookie 的取舍）。"
            "处置：python -m xjtu_calendar login --force，在窗口里等到课表页真正渲染出来"
            "再按 Enter。",
        )
    age_days = max(0, (datetime.now(UTC) - info.saved_at).days)
    if age_days >= SESSION_AGE_WARN_DAYS:
        return Check(
            "session",
            STATUS_WARN,
            f"会话已保存 {age_days} 天（旧不等于失效，能取到数就不用管）。"
            "若 fetch 返回 401：python -m xjtu_calendar login --force",
        )
    return Check("session", STATUS_OK, info.describe())


def _check_semester_config(cfg: Settings, semester: str) -> Check:
    path = cfg.semester_config_path(semester)
    if not path.is_file():
        return Check(
            "semester_config",
            STATUS_FAIL,
            f"学期校历缺失（{semester}）。这一项必须由你按官方校历填写，工具刻意不内置猜测值。",
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return Check(
            "semester_config",
            STATUS_WARN,
            f"学期校历文件无法解析（{semester}）：{exc}。模板见 README"
            "「必须由你配置的内容 → 1. 教学日历（校历）」。",
        )
    body = payload.get("semester") if isinstance(payload, dict) else None
    if not isinstance(body, dict):
        return Check(
            "semester_config",
            STATUS_WARN,
            f"学期校历文件无法解析（{semester}）：缺少 semester 对象。",
        )
    monday = body.get("first_week_monday") or "未填"
    weeks = body.get("total_weeks")
    echo = f"已配置（{semester}）：第 1 教学周周一 {monday}、" + (
        f"共 {weeks} 周" if weeks is not None else "未设总周次（按课表自身周次展开）"
    )
    inner_key = str(body.get("key") or "")
    if inner_key and inner_key != semester:
        return Check(
            "semester_config",
            STATUS_WARN,
            f"校历内部的 key（{inner_key}）与学期标识（文件名 {semester}）不一致。"
            "key 会进入每一条事件的 UID——把它改成和文件名一致，等于换掉整份日历的身份："
            f"客户端会把旧事件全留下、再导入一份新的。{echo}",
        )
    return Check("semester_config", STATUS_OK, echo)


def _check_schedule_config(cfg: Settings) -> Check:
    path = cfg.schedule_config_path()
    if not path.is_file():
        return Check(
            "schedule_config",
            STATUS_FAIL,
            "作息表缺失。可用 `schedule` 子命令从官方页获取，"
            "或按 README「必须由你配置的内容 → 2. 作息表」小节的模板手工填写。",
        )
    return Check("schedule_config", STATUS_OK, "已配置")


def _check_snapshot(cfg: Settings, semester: str) -> Check:
    age = snapshot_age_days(cfg, semester)
    if age is None:
        return Check("snapshot", STATUS_FAIL, "还没有课表快照，先运行 fetch")
    if age > SNAPSHOT_AGE_WARN_DAYS:
        return Check("snapshot", STATUS_WARN, f"快照 {age:.1f} 天前，建议重新 fetch")
    return Check("snapshot", STATUS_OK, f"快照 {age:.1f} 天前")


def _check_exam_snapshot(cfg: Settings, semester: str) -> Check:
    age = snapshot_age_days(cfg, semester, kind="exams")
    if age is None:
        return Check(
            "exam_snapshot",
            STATUS_WARN,
            "无考试快照：未排考时本来就没有，属正常状态（不是故障）",
        )
    return Check("exam_snapshot", STATUS_OK, f"考试快照 {age:.1f} 天前")


def _check_render(cfg: Settings, semester: str) -> Check:
    try:
        result = build_ics_for_semester(cfg, semester)
    except XjtuCalendarError as exc:
        return Check("render", STATUS_FAIL, f"渲染失败：{exc}")
    events = int(str(result.info.get("events", 0)))
    exams = int(str(result.info.get("exam_events", 0)))
    if events == 0:
        return Check(
            "render",
            STATUS_WARN,
            "渲染出 0 个事件：核对学期标识与周次口径是否正确",
        )
    # ``info["events"]`` 是**按周次展开后**的事件数（一周五节上 16 周 = 80 个事件），
    # 不是课程门数。写成"门课程"会让人以为课表少了 87 门课。
    return Check("render", STATUS_OK, f"{events} 个事件、考试 {exams} 场进入产物")


def _check_subscription(cfg: Settings, semester: str, state: Any) -> Check:
    if state is None:
        return Check(
            "subscription",
            STATUS_WARN,
            "未配置订阅通道（只用一次性导入的话可以忽略）",
        )
    # 先跑与 `subscribe push` **同一套**判定（2026-10-14 审阅复核第 6 条）：门会拦的事
    # doctor 不能看不见。2026-10-15 的裁决把这一格从**警告**升成**阻塞**：
    # 本地四样已经散架、下一次发布必被拒（退出码 7）时，体检不许只留一行"注意到但不阻止"，
    # 否则用户要等到真正 push 才发现自己需要 `repair`。退出码不新增，复用 `5`。
    compat = check_local_compatibility(cfg, state, semester)
    if not compat.ok:
        return Check(
            "subscription",
            STATUS_FAIL,
            f"本地发布状态与已发布版本不相容（{compat.detail}）："
            f"push 会被拦住（退出码 7），先运行 subscribe repair --semester {semester}",
        )
    last_push = getattr(state, "last_push", None)
    if last_push is None:
        return Check("subscription", STATUS_WARN, "订阅已初始化但尚未发布过，运行 subscribe push")
    if compat.reason == "no_snapshot_verified":
        # 升级那一格：push 是**放行**的（弱判据），所以 doctor 也只能警告——
        # 体检不许比门更凶，否则用户会去做一次根本不必的 repair。
        return Check(
            "subscription",
            STATUS_WARN,
            "本地没有发布恢复快照（0.8.5 之前发布过的状态），留底字节与最后一次发布记录相符，"
            "但两份台账与迁移标记这次没能校验：下一次 push 会把恢复依据补落",
        )
    last_local: Path = cfg.subscribe_dir / f"last-{semester}.ics"
    if not last_local.is_file():
        return Check("subscription", STATUS_WARN, "本地留底缺失，运行 subscribe push 重新发布")
    # 必须 bytes→decode：read_text 的通用换行翻译会把 CRLF 折成 LF，
    # 于是 sha 永远对不上，doctor 会一直误报"有未发布变更"（真机跑出来的第一个 bug）。
    text = last_local.read_bytes().decode("utf-8")
    same_token = str(getattr(last_push, "token", "")) == str(state.token)
    # 再做一条**不依赖重建**的直比：留底应当逐字节等于最后一次发布的产物。
    # 只比"留底 vs 当前重建结果"是不够的——数据一旦也变了，
    # "上次发布在写留底前中断"这个状态就完全隐身，而它是下次整学期误撤销的基线。
    published_sha = str(getattr(last_push, "content_sha256", "") or "")
    local_sha = hashlib.sha256(last_local.read_bytes()).hexdigest()
    if same_token and published_sha and local_sha != published_sha:
        return Check(
            "subscription",
            STATUS_WARN,
            "本地留底与最后一次已发布的字节不一致（上次发布可能在写留底前中断）："
            "运行 subscribe repair 按恢复凭据补回，不产生新发布",
        )
    if same_token and not has_unpublished_changes(cfg, state, text):
        return Check("subscription", STATUS_OK, "留底一致，无未发布变更")
    return Check("subscription", STATUS_WARN, "有未发布变更或 token 已轮换，运行 subscribe push")


def _check_uid_recipe(cfg: Settings, semester: str, state: Any) -> Check:
    """UID 配方与迁移状态（CD18）。**警告级**——迁移是升级的预期动作，不是故障，

    所以本函数**永不返回 STATUS_FAIL**：读坏课程台账只是少一个计数，在函数内部就地
    吸收、降级成警告（`_safe` 的兜底形态是 fail，这一项不许把它走到）。
    文案只带数量与配方号（规格 §8：无课程名/教师/教室、无台账内容、无 token、无订阅 URL）。
    """
    count: int | None
    read_error: str | None = None
    try:
        ledger = cfg.course_ledger_path(semester)
        count = (
            len(load_ledger(ledger.read_text(encoding="utf-8"), label="课程台账"))
            if ledger.is_file()
            else 0
        )
    except (OSError, ValueError) as exc:
        # 只报失败**种类**（类名），不引用台账原文（§8 同口径于 load_ledger 自身）。
        count = None
        read_error = type(exc).__name__
    if state is None:
        if read_error is not None:
            # 未订阅也要如实交代台账读不出来（终审 minor d）：这一项对未订阅用户
            # 仍是"没有迁移可言"，但坏台账是真实可用性信号——折成警告，永不 FAIL。
            return Check(
                "uid_recipe",
                STATUS_WARN,
                f"配方 v{UID_RECIPE_VERSION}；未配置订阅，无迁移；课程台账暂不可读（{read_error}）",
            )
        return Check("uid_recipe", STATUS_OK, f"配方 v{UID_RECIPE_VERSION}；未配置订阅，无迁移")
    if needs_uid_migration(state):
        ledger_part = (
            f"课程台账现有 {count} 条" if count is not None else f"课程台账暂不可读（{read_error}）"
        )
        return Check(
            "uid_recipe",
            STATUS_WARN,
            f"待迁移：上次发布使用 v1 配方；下次 subscribe push 会一次性撤销旧事件并原地重建"
            f"（{ledger_part}）",
        )
    ledger_part = (
        f"课程台账 {count} 条" if count is not None else f"课程台账暂不可读（{read_error}）"
    )
    status = STATUS_OK if count is not None else STATUS_WARN
    return Check("uid_recipe", status, f"配方 v{state.uid_recipe}；{ledger_part}")


def _check_calendar_events(cfg: Settings, semester: str, include_calendar_events: bool) -> Check:
    """校历事件那一格（v0.9-P1-03）。口径与发布门**必须一致**（§12.2）。

    判据全部走 `calendar_events.doctor_calendar_status`，它内部就是那条门，
    所以这里不可能出现"门会拦而体检是绿的"。订阅状态不需要传进来：
    这一格读的是留底字节，与 `push` 那道门同一份证据。
    """
    from .academic_calendar import AcademicCalendar
    from .calendar_events import doctor_calendar_status
    from .subscribe import subscribe_dir

    path = cfg.semester_config_path(semester)
    if not path.is_file():
        return Check("calendar_events", STATUS_OK, "未配置教学日历，无校历事件")
    try:
        academic = AcademicCalendar.from_file(path)
    except XjtuCalendarError as exc:
        # 学期配置本身有问题时这一格不重复报错：`semester_config` 那格已经判了。
        return Check("calendar_events", STATUS_WARN, f"读不到校历事件（{type(exc).__name__}）")

    config = academic.calendar_events
    if config.declared_events == 0 and config.declared_conflicts == 0:
        return Check(
            "calendar_events",
            STATUS_OK,
            "未配置（学期配置里没有 events 段；要用的话加 --include-calendar-events 发布）",
        )

    # 「上一版发布里有没有校历事件」的唯一凭据就是留底 —— 与 push 那道门读同一份文件。
    last_local = subscribe_dir(cfg) / f"last-{semester}.ics"
    published_uids: set[str] = set()
    if last_local.is_file():
        with contextlib.suppress(Exception):
            published_uids = set(parse_baseline(last_local.read_text(encoding="utf-8")))

    verdict, reasons = doctor_calendar_status(
        config,
        semester_key=academic.semester.key,
        published_uids=published_uids,
        include_calendar_events=include_calendar_events,
    )
    sources = len({spec.source_url for spec in config.specs})
    summary = f"{len(config.specs)} 条可用、来源 {sources} 个"
    if config.errors:
        summary += f"；{len(config.errors)} 条配置错误"
    if config.has_unresolved_conflicts:
        summary += f"；{sum(1 for c in config.conflicts if not c.resolved)} 组冲突未裁决"
    if verdict != "ok":
        return Check(
            "calendar_events",
            STATUS_FAIL,
            f"{summary}（{'、'.join(reasons)}）："
            f"这个状态下 subscribe push 会被拒绝发布（退出码 8），先把配置改过来",
        )
    status = STATUS_OK if not config.errors else STATUS_WARN
    return Check("calendar_events", status, summary)


def _check_url(state: Any) -> Check:
    url = str(getattr(state, "subscription_url", ""))
    try:
        ok, msg = verify_url(url)
    except Exception as exc:
        # 自检失败是"未通过"，不是 traceback
        return Check("url", STATUS_FAIL, f"URL 自检异常：{type(exc).__name__}: {exc}")
    if not ok:
        return Check("url", STATUS_FAIL, f"URL 自检未通过：{msg}")
    return Check("url", STATUS_OK, f"URL 可达（{msg}）")


def _safe(name: str, probe: Callable[..., Check], *args: object) -> Check:
    """检查项的名字由调用方给：函数改名不该悄悄改变 CLI 的标签映射。"""
    try:
        return probe(*args)
    except Exception as exc:
        # 体检仪本身不许炸：任何一条判据抛错都要降级成一条阻塞项
        return Check(name, STATUS_FAIL, f"检查内部异常：{type(exc).__name__}: {exc}")


def run_checks(
    cfg: Settings, semester: str, *, verify: bool = False, include_calendar_events: bool = False
) -> list[Check]:
    """跑完全部判据。顺序即 CLI 打印顺序；任何单项内部异常都折成 fail，本函数不抛。"""
    checks: list[Check] = [
        _safe("version", _check_version),
        _safe("session", _check_session, cfg),
        _safe("semester_config", _check_semester_config, cfg, semester),
        _safe("schedule_config", _check_schedule_config, cfg),
        _safe("snapshot", _check_snapshot, cfg, semester),
        _safe("exam_snapshot", _check_exam_snapshot, cfg, semester),
        _safe("render", _check_render, cfg, semester),
    ]
    state = load_state(cfg, semester)
    # 校历事件这一格排在订阅之前：它自己就要读留底，与 `push` 那道门读同一份证据。
    checks.append(
        _safe("calendar_events", _check_calendar_events, cfg, semester, include_calendar_events)
    )
    checks.append(_safe("subscription", _check_subscription, cfg, semester, state))
    checks.append(_safe("uid_recipe", _check_uid_recipe, cfg, semester, state))
    if verify and state is not None:
        checks.append(_safe("url", _check_url, state))
    return checks


def worst_status(checks: list[Check]) -> str:
    """空清单按 ok 处理：没有检查项就没有阻塞项。"""
    if any(c.status == STATUS_FAIL for c in checks):
        return STATUS_FAIL
    if any(c.status == STATUS_WARN for c in checks):
        return STATUS_WARN
    return STATUS_OK


def doctor_exit_code(checks: list[Check]) -> int:
    return DOCTOR_EXIT_BLOCKED if worst_status(checks) == STATUS_FAIL else 0
