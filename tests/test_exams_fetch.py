"""`_fetch_exams` 的落盘行为（设计文档 §6.2 / §7）。

核心断言只有一条：**状态未知时既有快照的字节不许变**。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from exam_support import SEMESTER, captured_logs, exam_payload, exam_row

from xjtu_calendar import cli, fetcher
from xjtu_calendar.cli import _exam_fallback_note, _fetch_exams, cmd_export, main
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import AuthenticationExpired
from xjtu_calendar.exams import (
    ExamState,
    classify_exam_payload,
    exam_snapshot_lag_days,
    parse_exam_rows,
)
from xjtu_calendar.exporter import ExportResult
from xjtu_calendar.logging_setup import redact
from xjtu_calendar.subscribe import snapshot_age_days


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    return cfg


def _endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="exam_schedule",
        method="POST",
        path="/jwapp/sys/studentWdksapApp/modules/wdksap/wdksap.do",
        description="我的考试安排",
    )


def _endpoints(**over: object) -> dict[str, fetcher.Endpoint]:
    table: dict[str, fetcher.Endpoint] = {"exam_schedule": _endpoint()}
    table.update(over)  # 传 exam_schedule=None 再由调用方 pop 可模拟缺端点
    return table


def _write_old_snapshot(cfg: Settings) -> str:
    """预置一份"上次抓到的"快照，用来验证未知态不会覆盖它。"""
    payload = exam_payload([exam_row(KCM="旧课程")])
    cfg.raw_exams_path(SEMESTER).write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    return cfg.raw_exams_path(SEMESTER).read_text(encoding="utf-8")


def _patch(monkeypatch: pytest.MonkeyPatch, impl) -> None:
    monkeypatch.setattr(fetcher, "fetch_via_http", impl)


def test_fetch_exams_saves_snapshot_when_scheduled(home: Settings, monkeypatch) -> None:
    calls = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        calls.append((endpoint.name, params))
        return exam_payload([exam_row()])

    _patch(monkeypatch, fake)
    path = _fetch_exams(_endpoints(), home, SEMESTER)

    assert path == home.raw_exams_path(SEMESTER)
    assert path is not None and path.is_file()
    assert calls == [("exam_schedule", {"XNXQDM": SEMESTER})]  # 参数由调用方传，不写进端点表


@pytest.mark.skipif(os.name == "nt", reason="Windows 不支持 POSIX 权限位语义")
def test_exam_snapshot_is_owner_only(home: Settings, monkeypatch) -> None:
    """§8：考试快照落盘必须 0600。权限位断言按 test_fileutil.py:69 的同款 skipif 走。"""
    _patch(monkeypatch, lambda *a, **k: exam_payload([exam_row()]))
    path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_unknown_state_keeps_existing_snapshot_byte_for_byte(home, monkeypatch) -> None:
    before = _write_old_snapshot(home)
    _patch(monkeypatch, lambda *a, **k: exam_payload([], code=0, msg="查询失败"))  # 实测未排考形态
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    assert home.raw_exams_path(SEMESTER).read_text(encoding="utf-8") == before
    assert any("不覆盖" in rec.getMessage() for rec in records)


def test_expired_session_is_unknown_not_empty(home: Settings, monkeypatch) -> None:
    before = _write_old_snapshot(home)

    def boom(*a, **k):
        raise AuthenticationExpired("会话已过期")

    _patch(monkeypatch, boom)
    assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    assert home.raw_exams_path(SEMESTER).read_text(encoding="utf-8") == before


@pytest.fixture(params=("unknown", "expired"))
def degraded(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """把 `fetch_via_http` 打成两种「拿不到考试但绝不许动本地快照」的形态之一。

    §7:376 要求这两条降级分支都报出旧数据的日期，所以每条都要过一遍 F1 的两个用例。
    返回形态名，供断言失败时连同消息一起打出来。
    """
    if request.param == "unknown":
        _patch(monkeypatch, lambda *a, **k: exam_payload([], code=0, msg="查询失败"))
    else:

        def boom(*a, **k):
            raise AuthenticationExpired("会话已过期")

        _patch(monkeypatch, boom)
    return str(request.param)


def test_degraded_warns_with_existing_snapshot_date(home: Settings, degraded: str) -> None:
    """§7:376「有旧快照则沿用并**提示其日期**」：告警报的必须是快照那一天，不是运行当天。

    旧写法（`_snapshot_day`）直接从快照 mtime 反推期望值，而 mtime 刚写完 ≈ 今天，于是
    「快照那天」与「今天」在这条用例里恒等——实现若错报 `datetime.now()` 也照样绿。改成
    先用 `os.utime` 把 mtime 钉到一个**明显不是今天**的固定日（40 天前，写法同 Task 11
    那批用例），期望值独立算出，再断言日志里出现的就是那一天。不改实现。
    """
    _write_old_snapshot(home)
    snapshot_day = date.today() - timedelta(days=40)
    assert (
        snapshot_day != date.today()
    )  # 前置：快照那天必须与今天可分，否则用例退化成"报今天也算对"
    stamp = datetime(snapshot_day.year, snapshot_day.month, snapshot_day.day, 12).timestamp()
    os.utime(home.raw_exams_path(SEMESTER), (stamp, stamp))
    expected = snapshot_day.isoformat()
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    messages = [rec.getMessage() for rec in records]
    assert any(expected in message for message in messages), (degraded, messages)


def test_degraded_without_snapshot_says_local_copy_is_missing(
    home: Settings, degraded: str
) -> None:
    """没有本地快照时那句"沿用已有快照"是**谎话**：必须改成明说不含考试。"""
    assert not home.raw_exams_path(SEMESTER).exists()
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is None
    messages = [rec.getMessage() for rec in records]
    assert any("没有" in message for message in messages), (degraded, messages)
    assert not any("沿用" in message for message in messages), (degraded, messages)


class _StubDateTime:
    """``cli.datetime`` 的替身类：``fromtimestamp`` 固定抛指定异常，模拟坏 mtime。

    刻意**不**去 monkeypatch 真正的 ``datetime.datetime``——那是 C 实现的类型，改它是
    全局副作用；换掉 ``cli`` 模块里的那个名字就够了，作用域由 monkeypatch 自己收回。
    """

    def __init__(self, error: type[Exception]) -> None:
        self._error = error

    def fromtimestamp(self, *args: object, **kwargs: object) -> None:
        raise self._error("快照 mtime 不可换算（本用例人造的坏值）")


@pytest.mark.parametrize("error", [ValueError, OverflowError])
def test_fallback_note_survives_unusable_mtime(
    home: Settings, monkeypatch: pytest.MonkeyPatch, error: type[Exception]
) -> None:
    """坏 mtime 只准丢掉日期，不准把异常抛进 fetch 的降级日志。

    `_exam_fallback_note`（本文件测的就是它）此前只兜 ``OSError``：Windows 上 `fromtimestamp`
    遇到坏值恰好抛 ``OSError[Errno 22]``，所以本地看不出来；而 POSIX 抛的是
    ``ValueError`` / ``OverflowError``（本机 Windows 上表现成 OSError，与平台相关，故
    这里用替身类把两类都钉住）。export 侧的同族写法在 Task 11 修复轮已经扩成三类
    （``cmd_export`` 里那句 ``contextlib.suppress(OSError, OverflowError, ValueError)``，
    终审 F3），fetch 侧是这次补的最后一处。

    破坏红：把 `except (OSError, OverflowError, ValueError)` 改回 `except OSError` →
    本用例直接以 ``ValueError`` / ``OverflowError`` 失败，而不是拿到那句兜底文案。
    """
    _write_old_snapshot(home)  # 快照必须在：走的正是"有旧快照但日期算不出来"那一支
    monkeypatch.setattr(cli, "datetime", _StubDateTime(error))
    note = _exam_fallback_note(home, SEMESTER)
    assert note == "本地已有考试快照，本次导出继续沿用它"
    assert not any(char.isdigit() for char in note)  # 不含日期：宁可少说，不可说错


def test_confirmed_empty_writes_empty_snapshot(home: Settings, monkeypatch) -> None:
    """只有 code==1 + 空 rows（确认无考试）才允许把旧快照顶掉。"""
    _write_old_snapshot(home)
    _patch(monkeypatch, lambda *a, **k: exam_payload([], code=1, msg="操作成功"))
    path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["datas"]["wdksap"]["rows"] == []


def test_missing_endpoint_degrades_without_raising(home: Settings) -> None:
    with captured_logs() as records:
        assert _fetch_exams({}, home, SEMESTER) is None  # 没有 exam_schedule 键
    assert any("跳过考试安排" in rec.getMessage() for rec in records)
    assert not home.raw_exams_path(SEMESTER).exists()


def test_pagination_guard_warns_but_still_saves(home: Settings, monkeypatch) -> None:
    """§6.4「翻页护栏」：行数与 totalSize 不符只 warning，**照常落盘**（裁决 3）。

    实测 pageSize=999、totalSize 最大 7，今天一页够用；但一旦某学期超过一页，
    护栏保证「静默丢考试」不会发生——它只报警，绝不据此拒绝保存。
    """
    payload = exam_payload([exam_row()])
    payload["datas"]["wdksap"]["totalSize"] = 42  # 伪造「服务端说有更多行」
    _patch(monkeypatch, lambda *a, **k: payload)
    with captured_logs() as records:
        path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None and path.is_file()  # 只告警，不阻断落盘
    assert any("totalSize" in rec.getMessage() for rec in records)

    # 同族接口的数字经常是字符串形态（这份额外层 `code` 就是 "0"）：护栏不能因此静默关闭。
    payload["datas"]["wdksap"]["totalSize"] = "42"
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is not None
    assert any("totalSize" in rec.getMessage() and "42" in rec.getMessage() for rec in records)


def test_unparseable_total_size_leaves_the_guard_silent(home: Settings, monkeypatch) -> None:
    """§6.4 护栏的空白组合（终审 F6）：`totalSize` 根本转不了 int → **静默关闭**。

    既不许误报（把"无法核对"当成 0 与 1 行不符去告警；`_exam_total_size` 的 docstring
    明写"无法核对时返回 None 并**不**报警"），也不许因此不落盘 —— 课表是主功能，
    考试侧的噪音同样得按 §7 的宁缺毋滥处理。正例已有 `42` 与 `"42"` 两条（上面那条），
    "很多"这一类今天没人管过。
    """
    payload = exam_payload([exam_row()])
    payload["datas"]["wdksap"]["totalSize"] = "很多"
    _patch(monkeypatch, lambda *a, **k: payload)
    with captured_logs() as records:
        path = _fetch_exams(_endpoints(), home, SEMESTER)
    assert path is not None and path.is_file()  # 照常落盘
    assert not any("totalSize" in rec.getMessage() for rec in records)  # 且一个字都不报


def test_confirmed_empty_still_triggers_the_pagination_guard(home: Settings, monkeypatch) -> None:
    """§7:375 明令覆盖的组合态：`code==1` + `rows=[]` + `totalSize=5` 同时成立。

    两件事**都**得发生：空快照照常落盘（顶掉旧的），并且留下翻页告警 ——
    "接口说没有考试"与"接口说有 5 条却只给了 0 行"是互相矛盾的信号，
    后者不该被前者的 info 淹没。把护栏挪进 `if outcome.rows:`、
    或让 NO_EXAMS 分支提前 return 都会让这里红。
    """
    _write_old_snapshot(home)
    payload = exam_payload([], code=1, msg="操作成功")
    payload["datas"]["wdksap"]["totalSize"] = 5
    _patch(monkeypatch, lambda *a, **k: payload)
    with captured_logs() as records:
        path = _fetch_exams(_endpoints(), home, SEMESTER)

    assert path is not None
    assert json.loads(path.read_text(encoding="utf-8"))["datas"]["wdksap"]["rows"] == []
    messages = [rec.getMessage() for rec in records]
    assert any("行数（0）与 totalSize（5）" in message for message in messages), messages


def _two_modules(*, b_total_size: int) -> dict[str, object]:
    """``datas`` 里放**两个**模块：A 只有 ``rows``（且排在前面），B 才带 ``extParams``。

    实测响应只有一个模块（§4.1），所以这是前瞻形态；正因如此它今天没有用例覆盖，
    两套判据的差异只写在注释里（终审 F2）。
    """
    payload = exam_payload(
        [exam_row(WID="B-1", KSRWID="KSRWID-B-1"), exam_row(WID="B-2", KSRWID="KSRWID-B-2")],
        code=1,
        module="B",
    )
    payload["datas"]["B"]["totalSize"] = b_total_size
    payload["datas"] = {
        "A": {"totalSize": 1, "rows": [exam_row(WID="A-1", KCM="示例课程乙")]},
        **payload["datas"],
    }
    return payload


def test_multi_module_response_gives_all_three_readers_the_same_module(
    home: Settings, monkeypatch
) -> None:
    """Task 4 的裁定「以 classify 选中的模块为准」必须由代码执行，不是靠注释（终审 F2）。

    A（rows-only）排在前面、B 才是带 ``extParams`` 的那个。三个读取者 ——
    `classify_exam_payload`、翻页护栏（`cli._exam_total_size`）、`parse_exam_rows` ——
    必须都看 B：否则 fetch 侧按 B 的 2 行／5 总数告警并落盘，导出侧却把 A 那一行
    当成本学期的考试，两处对「有哪些考试」各说各话（`diff` 还会把它报成取消＋新增）。
    """
    payload = _two_modules(b_total_size=5)

    outcome = classify_exam_payload(payload)
    assert outcome.state is ExamState.HAS_EXAMS
    assert [row["WID"] for row in outcome.rows] == ["B-1", "B-2"]
    assert [exam.row_id for exam in parse_exam_rows(payload)] == ["B-1", "B-2"]

    _patch(monkeypatch, lambda *a, **k: payload)
    with captured_logs() as records:
        assert _fetch_exams(_endpoints(), home, SEMESTER) is not None
    # 护栏两侧的数字都出自 B：A 的 totalSize 与行数都是 1，串了模块这条就红。
    assert any("行数（2）与 totalSize（5）" in rec.getMessage() for rec in records)


def test_cmd_fetch_still_returns_zero_when_exams_explode(home: Settings, monkeypatch) -> None:
    """课表是主功能：考试侧任何异常都不能让 cmd_fetch 非零退出（§7 末条）。"""
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    monkeypatch.setattr(fetcher, "fetch_via_http", _timetable_then_boom)

    assert main(["fetch", "--semester", SEMESTER, "--source", "http"]) == 0
    assert not home.raw_exams_path(SEMESTER).exists()  # 考试失败没落任何半个快照


def test_cmd_fetch_no_exams_flag_never_requests_exams(home: Settings, monkeypatch) -> None:
    """`--no-exams`：三条分支一律不抓考试——连考试请求都不发，本地快照不动。

    只断言可观测行为（请求了哪些端点、有没有落盘），不抓日志：`main()` 会经
    `setup_logging` 清空 `xjtu_calendar` logger 的 handler，`captured_logs` 在 `main`
    路径里收不到记录（brief 的 `main()` 用例同样不依赖日志断言，即为此故）。
    """
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    requested: list[str] = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        requested.append(endpoint.name)
        return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}

    _patch(monkeypatch, fake)
    assert main(["fetch", "--semester", SEMESTER, "--source", "http", "--no-exams"]) == 0
    assert requested == ["timetable"]  # 关掉后连考试请求都不发
    assert not home.raw_exams_path(SEMESTER).exists()


def test_cmd_fetch_http_branch_fetches_exams_after_timetable(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """正向接线（评审 F3）：课表抓完之后**真的**去抓考试，并把考试快照落了盘。

    上面两条 `main()` 用例断言的全是否定式（退出码 0／没落盘／没发请求），
    把 `cmd_fetch` 里的整个考试块删掉它们依然全绿——本任务最关键的那根线在测试里
    是不可见的。这条钉住请求顺序与落盘，是头号回归护栏。
    """
    import xjtu_calendar.auth as auth

    session = home.home / "session"
    session.mkdir(parents=True, exist_ok=True)
    (session / "storage_state.json").write_text(
        json.dumps({"cookies": [{"name": "SESSIONID", "value": "x"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(auth, "has_session", lambda _cfg: True)
    monkeypatch.setattr(
        fetcher, "load_endpoints", lambda *a, **k: _endpoints(timetable=_timetable_endpoint())
    )
    requested: list[str] = []

    def fake(endpoint, *, cfg=None, params=None, transport=None):
        requested.append(endpoint.name)
        if endpoint.name == "exam_schedule":
            return exam_payload([exam_row()])
        return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}

    _patch(monkeypatch, fake)
    assert main(["fetch", "--semester", SEMESTER, "--source", "http"]) == 0
    assert requested == ["timetable", "exam_schedule"]
    assert home.raw_exams_path(SEMESTER).exists()


def _timetable_endpoint() -> fetcher.Endpoint:
    return fetcher.Endpoint(
        name="timetable", method="POST", path="/jwapp/sys/xskcb/xskcb.do", description="我的课表"
    )


def _timetable_then_boom(endpoint, *, cfg=None, params=None, transport=None):
    """按端点分流：课表正常返回一份 `kbList`，考试直接抛未预期异常。"""
    if endpoint.name == "exam_schedule":
        raise RuntimeError("考试接口炸了")
    return {"kbList": [{"KCM": "示例课程甲", "KCH": "D-1"}]}


# --------------------------------------------------------------------------- #
# Task 11：陈旧口径（考试快照 vs 课表快照）与打码名单
# --------------------------------------------------------------------------- #


def _touch(path: Path, *, days_ago: float) -> None:
    """把 mtime 拨到 N 天前；跨平台都用 os.utime，不靠 sleep。"""
    stamp = time.time() - days_ago * 86400
    os.utime(path, (stamp, stamp))


def test_snapshot_age_days_defaults_to_timetable_kind(tmp_path: Path) -> None:
    """既有调用点（cli.py 的两处 publish/status 调用）不传 kind，行为必须与改造前逐字一致。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    assert snapshot_age_days(cfg, SEMESTER) is None  # 还没 fetch 过

    cfg.raw_timetable_path(SEMESTER).write_text('{"kbList": []}', encoding="utf-8")
    assert snapshot_age_days(cfg, SEMESTER) < 1
    assert snapshot_age_days(cfg, SEMESTER, kind="exams") is None  # 考试侧仍为空


