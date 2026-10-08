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
import json
import shutil
import subprocess
import sys
import urllib.error
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import __version__
from .academic_calendar import AcademicCalendar
from .config import Settings
from .errors import (
    AuthenticationExpired,
    AuthenticationRequired,
    EndpointNotConfigured,
    GitNotAvailable,
    SemesterNotConfigured,
    SubscribeNotConfigured,
    TimetableFetchError,
    XjtuCalendarError,
)
from .exporter import build_ics_for_semester
from .logging_setup import get_logger, setup_logging

if TYPE_CHECKING:
    # cmd_subscribe 各分支内部惰性 `from . import subscribe`；这里只为类型标注。
    from . import subscribe
    from .fetcher import Endpoint

__all__ = ["build_parser", "main"]

logger = get_logger()


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
    rot_p = sact.add_parser("rotate", help="更换订阅 token（旧 URL 立即失效）")
    rot_p.add_argument("--semester")
    rot_p.add_argument(
        "--no-exams",
        action="store_true",
        help="不并入考试安排（默认并入；本地快照不删）",
    )
    st_p = sact.add_parser("status", help="查看订阅状态、URL 与新鲜度")
    st_p.add_argument("--semester")
    st_p.add_argument("--verify", action="store_true", help="匿名 GET 自检 URL 可达性")

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
    logger.info("会话就绪：%s", info.describe())
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
    except OSError:  # 快照刚被移走：宁可不报日期，也不反过来谎称"没有快照"
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
        include_exams=not args.no_exams,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    # newline=""：render_ics 已按 RFC 5545 产出 CRLF 行尾，
    # 若用默认 newline=None，Windows 会再翻译一次得到 \r\r\n。
    output.write_text(result.ics, encoding="utf-8", newline="")

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
        if any(exam_change.kind == EXAM_KIND_CANCELLED for exam_change in exam_diff.changes):
            # §7.1：v1 不发布 STATUS:CANCELLED / METHOD:CANCEL，报出取消≠客户端删掉它。
            print(
                "注意：被取消的考试不会从已订阅的日历里自动消失（本工具不发布取消事件），"
                "必要时请在日历中手动删除。"
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
    if args.action == "rotate":
        return _subscribe_rotate(cfg, state, semester, include_exams=not args.no_exams)
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
    print()
    print("一次性开启 Pages（GitHub）：仓库 Settings → Pages → Deploy from branch，")
    print(f"分支选 {state.branch}、目录选 /(root)。完成后运行 subscribe push。")
    print("⚠️ 知道该 URL 的人即可读取你的课表；有泄露疑虑时运行 subscribe rotate。")
    return 0


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
    result_ics = build_ics_for_semester(
        cfg,
        semester,
        input_path=getattr(args, "input", None),
        baseline_probe=str(last_local) if last_local.is_file() else None,
        include_exams=not args.no_exams,
    )
    res = subscribe.publish(cfg, state, result_ics.ics)
    if res.outcome is subscribe.PublishOutcome.NO_CHANGE:
        print("无变化，跳过推送。")
        return 0
    # newline=""：留底必须与远端产物字节一致（CRLF 完整），否则下次把它当
    # SEQUENCE 基线读回、以及 status 的 sha 比对都会错位（同 cmd_export）。
    last_local.write_text(result_ics.ics, encoding="utf-8", newline="")
    print(f"已发布：{res.url}")
    return 0


def _subscribe_rotate(
    cfg: Settings, state: subscribe.SubscriptionState, semester: str, *, include_exams: bool = True
) -> int:
    """换 token 并在有本地留底时重新发布。

    ``include_exams``：与 push 同口径透传给构建管线（spec §6.7）。rotate 若不接
    ``--no-exams``，用户明确关掉的考试会被悄悄塞回订阅 URL——正是本参数要防的缺陷。
    """
    from . import subscribe

    _validate_publish_branch(state.branch)
    old = state.token
    subscribe.rotate_token(cfg, state)
    print(f"新订阅 URL：{state.subscription_url}（补发成功前旧 URL 仍可读取）")
    print(f"旧 token（{old[:4]}…）在本次补发成功后失效，请更新所有日历客户端的订阅地址。")
    last_local = subscribe.subscribe_dir(cfg) / f"last-{semester}.ics"
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
            cfg, semester, baseline_probe=str(last_local), include_exams=include_exams
        )
        subscribe.publish(cfg, state, result_ics.ics)
    except XjtuCalendarError as exc:
        # token 已落盘换新、发布却失败：不能报成功。远端还挂在旧文件名上
        # （补发成功前旧 URL 仍可读取），按指引修好后 push 即可补发。
        reloaded = _load_subscribe_state(cfg, semester)
        if reloaded is not None:
            state = reloaded
        print(f"⚠️ token 已换但尚未重新发布：{exc}")
        if exc.hint:
            print(f"   建议：{exc.hint}")
        print(f"修复后运行 subscribe push --semester {semester} 完成发布。")
        return exc.exit_code
    last_local.write_text(result_ics.ics, encoding="utf-8", newline="")
    print("已用新文件名重新发布。")
    return 0


def _subscribe_status(
    args: argparse.Namespace, cfg: Settings, state: subscribe.SubscriptionState, semester: str
) -> int:
    from . import subscribe

    print(f"仓库：{state.repo_url}（分支 {state.branch}）")
    print(f"URL ：{state.subscription_url}")
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
