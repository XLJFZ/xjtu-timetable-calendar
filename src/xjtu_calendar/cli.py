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
import sys
from pathlib import Path

from . import __version__
from .academic_calendar import AcademicCalendar
from .config import Settings
from .errors import (
    AuthenticationRequired,
    EndpointNotConfigured,
    ScheduleNotConfigured,
    SemesterNotConfigured,
    XjtuCalendarError,
)
from .exporter import DEFAULT_CALENDAR_NAME, build_events, render_ics, summarize
from .logging_setup import get_logger, setup_logging
from .parser import TimetableParser
from .schedules import ScheduleTable
from .weeks import DEFAULT_EXPANSION_LIMIT

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

    # --- export ---
    export = sub.add_parser("export", help="解析课表并生成 .ics")
    export.add_argument("--semester", help="学期标识，例如 2026-fall")
    export.add_argument("-o", "--output", default="timetable.ics", help="输出文件路径")
    export.add_argument("--input", help="直接指定课表 JSON（默认用 fetch 的本地缓存）")
    export.add_argument("--calendar-config", help="教学日历 JSON 路径（覆盖默认查找）")
    export.add_argument("--schedule-config", help="作息表 JSON 路径（覆盖默认查找）")
    export.add_argument("--name", default=DEFAULT_CALENDAR_NAME, help="日历名称")
    export.add_argument("--from-date", help="只导出该日期（含）之后的事件，ISO 格式")
    export.add_argument("--to-date", help="只导出该日期（含）之前的事件，ISO 格式")

    # --- inspect ---
    inspect = sub.add_parser("inspect", help="对原始课表 JSON 做脱敏结构分析")
    inspect.add_argument("--input", help="课表 JSON 路径（默认用本地缓存）")
    inspect.add_argument("--semester", help="学期标识")
    inspect.add_argument("-o", "--output", default="_notes/timetable-structure.md", help="报告输出路径")

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
    print(f"  接口定义    : {'已载入 ' + str(len(endpoints)) + ' 个' if endpoints else '缺失（需先完成 Phase 1 接口分析）'}")
    if not endpoints:
        print(f"              期望位置：config/{ENDPOINTS_FILE}")

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
        ENDPOINTS_FILE,
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
                "未指定学期", hint="--from-file 时请用 --semester 指定缓存名，"
                                  "或设置环境变量 XJTU_SEMESTER"
            )
        source = Path(args.from_file)
        if not source.is_file():
            raise XjtuCalendarError(f"文件不存在：{source}")
        payload = json.loads(source.read_text(encoding="utf-8"))
        logger.info("已从本地文件读取课表：%s", source)
        path = save_raw(payload, cfg, semester)
        logger.info("已缓存到 %s", path)
        return 0

    # --- 网络路径 ---
    if not has_session(cfg):
        raise AuthenticationRequired("本地没有可用会话，无法获取课表")

    endpoints = load_endpoints(cfg=cfg)
    has_timetable = "timetable" in endpoints
    use_http = args.source in ("auto", "http") and has_timetable

    if args.source == "http" and not has_timetable:
        raise EndpointNotConfigured(
            "尚未确认课表接口路径，无法使用 HTTP 方式",
            hint="接口路径必须来自真实观测，本项目不会猜测端点。"
                 "请运行 scripts/probe_ehall.py 完成探测后填写 "
                 f"config/{ENDPOINTS_FILE}，或改用 --source browser。",
        )

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
                hint="用 --semester 指定（格式如 2026-2027-1），"
                     "或确认学期发现接口可用。",
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
        raise SemesterNotConfigured(
            "无法确定学期", hint="用 --semester 指定（格式如 2026-2027-1）"
        )
    path = save_raw(payload, cfg, semester)
    logger.info("已获取课表原始数据，缓存到 %s", path)
    print()
    print("提示：该缓存文件含个人信息，已在 .gitignore 中排除，请勿提交或分享。")
    return 0


