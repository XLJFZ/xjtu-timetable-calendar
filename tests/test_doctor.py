"""`doctor` 判据核心：每条检查都要有成功 / 警告 / 阻塞三态，且全程只读。

夹具直接用 `subscribe_support.make_home`——它是仓库里"配置齐全的离线 home"的
唯一权威搭法（校历 / 作息 / raw 快照三件套），自己发明 JSON 形状只会造成 schema 漂移。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from subscribe_support import SEMESTER, make_home

from xjtu_calendar.config import Settings
from xjtu_calendar.doctor import (
    DOCTOR_EXIT_BLOCKED,
    MIN_DOC_VERSION,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_WARN,
    Check,
    doctor_exit_code,
    run_checks,
    worst_status,
)


def _write_session(cfg: Settings, *, age_days: int = 0, with_app_cookie: bool = True) -> None:
    cfg.session_dir.mkdir(parents=True, exist_ok=True)
    saved = datetime.now(UTC) - timedelta(days=age_days)
    cookies: list[dict[str, object]] = [
        {"name": "JSESSIONID", "value": "v", "domain": ".example.edu.cn"}
    ]
    if with_app_cookie:
        # 走到课表页之后教务侧才会发 /jwapp 作用域的 cookie（2026-10-14 实测）
        cookies.append(
            {"name": "_WEU", "value": "w", "domain": "ehall.xjtu.edu.cn", "path": "/jwapp/"}
        )
    (cfg.session_dir / "storage_state.json").write_text(
        json.dumps(
            {
                "cookies": cookies,
                "_saved_at": saved.isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


# --------------------------------------------------------------------------- #
# 整体形状：齐全的环境不该有阻塞项
# --------------------------------------------------------------------------- #


def test_configured_home_has_no_blocked_items(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    checks = run_checks(cfg, SEMESTER)
    assert doctor_exit_code(checks) == 0
    assert worst_status(checks) == STATUS_WARN  # 没配订阅是警告，不是失败
    assert doctor_exit_code(run_checks(cfg, SEMESTER, verify=False)) == 0


def test_every_check_name_appears_once(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    names = [c.name for c in run_checks(cfg, SEMESTER)]
    assert names == [
        "version",
        "session",
        "semester_config",
        "schedule_config",
        "snapshot",
        "exam_snapshot",
        "render",
        "subscription",
        "uid_recipe",
    ]
    assert names.count("uid_recipe") == 1


# --------------------------------------------------------------------------- #
# 逐项三态
# --------------------------------------------------------------------------- #


def test_version_floor_is_the_release_that_shipped_doctor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """判据下限必须等于 `doctor` 问世的版本（0.7.0），不能更低。

    写 0.6.x 的话，一份没刷新的旧安装记录（可编辑安装在本项目真踩过）会被判成
    "版本够新"，而它其实跑不出这份判据描述的那套检查——体检仪拿着对不上号的产物
    继续往下走，比直接报错更糟。
    """
    import xjtu_calendar.doctor as doctor_module

    assert MIN_DOC_VERSION == (0, 7, 0)

    monkeypatch.setattr(doctor_module, "__version__", "0.6.1")
    check = doctor_module._check_version()
    assert check.status == STATUS_WARN
    assert "0.7.0" in check.detail


def test_missing_session_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    check = by_name(run_checks(cfg, SEMESTER))["session"]
    assert check.status == STATUS_FAIL
    assert "login" in check.detail


def test_fresh_session_is_ok(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    assert by_name(run_checks(cfg, SEMESTER))["session"].status == STATUS_OK


def test_session_without_app_cookie_warns_and_names_the_login_cause(tmp_path: Path) -> None:
    """会话过得了 CAS、却一条教务侧（``/jwapp``）cookie 都没有 ⇒ 必然 403，要当场说破。

    这条判据来自 2026-10-14 的排查：真人登录停在「请选择身份」页面就按 Enter，
    fetch 报的是 403「没有访问该接口的权限」，而同一个会话在窗口里点一下「学生」
    就 200、18 条课程全回来。旧体检只看"有没有会话文件"，对这种"登录走了一半"
    完全无感，用户会被"没权限"三个字支使去折腾账号权限。
    """
    cfg = make_home(tmp_path)
    _write_session(cfg, with_app_cookie=False)
    check = by_name(run_checks(cfg, SEMESTER))["session"]
    assert check.status == STATUS_WARN
    assert "403" in check.detail
    assert "身份" in check.detail
    assert "login" in check.detail


def test_stale_session_warns_without_claiming_expiry(tmp_path: Path) -> None:
    """会话旧只警告，**不许**说它过期——旧 ≠ 失效，误报会把人推向错误的修复动作。"""
    cfg = make_home(tmp_path)
    _write_session(cfg, age_days=45)
    check = by_name(run_checks(cfg, SEMESTER))["session"]
    assert check.status == STATUS_WARN
    assert "过期" not in check.detail
    assert "login --force" in check.detail


def test_missing_semester_config_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.semester_config_path(SEMESTER).unlink()
    check = by_name(run_checks(cfg, SEMESTER))["semester_config"]
    assert check.status == STATUS_FAIL
    assert "学期校历缺失" in check.detail


def test_semester_config_detail_echoes_the_configured_dates(tmp_path: Path) -> None:
    """「学期校历」通过时必须把**配置里的日期**打出来。

    2026-10-14 新用户旅程走查实测：这一项原本只判"文件在不在"，而把 README 的
    格式示例（`first_week_monday=2026-09-07`、`total_weeks=16`）逐字抄进
    `2026-2027-1.json` 之后，doctor 通过、`export` 成功、产物看起来完全正常，
    而真实官方值是 `2026-09-14` / 18 周 ⇒ 整学期错一周且零提示。
    判定发生的那一刻必须让用户与 Agent 看见数字，才有机会发现它不是官方那一个。
    """
    cfg = make_home(tmp_path)
    check = by_name(run_checks(cfg, SEMESTER))["semester_config"]
    assert check.status == STATUS_OK
    assert "2026-09-07" in check.detail
    assert "16" in check.detail


def test_semester_config_warns_when_inner_key_disagrees_with_the_filename(
    tmp_path: Path,
) -> None:
    """配置内部的 `key` 会进入**每一条事件的 UID**，与文件名不一致要警告。

    同一次走查实测：只把 `semester.key` 从 `2026-fall` 改成别的值
    （文件名、日期一律不动）再导出，103 个 UID **零交集**；随后 `subscribe push`
    把这批"旧 UID 凭空消失"当成退课，报出「无法安全下发撤销」的警告。
    README 的模板恰好写的是示例 key，用户"顺手改成和文件名一致"就会踩中。
    """
    cfg = make_home(tmp_path)
    path = cfg.semester_config_path(SEMESTER)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["semester"]["key"] = "2026-2027-1"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    check = by_name(run_checks(cfg, SEMESTER))["semester_config"]
    assert check.status == STATUS_WARN
    assert "UID" in check.detail
    assert SEMESTER in check.detail and "2026-2027-1" in check.detail


def test_semester_config_unparseable_warns_instead_of_crashing(tmp_path: Path) -> None:
    """文件存在但解析不了：折成一条警告继续跑完其余项（原则 3：体检仪自己炸等于没报告）。

    渲染项会把它报成阻塞，这里只负责让「学期校历」这一项不再对坏文件亮绿灯。
    """
    cfg = make_home(tmp_path)
    cfg.semester_config_path(SEMESTER).write_text("{ 这不是 JSON", encoding="utf-8")
    check = by_name(run_checks(cfg, SEMESTER))["semester_config"]
    assert check.status == STATUS_WARN
    assert "无法解析" in check.detail


def test_missing_schedule_config_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.schedule_config_path().unlink()
    check = by_name(run_checks(cfg, SEMESTER))["schedule_config"]
    assert check.status == STATUS_FAIL
    assert "作息表缺失" in check.detail


def test_missing_snapshot_is_fail(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.raw_timetable_path(SEMESTER).unlink()
    check = by_name(run_checks(cfg, SEMESTER))["snapshot"]
    assert check.status == STATUS_FAIL
    assert "fetch" in check.detail


def test_no_exam_snapshot_is_warn_not_fail(tmp_path: Path) -> None:
    """未排考时本来就没有考试快照——这是常态，判成阻塞项会逼用户去查不存在的问题。"""
    cfg = make_home(tmp_path)
    _write_session(cfg)
    check = by_name(run_checks(cfg, SEMESTER))["exam_snapshot"]
    assert check.status == STATUS_WARN
    assert "未排考" in check.detail


def test_render_reports_event_and_exam_counts(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    check = by_name(run_checks(cfg, SEMESTER))["render"]
    assert check.status == STATUS_OK
    assert "个事件" in check.detail
    assert "考试" in check.detail


def test_subscription_reads_the_ledger_without_newline_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """留底必须按 bytes 读。

    `read_text` 的通用换行翻译会把 CRLF 折成 LF，于是内容 sha 永远对不上，
    doctor 会**永久误报**"有未发布变更"——真机上第一个跑出来的 bug 就是这个。
    """
    import xjtu_calendar.doctor as doctor_module
    from xjtu_calendar.subscribe import PushRecord, SubscriptionState

    cfg = make_home(tmp_path)
    _write_session(cfg)
    baseline = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    state = SubscriptionState(
        semester=SEMESTER,
        repo_url="https://example.invalid/r.git",
        branch="cal",
        token="t" * 32,
        url_base="https://example.invalid/cal",
        # sha 必须是留底字节的**真实**摘要：0.8.5 起 doctor 先做一条"留底 vs 已发布字节"
        # 的直比，假 sha 会被那条判据提前拦下，这个用例要测的换行翻译路径就走不到了。
        last_push=PushRecord(
            pushed_at="2026-10-11T00:00:00Z",
            content_sha256=hashlib.sha256(baseline).hexdigest(),
            token="t" * 32,
        ),
    )
    monkeypatch.setattr(doctor_module, "load_state", lambda *a, **k: state)

    seen: dict[str, str] = {}

    def _spy(_cfg: object, _state: object, text: str) -> bool:
        seen["text"] = text
        return False

    monkeypatch.setattr(doctor_module, "has_unpublished_changes", _spy)

    cfg.subscribe_dir.mkdir(parents=True, exist_ok=True)
    (cfg.subscribe_dir / f"last-{SEMESTER}.ics").write_bytes(baseline)

    check = by_name(run_checks(cfg, SEMESTER))["subscription"]
    assert "\r\n" in seen["text"]
    assert check.status == STATUS_OK


def test_render_failure_is_fail_and_does_not_raise(tmp_path: Path) -> None:
    """校历坏掉时 build 会抛错；doctor 必须把它折成一条 fail，而不是让命令炸掉。"""
    cfg = make_home(tmp_path)
    _write_session(cfg)
    cfg.semester_config_path(SEMESTER).write_text("{ 坏 JSON", encoding="utf-8")
    checks = run_checks(cfg, SEMESTER)
    assert by_name(checks)["render"].status == STATUS_FAIL


def test_unconfigured_subscription_is_warn(tmp_path: Path) -> None:
    cfg = make_home(tmp_path)
    _write_session(cfg)
    check = by_name(run_checks(cfg, SEMESTER))["subscription"]
    assert check.status == STATUS_WARN
    assert "未配置订阅" in check.detail


# --------------------------------------------------------------------------- #
# 两条全局承诺
# --------------------------------------------------------------------------- #


def test_doctor_is_read_only(tmp_path: Path) -> None:
    """只读承诺：跑完 doctor 后 home 下不得多出一个文件、也不得改动任何一个。"""
    cfg = make_home(tmp_path)
    _write_session(cfg)
    before = {p: p.stat().st_mtime_ns for p in cfg.home.rglob("*") if p.is_file()}
    run_checks(cfg, SEMESTER)
    after = {p: p.stat().st_mtime_ns for p in cfg.home.rglob("*") if p.is_file()}
    assert after == before


def test_exit_code_and_worst_status_are_pure() -> None:
    """退出码与最差状态是纯映射，不依赖夹具的真实渲染结果。"""
    assert worst_status([]) == STATUS_OK
    assert worst_status([Check("a", STATUS_OK, ""), Check("b", STATUS_WARN, "")]) == STATUS_WARN
    assert worst_status([Check("a", STATUS_OK, ""), Check("b", STATUS_FAIL, "")]) == STATUS_FAIL
    assert doctor_exit_code([Check("a", STATUS_WARN, "")]) == 0
    assert doctor_exit_code([Check("a", STATUS_FAIL, "")]) == DOCTOR_EXIT_BLOCKED


def test_verify_flag_adds_the_url_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """只有 `--verify` 才联网；默认一次 HTTP 都不发（由 test_cli_doctor 再兜一层）。"""
    import xjtu_calendar.doctor as doctor_module

    cfg = make_home(tmp_path)
    _write_session(cfg)
    state = _FakeState()
    monkeypatch.setattr(doctor_module, "load_state", lambda *a, **k: state)

    names = [c.name for c in run_checks(cfg, SEMESTER, verify=True)]
    assert "url" in names
    assert "url" not in [c.name for c in run_checks(cfg, SEMESTER, verify=False)]


class _FakeState:
    subscription_url = "https://example.invalid/cal.ics"
    token = "t"
    last_push = None


# --------------------------------------------------------------------------- #
# Task 10（CD18）：UID 配方与迁移状态——警告级，永不阻塞，输出过隐私 grep
# --------------------------------------------------------------------------- #


def _uid_state(uid_recipe: int | None):
    from xjtu_calendar.subscribe import SubscriptionState

    return SubscriptionState(
        semester=SEMESTER,
        repo_url="https://example.invalid/r.git",
        branch="cal",
        token="t" * 32,
        url_base="https://example.invalid/cal",
        uid_recipe=uid_recipe,
    )


def _write_course_ledger(cfg: Settings) -> None:
    """落一份**真实渲染出来的**课程台账（含课程名/教师/教室），供隐私断言使用。"""
    from xjtu_calendar.exporter import build_ics_for_semester

    ledger = cfg.course_ledger_path(SEMESTER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    text = build_ics_for_semester(cfg, SEMESTER).course_ledger_text
    assert text is not None
    ledger.write_text(text, encoding="utf-8", newline="")


def test_doctor_warns_when_migration_pending(tmp_path: Path) -> None:
    import xjtu_calendar.doctor as doctor_module

    cfg = make_home(tmp_path)
    got = doctor_module._check_uid_recipe(cfg, SEMESTER, _uid_state(None))
    assert got.status == STATUS_WARN and "迁移" in got.detail
    assert got.name == "uid_recipe"


def test_doctor_ok_after_migration(tmp_path: Path) -> None:
    import xjtu_calendar.doctor as doctor_module
    from xjtu_calendar.exporter import UID_RECIPE_VERSION

    cfg = make_home(tmp_path)
    got = doctor_module._check_uid_recipe(cfg, SEMESTER, _uid_state(UID_RECIPE_VERSION))
    assert got.status == STATUS_OK
    assert str(UID_RECIPE_VERSION) in got.detail


def test_doctor_ok_when_no_subscription(tmp_path: Path) -> None:
    """没配订阅就没有迁移可言——这一项对全新用户是 OK，不是警告。"""
    import xjtu_calendar.doctor as doctor_module

    cfg = make_home(tmp_path)
    got = doctor_module._check_uid_recipe(cfg, SEMESTER, None)
    assert got.status == STATUS_OK
    assert "未配置订阅" in got.detail


def test_doctor_warns_when_unsubscribed_ledger_unreadable(tmp_path: Path) -> None:
    """未订阅 + 课程台账存在但读不出 ⇒ WARN 并带上异常类名（终审 minor d）。

    旧早退把刚算出的 `read_error` 丢掉、对未订阅用户报成 OK——坏台账是真实的可用性
    信号，不许被"没有迁移可言"顺手掩掉；同时保持这一项**永不 FAIL**。
    """
    import xjtu_calendar.doctor as doctor_module

    cfg = make_home(tmp_path)
    ledger = cfg.course_ledger_path(SEMESTER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe not utf-8")

    got = doctor_module._check_uid_recipe(cfg, SEMESTER, None)
    assert got.status == STATUS_WARN
    assert got.name == "uid_recipe"
    assert "课程台账暂不可读（UnicodeDecodeError）" in got.detail
    assert doctor_exit_code([got]) == 0, "这一项永不阻塞"


def test_doctor_message_carries_no_personal_data(tmp_path: Path) -> None:
    """输出要过隐私 grep：无课程名/教师/教室/token/订阅 URL（规格 §8）。

    台账里躺着实打实的「示例课程甲」「教师甲」「A-1001」，三个分支的文案都只许报数。
    """
    import xjtu_calendar.doctor as doctor_module
    from xjtu_calendar.exporter import UID_RECIPE_VERSION

    cfg = make_home(tmp_path)
    _write_course_ledger(cfg)
    for state in (None, _uid_state(None), _uid_state(UID_RECIPE_VERSION)):
        got = doctor_module._check_uid_recipe(cfg, SEMESTER, state)
        for forbidden in ("示例课程", "教师", "A-1001", "t" * 32, "http", "@"):
            assert forbidden not in got.detail
    # 计数的确来自台账（不是恒 0 的假绿）。
    got = doctor_module._check_uid_recipe(cfg, SEMESTER, _uid_state(None))
    assert "2 条" in got.detail


def test_doctor_uid_recipe_never_blocks_even_with_unreadable_ledger(
    tmp_path: Path,
) -> None:
    """坏台账只少一个计数：本项**永不 FAIL**，退出码 0/5 契约不因它改变（约束 3/CD18）。"""
    from xjtu_calendar import subscribe
    from xjtu_calendar.exporter import UID_RECIPE_VERSION

    cfg = make_home(tmp_path)
    _write_session(cfg)
    subscribe.save_state(cfg, _uid_state(UID_RECIPE_VERSION))
    ledger = cfg.course_ledger_path(SEMESTER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_bytes(b"\xff\xfe not utf-8")

    got = by_name(run_checks(cfg, SEMESTER))["uid_recipe"]
    assert got.status == STATUS_WARN, "读不出台账降级为警告，不升级为阻塞项"
    assert doctor_exit_code(run_checks(cfg, SEMESTER)) == 0
