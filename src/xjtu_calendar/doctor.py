"""`xjtu-calendar doctor` 的判据核心：把"部署到位没有"变成结构化、只读的检查清单。

设计出处见 `docs/design/2026-10-11-agent-skills.md` §3.1。三条硬约束：

1. **只读** —— 不写文件、不 fetch、不碰 git；HTTP 仅在 ``verify=True`` 时发一次匿名 GET。
2. **警告不等于失败** —— "会话很旧"、"没有考试快照"、"还没配订阅"都是正常状态。
   判成失败会把用户推向错误的修复动作（尤其"重登"这种有成本、且需要真人配合的操作）。
3. **不抛异常** —— 任何一条检查内部炸了都折成一条 ``fail`` 继续跑完其余项。
   体检仪自己炸等于没有报告，而它常常是用户卡住时最后能跑的东西。

输出不得含可识别个人信息（姓名 / 学号 / 课程名 / 考场 / 座位 / token）。
本机路径允许打印，与 ``status`` 口径一致——那是用户自己机器上的目录，且输出不落盘。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from . import __version__
from .auth import inspect_session
from .cancellation import load_ledger
from .errors import XjtuCalendarError
from .exporter import UID_RECIPE_VERSION, build_ics_for_semester
from .subscribe import (
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
    return Check("semester_config", STATUS_OK, f"已配置（{semester}）")


def _check_schedule_config(cfg: Settings) -> Check:
    path = cfg.schedule_config_path()
    if not path.is_file():
        return Check(
            "schedule_config",
            STATUS_FAIL,
            "作息表缺失。可用 `schedule` 子命令从官方页获取，或按 examples/ 手工填写。",
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
    last_push = getattr(state, "last_push", None)
    if last_push is None:
        return Check("subscription", STATUS_WARN, "订阅已初始化但尚未发布过，运行 subscribe push")
    last_local: Path = cfg.subscribe_dir / f"last-{semester}.ics"
    if not last_local.is_file():
        return Check("subscription", STATUS_WARN, "本地留底缺失，运行 subscribe push 重新发布")
    # 必须 bytes→decode：read_text 的通用换行翻译会把 CRLF 折成 LF，
    # 于是 sha 永远对不上，doctor 会一直误报"有未发布变更"（真机跑出来的第一个 bug）。
    text = last_local.read_bytes().decode("utf-8")
    same_token = str(getattr(last_push, "token", "")) == str(state.token)
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


def run_checks(cfg: Settings, semester: str, *, verify: bool = False) -> list[Check]:
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