def cmd_export(args: argparse.Namespace, cfg: Settings) -> int:
    from .fetcher import load_raw

    semester = args.semester or cfg.semester_key
    if not semester:
        raise SemesterNotConfigured(
            "未指定学期", hint="请用 --semester 指定，或设置环境变量 XJTU_SEMESTER"
        )

    # --- 课表数据 ---
    if args.input:
        source = Path(args.input)
        if not source.is_file():
            raise XjtuCalendarError(f"课表文件不存在：{source}")
        payload = json.loads(source.read_text(encoding="utf-8"))
    else:
        payload = load_raw(cfg, semester)

    # --- 教学日历 ---
    calendar_path = Path(args.calendar_config) if args.calendar_config else cfg.semester_config_path(semester)
    if not calendar_path.is_file():
        raise SemesterNotConfigured(
            f"未找到学期 {semester} 的教学日历：{calendar_path}",
            hint="请参考 examples/academic_calendar.example.json 创建该文件。"
                 "注意：第 1 教学周的星期一等日期必须来自官方校历，不要凭空填写。",
        )
    academic = AcademicCalendar.from_file(calendar_path)
    logger.info("教学日历：%s", academic.semester.name)

    # --- 作息表 ---
    schedule_path = Path(args.schedule_config) if args.schedule_config else cfg.schedule_config_path()
    if not schedule_path.is_file():
        raise ScheduleNotConfigured(
            f"未找到作息表配置：{schedule_path}",
            hint="请参考 examples/schedule.example.json 创建该文件。"
                 "注意：本项目不内置任何未经官方确认的作息时间，必须由你提供。",
        )
    schedules = ScheduleTable.from_file(schedule_path)

    problems = schedules.validate()
    for problem in problems:
        logger.warning("作息表配置问题：%s", problem)

    logger.info("作息表：%d 套作息、%d 个生效区间", len(schedules.profiles), len(schedules.periods))

    # --- 解析 ---
    # total_weeks 可省略：校历没给就用解析侧的默认安全上限。
    # 注意别把「解析边界」和「导出校验」混为一谈 ——
    # 导出侧的越界判定在 build_events 里独立进行，且是 fail-closed。
    parser = TimetableParser(
        expansion_limit=academic.semester.total_weeks or DEFAULT_EXPANSION_LIMIT
    )
    courses, meetings = parser.parse(payload)
    logger.info("已解析：%d 门课程、%d 条课程安排", len(courses), len(meetings))
    logger.info("解析报告：%s", parser.report.summary())
    for warning in parser.report.warnings:
        logger.warning("%s", warning)
    for skip in parser.report.skipped:
        logger.warning("跳过：%s", skip)

    if not meetings and parser.report.total_candidates == 0:
        # 「响应里根本没有课程记录」与「有记录但字段没对上」是两件事：
        # 前者是该学期真的没课，后者是适配问题，不能笼统报同一个错。
        logger.warning("课表为空：响应中没有找到任何课程记录，将生成不含事件的日历")
    elif not meetings:
        raise XjtuCalendarError(
            f"响应里有 {parser.report.total_candidates} 条候选记录，但没有一条能解析出课程安排",
            hint="字段映射很可能与实际响应不符。请运行 inspect 子命令查看脱敏结构，"
                 "并把真实字段名补进 parser.py 的 FIELD_CANDIDATES。",
        )

    # --- 展开 ---
    events = build_events(meetings, academic, schedules)
    logger.info("已展开：%d 次实际上课", len(events))

    # --- 可选日期过滤 ---
    if args.from_date or args.to_date:
        from datetime import date

        lower = date.fromisoformat(args.from_date) if args.from_date else None
        upper = date.fromisoformat(args.to_date) if args.to_date else None
        events = [
            e for e in events
            if (lower is None or e.start.date() >= lower)
            and (upper is None or e.start.date() <= upper)
        ]
        logger.info("按日期过滤后剩余 %d 次上课", len(events))

    if not events:
        logger.warning("没有生成任何事件（可能全部落在停课日期或被日期过滤排除）")

    # --- 渲染 ---
    ics = render_ics(events, calendar_name=args.name)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(ics, encoding="utf-8")

    # --- 汇总 ---
    info = summarize(meetings, events)
    print()
    print("Semester:")
    print(f"  {academic.semester.name}")
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
_HANDLERS = {
    "login": cmd_login,
    "status": cmd_status,
    "fetch": cmd_fetch,
    "export": cmd_export,
    "inspect": cmd_inspect,
}


def main(argv: list[str] | None = None) -> int:
    """CLI 主函数。返回进程退出码。"""
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
