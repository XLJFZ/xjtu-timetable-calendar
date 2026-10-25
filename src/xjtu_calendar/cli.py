"""命令行入口。

子命令
------
``login``
    打开浏览器让用户本人完成统一身份认证，保存本地会话。
``status``
    查看本地会话与配置状态（不发起网络请求）。
``fetch``
    获取当前账号的课表原始 JSON（需先登录），缓存到本地。
``export``
    解析 -> 展开 -> 生成 ``.ics``。
``inspect``
    对原始课表 JSON 做脱敏结构分析（Phase 1 产物，便于适配字段）。

设计原则
--------
- 正常情况**不向用户抛 Python traceback**，只给可读中文提示与修复建议。
- ``--debug`` 时才输出详细异常（且仍然脱敏）。
- 退出码有语义，便于脚本串联（见 :mod:`xjtu_calendar.errors`）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import shutil
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from . import __version__
from .academic_calendar import AcademicCalendar
from .cancellation import CANCELLATION_SUMMARY
from .config import Settings
from .errors import (
    AuthenticationExpired,
    AuthenticationRequired,
    CalendarEventsBlocked,
    EndpointNotConfigured,
    GitNotAvailable,
    PublishStateIncompatible,
    SemesterNotConfigured,
    SubscribeNotConfigured,
    TimetableFetchError,
    XjtuCalendarError,
)
from .exporter import UID_RECIPE_VERSION, ExportResult, MigrationSummary, build_ics_for_semester
from .fileutil import atomic_write_text
from .logging_setup import get_logger, setup_logging
from .sequence import parse_baseline

if TYPE_CHECKING:
    # cmd_subscribe 各分支内部惰性 `from . import subscribe`；这里只为类型标注。
    from . import subscribe
    from .fetcher import Endpoint

__all__ = ["build_parser", "main"]

logger = get_logger()

#: 考试快照陈旧告警的滞后阈值（天，spec §7:382-386 的相对口径；数值由 plan 锁定为 7）。
#: 教务的考试排期按学期跟着课表走，落后一周以内多半只是「本周课表又刷过一次」，
#: 每次 export 都报警太聒噪；超过 7 天才值得提一句「考试可能已改期」。
EXAM_STALE_LAG_DAYS = 7.0


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xjtu-calendar",
        description="将西安交通大学 eHall 个人课表导出为 iCalendar (.ics)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m xjtu_calendar login\n"
            "  python -m xjtu_calendar fetch --semester 2026-fall\n"
            "  python -m xjtu_calendar export --semester 2026-fall --output timetable.ics\n"
            "  python -m xjtu_calendar subscribe push --semester 2026-fall\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--debug", action="store_true", help="输出详细异常与调试日志")
    parser.add_argument("-q", "--quiet", action="store_true", help="只输出警告与错误")

    sub = parser.add_subparsers(dest="command", metavar="<命令>")

    # --- login ---
    login = sub.add_parser("login", help="登录并保存本地会话（浏览器手动完成认证）")
    login.add_argument("--force", action="store_true", help="已有会话时也重新登录")

    # --- status ---
    sub.add_parser("status", help="查看本地会话与配置状态")

    # --- fetch ---
    fetch = sub.add_parser("fetch", help="获取课表原始 JSON 并缓存到本地")
    fetch.add_argument("--semester", help="学期标识，例如 2026-fall")
    fetch.add_argument(
        "--source",
        choices=("auto", "http", "browser"),
        default="auto",
        help="抓取方式：http 复用会话请求接口；browser 用浏览器拦截前端请求；auto 优先 http",
    )
    fetch.add_argument("--from-file", help="跳过网络，直接从本地 JSON 文件读取（用于离线调试）")
    fetch.add_argument(
        "--no-exams",
        action="store_true",
        help="不抓考试安排（默认抓；抓失败不影响课表导出）",
    )

    # --- export ---
    export = sub.add_parser("export", help="解析课表并生成 .ics")
    export.add_argument("--semester", help="学期标识，例如 2026-fall")
    export.add_argument("-o", "--output", default="timetable.ics", help="输出文件路径")
    export.add_argument("--input", help="直接指定课表 JSON（默认用 fetch 的本地缓存）")
    export.add_argument("--calendar-config", help="教学日历 JSON 路径（覆盖默认查找）")
    export.add_argument("--schedule-config", help="作息表 JSON 路径（覆盖默认查找）")
    export.add_argument(
        "--name",
        default=None,
        help="日历名称；不填则自动按课表推导（如「西安交通大学课表 · 大三-上」），"
        "显式给出则原样使用",
    )
    export.add_argument("--from-date", help="只导出该日期（含）之后的事件，ISO 格式")
    export.add_argument("--to-date", help="只导出该日期（含）之前的事件，ISO 格式")
    export.add_argument(
        "--allow-unsupported-adjustments",
        action="store_true",
        help="教学日历声明了无法表达的调课时仍继续导出（带显著警告，事件会缺失）",
    )
    export.add_argument(
        "--sequence-from",
        help="以指定 ICS 为 SEQUENCE/LAST-MODIFIED 基线（默认自动探测输出文件的旧版本）",
    )
    export.add_argument(
        "--no-sequence",
        action="store_true",
        help="不使用基线：所有事件按新增处理（SEQUENCE: 0）",
    )
    export.add_argument(
        "--no-exams",
        action="store_true",
        help="不并入考试安排（默认并入；本地快照不删）",
    )
    export.add_argument(
        "--allow-mass-cancellation",
        action="store_true",
        help="课程侧撤销规模触发门 2 熔断时仍强行发布（默认拒绝——疑似课表没抓全时"
        "会拒绝发布并退出码 6；先确认课表真的少了一批课，再带此旗标重跑）",
    )
    export.add_argument(
        "--include-calendar-events",
        action="store_true",
        help="把学期配置里 events 段声明的**校历事件**（起课 / 报到 / 考试周 / 寒暑假 / "
        "放假调休）并进同一份 .ics。默认**不并**——这些日期由人工按学校公告抄写，"
        "抄错的可能性比课表高，而订阅发布是一次性影响所有客户端的动作",
    )

    # --- notice ---
    notice = sub.add_parser(
        "notice",
        help="抓取学校停课/调课通知，解析出停课日与调课日并校验（公开页面，无需登录）",
    )
    notice.add_argument("--url", help="通知页 URL（教务处 due.xjtu.edu.cn 的通知地址）")
    notice.add_argument("--from-file", help="离线模式：直接读取本地 HTML 文件")
    notice.add_argument("--semester", help="学期标识，例如 2026-2027-1（用于周次交叉校验）")
    notice.add_argument(
        "--apply",
        action="store_true",
        help="把解析结果合并写回学期配置（只新增、不覆盖现有条目）；默认只生成报告。"
        "存在无法解析的行时拒绝写入（fail-closed）。",
    )

    # --- schedule ---
    from .schedule_notice import DEFAULT_SCHEDULE_URL

    schedule = sub.add_parser(
        "schedule",
        help="获取教务处公开「学生作息时间表」页，预览或合并进作息表配置（默认官方页，无需登录）",
    )
    schedule.add_argument("--url", help=f"作息页地址，默认 {DEFAULT_SCHEDULE_URL}")
    schedule.add_argument("--from-file", help="离线模式：读取本地保存的 HTML")
    schedule.add_argument(
        "--semester",
        help="学期标识。生效区间的覆盖范围来自该学期校历（--apply 时必填）",
    )
    schedule.add_argument(
        "--apply",
        action="store_true",
        help="合并进 schedules/schedule.json（只新增、不覆盖；有无法归类的行时拒绝写入）",
    )

    # --- diff ---
    diff = sub.add_parser(
        "diff",
        help="比对两份课表快照，列出新增/删除/调整（纯本地，不发网络请求）",
    )
    diff.add_argument("--semester", help="学期标识；未显式给路径时用它定位 fetch 的快照")
    diff.add_argument(
        "--old",
        help="旧 raw JSON（默认：fetch 轮转出的上一份快照 timetable-<学期>.prev.json）",
    )
    diff.add_argument("--new", help="新 raw JSON（默认：当前缓存 timetable-<学期>.json）")

    # --- subscribe ---
    sub_p = sub.add_parser(
        "subscribe", help="把 .ics 发布到自己的 GitHub Pages 分支，日历客户端按 URL 订阅"
    )
    sact = sub_p.add_subparsers(dest="action", metavar="<动作>")
    sact.required = True
    init_p = sact.add_parser("init", help="登记发布目标并生成订阅 token")
    init_p.add_argument("--repo", required=True, help="git 远端 URL（GitHub Pages 仓库）")
    init_p.add_argument("--branch", default="cal", help="专用发布分支（默认 cal）")
    init_p.add_argument("--url-base", help="订阅 URL 前缀（GitHub 远端可自动推导）")
    init_p.add_argument("--semester", help="学期标识，例如 2026-fall")
    push_p = sact.add_parser("push", help="构建 .ics 并强推到发布分支")
    push_p.add_argument("--semester")
    push_p.add_argument("--input", help="直接指定课表 JSON（默认用 fetch 缓存）")
    push_p.add_argument(
        "--no-exams",
        action="store_true",
        help="不并入考试安排（默认并入；本地快照不删）",
    )
    push_p.add_argument(
        "--allow-mass-cancellation",
        action="store_true",
        help="课程侧撤销规模触发门 2 熔断时仍强行发布（默认拒绝——疑似课表没抓全时"
        "会拒绝发布并退出码 6；先确认课表真的少了一批课，再带此旗标重跑）",
    )
    push_p.add_argument(
        "--allow-unverified-baseline",
        action="store_true",
        help="本地既没有『最后一次发布』的恢复依据、留底与发布记录也对不上时仍继续"
        "（默认拒绝并退出码 7；先跑 subscribe repair。升级过来的老状态只要留底与发布记录"
        "逐字节相符就会自动放行，不需要这个旗标。注意：上一次发布结果**未裁决**、"
        "或恢复凭据**读不懂**这两格不在本旗标范围内，只能由 subscribe repair 处理）",
    )
    push_p.add_argument(
        "--revive",
        action="append",
        default=[],
        metavar="课程号",
        help="把手机上误删的那门课重新编号后再发一次（可重复）。ICSx⁵ 的删除记忆按 UID 记，"
        "同一 UID 再出现它不会补回来，所以救回来的唯一办法是让这门课换个 UID。"
        "代价：这门课在所有设备上算「旧事件消失 + 新事件出现」，忽略撤销字段的客户端"
        "（ColorOS）上旧那条会永久残留。只对订阅通路有意义，`export` 不认这个旗标。",
    )
    push_p.add_argument(
        "--include-calendar-events",
        action="store_true",
        help="把学期配置 events 段里的校历事件一起发布（默认不带，与 export 同口径）。"
        "开了之后，正式发布的拒绝条件也变严：配置里有任何错误或有未裁决的校历冲突 ⇒ "
        "整体不发布（退出码 8），因为校历事件**不接撤销通路**、少一条就恢复不回来",
    )
    rot_p = sact.add_parser("rotate", help="更换订阅 token（旧 URL 立即失效）")
    rot_p.add_argument("--semester")
    rot_p.add_argument(
        "--no-exams",
        action="store_true",
        help="不并入考试安排（默认并入；本地快照不删）",
    )
    rot_p.add_argument(
        "--allow-mass-cancellation",
        action="store_true",
        help="课程侧撤销规模触发门 2 熔断时仍强行发布（默认拒绝——疑似课表没抓全时"
        "会拒绝发布并退出码 6；与 push 同一判据、同一旗标）",
    )
    rot_p.add_argument(
        "--include-calendar-events",
        action="store_true",
        help="补发时把学期配置 events 段里的校历事件一起带上（与 push 同一旗标、同一判据）。"
        "rotate 会重新构建整份产物并补发，不接这个旗标会让上一次发布里的校历事件"
        "在换 token 时被静默丢掉——它们不接撤销通路，客户端连「已取消」都收不到",
    )
    rot_p.add_argument(
        "--allow-unverified-baseline",
        action="store_true",
        help="本地发布状态与恢复凭据不相容时仍继续补发（默认拒绝并退出码 7，且**在换 token"
        "之前**就停手；与 push 同一道门、同一判据，先跑 subscribe repair）",
    )
    st_p = sact.add_parser("status", help="查看订阅状态、URL 与新鲜度")
    st_p.add_argument("--semester")
    st_p.add_argument("--verify", action="store_true", help="匿名 GET 自检 URL 可达性")
    rep_p = sact.add_parser(
        "repair",
        help="按发布恢复快照把本地四样（留底 / 两份台账 / 迁移标记）对齐到线上那一版",
    )
    rep_p.add_argument("--semester")

    # --- doctor ---
    doctor = sub.add_parser(
        "doctor",
        help="只读体检：会话 / 配置 / 快照 / 渲染 / 订阅通道一次看全（不写任何文件）",
    )
    doctor.add_argument("--semester", help="学期标识，例如 2026-2027-1")
    doctor.add_argument(
        "--verify", action="store_true", help="额外对订阅 URL 发一次匿名 GET（默认不联网）"
    )
    doctor.add_argument(
        "--include-calendar-events",
        action="store_true",
        help="体检按「本次会带校历事件发布」这条路径判定（默认按不带的那条）。"
        "这一格与 subscribe push 的那道门共用判据，所以必须知道旗标状态",
    )

    # --- inspect ---
    inspect = sub.add_parser("inspect", help="对原始课表 JSON 做脱敏结构分析")
    inspect.add_argument("--input", help="课表 JSON 路径（默认用本地缓存）")
    inspect.add_argument("--semester", help="学期标识")
    inspect.add_argument(
        "-o", "--output", default="_notes/timetable-structure.md", help="报告输出路径"
    )

    return parser


# --------------------------------------------------------------------------- #
# 子命令实现
# --------------------------------------------------------------------------- #
def cmd_login(args: argparse.Namespace, cfg: Settings) -> int:
    from .auth import ensure_login

    info = ensure_login(cfg, force=args.force)
    logger.info("本地会话：%s", info.describe())
    return 0


def cmd_status(args: argparse.Namespace, cfg: Settings) -> int:
    from .auth import inspect_session
    from .fetcher import ENDPOINTS_FILE, load_endpoints

    info = inspect_session(cfg)
    print("会话状态")
    print(f"  状态        : {'可用' if info.usable else '未登录'}")
    print(f"  详情        : {info.describe()}")
    print(f"  数据目录    : {cfg.home}")
    print(f"  会话目录    : {cfg.session_dir}")
    print()
    print("配置状态")
    print(f"  eHall       : {cfg.ehall_base}")
    print(f"  appId       : {cfg.app_id}")
    endpoints = load_endpoints(cfg=cfg)
    print(
        f"  接口定义    : {'已载入 ' + str(len(endpoints)) + ' 个' if endpoints else '缺失（需先完成 Phase 1 接口分析）'}"
    )
    if not endpoints:
        print(f"              覆盖位置：{cfg.home / ENDPOINTS_FILE}（缺省用包内默认）")

    semesters = sorted(cfg.semesters_dir.glob("*.json")) if cfg.semesters_dir.is_dir() else []
    print(f"  学期校历    : {len(semesters)} 份")
    for path in semesters:
        print(f"                - {path.stem}")

    schedule_cfg = cfg.schedule_config_path()
    print(f"  作息表      : {'已配置' if schedule_cfg.is_file() else '缺失'}（{schedule_cfg}）")
    return 0


#: `doctor` 判据的内部名 → 面向用户的中文标签。名字由 `doctor.run_checks` 显式给出，
#: 这里只做展示映射；新增判据必须同时加一行，否则用户会看到英文 name。
_DOCTOR_LABELS = {
    "version": "版本",
    "session": "会话",
    "semester_config": "学期校历",
    "schedule_config": "作息表",
    "snapshot": "课表快照",
    "exam_snapshot": "考试快照",
    "render": "渲染",
    "calendar_events": "校历事件",
    "subscription": "订阅",
    "uid_recipe": "UID 配方与迁移",
    "url": "URL 自检",
}

_DOCTOR_MARKS = {"ok": "[通过]", "warn": "[警告]", "fail": "[阻塞]"}


def cmd_doctor(args: argparse.Namespace, cfg: Settings) -> int:
    from . import doctor as doctor_module

    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured(
            "未指定学期", hint="用 --semester 指定，或设置环境变量 XJTU_SEMESTER。"
        )

    checks = doctor_module.run_checks(
        cfg,
        semester,
        verify=args.verify,
        include_calendar_events=getattr(args, "include_calendar_events", False),
    )
    print(f"体检（学期 {semester}）")
    for check in checks:
        label = _DOCTOR_LABELS.get(check.name, check.name)
        mark = _DOCTOR_MARKS.get(check.status, check.status)
        print(f"  {mark} {label}：{check.detail}")
    blocked = sum(1 for c in checks if c.status == doctor_module.STATUS_FAIL)
    print(f"\n结论：{blocked} 项阻塞 / {len(checks)} 项检查。")
    return doctor_module.doctor_exit_code(checks)


def cmd_fetch(args: argparse.Namespace, cfg: Settings) -> int:
    from .auth import has_session
    from .fetcher import (
        fetch_current_semester,
        fetch_via_browser,
        fetch_via_http,
        load_endpoints,
        require_endpoint,
        save_raw,
    )

    semester = args.semester or cfg.semester_key

    cfg.ensure_dirs()

    payload = None

    # --- 离线：直接读文件 ---
    if args.from_file:
        if not semester:
            raise SemesterNotConfigured(
                "未指定学期",
                hint="--from-file 时请用 --semester 指定缓存名，或设置环境变量 XJTU_SEMESTER",
            )
        source = Path(args.from_file)
        if not source.is_file():
            raise XjtuCalendarError(f"文件不存在：{source}")
        payload = json.loads(source.read_text(encoding="utf-8"))
        logger.info("已从本地文件读取课表：%s", source)
        path = save_raw(payload, cfg, semester)
        logger.info("已缓存到 %s", path)
        _fetch_exams(
            {},
            cfg,
            semester,
            reason_if_skipped=(
                "已指定 --no-exams" if args.no_exams else "--from-file 导入没有会话，考试需在线获取"
            ),
        )
        return 0

    # --- 网络路径 ---
    if not has_session(cfg):
        raise AuthenticationRequired("本地没有可用会话，无法获取课表")

    endpoints = load_endpoints(cfg=cfg)
    has_timetable = "timetable" in endpoints
    use_http = args.source in ("auto", "http") and has_timetable

    if args.source == "http" and not has_timetable:
        # 不写自定义 hint：errors.EndpointNotConfigured 的默认指引
        # 列出的就是 load_endpoints 真正会读取的位置（显式路径 >
        # 用户覆盖 > 包内默认）。曾经这里让用户写 config/ 下的文件，
        # 而该目录从来不在查找链里（见 tests/test_cli_endpoints.py）。
        raise EndpointNotConfigured("尚未确认课表接口路径，无法使用 HTTP 方式")

    if use_http:
        endpoint = require_endpoint(endpoints, "timetable")
        logger.info("通过 HTTP 接口获取课表：%s", endpoint.description or endpoint.path)

        # 学期代码格式为 YYYY-YYYY-N（如 2026-2027-1），对用户可读；
        # 未指定时通过学期发现接口自动取得，用户不需要接触内部 ID。
        semester_code: str | None = semester
        if not semester_code and "current_semester" in endpoints:
            sem_endpoint = require_endpoint(endpoints, "current_semester")
            logger.info("未显式指定学期，通过学期发现接口获取当前学期")
            semester_code = fetch_current_semester(sem_endpoint, cfg=cfg)
            logger.info("当前学期代码：%s", semester_code)
        if not semester_code:
            raise SemesterNotConfigured(
                "无法确定学期代码",
                hint="用 --semester 指定（格式如 2026-2027-1），或确认学期发现接口可用。",
            )
        semester = semester_code  # 供后续 save_raw 使用

        payload = fetch_via_http(endpoint, cfg=cfg, params={"XNXQDM": semester_code})
    else:
        logger.info("通过浏览器拦截获取课表（不猜测接口路径）")
        captured = fetch_via_browser(cfg=cfg)
        if not captured:
            raise XjtuCalendarError(
                "未能捕获课表数据",
                hint="请在浏览器里手动进入「我的课表」页面后再试，"
                "或先运行 scripts/probe_ehall.py 完成接口分析。",
            )
        # 取课程记录最多的那份响应
        payload = max(captured, key=lambda item: _payload_size(item["payload"]))["payload"]

    if not semester:
        # HTTP 路径上方的学期发现已尝试过；走到这里说明仍无法确定缓存名
        raise SemesterNotConfigured("无法确定学期", hint="用 --semester 指定（格式如 2026-2027-1）")
    path = save_raw(payload, cfg, semester)
    logger.info("已获取课表原始数据，缓存到 %s", path)

    # 考试安排（行为矩阵见设计文档 §6.2）：只有 HTTP 分支抓；--no-exams 与浏览器分支只记日志。
    # 内层 `_fetch_exams` 管可预期的业务失败（认证/抓取/三态判定），这里外层再兜一层
    # "不可预期的一律降级"——考试是增量，绝不能把它的异常带崩课表主功能的退出码（§7 末条）。
    if args.no_exams:
        _fetch_exams(endpoints, cfg, semester, reason_if_skipped="已指定 --no-exams")
    elif use_http:
        try:
            _fetch_exams(endpoints, cfg, semester)
        except Exception as exc:
            logger.warning("考试安排出现未预期错误，已跳过（课表不受影响）：%s", exc)
    else:
        _fetch_exams(endpoints, cfg, semester, reason_if_skipped="浏览器拦截只覆盖课表接口")

    print()
    print("提示：该缓存文件含个人信息，已在 .gitignore 中排除，请勿提交或分享。")
    return 0


def _exam_total_size(payload: Any) -> int | None:
    """从三态判定**选中的同一 module** 读 `totalSize`（翻页护栏用，设计文档 §6.4）。

    模块选取直接调 `exams._exam_module`（判据唯一的归属地），不在这里抄一份同样的条件：
    抄本会让"行数与总数出自同一个 module"这条保证只靠注释维持——将来谁改选模块的口径
    （加模块名偏好、改 `extParams` 条件），护栏就会跨模块比较，正好废掉它存在的意义。

    无法核对时返回 ``None`` 并**不**报警（避免噪音）。数字以字符串形态给出（这个 API 族
    的外层 `code` 就是字符串 ``"0"``，同族字段同样可能带引号）时兜一层 `int(str(...))`，
    否则非数字／缺键才会真的返回 ``None``。
    """
    from .exams import _exam_module

    module = _exam_module(payload)
    if module is None:
        return None
    try:
        return int(str(module.get("totalSize")))
    except (TypeError, ValueError):
        return None


def _exam_fallback_note(cfg: Settings, semester_code: str) -> str:
    """考试降级提示里"本地这份数据现在算什么"的后半句（设计文档 §7:376）。

    有旧快照就报出**它是哪天的**（取 `raw_exams_path` 的 mtime），让用户知道自己正在沿用
    哪一份数据；"距今多少天"的算式属于 Task 11 的 `snapshot_age_days(..., kind="exams")`，
    这里只给日期。没有旧快照就明说不含考试——**绝不**写"沿用已有快照"，那是让用户相信
    自己正在依赖一个根本不存在的东西。
    """
    path = cfg.raw_exams_path(semester_code)
    if not path.is_file():
        return "本地没有考试快照，本次导出不含考试"
    try:
        day = datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
    except (OSError, OverflowError, ValueError):
        # 快照刚被移走（OSError）：宁可不报日期，也不反过来谎称"没有快照"。
        # 坏 mtime（文件系统抽风/时钟回拨）：`fromtimestamp` 在 Windows 上抛 OSError[Errno 22]，
        # 在 POSIX 上抛 OverflowError/ValueError——与 export 侧同族写法（本文件
        # cmd_export 的 `contextlib.suppress(OSError, OverflowError, ValueError)`，终审 F3）
        # 一致，三类一起兜，否则未评审过的通用异常文案会顶掉这句降级说明。
        return "本地已有考试快照，本次导出继续沿用它"
    return f"不覆盖已有快照，沿用 {day} 的考试快照"


def _fetch_exams(
    endpoints: Mapping[str, Endpoint],
    cfg: Settings,
    semester_code: str,
    *,
    reason_if_skipped: str | None = None,
) -> Path | None:
    """宁缺毋滥：任何失败都只记日志，**绝不覆盖**已有快照，绝不非零退出。

    三态判定唯一归属点是 `exams.classify_exam_payload`（裁决 1）：只有 HAS_EXAMS /
    NO_EXAMS 才 `save_raw(kind="exams")`；UNKNOWN（无法判定的响应／会话过期／抓取失败）
    一律不动本地快照。缺端点走 `require_endpoint` + `EndpointNotConfigured`（裁决 2），
    与课表同口径——`load_endpoints` 会过滤占位符路径，"缺失"有两种来源，这里一并兜住。
    """
    from .exams import ExamState, classify_exam_payload
    from .fetcher import fetch_via_http, require_endpoint, save_raw

    if reason_if_skipped:
        logger.info("%s，本次不抓考试安排", reason_if_skipped)
        return None
    try:
        endpoint = require_endpoint(endpoints, "exam_schedule")
    except EndpointNotConfigured:
        logger.warning("未配置 exam_schedule 端点，跳过考试安排（课表不受影响）")
        return None
    try:
        payload = fetch_via_http(endpoint, cfg=cfg, params={"XNXQDM": semester_code})
    except (AuthenticationExpired, TimetableFetchError) as exc:
        logger.warning("考试安排获取失败（%s）；%s", exc, _exam_fallback_note(cfg, semester_code))
        return None
    outcome = classify_exam_payload(payload)
    if outcome.state is ExamState.UNKNOWN:
        logger.warning(
            "考试安排响应无法判定（extParams.code=%r msg=%r）；%s",
            outcome.code,
            outcome.msg,
            _exam_fallback_note(cfg, semester_code),
        )
        return None
    if outcome.state is ExamState.NO_EXAMS:
        logger.info("本学期暂无考试安排（接口确认：空）")
    total_size = _exam_total_size(payload)
    if total_size is not None and total_size != len(outcome.rows):
        # §6.4 翻页护栏：只告警，绝不据此拒绝落盘（裁决 3）——今天一页够用，
        # 但将来某学期超过一页时，这条 warning 阻止考试被静默丢弃而无任何痕迹。
        logger.warning(
            "考试响应行数（%d）与 totalSize（%d）不一致，可能超过一页；本次仍照常缓存",
            len(outcome.rows),
            total_size,
        )
    path = save_raw(payload, cfg, semester_code, kind="exams")
    logger.info("考试安排已缓存到 %s", path)
    return path


def cmd_export(args: argparse.Namespace, cfg: Settings) -> int:
    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured(
            "未指定学期", hint="请用 --semester 指定，或设置环境变量 XJTU_SEMESTER"
        )

    result = build_ics_for_semester(
        cfg,
        semester,
        input_path=args.input,
        calendar_config=args.calendar_config,
        schedule_config=args.schedule_config,
        calendar_name=args.name,
        from_date=args.from_date,
        to_date=args.to_date,
        allow_unsupported_adjustments=args.allow_unsupported_adjustments,
        sequence_from=args.sequence_from,
        baseline_probe=args.output,
        no_sequence=args.no_sequence,
        # getattr 而不是直接读属性：既有测试与外部调用方会手工拼 Namespace，
        # 它们不可能知道 0.9 新加了这一个旗标（同 `--allow-mass-cancellation` 的处置）。
        include_exams=not args.no_exams,
        include_calendar_events=getattr(args, "include_calendar_events", False),
        # CD4/§6.6：export 与订阅路径**同一判据同一心智**——熔断在构建内部抛出
        # `MassCancellationBlocked`（退出码 6），沿 `main()` 的异常出口变成退出码；
        # 产物与台账都在 `build_ics_for_semester` 返回之后才写，异常时一行都没落盘。
        # CD3：刻意**不传** `published_baseline_text`——迁移与 export 无关，
        # 手工导入的人把几百条撤销当新事件导进去更糟（§7「export 路径」行）。
        allow_mass_cancellation=args.allow_mass_cancellation,
    )

    # §7:382-386 陈旧口径：考试快照 vs **同学期课表快照**（不是"距今多少天"）。
    # 文案严格按 spec 的「考试数据来自 X 日的课表同期快照」，再加补救动作与"本次仍
    # 继续"的 fail-closed 说明。日期取考试快照 mtime，与 Task 7 `_exam_fallback_note`
    # 读同一份 mtime——两处刻意不合并（见 Task 11 报告的重复说明）。
    # --no-exams 时本次产物里根本没有考试事件，任何「考试可能已改期」的提示对本次
    # 运行都是谎话（spec §7:390-392 把 --no-exams 当作有意的回滚出口）：整块跳过。
    # 任何滞后计算/stat 失败（如并发 fetch 正在轮转快照的窗口）都视作"没有可提醒的"：
    # 宁缺毋滥，export 绝不因此报错或非零退出。坏 mtime（文件系统抽风/时钟回拨）还会让
    # `datetime.fromtimestamp` 抛 OverflowError/ValueError——本机 Windows 上表现成
    # OSError[Errno 22]、POSIX 上才是这两类，三类一起 suppress 才封得住（终审 F3，§7:387）。
    if not args.no_exams:
        from .exams import exam_snapshot_lag_days

        with contextlib.suppress(OSError, OverflowError, ValueError):
            lag = exam_snapshot_lag_days(cfg, semester)
            if lag is not None and lag > EXAM_STALE_LAG_DAYS:
                exam_day = (
                    datetime.fromtimestamp(cfg.raw_exams_path(semester).stat().st_mtime)
                    .date()
                    .isoformat()
                )
                logger.warning(
                    "考试数据来自 %s 的课表同期快照，考试可能已改期，建议重新 fetch（本次仍按现有快照导出）",
                    exam_day,
                )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # newline=""：render_ics 已按 RFC 5545 产出 CRLF 行尾，
    # 若用默认 newline=None，Windows 会再翻译一次得到 \r\r\n。
    output.write_text(result.ics, encoding="utf-8", newline="")

    # 考试台账：产物写成功之后才回写（spec D11），且 `None` 时**不创建文件**。
    # 目录可能不存在（ensure_dirs 刻意不含 subscribe/，D24①），写入失败一律不许
    # 影响 export 的退出码——课程是主功能。
    if result.exam_ledger_text is not None:
        _write_exam_ledger(cfg, semester, result.exam_ledger_text)

    # 课程台账（CD8/§7「export 路径」行）：与考试台账**同一时机**——产物落地之后才回写，
    # 写失败只 warning 不改退出码。export 只写台账这一份证据：它**不读也不写**
    # `uid_recipe` 标记、不发迁移撤销（CD3），标记的回写只属于 publish 成功的
    # push/rotate 路径（CD7）。
    if result.course_ledger_text is not None:
        _write_course_ledger(cfg, semester, result.course_ledger_text)

    # --- 汇总 ---
    info = result.info
    print()
    print("Semester:")
    print(f"  {info['semester_name']}")
    print()
    print("Courses:")
    print(f"  {info['courses']}")
    print()
    print("Meetings:")
    print(f"  {info['meetings']}")
    print()
    print("Events:")
    print(f"  {info['events']}")
    print()
    if result.sequence_stats is not None:
        stats = result.sequence_stats
        print("Changes vs baseline:")
        print(
            f"  unchanged {stats['preserved']} / updated {stats['updated']} / new {stats['added']}"
        )
        print()
    print("Date range:")
    print(f"  {info['date_range']}")
    print()
    print("Output:")
    print(f"  {output}")
    print()
    # 撤销数量（spec §6.8）：`N == 0` 时整行不打印，摘要与 v0.5 保持一致。
    cancelled = int(str(result.info.get("exam_cancellations", 0)))
    if cancelled:
        print(f"撤销：{cancelled} 条（已发布考试事件在本次产物中标记为取消）")
        print()
    # 校历事件的三格计数（P1-03）：进产物的、被未裁决冲突扣下的、配置有错的。
    # 独立 `export` 走**预览口径**（§12.2）：有错也照样带着好事件出，但必须说清楚少了什么，
    # 否则用户会拿着一份看起来正常、实则缺了寒假的日历去订阅。
    cal_events = int(str(result.info.get("calendar_events", 0)))
    cal_withheld = int(str(result.info.get("calendar_withheld", 0)))
    cal_errors = int(str(result.info.get("calendar_config_errors", 0)))
    if cal_events or cal_withheld or cal_errors:
        print(f"校历事件：{cal_events} 条进入产物")
        if cal_withheld:
            print(f"  另有 {cal_withheld} 条因未裁决的来源冲突本次不生成（两边都不猜）")
        if cal_errors:
            print(f"  另有 {cal_errors} 条配置错误被跳过（明细见日志/doctor；正式发布会被拒绝）")
        print()
    logger.info("已生成 %s", output)
    return 0


def cmd_inspect(args: argparse.Namespace, cfg: Settings) -> int:
    """对原始课表 JSON 生成脱敏结构报告，便于适配字段。"""
    from .fetcher import load_raw
    from .logging_setup import redact_url  # noqa: F401 - 保持与其他模块一致的脱敏入口

    if args.input:
        payload = json.loads(Path(args.input).read_text(encoding="utf-8"))
        source_label = args.input
    else:
        semester = args.semester or cfg.semester_key
        if not semester:
            raise SemesterNotConfigured("未指定学期", hint="请用 --semester 指定")
        payload = load_raw(cfg, semester)
        source_label = str(cfg.raw_timetable_path(semester))

    report = _structure_report(payload)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        f"# 课表结构分析（脱敏）\n\n来源：`{source_label}`\n\n```json\n{report}\n```\n",
        encoding="utf-8",
    )
    print(report)
    print()
    logger.info("报告已写入 %s", output)
    print("提示：报告已脱敏，但仍建议只保留字段名与类型用于适配，不要公开原始值。")
    return 0


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _write_exam_ledger(cfg: Settings, semester: str, text: str) -> None:
    """回写考试台账；失败只记 warning，不改调用方的成败。"""
    path = cfg.exam_ledger_path(semester)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text, private=True)
    except OSError as exc:
        logger.warning("考试台账没能写入 %s（%s），下次仍按原台账判断撤销", path, exc)
        return
    logger.debug("考试台账已更新：%s", path)


def _write_course_ledger(cfg: Settings, semester: str, text: str) -> None:
    """回写课程台账（CD8 的第二份台账，与考试侧同等待遇）；失败只记 warning。

    措辞与时机逐字对齐 :func:`_write_exam_ledger`：`private=True` 原子写、写之前自己
    `mkdir`（`ensure_dirs` 不建 `subscribe/`，D24①）、`OSError` 只降级为告警而不许改
    调用方的退出码。**永不**打印台账内容（§8）。
    """
    path = cfg.course_ledger_path(semester)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, text, private=True)
    except OSError as exc:
        logger.warning("课程台账没能写入 %s（%s），下次仍按原台账判断撤销", path, exc)
        return
    logger.debug("课程台账已更新：%s", path)


def _print_migration_notice(summary: MigrationSummary) -> None:
    """v1→v2 迁移那一次的预告（CD11「自动但不静默」）——只在发布之前调用。

    迁移是配方切换、天然整学期量级，所以它**豁免**门 2 的熔断；换来的代价是必须在这里
    把规模说清楚，不许悄悄把几百条撤销塞进订阅者的日历。措辞的两条硬约束：
    ①「这是升级到 v0.8 的预期行为、不需要额外操作」（否则用户会以为出了故障）；
    ② 客户端删除行为仍写「通常会」——整学期规模的残条**没有在真机上演练过**（CD6/§11，
    演练是发版前的独立一步），只有考试侧那一次单独读数（Google 网页版会移除）可以点名。
    只报数量与占比，绝不带课程名（§8）。
    """
    print(
        f"提示：检测到上次发布使用 v1 UID 配方，本次将一次性撤销 {summary.candidates} 条"
        "旧事件并按新配方原地重建——这是升级到 v0.8 的预期行为，不需要额外操作。\n"
        "  会回源的订阅客户端通常会随之移除旧事件（Google 网页版已实测）；"
        "忽略该字段的客户端（Outlook.com、ColorOS）会留下一批"
        f"「{CANCELLATION_SUMMARY}」占位，直到原定时刻过去——"
        "2026-10 真机演练实测：占位与新课程 1:1 成对，且占位由产物自身携带，"
        "退订再重新添加也清不掉，只能等各自原定时刻过去后由后续发布收掉。"
    )


def _print_publish_cancellation_summary(result_ics: ExportResult) -> None:
    """发布成功后的撤销摘要（spec §6.8）：`N == 0` 时整行不打印。

    push 与 rotate 两条发布路径**共用**这一份文案（评审统一口径：报数口径要一致，
    不许各自发明措辞）。数量读 `info["exam_cancellations"]`——本次随产物真正
    下发的撤销条数（被 D20 判为不可发的不在其中）；只在 publish 成功之后调用
    （D11：没发出去就不许报"已撤销"）。

    客户端删除行为按 spec §11/D5 尚未做真机演练，措辞一律用"通常会"，不把话说满。
    """
    cancelled = int(str(result_ics.info.get("exam_cancellations", 0)))
    if cancelled:
        print(
            f"撤销：{cancelled} 条（已发布的考试事件在本次产物中标记为取消，"
            "支持删除的客户端通常会移除它们；一次性导入的客户端仍需手动删除）"
        )


def _print_webcal_hint(state: subscribe.SubscriptionState | None) -> None:
    """iOS/macOS 的一行 webcal 订阅链接（followups §3）：
    init / push（含 NO_CHANGE）/ rotate / status 四处共用一份文案。

    `webcal://` 是 URL scheme 而非发布物——只把已打印的 https URL 换个 scheme，发布分支上
    不多放任何文件（多一个 `<token>.webcal` 会撞 tree 护栏与 rotate 的 `*.ics` 清理）。
    非 https 的 url_base（`webcal_url` 为 `None`）整行不打印：宁可不提示，也不给一个
    看起来能点其实无效的链接；`state is None`（尚未 init）同样静默返回。

    口径两条：① 用 `print` 走 stdout（与相邻输出一致，`--debug` 时 token 仍不进日志通道）；
    ② 文案必须是 GBK 可编码的纯中文 + ASCII，不得出现 emoji——重定向到 cp936 管道时非 GBK
    字符会抛 UnicodeEncodeError（v0.4 真机踩过的坑，见 :func:`_harden_output_streams`）。
    「点链接即可订阅」在本项目**没有真机验证过**（红线 3），所以措辞停在「通常」并指回
    README 的手动路径，不许说满。
    """
    if state is None:
        return
    webcal_url = state.webcal_url
    if webcal_url is None:
        return
    print(
        f"iOS/macOS：通常可直接打开 {webcal_url} 完成订阅"
        "（真机未验证，失败就走 README 的手动路径）。"
    )


def _payload_size(payload: object) -> int:
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        return sum(_payload_size(v) for v in payload.values()) + len(payload)
    return 0


def _structure_report(payload: object, depth: int = 0, max_depth: int = 5) -> str:
    """生成脱敏的结构骨架 JSON 文本。"""
    from .parser import TimetableParser

    def skeleton(value: object, level: int = 0) -> object:
        if level >= max_depth:
            return f"<{type(value).__name__}>"
        if isinstance(value, dict):
            return {str(k): skeleton(v, level + 1) for k, v in value.items()}
        if isinstance(value, list):
            if not value:
                return {"__list_len__": 0}
            return {"__list_len__": len(value), "__item__": skeleton(value[0], level + 1)}
        if isinstance(value, str):
            return {"__type__": "str", "__len__": len(value)}
        return {"__type__": type(value).__name__}

    lines = [json.dumps(skeleton(payload), ensure_ascii=False, indent=2)]

    # 顺带给出解析器对每条记录的字段识别结果
    parser = TimetableParser()
    try:
        records = list(parser._iter_records(payload))
    except XjtuCalendarError as exc:
        lines.append(f"\n（解析器无法定位课程列表：{exc}）")
        return "\n".join(lines)

    lines.append(f"\n// 解析器识别到 {len(records)} 条候选记录")
    from .parser import FIELD_CANDIDATES

    for index, record in enumerate(records[:3]):
        if not isinstance(record, dict):
            continue
        lines.append(f"\n// --- 第 {index} 条的字段映射 ---")
        for field_name in FIELD_CANDIDATES:
            if field_name == "course_list":
                continue
            value = parser._get(record, field_name)
            if value is None:
                continue
            keys = [str(k) for k, v in record.items() if v is value]
            lines.append(f"// {field_name:12s} <- {keys!r}")

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def cmd_notice(args: argparse.Namespace, cfg: Settings) -> int:
    """抓取并解析学校停课/调课通知，产出可合并进学期配置的条目。

    fail-closed 契约（本命令的核心）：

    - 只要有任意一行无法可靠解析（``application.unresolved`` 非空），
      就**拒绝写盘**并报错退出，配置文件字节级不变；
      绝不「写进去一半」，产出一份半完整的校历。
    - 预览模式（不加 ``--apply``）仍然可用，但会明确标注
      「存在无法解析的行，当前结果不可直接应用」。
    """
    import json as json_module

    from .errors import XjtuCalendarError as _Err
    from .notices import (
        NoticeParseError,
        apply_notice,
        fetch_notice_html,
        merge_into_config,
        parse_teaching_notice,
    )

    if not args.url and not args.from_file:
        raise _Err(
            "需要提供通知来源",
            hint="用 --url 指定教务处通知页地址，或用 --from-file 读取本地保存的 HTML。",
        )

    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured(
            "未指定学期", hint="请用 --semester 指定，或设置环境变量 XJTU_SEMESTER"
        )
    config_path = cfg.semester_config_path(semester)
    if not config_path.is_file():
        raise SemesterNotConfigured(
            f"未找到学期 {semester} 的教学日历：{config_path}",
            hint="通知里的「第 N 周星期 X」必须用学期第一周周一做交叉校验，请先建好校历配置。",
        )

    # 正式领域模型是学期配置 schema 的唯一真源：
    # 这里刻意不自己解析 JSON 取 first_week_monday，否则 CLI 会与
    # AcademicCalendar 出现两套并行、迟早会漂移的解析逻辑。
    academic = AcademicCalendar.from_file(config_path)
    first_monday = academic.semester.first_week_monday

    if args.from_file:
        html = Path(args.from_file).read_text(encoding="utf-8")
        source = str(args.from_file)
    else:
        html = fetch_notice_html(str(args.url))
        source = str(args.url)

    table = parse_teaching_notice(html)
    application = apply_notice(table, first_monday)

    print(f"通知来源: {source}")
    print(
        f"学期: {semester}（{academic.semester.name}，第一教学周周一 {first_monday.isoformat()}）"
    )
    print()
    print(f"停课日（{len(application.excluded_dates)} 天）:")
    for day in application.excluded_dates:
        print(f"  {day.isoformat()}")
    print()
    print(f"调课（{len(application.makeups)} 条）:")
    for target, source_date in application.makeups:
        print(f"  {target.isoformat()} <- 按 {source_date.isoformat()}（来源教学日）课表上课")
    print()
    if application.unresolved:
        print(f"⚠️ 需人工确认（{len(application.unresolved)} 行，不会自动写入）:")
        for row in application.unresolved:
            print(f"  {row.date_text} | {row.week_text} | {row.arrangement}")
            print(f"    原因：{row.reason}")
        print()
    for note in table.notes:
        print(f"说明：{note}")
    print()

    if args.apply and application.unresolved:
        # fail-closed：宁可什么都不写，也不写一份半完整的校历。
        details = "\n".join(
            f"  {row.date_text} | {row.week_text} | {row.arrangement}（{row.reason}）"
            for row in application.unresolved
        )
        raise NoticeParseError(
            f"发现 {len(application.unresolved)} 行通知无法可靠解析，因此拒绝修改学期配置。\n"
            f"{details}\n"
            "请先人工确认或更新解析规则。",
            hint=f"学期配置未被改动（fail-closed）：{config_path}",
        )

    if not args.apply:
        proposed = {
            "excluded_dates（建议新增）": [d.isoformat() for d in application.excluded_dates],
            "overrides（建议新增）": {
                t.isoformat(): {"source_date": s.isoformat()} for t, s in application.makeups
            },
        }
        if application.unresolved:
            print(
                "⚠️ 存在无法可靠解析的行（见上），当前结果**不完整，不可直接应用**；"
                "请先人工确认或更新解析规则。"
            )
            print("（加 --apply 会因这些行而拒绝写入。）")
        else:
            print("以上为解析结果预览（未写入任何文件）。确认无误后加 --apply 合并进学期配置：")
        print(json_module.dumps(proposed, ensure_ascii=False, indent=2))
        return 0

    summary = merge_into_config(config_path, application, source_url=source)
    print("已合并进学期配置（只新增，现有条目未被覆盖）:")
    for item in summary["added_excluded"]:
        print(f"  + 停课日 {item}")
    for item in summary["added_makeups"]:
        print(f"  + 调课 {item}")
    for warning in summary["warnings"]:
        logger.warning("%s", warning)
    print(f"\n配置文件：{config_path}")
    return 0


def cmd_schedule(args: argparse.Namespace, cfg: Settings) -> int:
    """获取并解析教务处公开的「学生作息时间表」，预览或合并进作息表配置。

    fail-closed 契约（与 ``notice`` 同口径）：

    - 有无法归类的行 → ``--apply`` 整体拒绝写入；
    - 校历给不出学期末（``total_weeks`` / ``end_date`` 都没有）→ 拒绝并说明，
      **不猜**覆盖截止日期；
    - 合并只新增，现配置永远优先。
    """
    from .notices import NoticeParseError, fetch_notice_html
    from .schedule_notice import (
        DEFAULT_SCHEDULE_URL,
        merge_schedule_config,
        parse_schedule_page,
        plan_schedule,
    )

    if args.from_file:
        html = Path(args.from_file).read_text(encoding="utf-8")
        source = str(args.from_file)
    else:
        url = args.url or DEFAULT_SCHEDULE_URL
        html = fetch_notice_html(url)
        source = url

    notice = parse_schedule_page(html)

    def fmt(pair: tuple[str, str] | None) -> str:
        return f"{pair[0]}-{pair[1]}" if pair else "—"

    print(f"来源: {source}")
    if notice.summer_switch and notice.winter_switch:
        s, w = notice.summer_switch, notice.winter_switch
        print(f"切换点: 夏秋季 {s[0]}月{s[1]}日起 | 冬春季 {w[0]}月{w[1]}日起（取自列表头原文）")
    print()
    print("教学节次（节次 | 夏、秋季 | 冬、春季）:")
    for number in sorted(set(notice.summer) | set(notice.winter)):
        print(f"  第{number}节  {fmt(notice.summer.get(number))}  {fmt(notice.winter.get(number))}")
    print()
    if notice.notes:
        print(f"非教学行（仅作说明，共 {len(notice.notes)} 条，不写入配置）:")
        for item, summer_raw, winter_raw in notice.notes:
            print(f"  {item}: {summer_raw} / {winter_raw}")
        print()
    if notice.unresolved:
        print(f"⚠️ 需人工确认（{len(notice.unresolved)} 行，不会自动写入）:")
        for row in notice.unresolved:
            print(f"  {row.item_text} | {row.summer_text} / {row.winter_text}（{row.reason}）")
        print()

    semester = args.semester or cfg.semester_key

    if not args.apply:
        if notice.unresolved:
            print(
                "⚠️ 存在无法可靠归类的行，当前结果**不完整，不可直接应用**；--apply 会因此拒绝写入。"
            )
        else:
            print("以上为预览（未写入任何文件）。")
        print(
            "加 --semester <学期> --apply 可合并进 schedules/schedule.json："
            "生效区间的覆盖范围来自该学期校历。"
        )
        return 0

    if not semester:
        raise SemesterNotConfigured(
            "未指定学期",
            hint="--apply 需要学期校历确定生效区间覆盖范围：用 --semester 指定，"
            "或设置环境变量 XJTU_SEMESTER。",
        )
    calendar_path = cfg.semester_config_path(semester)
    if not calendar_path.is_file():
        raise SemesterNotConfigured(
            f"未找到学期 {semester} 的教学日历：{calendar_path}",
            hint="作息生效区间要用 first_week_monday 与学期末确定覆盖范围，请先建好校历配置。",
        )
    academic = AcademicCalendar.from_file(calendar_path)
    lower = academic.semester.first_week_monday
    _, upper = academic.semester.export_date_range()
    if upper is None:
        raise XjtuCalendarError(
            f"无法确定学期 {semester} 的学期末（校历既没有 total_weeks 也没有 end_date），"
            "拒绝猜测覆盖截止日期。"
        )

    if notice.unresolved:
        details = "\n".join(
            f"  {row.item_text} | {row.summer_text} / {row.winter_text}（{row.reason}）"
            for row in notice.unresolved
        )
        raise NoticeParseError(
            f"发现 {len(notice.unresolved)} 行无法可靠归类的作息条目，因此拒绝修改作息表配置。\n"
            f"{details}\n"
            "请先人工确认（新行类别需要先补充解析白名单，不做静默归类）。",
            hint="作息表配置未被改动（fail-closed）。",
        )

    plan = plan_schedule(notice, lower=lower, upper=upper)
    target = cfg.schedule_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    summary = merge_schedule_config(target, plan)

    print(
        f"已合并进作息表配置（只新增，现有条目未被覆盖；覆盖范围 "
        f"{lower.isoformat()} ~ {upper.isoformat()}）:"
    )
    for key in summary["added_profiles"]:
        print(f"  + 作息表 {key}")
    for item in summary["added_periods"]:
        print(f"  + 区间 {item}")
    if not summary["added_profiles"] and not summary["added_periods"]:
        print("  （无新增：配置已是最新，文件未被改写）")
    for warning in summary["warnings"]:
        logger.warning("%s", warning)
    print(f"\n配置文件：{target}")
    print("  已通过 ScheduleTable 校验；节次钟点以页面原文为准，如需修订请直接编辑该文件。")
    return 0


def cmd_diff(args: argparse.Namespace, cfg: Settings) -> int:
    """比对新旧课表快照与考试快照。

    默认比较「上一次 fetch」与「这一次 fetch」——``fetch`` 会在覆盖前把旧快照
    原子轮转成 ``*.prev.json``，所以正常用过两次 fetch 后本命令零参数可用。
    只 fetch 过一次时不猜、不假装成功，明确说明基线缺失以及如何补救。

    考试侧（设计文档 §6.6）走同一对轮转出来的快照（``raw/exams-*.json``），
    比对结果作为并列的「考试变更」小节输出；``--old/--new`` 只对课表生效。
    """
    from .diff import describe_periods, describe_slot, diff_meetings
    from .exams import (
        EXAM_KIND_ADDED,
        EXAM_KIND_CANCELLED,
        ExamDiff,
        campus_names_from_timetable,
        describe_exam_change,
        diff_exams,
        parse_exam_rows,
    )
    from .models import ExamSchedule
    from .parser import ParseReport, TimetableParser

    semester = args.semester or cfg.semester_key

    if args.old:
        old_path = Path(args.old)
    elif semester:
        old_path = cfg.raw_timetable_prev_path(semester)
    else:
        raise SemesterNotConfigured(
            "未指定学期",
            hint="diff 需要知道比较哪两份快照：用 --semester 定位 fetch 的缓存，"
            "或用 --old/--new 显式给文件。",
        )

    if args.new:
        new_path = Path(args.new)
    elif semester:
        new_path = cfg.raw_timetable_path(semester)
    else:
        new_path = old_path  # 不会走到：old 分支已要求 semester 或 --old

    def _read_json(path: Path, label: str) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise XjtuCalendarError(f"{label}快照不是合法 JSON：{path}（{exc}）") from exc

    def _load(path: Path, label: str) -> object:
        if not path.is_file():
            raise XjtuCalendarError(
                f"{label}课表快照不存在：{path}",
                hint="快照来自 fetch（每次 fetch 会把上一份轮转为 *.prev.json 作为比较基线）。"
                "刚 fetch 过一次还没有基线属正常；也可以先用 --old 指定一份之前保存的 raw JSON。",
            )
        return _read_json(path, label)

    old_parser = TimetableParser()
    new_parser = TimetableParser()
    old_payload = _load(old_path, "旧")
    new_payload = _load(new_path, "新")
    _, old_meetings = old_parser.parse(old_payload)
    _, new_meetings = new_parser.parse(new_payload)

    print(f"旧快照: {old_path}（{old_parser.report.summary()}）")
    print(f"新快照: {new_path}（{new_parser.report.summary()}）")
    for parser in (old_parser, new_parser):
        for reason in parser.report.skipped:
            logger.warning("解析跳过（可能影响比对完整性）：%s", reason)
    print()

    def _exam_rows(path: Path, label: str, campus: Mapping[str, str]) -> list[ExamSchedule]:
        rep = ParseReport()
        rows = parse_exam_rows(_read_json(path, label), campus_names=campus, report=rep)
        for reason in rep.skipped:
            logger.warning("%s快照解析跳过（可能影响比对完整性）：%s", label, reason)
        for warning in rep.warnings:
            logger.warning("%s快照：%s", label, warning)
        return rows

    def _diff_exam_snapshots() -> tuple[ExamDiff, str, str]:
        """比对考试快照，返回 ``(变更, 「本次没比对考试」的说明, 考试小节抬头)``。

        取数口径（§6.6:333-336）：考试侧**只有**默认路径这一种来源 —— 快照按学期存放
        （``raw/exams-<学期>.json`` 与其 ``.prev``），``--old/--new`` 只对课表生效。
        三种"没法比对"都要明说，不能拿一句「无变化」糊过去：没给学期、
        本地根本没有考试快照（从没抓过考试／状态未知时按宁缺毋滥没有落盘）、
        或考试快照读不出／不是合法 JSON（评审 F1：坏快照只跳过考试，不杀课程报告）。
        """
        if not semester:
            return (
                ExamDiff(),
                "本次未比对考试：--old/--new 只指定课表快照，考试快照按学期存放（需要 --semester）。",
                "",
            )
        new_exams_path = cfg.raw_exams_path(semester)
        if not new_exams_path.is_file():
            return (
                ExamDiff(),
                f"本次未比对考试：本地没有 {semester} 的考试快照"
                "（由 fetch 写入；本学期尚未排考时本来就没有）。",
                "",
            )
        prev_exams_path = cfg.raw_exams_prev_path(semester)
        first_snapshot = not prev_exams_path.is_file()
        # 两侧**共用同一份**校区对照（取自新的课表快照，§6.3）：各取各的话，两份课表快照里
        # XXXQDM_DISPLAY 的差别会变成一条根本不存在的「教室变更」。
        campus_names = campus_names_from_timetable(new_payload)
        try:
            new_exams = _exam_rows(new_exams_path, "新考试", campus_names)
            # 没有 .prev = 本学期**第一次**拿到考试快照：旧侧按空表比对，于是每行都报「新增」
            # （§6.6:336-337）。计划稿写的"任一侧缺失就打说明、返回空 diff"是错的 —— 那会让
            # diff 打出「无变化」，而日历里实实在在多出了一整批考试事件。
            old_exams: list[ExamSchedule] = (
                [] if first_snapshot else _exam_rows(prev_exams_path, "旧考试", campus_names)
            )
        except (XjtuCalendarError, OSError, UnicodeDecodeError) as exc:
            # 评审 F1（Important）：考试是**增量侧**，坏快照（读不出／非合法 JSON）只能跳过考试
            # 比对，绝不能像课程快照那样抛错杀掉整份报告 —— 上面一屏还是「缺快照 → 打一行说明、
            # 继续比课程」的口径，这里必须一致：走 exam_skip_note 通道、点名是考试快照坏掉，
            # 课程照常往下比、exit 0。
            return ExamDiff(), f"本次未比对考试：考试快照读取失败（{exc}）", ""
        preface = (
            "无上一份考试快照（本学期首次抓到考试安排），以下考试变更全部按新增报告。"
            if first_snapshot
            else ""
        )
        return diff_exams(old_exams, new_exams), "", preface

    result = diff_meetings(old_meetings, new_meetings)
    # 考试小节必须在 `result.is_empty` 短路**之前**取好（§6.6:327-331）：只改考试
    # （座位重排、换考场正是学期中最常见的事件）时课程侧为空，旧写法在打印任何小节
    # 之前就 return，考试变更一个字都不会出现。
    exam_diff, exam_skip_note, exam_preface = _diff_exam_snapshots()
    if exam_skip_note:
        print(exam_skip_note)
        print()

    if result.is_empty and exam_diff.is_empty:
        print("无变化：两份快照的课程、时段、周次、教室与教师完全一致。")
        return 0

    if result.added_courses:
        print(f"新增课程（{len(result.added_courses)} 门）:")
        for name in result.added_courses:
            print(f"  + {name}")
        print()
    if result.removed_courses:
        print(f"删除课程（{len(result.removed_courses)} 门）:")
        for name in result.removed_courses:
            print(f"  - {name}")
        print()
    if result.added_slots:
        print(f"新增上课时段（{len(result.added_slots)} 个）:")
        for slot in result.added_slots:
            print(f"  + {describe_slot(slot)}")
        print()
    if result.removed_slots:
        print(f"取消上课时段（{len(result.removed_slots)} 个）:")
        for slot in result.removed_slots:
            print(f"  - {describe_slot(slot)}")
        print()
    if result.changes:
        print(f"调整明细（{len(result.changes)} 项）:")
        for change in result.changes:
            print(
                f"  ~ {change.course_name} 星期{'一二三四五六日'[change.weekday - 1]}"
                f" {describe_periods(change.periods)}：{change.field}: "
                f"{change.old} → {change.new}"
            )
        print()

    if exam_diff.changes:
        if exam_preface:
            print(exam_preface)
            print()
        print(f"考试变更（{len(exam_diff.changes)} 项）:")
        # 记号与课程小节一致：+ 新增、- 取消、~ 改了某个字段。
        # 循环变量不能复用上面的 `change`：那个是 `SlotChange`，mypy strict 会把两个
        # 形状的字段名混在一起报错（课程与考试的变更记录**本来就不该共用一个名字**）。
        exam_markers = {EXAM_KIND_ADDED: "+", EXAM_KIND_CANCELLED: "-"}
        for exam_change in exam_diff.changes:
            marker = exam_markers.get(exam_change.kind, "~")
            print(f"  {marker} {describe_exam_change(exam_change)}")
        print()
        cancelling = [
            exam_change
            for exam_change in exam_diff.changes
            if exam_change.kind == EXAM_KIND_CANCELLED
        ]
        if cancelling:
            # spec D13 后半句：摘要行预告下次发布将撤销的条数——只数取消类变更，
            # 措辞用「将」：diff 是发布前的预览，此刻什么都没发生（§6.8）。这是**上界**：
            # 渲染侧的门槛（终审 F1/D18）、D20 基线可发性、D2 时刻已过都可能把它减到更少
            # 甚至 0，所以补一句「最多…以下次实际发布为准」，不把预览说成既定结果。
            print(
                f"本次将撤销：最多 {len(cancelling)} 条"
                "（已发布考试事件将在下次产物中标记为取消，以下次实际发布为准）"
            )
            print()
            # spec D13（docs/design/2026-10-09-exam-cancellation.md）：取消会以
            # STATUS:CANCELLED 真的下发；但一次性导入型客户端不回源，那半句照旧要说。
            # 术语与 README「已知边界」一致："不再回源"是一次性导入客户端的特征（它们
            # 恰恰不会自动移除）；会自动移除的是会定期回源拉取的订阅客户端——实机表现
            # 未验证（spec §11/D5），所以只说"通常会"，不把话说满。
            print(
                "注意：本次 diff 报出的取消会在下次 export/subscribe push 时以 "
                "STATUS:CANCELLED 下发；会定期回源拉取的订阅客户端通常会随之移除，"
                "而一次性导入后不再回源的客户端（部分国产 ROM 系统日历）仍需手动删除。"
            )
            print()

    print("提示：确认无误后重新 export 即可拿到更新后的 .ics（UID 稳定，原地更新）。")
    return 0


# --------------------------------------------------------------------------- #
# subscribe（URL 订阅发布）
# --------------------------------------------------------------------------- #
def _require_git() -> None:
    """git 可用性前置探测（spec §7，探测风格同 ``config.find_browser``：which）。

    init/push/rotate 都会调用 git 子进程（check-ref-format、ls-remote、强推）；
    git 缺失时 ``subprocess.run`` 抛 ``FileNotFoundError`` traceback，必须在这里
    先拦成业务错误。``status`` 纯本地读 + HTTP 自检，不触碰 git，不必拦。
    """
    if shutil.which("git") is None:
        raise GitNotAvailable("未在 PATH 中找到 git 可执行文件")


def _validate_publish_branch(branch: str) -> None:
    """任何 publish 之前必须先验分支名（git check-ref-format，退出码判定）。

    空/垃圾分支若放过去，会带进 ls-remote / fetch refspec / 强推的参数拼接，
    不同 git 版本行为不一——必须在这里以业务错误拦下，绝不触碰远端。
    """
    proc = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise XjtuCalendarError(
            f"发布分支名不合法：{branch!r}",
            hint="分支名须符合 git 规则（非空，不含空格与 ~^:?*[\\ 等）。"
            "重新 subscribe init --branch cal-<名字>，或修正状态文件后重试。",
        )


def _load_subscribe_state(cfg: Settings, semester: str) -> subscribe.SubscriptionState | None:
    """读取订阅状态文件；损坏时以业务错误呈现，而不是把解码异常甩给用户。

    load_state 只做 `json.loads` + `from_dict`：非法 JSON（ValueError）、顶层
    类型不对（AttributeError/TypeError）、缺键（KeyError）都会原样上抛。
    """
    from . import subscribe

    try:
        return subscribe.load_state(cfg, semester)
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise XjtuCalendarError(
            f"学期 {semester} 的订阅状态文件已损坏：{subscribe.state_path(cfg, semester)}",
            hint=f"删除该文件后重新 subscribe init --semester {semester}（会换新 token）。",
        ) from exc


def cmd_subscribe(args: argparse.Namespace, cfg: Settings) -> int:
    """subscribe 子命令入口：学期解析 + init / push / rotate / status 分发。

    学期口径与 cmd_export/cmd_diff 一致：``--semester`` 优先，其次配置默认值。
    """
    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured("未指定学期", hint="用 --semester 指定，或设置 XJTU_SEMESTER。")

    if args.action != "status":
        # init/push/rotate 都会调用 git；status 不触碰 git（纯本地 + HTTP 自检）。
        _require_git()

    if args.action == "init":
        return _subscribe_init(args, cfg, semester)

    state = _load_subscribe_state(cfg, semester)
    if state is None:
        raise SubscribeNotConfigured(
            f"学期 {semester} 尚未登记订阅",
            hint=f"先运行：xjtu-calendar subscribe init --repo <URL> --semester {semester}",
        )

    if args.action == "push":
        return _subscribe_push(args, cfg, state, semester)
    if args.action == "repair":
        return _subscribe_repair(cfg, state, semester)
    if args.action == "rotate":
        return _subscribe_rotate(
            cfg,
            state,
            semester,
            include_exams=not args.no_exams,
            include_calendar_events=getattr(args, "include_calendar_events", False),
            # Task 9 已在 rot_p 上声明该旗标；`getattr` 保留为对直接调用方的兜底，缺省即 False
            # （门 2 默认收紧）。
            allow_mass_cancellation=getattr(args, "allow_mass_cancellation", False),
            # P0-01：相容性那道门与 push 同判据，旗标也必须两侧都接得住（缺省收紧）。
            allow_unverified_baseline=getattr(args, "allow_unverified_baseline", False),
        )
    return _subscribe_status(args, cfg, state, semester)


def _subscribe_init(args: argparse.Namespace, cfg: Settings, semester: str) -> int:
    from . import subscribe

    if _load_subscribe_state(cfg, semester) is not None:
        raise XjtuCalendarError(
            f"学期 {semester} 已登记过订阅",
            hint="rotate 换 token，或直接 push；重新登记请先删除 "
            f"{subscribe.state_path(cfg, semester)}。",
        )
    _validate_publish_branch(args.branch)
    url_base = args.url_base or subscribe.derive_url_base(args.repo)
    if not url_base:
        raise XjtuCalendarError(
            f"无法从 repo 推导 Pages 地址：{args.repo}",
            hint="非 GitHub 远端请用 --url-base 显式给出 .ics 的公开访问前缀。",
        )
    state = subscribe.SubscriptionState(
        semester=semester,
        repo_url=args.repo,
        branch=args.branch,
        token=subscribe.new_token(),
        url_base=url_base,
    )
    subscribe.save_state(cfg, state)
    print(f"订阅 URL：{state.subscription_url}")
    _print_webcal_hint(state)
    print()
    _print_hosting_hint(state)
    print("⚠️ 知道该 URL 的人即可读取你的课表；有泄露疑虑时运行 subscribe rotate。")
    return 0


def _print_hosting_hint(state: subscribe.SubscriptionState) -> None:
    """init 收尾的「下一步怎么对外服务」两行，按 `url_base` 落在哪个托管方分支。

    旧实现无条件打印 GitHub Pages 的操作指引，而路径 B（私有仓 + Cloudflare Pages，
    `docs/user-guide/subscription.md`「先选一条托管路」）照着做会走进死路：GitHub Pages
    对免费账户的私有仓不提供托管。判据只看 URL 前缀本身，不看仓库是否私有 ——
    工具无从知道可见性，而 `*.github.io` 这个前缀恰好只有路径 A 会用。
    其余前缀（`pages.dev`、自建托管、`file://` 彩排远端）一律给中性的「交给你的托管方」，
    不猜具体控制台路径（各托管方的界面本项目只在迁移当天实测过 Cloudflare 那一条）。
    """
    host = urlparse(state.url_base).hostname or ""
    if host.endswith(".github.io"):
        print("一次性开启 Pages（GitHub）：仓库 Settings → Pages → Deploy from branch，")
        print(f"分支选 {state.branch}、目录选 /(root)。完成后运行 subscribe push。")
        # 2026-10-24：文档把私有仓 + Cloudflare Pages 定为推荐路，而 `derive_url_base`
        # 只看远端是不是 GitHub、不看可见性 ⇒ 漏了 `--url-base` 的那次 init 会静默推出
        # 这条前缀，然后教用户去开一个开也开不起来的 Pages。加警告，不改判据、不拒绝：
        # 路径 A 仍是公开仓用户的正解，工具无从知道仓库到底公不公开。
        print()
        print("⚠️ 上面这两行只对公开仓库成立：GitHub Pages 对免费账户的私有仓库不提供托管，")
        print("   私有仓上这条订阅 URL 是 404（而 init 不会为此报错）。想让发布仓库保持私有，")
        print("   重新 init 时带 --url-base（例如 https://<项目名>.pages.dev/），")
        print("   两条路的代价与步骤见 docs/user-guide/subscription.md 的「先选一条托管路」。")
        return
    print("下一步：把这个发布分支交给你的托管方对外服务（GitHub Pages 只在公开仓上提供托管）。")
    print(
        f"分支 {state.branch}、目录 /(root)；两条托管路的代价与步骤见 "
        "docs/user-guide/subscription.md 的「先选一条托管路」。完成后运行 subscribe push。"
    )


def _restore_from_journal(
    cfg: Settings,
    journal: subscribe.PublishJournal,
    semester: str,
) -> list[str]:
    """按发布恢复快照把本地三样文件对齐到"最后一次真正发出去的版本"，返回动过的名字。

    已经是目标字节的那一份**原样不动**：这让函数天然幂等（恢复中途再崩一次，第二次
    只会补剩下的），也不会把用户手改过的东西重新盖一遍写痕，还让 `repair` 能如实说
    "无需恢复"。台账的 `keep` 语义在这里生效：快照记 ``None`` ⇒ 那次发布本就不该动这份
    台账，跳过而不是写空文件（写空 = 清空整份考试账本）。
    """
    from . import subscribe

    restored: list[str] = []
    targets: list[tuple[str, Path, str]] = [
        ("留底", subscribe.subscribe_dir(cfg) / f"last-{semester}.ics", journal.ics_text),
    ]
    if journal.course_ledger_text is not None:
        targets.append(("课程台账", cfg.course_ledger_path(semester), journal.course_ledger_text))
    if journal.exam_ledger_text is not None:
        targets.append(("考试台账", cfg.exam_ledger_path(semester), journal.exam_ledger_text))
    for label, path, want in targets:
        if path.is_file() and path.read_bytes() == want.encode("utf-8"):
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        # newline=""：留底/台账都必须与快照逐字节一致（CRLF 完整），否则下次的 sha
        # 比对与 SEQUENCE 基线读取都会错位。
        atomic_write_text(path, want, private=True)
        restored.append(label)
    return restored


def _subscribe_repair(cfg: Settings, state: subscribe.SubscriptionState, semester: str) -> int:
    """`subscribe repair`：以**远端真实字节**为准，把本地对齐回线上那一版。

    四条契约（2026-10-14 审查 + 复核对齐）：
    ①不只看本地——必须从发布分支拉回真实字节，与凭据的 sha 对上才动手；
    ②可重复执行（幂等），中途再崩一次，第二次照样能续上；
    ③没有凭据就**不假装能反推**——产物里课程与考试事件长得一样（CD8），从 ICS 推不出
    两份台账，此时只报判据、给手工出口，绝不静默重置订阅历史；
    ④**本地有两份凭据时由远端裁决**（审阅 T1/T2）：已发布凭据（committed）之外可能还留着
    一份准备稿（pending，死在推送途中）。远端等于准备稿 ⇒ 那一版其实发出去了，提升并恢复；
    远端等于已发布凭据 ⇒ 准备稿从未落地，安全丢弃；两者都不是 ⇒ 冲突，拒绝自动操作。
    同时把状态文件里的「上次发布记录」一并修对——只恢复文件不修元数据，`doctor`/`status`
    会长期报告错误的已发布版本。
    """
    from . import subscribe

    committed = subscribe.read_journal(cfg, semester)
    pending = subscribe.read_journal(cfg, semester, pending=True)
    if committed.status == subscribe.JOURNAL_INVALID or pending.status == subscribe.JOURNAL_INVALID:
        raise XjtuCalendarError(
            "本地发布凭据读不懂，无法据此恢复",
            hint="快照在 `subscribe/` 下（committed 与 pending 两份）。读不懂说明它被截断或改坏："
            "可从订阅 URL 下载当前 .ics 原样放回留底路径，其余两份台账无法凭空重建；"
            "确认可以接受全新订阅时删除状态文件与快照后重新 subscribe init（会清空客户端历史）。",
        )
    committed_j = committed.journal
    pending_j = pending.journal
    candidates = [j for j in (committed_j, pending_j) if j is not None]
    if not candidates:
        raise XjtuCalendarError(
            "本地没有发布恢复快照，无法自动对齐",
            hint=f"快照自 0.8.5 起随每次发布写入（{subscribe.journal_path(cfg, semester)}）。"
            "更早的发布没有依据：可从订阅 URL 下载当前 .ics 原样放回留底路径，"
            "或确认接受全新订阅后删除状态文件重新 subscribe init（会清空客户端历史）。",
        )
    # token 判据放宽到"**任一份**凭据属于当前订阅"（P0-01）：`rotate` 崩在推送途中时，
    # 已发布凭据必然是旧 token、准备稿才是新 token，按旧实现那句 `journal.token !=
    # state.token` 会在唯一需要它的场合直接拒绝。两凭据都属于别的 token 才叫无从恢复。
    journal = next((j for j in candidates if j.token == state.token), None)
    if journal is None:
        raise XjtuCalendarError(
            "发布恢复快照的 token 与当前订阅不一致，拒绝据此恢复",
            hint="多半是 rotate 之后的残留快照。确认 state 与远端分支后重跑一次 subscribe push。"
            "确认可接受全新订阅时删除状态文件与快照后重新 subscribe init。",
        )

    remote_name, remote = subscribe.fetch_published_file(cfg, state, semester)
    remote_sha = hashlib.sha256(remote).hexdigest()
    notes: list[str] = []
    pending_matches = pending_j is not None and remote_sha == pending_j.ics_sha256
    committed_matches = committed_j is not None and remote_sha == committed_j.ics_sha256
    # 「远端等于哪一版」先看字节，字节平票时**用文件名破平**（P0-01）：`rotate` 的补发
    # 经常内容一字未改、只换了文件名，此时准备稿与已发布凭据哈希相同，单比字节会把
    # "新文件名的那一版已经上线"误判成"准备稿从未落地"，用户只好再推一次同样的字节。
    pending_is_remote = bool(
        pending_j is not None
        and remote_sha == pending_j.ics_sha256
        and remote_name == f"{pending_j.token}.ics"
    )
    if pending_matches and (not committed_matches or pending_is_remote):
        assert pending_j is not None
        # 远端 == 准备稿 ⇒ 那一版其实发出去了，只是没登记（死在 save_state / 提升之前）。
        # 提升它，本地就有线上那一版的完整凭据（审阅 T2）。
        promoted = subscribe.promote_pending(cfg, semester)
        if promoted is None:
            raise XjtuCalendarError("准备稿提升为已发布凭据失败，未做任何改动")
        journal = promoted
        notes.append("线上那一版正是没登记过的准备稿，已确认为已发布并补上凭据")
    elif committed_matches:
        # 远端仍是已发布凭据 ⇒ 本地那份准备稿从未落地，丢掉它（审阅 T1）。
        assert committed_j is not None
        journal = committed_j
        if pending_j is not None:
            subscribe.discard_pending(cfg, semester)
            notes.append("丢弃从未发出的准备稿")
    else:
        raise XjtuCalendarError(
            f"线上那版与恢复凭据不符（远端 {remote_name[:8]}… {remote_sha[:12]}… ≠ 凭据 "
            f"{journal.ics_sha256[:12]}…），拒绝恢复",
            hint="凭据没对上，说明中断点不在本地：先 subscribe status 核对，"
            "确认可接受全新订阅时删除状态文件后重新 subscribe init。",
        )

    restored = _restore_from_journal(cfg, journal, semester)
    new_state = state
    if (
        new_state.last_push is None
        or new_state.last_push.content_sha256 != journal.ics_sha256
        # token 也在判据里（P1-05 同一口径）：rotate 崩过一次的现场，字节可能一字未改而
        # 文件名已换新 token——只比哈希会把"上次发布记录"留在旧 token 上，与刚提升的
        # 凭据不符，收尾复核立刻命中 `journal_token`，repair 自己把自己判成失败。
        or new_state.last_push.token != journal.token
    ):
        # 已发布状态元数据本身也得修对：只恢复文件不修它，`doctor`/`status` 会长期报错版本。
        # 时刻取凭据自己的 created_at（准备发布那一刻，与真实推送只差几秒），不假装知道精确时间。
        new_state = replace(
            new_state,
            last_push=subscribe.PushRecord(
                pushed_at=journal.created_at,
                content_sha256=journal.ics_sha256,
                token=journal.token,
            ),
        )
        restored.append("上次发布记录")
    if journal.uid_recipe is not None and new_state.uid_recipe != journal.uid_recipe:
        new_state = replace(new_state, uid_recipe=journal.uid_recipe)
        restored.append("迁移标记")
    if new_state is not state:
        subscribe.save_state(cfg, new_state)
    if not restored and not notes:
        print("本地已与线上一致，无需恢复。")
    else:
        # 只丢弃了一份过期准备稿、什么都没补的场合也必须说清楚：静默动过本地文件
        # 是这一整套机制最不该出现的样子。
        if restored:
            print(f"已按发布恢复快照对齐：{'、'.join(restored)}（远端字节校验通过）")
        for note in notes:
            print(f"· {note}。")
    # 收尾必须用**同一套**判定复核一遍（2026-10-14 审阅复核第 3 条）：`keep` 只记指纹不记内容，
    # 那份被保留的台账要是被删了，repair 没有内容可以凭空重建。此时若报"已与线上一致"，
    # 就是把"无法恢复"说成"相容"——用户会以为修好了，回头 push 照样被拦。
    leftover = subscribe.check_local_compatibility(cfg, new_state, semester)
    if not leftover.ok:
        raise XjtuCalendarError(
            f"仍有本地状态无法恢复（{leftover.detail}）",
            hint="恢复快照对『这次不该动』的那份台账只记指纹、不记内容，删掉的内容补不回来："
            "从备份或仓库历史找回原文件；确认可以接受丢失这段撤销历史时，"
            "带 subscribe push --allow-unverified-baseline 继续（本次会同时补回恢复依据）。",
        )
    logger.info("订阅本地状态处理完毕；未产生任何新的发布。")
    return 0


def _require_compatible_local_state(
    cfg: Settings,
    state: subscribe.SubscriptionState,
    semester: str,
    *,
    allow_unverified: bool,
) -> subscribe.CompatibilityCheck:
    """构建之前先查"本地四样是否与最后一次已发布版本相容"（`push` 与 `rotate` 同一道门）。
    发布路径要同时推进远端 ICS、本地留底、两份台账与迁移标记，它们**不在一个原子域里**；
    死在中途的话：
    ①留底落后而数据又变了 ⇒ 这里拿过期留底当 SEQUENCE 基线，产出的版本号从起点就错；
    ②留底新、课程台账旧 ⇒ `cancel_candidates` 的差集是「台账 − live」，那条 UID 根本不
    成候选，退课**静默漏发**。两条都是 2026-10-14 外部审查补验、已用回归测试钉住的。
    """
    from . import subscribe

    compat = subscribe.check_local_compatibility(cfg, state, semester)
    if not compat.ok and not (allow_unverified and compat.bypassable):
        raise PublishStateIncompatible(
            f"本地发布状态不相容（{compat.detail}）",
            hint=(
                "先跑 xjtu-calendar subscribe repair --semester "
                f"{semester}：它按发布恢复快照核对线上那一版，再把留底/台账/迁移标记对齐。"
                + (
                    ""
                    if compat.reason
                    not in {"no_journal", "journal_token", "journal_sha", "journal_branch"}
                    else " 这一格**没有可用的恢复依据**：快照缺失、属于别的 token，"
                    "或与你确认过的线上那一版对不上。repair 同样核不出该按哪一版对齐："
                    "确认远端就是状态里记的那一版后，带 --allow-unverified-baseline 继续，"
                    "本次会把恢复依据重新落下。"
                )
                + (
                    ""
                    if compat.bypassable
                    else " 这一格**不在旗标的放行范围内**：`--allow-unverified-baseline` "
                    "不能替你裁决上一次发布的结果，也不能把读不懂的凭据当作没有凭据。"
                )
            ),
        )
    if not compat.ok:
        logger.warning(
            "本地发布状态不相容（%s），已按 --allow-unverified-baseline 强行继续："
            "本次的 SEQUENCE 基线与撤销判定可能建立在过期留底上。",
            compat.detail,
        )
    elif compat.reason == "no_snapshot_verified":
        # 升级路径（0.8.5 之前发布过的状态）：留底与发布记录逐字节相符 ⇒ 放行，
        # 但必须说清"台账与迁移标记这次没能校验"，别让人以为四样都查过了。
        logger.warning(
            "本地没有发布恢复快照，按弱判据放行：留底字节与最后一次发布记录相符，"
            "但两份台账与迁移标记未能校验；本次会在推送前把恢复依据补落。"
        )
    elif compat.reason == "ledger_pruned_by_time":
        # 门自己放行的一类差异（台账按设计剪掉已过时刻的行，而 export 与发布共用它）。
        # 「自动但不静默」：放行要说一声，否则用户以为四样都原样对上了。
        logger.warning(
            "%s —— 这不是状态散架，不必 repair；跑过 `export` 或过了几节课都会这样。",
            compat.detail,
        )
    return compat


def _resolve_revive_requests(cfg: Settings, semester: str, requested: list[str]) -> list[str]:
    """把 `--revive` 点名的课换算成 `stable_course_key`，认不出来就拒绝发布。

    接受两种写法：课程号（`course_id`，也就是 `stable_course_key` 本身），或课程名
    （`course_id` 缺失时配方退回 `name:课程名`，CD16 —— 那种行对改名敏感，所以按名字
    点名是有意支持的，不是兜底）。

    刻意**不静默忽略**认不出的值：用户点名的就是一节课，悄悄什么都不做比报错危险。
    """
    if not requested:
        return []
    path = cfg.raw_timetable_path(semester)
    if not path.is_file():
        raise XjtuCalendarError(
            f"要用 --revive 就得先有本学期的课表快照：{path} 不存在",
            hint="先运行 fetch --semester " + str(semester) + "，再带 --revive 重跑 push。",
        )
    from .parser import TimetableParser

    payload = json.loads(path.read_text(encoding="utf-8"))
    parser = TimetableParser()
    _, meetings = parser.parse(payload)
    keys = {m.stable_course_key for m in meetings}
    by_name = {m.course_name.strip(): m.stable_course_key for m in meetings if m.course_name}

    resolved: list[str] = []
    unknown: list[str] = []
    for raw in requested:
        want = raw.strip()
        if want in keys:
            resolved.append(want)
        elif want in by_name:
            resolved.append(by_name[want])
        else:
            unknown.append(want)
    if unknown:
        raise XjtuCalendarError(
            f"--revive 点名的课在本学期课表里找不到：{'、'.join(sorted(unknown))}",
            hint="可点名的值来自当前快照：先跑 diff 或看课表里的课程号；"
            "已经不在课表上的课不需要 revive —— 它本来就该以撤销形态退场。",
        )
    # 同一门课点两次只算一次（盐值只 +1）
    return sorted(set(resolved))


def _revived_salts(state: subscribe.SubscriptionState, keys: list[str]) -> dict[str, int]:
    """在现有计数器上给点名的课各 +1，返回**新**的盐值表（不改传入的 state）。"""
    salts = dict(state.revive_salts)
    for key in keys:
        salts[key] = salts.get(key, 0) + 1
    return salts


def _publish_with_journal(
    cfg: Settings,
    state: subscribe.SubscriptionState,
    semester: str,
    result_ics: ExportResult,
    migrating: bool,
    revive_salts: dict[str, int] | None = None,
) -> subscribe.PublishResult:
    """`push` 与 `rotate` **共用**的那一条发布事务（2026-10-15 审阅 P0-01）。

    ``revive_salts``：本次构建实际用的重新编号表（误删恢复，§16）。**只有发布成功才回写**
    进状态文件 —— 中途被门拦下时计数器不许前进，否则下一次构建的 UID 与"最后一次已发布
    版本"对不上，凭空多一次重新编号。传 ``None`` 表示这次没有新的编号请求。

    顺序就是全部契约（spec §5 不变量 1—3）：准备稿 → 推送 → 提升已发布凭据 → 回写本地
    四样。rotate 过去直接调 `publish()` 再自己回写，等于绕过前两步：换 token 补发成功后
    `last_push.token` 已经是新的，凭据还停在旧 token ⇒ 下一次 `push` 被自己的
    `journal_token` 拦死（7），`repair` 又拒绝恢复，正常操作把订阅永久锁住。
    收尾补一份快照也**不够**——那解决不了"推送成功、本地写入失败"的崩溃窗口，
    必须让准备稿先落盘、由 `subscribe repair` 拿远端字节裁决。

    返回 `PublishResult` 让调用方按各自措辞打印（`NO_CHANGE` 只有 push 走得到：
    rotate 刚换 token，`publish` 的跳过条件要求 token 一致，必然重推）。
    """
    from . import subscribe

    # 推送之前先落「发布准备稿」（pending）：这次要发的 ICS + 两份台账的目标字节 +
    # 迁移标记。写不进去就不该开始推送——宁可远端停在上一版，也不要发一次事后无法恢复的发布。
    # 台账为 ``None`` 时记成"这次别动"（`--no-exams` 那次绝不该清空考试台账），并留下它
    # 当时的字节指纹，日后被删/被改照样能发现。**这里不推进已发布凭据（committed）**：
    # 那份只能代表远端真正收下过的版本（审阅 P1-①）。
    subscribe.write_journal(
        cfg,
        semester=semester,
        token=state.token,
        branch=state.branch,
        ics_text=result_ics.ics,
        course_ledger_text=result_ics.course_ledger_text,
        exam_ledger_text=result_ics.exam_ledger_text,
        uid_recipe=UID_RECIPE_VERSION if migrating else state.uid_recipe,
    )
    try:
        res = subscribe.publish(cfg, state, result_ics.ics)
    except Exception as exc:
        # 只有"能证明还没执行到 push"时才丢准备稿。`git push` 返回非零也可能是远端已更新、
        # 响应没回到客户端，或 push 之后 `save_state` 抛错——那些情况下远端**可能**已经是新版，
        # 把准备稿留着让 `subscribe repair` 拿远端字节裁决（审阅的 T1/T2 表格）。
        if getattr(exc, subscribe.REMOTE_UNCERTAIN_ATTR, False):
            logger.warning(
                "发布中断且远端可能已经推进，准备稿已保留：%s —— "
                "subscribe repair 会用线上真实字节判定这一版究竟发出去了没有。",
                subscribe.pending_path(cfg, semester),
            )
        else:
            subscribe.discard_pending(cfg, semester)
        raise
    if res.outcome is subscribe.PublishOutcome.NO_CHANGE:
        subscribe.discard_pending(cfg, semester)
        # 这里**不**再顺手补写本地文件（v0.8.4 那版自愈的形态）：构建之前的相容性检查
        # 已经保证"本地四样 == 恢复凭据 == 线上那一版"，走到 NO_CHANGE 时没有东西可补；
        # 真落后就是检查那一头拒绝（退出码 7）并让用户跑 `subscribe repair`。
        # 判据只留一处，D11「NO_CHANGE 不回写台账」也因此继续成立。
        return res
    # 远端确实收下了这一版 ⇒ 先把准备稿提升为已发布凭据，**再**落本地文件。
    # 顺序要紧：反过来一旦死在中间，留底会比凭据新，`repair` 拿远端一比对就发现凭据落后、
    # 拒绝恢复，只剩人工出口。提升失败不推翻发布——远端已是新版，准备稿还在，
    # 下一次 `subscribe repair` 能把它认出来并补做这一步。
    if subscribe.promote_pending(cfg, semester) is None:
        logger.warning(
            "已发布凭据没能推进（准备稿仍留在 %s）：远端已经是新版，请跑一次 "
            "subscribe repair 让本地跟上，否则下次发布会被相容性检查拦住。",
            subscribe.pending_path(cfg, semester),
        )
    last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
    # newline=""：留底必须与远端产物字节一致（CRLF 完整），否则下次把它当
    # SEQUENCE 基线读回、以及 status 的 sha 比对都会错位（同 cmd_export）。
    last_local.write_text(result_ics.ics, encoding="utf-8", newline="")
    # 台账与留底**同一时刻**更新（spec D11）：publish 失败或 NO_CHANGE 都在上面
    # 提前离开，走不到这里；`None`（门槛没过 / --no-exams）不创建也不改写文件。
    # 写失败只 warning（D24①），不改 push 的退出码——但**不再是无人发现的失配**：
    # 快照记着这次应当落什么，下次 push 的相容性检查会拦住、`subscribe repair` 能补回。
    if result_ics.exam_ledger_text is not None:
        _write_exam_ledger(cfg, semester, result_ics.exam_ledger_text)
    if result_ics.course_ledger_text is not None:
        _write_course_ledger(cfg, semester, result_ics.course_ledger_text)
    # 迁移标记是这一串回写的**最后一步**（CD7/§5 不变量 3）：发出去才算迁移过。
    # 判据只看状态文件（不猜 UID），所以「没有留底 ⇒ 没有旧 UID 可撤」的第一次成功发布
    # 也要写上标记（§7：直接 `uid_recipe: 2`、无撤销），否则下次 push 还会重走迁移分支。
    updated = replace(state, uid_recipe=UID_RECIPE_VERSION) if migrating else state
    if revive_salts is not None and revive_salts != state.revive_salts:
        updated = replace(updated, revive_salts=revive_salts)
    if updated is not state:
        subscribe.save_state(cfg, updated)
    return res


def _require_calendar_events_admissible(
    cfg: Settings,
    semester: str,
    *,
    include_calendar_events: bool,
    baseline_text: str | None,
) -> None:
    """校历事件那两道发布门，**排在构建之前**（§12.2 与 §12.4）。

    为什么必须在构建前：发布路径要同时推进远端 ICS、本地留底、两份台账与凭据，
    拒绝就必须一行都不落。而判据读的是**学期配置 + 上次发布的留底**，
    与构建的产物无关，所以能独立成立。

    判据本体在 :func:`xjtu_calendar.calendar_events.publish_refusal_reasons`（纯函数），
    这里只做三件事：取证据、把理由码翻成人话、抛退出码 ``8``。
    文案遵守隐私 §8：只有序号、字段名、原因码与计数，**不打事件标题与来源 URL 的值**。
    """
    from .calendar_events import publish_refusal_reasons

    path = cfg.semester_config_path(semester)
    if not path.is_file():
        # 没有学期配置：这一格没什么可判的，让构建去报它那个更准的错
        # （`SemesterNotConfigured` 的文案带着模板指路，比这里复读一句强）。
        return
    academic = AcademicCalendar.from_file(path)
    config = academic.calendar_events

    published_uids: set[str] = set()
    if baseline_text:
        with contextlib.suppress(Exception):
            # 留底读不通不算校历错误：相容性那道门（退出码 7）专门管这件事。
            published_uids = set(parse_baseline(baseline_text))

    reasons = publish_refusal_reasons(
        config,
        semester_key=academic.semester.key,
        include_calendar_events=include_calendar_events,
        published_uids=published_uids,
    )
    if not reasons:
        return

    lines: list[str] = []
    if "config_errors" in reasons:
        lines.append(f"校历事件配置有 {len(config.errors)} 条错误：")
        lines.extend(f"  {error.describe()}" for error in config.errors)
        lines.append("  预览与独立 export 会带着其余好事件继续出，但**正式发布整体拒绝**——")
        lines.append("  校历事件不接撤销通路，配置写错一条就是线上少一条，且恢复不回来。")
    if "unresolved_conflicts" in reasons:
        unresolved = sum(1 for c in config.conflicts if not c.resolved)
        lines.append(
            f"有 {unresolved} 组校历来源冲突尚未裁决（conflicts 段里缺 adjudicated_by）："
            "两边都不生成事件，也不许自动择一，所以这次整体拒绝发布。"
        )
    if "flag_missing_after_publish" in reasons:
        lines.append("上一次发布的产物里含校历事件，而本次没有带 --include-calendar-events。")
        lines.append(
            "漏写一个旗标**不等于**"
            "「你要把校历事件全删掉」——这些事件不接撤销通路，"
            "直接从 feed 消失的话，客户端会不会清掉旧条目完全看它的脾气。"
        )
        lines.append("  想继续带上它们：本次命令加 --include-calendar-events；")
        lines.append("  确实不要了：把学期配置里的 events 条目删掉（那是明确选择，本门放行）。")
    raise CalendarEventsBlocked(
        "校历事件的发布准入未通过，本次不发布（远端与本地状态一字未动）：\n" + "\n".join(lines),
        hint=f"改完配置后重跑同一条命令；体检可先看状态：xjtu-calendar doctor --semester {semester}",
    )


def _subscribe_push(
    args: argparse.Namespace, cfg: Settings, state: subscribe.SubscriptionState, semester: str
) -> int:
    from . import subscribe

    _validate_publish_branch(state.branch)
    age = subscribe.snapshot_age_days(cfg, semester)
    if age is not None and age > 7:
        logger.warning("raw 快照已 %.0f 天未更新，建议先 fetch 再 push（本次继续）", age)
    # 留底 last-<semester>.ics：publish 成功后才更新，且是下次构建的 SEQUENCE 基线。
    last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
    if state.last_push is not None and not last_local.is_file():
        # 有上次发布记录却没了留底 = 没有 SEQUENCE 基线。放任重建的话所有事件
        # 打回 SEQUENCE:0，客户端把整份课表当新日历重新收，旧事件历史被静默重置。
        # fail-closed：拒绝重建，让用户先找回留底（远端上就有）。
        raise XjtuCalendarError(
            f"本地留底缺失：无法安全重发布（{last_local} 不存在，但已有过成功推送）",
            hint="没有留底作基线，重建会把全部事件重置为 SEQUENCE:0，客户端历史被清空。"
            "请从远端订阅 URL 下载当前 .ics 原样放回上述路径后重试；"
            f"确认可接受全新订阅的话，删除 {subscribe.state_path(cfg, semester)} "
            "后重新 subscribe init。",
        )
    # 构建之前那道门，与 rotate 共用一个函数（P0-01 之后两条发布路径口径必须一致）。
    _require_compatible_local_state(
        cfg, state, semester, allow_unverified=getattr(args, "allow_unverified_baseline", False)
    )
    # 迁移判据只有一处：状态文件里 `uid_recipe` 是否缺失（CD7）。留底文本是候选的唯一
    # 来源（v1 配方代码已删除，CD15 ⇒ 不重算旧 UID，只从上次发布的产物里读）。
    migrating = subscribe.needs_uid_migration(state)
    baseline_text = last_local.read_text(encoding="utf-8") if last_local.is_file() else None
    # 校历事件那道准入门（§12.2 / §12.4）：读同一份留底，排在构建之前。
    _require_calendar_events_admissible(
        cfg,
        semester,
        include_calendar_events=getattr(args, "include_calendar_events", False),
        baseline_text=baseline_text,
    )
    # 误删恢复（§16）：把 --revive 点名的课在计数器上 +1，本次构建就带新盐值。
    # 只改这几门课的 UID，其余逐字节不变 —— 这条不变量由用例钉住。
    revive_keys = _resolve_revive_requests(cfg, semester, list(getattr(args, "revive", []) or []))
    revive_salts = _revived_salts(state, revive_keys)
    if revive_keys:
        print(
            f"重新编号 {len(revive_keys)} 门课（误删恢复）："
            + "、".join(revive_keys)
            + "；它们在每台客户端上都会算「旧事件消失 + 新事件出现」。"
        )
    result_ics = build_ics_for_semester(
        cfg,
        semester,
        input_path=getattr(args, "input", None),
        baseline_probe=str(last_local) if last_local.is_file() else None,
        include_exams=not args.no_exams,
        include_calendar_events=getattr(args, "include_calendar_events", False),
        published_baseline_text=baseline_text if migrating else None,
        allow_mass_cancellation=getattr(args, "allow_mass_cancellation", False),
        revive_salts=revive_salts,
    )
    # 自动但不静默（CD11）：迁移豁免门 2，但规模必须在这一刻、**发布之前**说清楚。
    # `published_baseline_text` 只有 push 会传（CD3）：`export` 的产物是手工导入的，
    # 把几百条撤销交给一个永不回源的客户端比不迁移更糟。
    if result_ics.migration_summary:
        _print_migration_notice(result_ics.migration_summary)
    res = _publish_with_journal(
        cfg, state, semester, result_ics, migrating, revive_salts=revive_salts
    )
    if res.outcome is subscribe.PublishOutcome.NO_CHANGE:
        print("无变化，跳过推送。")
        # 幂等跳过也要给链接：用户可能第一次没记下，这一支是他唯一能看到入口的地方。
        _print_webcal_hint(state)
        return 0
    print(f"已发布：{res.url}")
    _print_webcal_hint(state)
    _print_publish_cancellation_summary(result_ics)
    return 0


def _subscribe_rotate(
    cfg: Settings,
    state: subscribe.SubscriptionState,
    semester: str,
    *,
    include_exams: bool = True,
    include_calendar_events: bool = False,
    allow_mass_cancellation: bool = False,
    allow_unverified_baseline: bool = False,
) -> int:
    """换 token 并在有本地留底时重新发布。

    ``include_exams``：与 push 同口径透传给构建管线（spec §6.7）。rotate 若不接
    ``--no-exams``，用户明确关掉的考试会被悄悄塞回订阅 URL——正是本参数要防的缺陷。

    ``include_calendar_events``：与 ``include_exams`` 完全同一理由（P1-03）。
    rotate 会**重新构建并补发**整份产物，不接这个旗标的话，上一次发布里那批校历事件
    会在换 token 的补发中被静默丢掉——而它们不接撤销通路，客户端那边连"已取消"都收不到。

    ``allow_mass_cancellation``：门 2（CD12）的显式放行旗标，与 push 同义（换 token 后
    的补发同样要撤课程事件，保险丝没有理由在这里松开）。

    ``allow_unverified_baseline``：相容性那道门的人工出口，与 push 同义、同一判据
    （P0-01）。补发走的必须是**同一条**发布事务，所以这道门也不能只装在一侧。

    行为（spec D12 / §6.8）：换 token **之前**先跑一次探针渲染，有已发布却尚未撤销的
    考试时打印警告；探针只算不写（台账文本一律丢弃），且与随后的真实渲染读**同一份**
    SEQUENCE 基线，所以警告条数就等于真正发出去的撤销条数。
    补发本身走 :func:`_publish_with_journal`——与 push 同一条发布事务（准备稿 → 推送 →
    提升凭据 → 回写本地四样），所以「换完 token 才崩」和「推送成功但本地没落盘」都还有
    `subscribe repair` 这个出口（P0-01）。

    v1→v2 迁移（CD7/CD11）：判据同样只看状态文件，预告走**同一条探针**——探针要拿到
    与真实渲染一样的留底文本，报出的 N 才等于补发真正带出去的条数（D24 的"警告=实发"）；
    "不产生迁移副作用"靠**丢弃探针返回的台账文本与不回写标记**来保证，而不是靠少传参数。
    """
    from . import subscribe

    _validate_publish_branch(state.branch)
    last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
    # 构建之前那道门，与 push 同一个函数（P0-01）。位置要紧：**必须在换 token 之前**——
    # 本地四样落后时 rotate 同样会拿过期留底重发布，而 token 一旦换掉，旧 URL 就再也
    # 拿不回上一版字节了；这里停手，用户还有 repair / push 两条路可走。
    # 唯一的例外是"留底根本不在场"：那一支下面直接早退、不构建也不发布，门要防的事
    # 不发生（spec §6.8 的早退文案保持原样）。但未裁决的准备稿不看这个例外——换 token
    # 会让两份凭据都变成旧 token 的，`repair` 从此无从裁决。
    if last_local.is_file() or subscribe.has_unresolved_pending(cfg, semester):
        _require_compatible_local_state(
            cfg, state, semester, allow_unverified=allow_unverified_baseline
        )
    old = state.token
    # 留底既是真实渲染的 SEQUENCE 基线，也**必须**是探针的基线——两个分支读同一份证据，
    # 量出来的撤销数才等于实际下发的撤销数。探针若不带基线就走 exporter
    # `_cancellation_baseline_uids` 的"台账是唯一凭据"分支，把全部候选算成可下发：
    # `push --no-exams` 之后（D21：留底里没有考试 UID、台账还记着）rotate 会警告 N 条，
    # 真实渲染却把这 N 条全判成 D20 `unresolvable`——一条不发、还当场把它们永久移出台账，
    # 报给用户的与实际发生的正相反（虚报只朝"多报"这一侧偏）。
    migrating = subscribe.needs_uid_migration(state)
    baseline_text = last_local.read_text(encoding="utf-8") if last_local.is_file() else None
    # 校历事件的准入门（§12.2 / §12.4）与上面那道门同一个位置理由：
    # **换 token 之前**停手，用户手里还得留着"旧 URL 仍然能用"这条退路。
    _require_calendar_events_admissible(
        cfg,
        semester,
        include_calendar_events=include_calendar_events,
        baseline_text=baseline_text,
    )
    # rotate 会换新 token = 换订阅 URL，旧地址此后永远收不到撤销（spec D12）。
    # 这里是**探针渲染**：只取撤销数量，返回的台账文本必须丢弃（D24②——此刻产物
    # 还没发布出去，落台账等于"假装撤销过"）。try **只罩探针**：撤销警告是增量，
    # 探针失败（渲染异常，或 `info` 里的计数坏成非整数/非数字类型）降级为
    # probe_cancel=0 + logger.warning（异常只报类型名，不带台账与考试个人数据，
    # spec §8），不许拖崩 rotate 主功能；换 token 之后的真实渲染在下面的另一个 try 里，
    # 其异常不受这一块影响（fail-closed 路径照旧）。
    probe_cancel = 0
    probe_migration: MigrationSummary | None = None
    try:
        probe = build_ics_for_semester(
            cfg,
            semester,
            baseline_probe=str(last_local) if last_local.is_file() else None,
            include_exams=include_exams,
            include_calendar_events=include_calendar_events,
            published_baseline_text=baseline_text if migrating else None,
            allow_mass_cancellation=allow_mass_cancellation,
            revive_salts=state.revive_salts,
        )
        probe_cancel = int(str(probe.info.get("exam_cancellations", 0)))
        probe_migration = probe.migration_summary
    except (XjtuCalendarError, ValueError, TypeError) as exc:
        probe_cancel = 0
        logger.warning(
            "subscribe rotate 的撤销探针渲染失败，本次跳过撤销警告（不影响换 token）：%s",
            type(exc).__name__,
        )
    # 警告只对"旧 URL 确实挂过事件、且这次真会把撤销对照留底发出去"的订阅成立：
    # 从没发布成功过就没有收到过考试的旧地址（D12 的措辞对其不成立）；留底缺失时下面
    # 直接早退、一条也发不出去，报数字就成了虚警。文案不含"重新运行以确认"之类的闸门
    # 承诺——这里没有停等确认的交互，也不许为了它新增 CLI 旗标。
    if probe_cancel and state.last_push is not None and last_local.is_file():
        print(
            f"注意：本次有 {probe_cancel} 条考试事件尚未从旧订阅地址撤销，"
            "rotate 后旧地址将永远收不到撤销。"
        )
    # 迁移预告同样**早于换 token**（D24「先警告再换」的形状，CD11「自动但不静默」）：
    # 换完才说，用户面对的是一个已经改了地址、还可能没补发成功的订阅。条数取自探针，
    # 与下面补发真正带出去的规模同源。
    if probe_migration:
        _print_migration_notice(probe_migration)
    subscribe.rotate_token(cfg, state)
    print(f"新订阅 URL：{state.subscription_url}（补发成功前旧 URL 仍可读取）")
    # 换 token 正是最需要新链接的时刻：旧 URL 一失效，iOS 侧只能靠这条 webcal 链接重订。
    _print_webcal_hint(state)
    print(f"旧 token（{old[:4]}…）在本次补发成功后失效，请更新所有日历客户端的订阅地址。")
    if not last_local.is_file():
        if state.last_push is not None:
            # 已有成功推送却没留底：push 会拒绝重建（SEQUENCE 归零护栏），
            # 这里同步口径，别让「运行 push」成为死路指引。
            print(
                "（本地留底缺失：push 会拒绝以防事件历史被重置；请先从旧 URL 下载 .ics 放回该路径。）"
            )
        else:
            print("（本地尚无发布留底，运行 subscribe push 完成首次发布。）")
        return 0
    try:
        result_ics = build_ics_for_semester(
            cfg,
            semester,
            baseline_probe=str(last_local),
            include_exams=include_exams,
            include_calendar_events=include_calendar_events,
            published_baseline_text=baseline_text if migrating else None,
            allow_mass_cancellation=allow_mass_cancellation,
            # 换 token 是补发同一份订阅，不是"重新开始"：盐值必须与 push 读同一份状态。
            # 漏传会让救回来的课退回旧 UID（手机上已被删过、删除记忆不补），
            # 等于把 0.10.0 的补救悄悄抹掉；探针与真实渲染也必须是同一份表，
            # 否则警告条数对不上实发。
            revive_salts=state.revive_salts,
        )
        # P0-01：补发走的是与 push **同一条**事务——先落准备稿、再推送、成功后提升凭据，
        # 最后才回写留底/台账/迁移标记。探针渲染的那份台账文本在这里**不参与**：
        # 只有随产物真正发布出去的才算撤销过（D24②）。
        res = _publish_with_journal(cfg, state, semester, result_ics, migrating)
    except Exception as exc:
        # 三种收尾，措辞必须分开（P0-01 的"推送结果不确定性"那一档以前在 rotate 里没有）：
        # ①带 REMOTE_UNCERTAIN_ATTR——远端可能已经挂上新文件名，本地准备稿就是它的字节，
        #   此时唯一正确的下一步是让 repair 拿线上真实字节裁决，直接 push 只会先撞上
        #   未裁决的准备稿；②业务错误——远端还在旧文件名上（补发成功前旧 URL 仍可读取），
        #   修好后 push 即可补发；③其余异常照旧向上抛，不在这里吞。
        reloaded = _load_subscribe_state(cfg, semester)
        if reloaded is not None:
            state = reloaded
        if getattr(exc, subscribe.REMOTE_UNCERTAIN_ATTR, False):
            print(f"⚠️ token 已换、发布结果待裁决：{type(exc).__name__}")
            print(
                f"   建议：subscribe repair --semester {semester}——它会用线上真实字节判定"
                "这一版究竟发出去了没有（准备稿已保留，未做裁决前 push 会被拦住）。"
            )
            return 7
        if not isinstance(exc, XjtuCalendarError):
            raise  # 其余异常照旧向上抛，不在这里吞掉（与修复前的行为一致）
        # token 已落盘换新、发布却失败：不能报成功。
        print(f"⚠️ token 已换但尚未重新发布：{exc}")
        if exc.hint:
            print(f"   建议：{exc.hint}")
        print(f"修复后运行 subscribe push --semester {semester} 完成发布。")
        return exc.exit_code
    if res.outcome is subscribe.PublishOutcome.NO_CHANGE:  # pragma: no cover - 换 token 后必推
        print("无变化，跳过推送。")
        return 0
    print("已用新文件名重新发布。")
    # 与 push 同口径的撤销摘要（评审统一口径：两条发布路径报数一致）；同样只在
    # publish 成功之后打印（D11），`N == 0` 时整行不打印。
    _print_publish_cancellation_summary(result_ics)
    return 0


def _subscribe_status(
    args: argparse.Namespace, cfg: Settings, state: subscribe.SubscriptionState, semester: str
) -> int:
    from . import subscribe

    print(f"仓库：{state.repo_url}（分支 {state.branch}）")
    print(f"URL ：{state.subscription_url}")
    _print_webcal_hint(state)
    if state.last_push:
        print(f"上次发布：{state.last_push.pushed_at}")
        last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
        # has_unpublished_changes 只比 sha；publish 的跳过条件还要求 token 一致，
        # rotate 刚落盘（未重发布）时 sha 相同也绝不能说「无变更」。
        synced = (
            state.last_push.token == state.token
            and last_local.is_file()
            and not subscribe.has_unpublished_changes(
                cfg, state, last_local.read_bytes().decode("utf-8")
            )
        )
        if synced:
            print("本地留底：一致，无变更。")
        else:
            # 「留底缺失」也走这条：synced 要求 last_local.is_file()。措辞不能再
            # 断言「内容或 token 已变」；缺失时 push 有 SEQUENCE 归零护栏（I4）。
            print("本地留底：缺失或已变（内容/token），运行 subscribe push 重新发布。")
    else:
        print("尚未发布过。")
    age = subscribe.snapshot_age_days(cfg, semester)
    print(f"raw 快照：{'缺失' if age is None else f'{age:.1f} 天前'}")
    if getattr(args, "verify", False):
        try:
            ok, msg = subscribe.verify_url(state.subscription_url)
        except (ValueError, urllib.error.URLError, TimeoutError) as exc:
            # CLI 边界兜底：自检失败永远是「未通过 + 原因」，不是 traceback。
            ok, msg = False, f"{type(exc).__name__}: {exc}"
        print(f"URL 自检：{'通过' if ok else '未通过'}（{msg}）")
    return 0


_HANDLERS = {
    "login": cmd_login,
    "status": cmd_status,
    "fetch": cmd_fetch,
    "export": cmd_export,
    "inspect": cmd_inspect,
    "notice": cmd_notice,
    "schedule": cmd_schedule,
    "diff": cmd_diff,
    "doctor": cmd_doctor,
    "subscribe": cmd_subscribe,
}


def _harden_output_streams() -> None:
    """把 stdout/stderr 调整为「永不因编码崩」的形态。

    中文 Windows 上 stdout 一旦被重定向/管道化，编码回落到 locale（cp936），
    ``print("⚠️ …")`` 直接抛 UnicodeEncodeError——对 ``subscribe rotate`` 而言
    崩溃点落在「token 已换、尚未补发」的中间态（spec §7 最危险状态），
    用户只会看到一条未预期异常。策略：

    - 非 TTY（重定向/管道，消费方多为文件与工具）：统一改 UTF-8，
      不可编码字符 ``replace`` 兜底；
    - TTY（控制台本身可能是 GBK）：保持原编码，仅把错误模式降为
      ``replace``——个别 emoji 变 ``?``，中文正文不受影响。

    一切调整都是尽力而为：stream 被宿主（pythonw、测试框架）换掉或缺
    ``reconfigure`` 时原样放行，绝不在护栏自身抛异常。
    """
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        try:
            if stream is None:
                continue
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
            elif (stream.encoding or "").lower().replace("-", "") != "utf8":
                stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            continue


def main(argv: list[str] | None = None) -> int:
    """CLI 主函数。返回进程退出码。"""
    _harden_output_streams()
    parser = build_parser()
    args = parser.parse_args(argv)

    setup_logging(verbose=args.debug, quiet=args.quiet)
    cfg = Settings.load()

    if not args.command:
        parser.print_help()
        return 0

    try:
        return _HANDLERS[args.command](args, cfg)
    except XjtuCalendarError as exc:
        # 已知业务异常：给可读信息 + 修复建议，不抛 traceback
        print(f"\n[错误] {exc}", file=sys.stderr)
        if exc.hint:
            print(f"[建议] {exc.hint}", file=sys.stderr)
        if args.debug:
            import traceback

            traceback.print_exc()
        return exc.exit_code
    except KeyboardInterrupt:
        print("\n[中断] 用户取消了操作", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"\n[错误] 发生未预期的异常：{exc}", file=sys.stderr)
        if args.debug:
            import traceback

            traceback.print_exc()
        else:
            print("[建议] 加 --debug 参数查看详细堆栈。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