def test_exam_kind_reads_the_exam_snapshot(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    _touch(exam_path, days_ago=3)

    assert 2.9 < snapshot_age_days(cfg, SEMESTER, kind="exams") < 3.1
    assert snapshot_age_days(cfg, SEMESTER, kind="timetable") is None


def test_lag_is_exam_snapshot_behind_timetable_snapshot(tmp_path: Path) -> None:
    """§7 选定口径：考试快照落后于**课表快照**多少天，不是「距今几天」。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    timetable = cfg.raw_timetable_path(SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=10)
    _touch(timetable, days_ago=1)

    assert 8.9 < exam_snapshot_lag_days(cfg, SEMESTER) < 9.1


def test_lag_is_none_when_either_snapshot_is_missing(tmp_path: Path) -> None:
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    assert exam_snapshot_lag_days(cfg, SEMESTER) is None
    cfg.raw_exams_path(SEMESTER).write_text("{}{}", encoding="utf-8")  # 只有考试，没有课表
    assert exam_snapshot_lag_days(cfg, SEMESTER) is None


def test_exam_ahead_of_timetable_is_not_stale(tmp_path: Path) -> None:
    """考试比课表新（先 fetch 考试再动课表）不该报警：滞后为负按 0 处理。"""
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    timetable = cfg.raw_timetable_path(SEMESTER)
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=1)
    _touch(timetable, days_ago=5)

    assert exam_snapshot_lag_days(cfg, SEMESTER) == 0.0


def test_teacher_and_log_id_keys_are_redacted() -> None:
    """redact 是**按键**打码，不是按值：喂 dict 而不是喂 json 字符串。"""
    assert redact({"ZJJSXM": "张三"})["ZJJSXM"] == "***"
    assert redact({"SJBH": "007"})["SJBH"] == "***"
    assert redact({"XM": "李四"})["XM"] == "***"  # 既有能力，回归护栏
    assert redact({"KCM": "示例课程甲"})["KCM"] == "示例课程甲"  # 课程名不敏感


def test_redact_reaches_the_nested_exam_rows() -> None:
    """生产日志喂进去的是**整个信封**，打码必须一路走到 `datas.<模块>.rows[]` 里（终审 F5）。

    `fetcher.py:442` 记的是 `redact(payload)`，形态就是 `datas.wdksap.rows[].ZJJSXM`；
    上面那条只喂扁平 dict，把 `redact` 改成"只处理最外层 key"它照样全绿。
    这里同时留一条反向断言（`KCM` 照旧可见），防止"图省事"改成无条件全量打码 ——
    那会让 DEBUG 日志再也看不出响应结构，等于把 inspect 的价值删掉。
    """
    cleaned = redact(exam_payload([exam_row(ZJJSXM="教师甲", SJBH="007")]))
    row = cleaned["datas"]["wdksap"]["rows"][0]
    assert row["ZJJSXM"] == "***"
    assert row["SJBH"] == "***"
    assert row["KCM"] == "示例课程甲"


# --------------------------------------------------------------------------- #
# Task 11 fix round 1：cmd_export 陈旧告警的三条口径（N1/N2/N3）
# --------------------------------------------------------------------------- #

#: 假的 info 必须凑齐 cmd_export 打印用的键，否则它会 KeyError 而不是走到断言。
_STUB_INFO = {
    "semester_name": "示例学期",
    "courses": 1,
    "meetings": 1,
    "events": 1,
    "date_range": "2030-02-25 ~ 2030-06-21",
}

_STALENESS_MARK = "考试数据来自"  # spec §7:384 句式的固定前缀，所有断言共用


def _stub_build(monkeypatch: pytest.MonkeyPatch) -> None:
    """拦下 build_ics_for_semester：本组用例只验 cmd_export 的告警块，不重建导出管线。"""

    def fake(_cfg: Settings, semester: str, **_kwargs: object) -> ExportResult:
        return ExportResult(
            ics="BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
            info=dict(_STUB_INFO),
            sequence_stats=None,
        )

    monkeypatch.setattr("xjtu_calendar.cli.build_ics_for_semester", fake)


def _stage_snapshots(cfg: Settings, *, exam_days_ago: float, timetable_days_ago: float) -> None:
    """写齐两份快照并把 mtime 拨到指定天数（lag = exam − timetable）。"""
    exam_path = cfg.raw_exams_path(SEMESTER)
    exam_path.write_text(json.dumps(exam_payload([exam_row()]), ensure_ascii=False), "utf-8")
    timetable = cfg.raw_timetable_path(SEMESTER)
    timetable.write_text('{"kbList": []}', encoding="utf-8")
    _touch(exam_path, days_ago=exam_days_ago)
    _touch(timetable, days_ago=timetable_days_ago)


def _export_args(cfg: Settings, *, no_exams: bool) -> argparse.Namespace:
    """cmd_export 读取的完整 Namespace（与 build_parser 的 export 子命令一一对应）。"""
    return argparse.Namespace(
        semester=SEMESTER,
        input=None,
        calendar_config=None,
        schedule_config=None,
        name=None,
        from_date=None,
        to_date=None,
        allow_unsupported_adjustments=False,
        sequence_from=None,
        output=str(cfg.home / "out.ics"),
        no_sequence=False,
        no_exams=no_exams,
    )


def test_export_warns_when_exams_included_and_stale(home: Settings, monkeypatch) -> None:
    """正向对照：同样环境下不排除考试时告警**必须**出现，否则下面三条「没有告警」都是空断言。"""
    _stub_build(monkeypatch)
    _stage_snapshots(home, exam_days_ago=10, timetable_days_ago=1)
    with captured_logs() as records:
        assert cmd_export(_export_args(home, no_exams=False), home) == 0
    assert any(_STALENESS_MARK in rec.getMessage() for rec in records)


def test_export_no_exams_never_warns_about_staleness(home: Settings, monkeypatch) -> None:
    """N1：--no-exams 时产物里根本没有考试事件，「考试可能已改期…仍按现有快照导出」对本次运行是谎话。

    打破红：删掉 cli.py cmd_export 里的 `if not args.no_exams:` 门控（改回无条件执行），
    本用例就会收到告警而失败。
    """
    _stub_build(monkeypatch)
    _stage_snapshots(home, exam_days_ago=10, timetable_days_ago=1)
    with captured_logs() as records:
        assert cmd_export(_export_args(home, no_exams=True), home) == 0
    assert not any(_STALENESS_MARK in rec.getMessage() for rec in records)


def test_export_staleness_check_oserror_degrades_to_silence(home: Settings, monkeypatch) -> None:
    """N2：并发 fetch 轮转快照时 stat 窗口能抛 OSError；整块必须如约退化成「没什么可提醒的」。

    打破红：去掉告警块的 contextlib.suppress(OSError)（或换成更窄的异常），
    boom 的 OSError 会穿出 cmd_export，本用例直接 error。
    """

    def boom(*_a: object, **_k: object) -> None:
        raise OSError("快照正在被并发轮转")

    _stub_build(monkeypatch)
    _stage_snapshots(home, exam_days_ago=10, timetable_days_ago=1)
    # 打补丁在 subscribe.snapshot_age_days（告警块唯一未设防的 stat 现场）：
    # exam_snapshot_lag_days 是函数体内 import，调用时才取模块属性，补丁生效。
    monkeypatch.setattr("xjtu_calendar.subscribe.snapshot_age_days", boom)
    with captured_logs() as records:
        assert cmd_export(_export_args(home, no_exams=False), home) == 0
    assert not any(_STALENESS_MARK in rec.getMessage() for rec in records)


@pytest.mark.parametrize("bad_class", [OverflowError, ValueError], ids=["overflow", "value"])
def test_export_staleness_check_survives_a_broken_mtime(
    home: Settings, monkeypatch: pytest.MonkeyPatch, bad_class: type[Exception]
) -> None:
    """终审 F3：告警块只 `suppress(OSError)`，坏 mtime 还能抛 OverflowError／ValueError。

    `datetime.fromtimestamp(path.stat().st_mtime)` 是全分支**唯一**一处"因为考试的事
    让 export 非零退出"的路径（§7:387）。为什么注入而不是真造一个坏 mtime：
    这两类到底是哪一类是**平台**说了算 —— 本机（Windows）上
    `fromtimestamp(253402300800)`（公元 10000 年）抛的是 `OSError[Errno 22]`，
    POSIX 上才是 `ValueError`／`OverflowError`，用真实 mtime 写的用例在这台机器上
    会被"已经 suppress 的 OSError"蒙过去。所以按仓里 N2 用例的同款形状
    （monkeypatch 打进告警块的调用点）直接把这两类喂进去，三条平台都跑得到。
    打破红：把 `contextlib.suppress` 元组里的 `OverflowError, ValueError` 删掉。
    """

    class _BrokenTimestamp:
        """只替 `datetime.fromtimestamp` 这一处；`cmd_export` 里没别的 datetime 用法。"""

        @staticmethod
        def fromtimestamp(_ts: float) -> object:
            raise bad_class("mtime 坏掉了")

    _stub_build(monkeypatch)
    # lag = 10 − 1 = 9 > 阈值：必须真的走进取日期那一步，否则整块被跳过 = 空断言。
    _stage_snapshots(home, exam_days_ago=10, timetable_days_ago=1)
    monkeypatch.setattr("xjtu_calendar.cli.datetime", _BrokenTimestamp)
    with captured_logs() as records:
        assert cmd_export(_export_args(home, no_exams=False), home) == 0
    assert not any(_STALENESS_MARK in rec.getMessage() for rec in records)
    assert (home.home / "out.ics").is_file()  # 导出照常写完


def test_lag_just_under_threshold_stays_silent(home: Settings, monkeypatch) -> None:
    """N3 边界：滞后差一点到阈值必须静默（超阈值分支由上面的正向对照钉住）。

    打破红：把 EXAM_STALE_LAG_DAYS 改小（例如 6），滞后 6.9 天就会误报警、本用例失败；
    实现前的 RED 则是 `from xjtu_calendar.cli import EXAM_STALE_LAG_DAYS` 直接 ImportError。
    """
    from xjtu_calendar.cli import EXAM_STALE_LAG_DAYS

    assert EXAM_STALE_LAG_DAYS == 7.0  # 数字本身由 plan/spec 锁定，重命名常量的修复不许动它
    _stub_build(monkeypatch)
    _stage_snapshots(home, exam_days_ago=EXAM_STALE_LAG_DAYS - 0.1, timetable_days_ago=0.0)
    with captured_logs() as records:
        assert cmd_export(_export_args(home, no_exams=False), home) == 0
    assert not any(_STALENESS_MARK in rec.getMessage() for rec in records)
