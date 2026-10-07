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
    SemesterNotConfigured,
    XjtuCalendarError,
)
from .exporter import DEFAULT_CALENDAR_NAME, build_ics_for_semester
from .logging_setup import get_logger, setup_logging

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
    print()
    print("提示：该缓存文件含个人信息，已在 .gitignore 中排除，请勿提交或分享。")
    return 0


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
    """比对新旧课表快照。

    默认比较「上一次 fetch」与「这一次 fetch」——``fetch`` 会在覆盖前把旧快照
    原子轮转成 ``*.prev.json``，所以正常用过两次 fetch 后本命令零参数可用。
    只 fetch 过一次时不猜、不假装成功，明确说明基线缺失以及如何补救。
    """
    from .diff import describe_periods, describe_slot, diff_meetings
    from .parser import TimetableParser

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

    def _load(path: Path, label: str) -> object:
        if not path.is_file():
            raise XjtuCalendarError(
                f"{label}课表快照不存在：{path}",
                hint="快照来自 fetch（每次 fetch 会把上一份轮转为 *.prev.json 作为比较基线）。"
                "刚 fetch 过一次还没有基线属正常；也可以先用 --old 指定一份之前保存的 raw JSON。",
            )
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise XjtuCalendarError(f"{label}快照不是合法 JSON：{path}（{exc}）") from exc

    old_parser = TimetableParser()
    new_parser = TimetableParser()
    _, old_meetings = old_parser.parse(_load(old_path, "旧"))
    _, new_meetings = new_parser.parse(_load(new_path, "新"))

    print(f"旧快照: {old_path}（{old_parser.report.summary()}）")
    print(f"新快照: {new_path}（{new_parser.report.summary()}）")
    for parser in (old_parser, new_parser):
        for reason in parser.report.skipped:
            logger.warning("解析跳过（可能影响比对完整性）：%s", reason)
    print()

    result = diff_meetings(old_meetings, new_meetings)
    if result.is_empty:
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

    print("提示：确认无误后重新 export 即可拿到更新后的 .ics（UID 稳定，原地更新）。")
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
