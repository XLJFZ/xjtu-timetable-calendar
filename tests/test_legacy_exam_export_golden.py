"""升级安全钉：新代码在**没有台账**时必须逐字节复现 v0.5.0 的考试侧产物（spec D14）。

基线来历：由 tag ``v0.5.0`` 的 ``src/`` 对 ``legacy_exam_inputs.json`` 渲染而成，
两次独立运行逐字节一致。本机的 venv 是 editable 安装、指向主检出的 ``src/``，
所以**必须在 detached worktree 里跑旧代码**，并靠 ``xjtu_calendar.__file__`` 与
``git rev-parse HEAD`` 的输出确认跑的是哪一份——不靠版本号（``__version__`` 读的是
已安装的 dist-info，两边都是旧号）。

golden 的确切来历（可复核）
--------------------------
"v0.6 无台账" 与 v0.5.0 逐字节相同**正是本用例要证的东西**，所以**光靠那条字节比较，
仓库内无法分辨**一份来自 tag ``v0.5.0`` 的 golden 与一份在主检出里被新代码"顺手重生成"
的 golden。为此把来历钉在**代码里**（不再只留在 gitignore 的报告文件里）：

- 旧代码 = 本仓库 tag ``v0.5.0`` 的**提交** ``1db2e07fdf39c27a8d3ed20df03d59f31ebb17be``
  （``git rev-parse v0.5.0^{commit}``；``v0.5.0`` 是附注标签，``git rev-parse v0.5.0``
  返回的是标签对象 ``bf20332``，基线出处以**提交** ``1db2e07`` 为准）。
- 生成方式 = 在 ``git worktree add --detach <路径> v0.5.0`` 出来的**分离检出**里，以
  ``PYTHONPATH=src`` 跑，动手前先打印 ``xjtu_calendar.__file__``（必须落在该 worktree
  的 ``src/`` 下）与 ``git rev-parse HEAD``（必须等于 ``1db2e07``）——editable 安装的
  ``sys.meta_path`` finder 或 ``.pth`` 追加的 ``src`` 都可能盖过 ``PYTHONPATH``，故以
  ``__file__`` 为准，不符就过滤 meta_path / 剔掉 sys.path 上的外来 ``src`` 再验一次。
- 输入 = :data:`INPUTS`（全合成，见其 ``_disclaimer``），``snapshot_mtime`` 钉在
  ``1913025600``（= 2030-08-15T12:00:00Z），据此推出 :data:`PINNED_STAMP`。
- 产物 = :data:`GOLDEN`，其 **sha256** =
  ``5a729a12576ee078f32cf5b1c9062e4167141adccfe8854fd960f45f1c164d1d``
  （2542 字节，4 个 VEVENT：2 课程 + 2 考试）。

**为什么还要那组 tripwire 断言** —— 未来的"重生成"若在主检出用**有台账**的新代码跑，
产物会多出 ``STATUS:CANCELLED`` 的撤销事件、Vevent 数与属性名也会变；:func:`
test_v050_golden_bears_only_v050_artifacts` 直接对着固件字节把这些情况变红，
让"来历被动过"不再是无人认领的静默改动。

重新生成（依赖升级等正当理由导致产物确实该变时）
----------------------------------------------
``git worktree add --detach <路径> v0.5.0``，在该目录以 ``PYTHONPATH=src`` 照
:func:`lay_out_home` 写一次性脚本渲染，先打印 ``xjtu_calendar.__file__`` 与 HEAD 再动手
（``__file__`` 不含该 worktree 就说明 editable 安装抢了 ``PYTHONPATH``，按上文过滤
``sys.meta_path`` 后重验）；逐行检查新产物不含真实个人信息（``LOCATION`` / ``DESCRIPTION``
/ ``X-WR-CALNAME`` 是真实数据会落进去的三个位置），更新上面的提交号与 sha256，再连同本用例
一起提交。脚本绑定本机路径，故不进仓库。

可伪性与自限
------------
:func:`test_render_is_reproducible_without_a_ledger` 比的是**同一进程内**连跑两次的自洽性，
只能抓住这两次之间越过 1 秒墙钟边界的漂移、或事件排序的非确定性；若挂钟被烤进产物但两次
调用恰落在同一秒，它会**保持绿**——把"产物里有挂钟"与"产物与 v0.5.0 不同"这两件事分开，
后一件事交给字节比较和上面的 tripwire 断言看守（与课程侧模块 :func:`
test_course_side_rendering_is_reproducible` 的自述限制一致）。
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import build_ics_for_semester

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INPUTS = FIXTURES / "legacy_exam_inputs.json"
GOLDEN = FIXTURES / "legacy_exam_export_v050.ics"

#: 钉住的快照 mtime（UTC epoch 秒）；与课程侧 golden 同源，DTSTAMP / LAST-MODIFIED 都由它推。
SNAPSHOT_MTIME = 1913025600

#: 由 :data:`SNAPSHOT_MTIME` 推出的 DTSTAMP / LAST-MODIFIED 文本（2030-08-15T12:00:00Z）。
#: 单独断言它，是为了让「golden 红了」与「mtime 推导变了」在失败信息里分得开。
PINNED_STAMP = "20300815T120000Z"

#: v0.5.0 的 golden 里事件总数（2 课程 + 2 考试）。0 或偏多的 golden 等于没钉住撤销通路。
EXPECTED_TOTAL_EVENTS = 4
#: 其中考试事件数（SUMMARY 以「（结课考试）」结尾的那两条）。
EXPECTED_EXAM_EVENTS = 2

#: v0.5.0 在 VEVENT 里发过的属性名全集。撤销通路新增的是 ``STATUS``（spec D3/D6），
#: 固件里若冒出这个集合之外的任何一个属性，就说明它不是 v0.5.0 渲染出来的。
#: 只在 VEVENT 层比对，VTIMEZONE 交给 icalendar 的漂移不牵连这条（见模块 docstring）。
ALLOWED_V050_EVENT_PROPERTIES = frozenset(
    {
        "UID",
        "DTSTAMP",
        "SEQUENCE",
        "LAST-MODIFIED",
        "DTSTART",
        "DTEND",
        "SUMMARY",
        "LOCATION",
        "DESCRIPTION",
    }
)

_EXAM_SUMMARY_SUFFIX = "（结课考试）"


def _logical_lines(ics: str) -> list[str]:
    """按 RFC 5545 把折行（续行以空格/制表符开头）并回逻辑行，行尾风格无关。"""
    lines: list[str] = []
    for raw in ics.splitlines():
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _event_property_names(ics: str) -> set[str]:
    """VEVENT 块内出现过的属性名（去掉参数、大写），供白名单断言用。"""
    names: set[str] = set()
    in_event = False
    for line in _logical_lines(ics):
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            continue
        if upper == "END:VEVENT":
            in_event = False
            continue
        if not in_event or ":" not in line:
            continue
        head = line.split(":", 1)[0]
        names.add(head.split(";", 1)[0].strip().upper())
    return names


def _exam_event_count(ics: str) -> int:
    """SUMMARY 以「（结课考试）」收尾的事件数——即 v0.5.0 并进同一份 .ics 的考试行。"""
    count = 0
    for line in _logical_lines(ics):
        if line.startswith("SUMMARY:") and line[len("SUMMARY:") :].endswith(_EXAM_SUMMARY_SUFFIX):
            count += 1
    return count


def lay_out_home(tmp_path: Path) -> tuple[Settings, str]:
    """按固件布置 home：三件套 + 课表快照 + **考试快照**，两份快照打同一个 mtime。

    本函数是 ``tests/test_legacy_course_export_golden.py`` 里同名 ``lay_out_home`` 的近拷贝
    （plan 逐字要求，两处 golden 各自独立可读，故本轮不抽公共 support 文件）：课程侧只布课表
    快照，这里多布一份考试快照并给两份都打同一个 ``os.utime``。漂移风险由此行注明——改一处
    记得对照另一处。
    """
    bundle = json.loads(INPUTS.read_text(encoding="utf-8"))
    semester = str(bundle["semester_key"])
    cfg = Settings(home=tmp_path)
    cfg.ensure_dirs()
    cfg.semester_config_path(semester).write_text(
        json.dumps(bundle["semester_config"], ensure_ascii=False), encoding="utf-8"
    )
    cfg.schedule_config_path().write_text(
        json.dumps(bundle["schedule_config"], ensure_ascii=False), encoding="utf-8"
    )
    stamp = float(bundle["snapshot_mtime"])
    timetable = cfg.raw_timetable_path(semester)
    timetable.write_text(
        json.dumps(bundle["timetable_payload"], ensure_ascii=False), encoding="utf-8"
    )
    os.utime(timetable, (stamp, stamp))
    exams = cfg.raw_exams_path(semester)
    exams.write_text(json.dumps(bundle["exam_payload"], ensure_ascii=False), encoding="utf-8")
    os.utime(exams, (stamp, stamp))
    return cfg, semester


def test_upgrade_first_render_matches_v050_bytes(tmp_path: Path) -> None:
    cfg, semester = lay_out_home(tmp_path)
    assert not cfg.exam_ledger_path(semester).exists()  # 前置：测的就是"升级后第一次渲染"

    ics = build_ics_for_semester(cfg, semester).ics
    assert ics.encode("utf-8") == GOLDEN.read_bytes()
    # 下面几条紧挨字节比较，是「红了以后一眼看懂红在哪」的前置断言，不参与字节比较（同课程侧）。
    assert ics.count("BEGIN:VEVENT") == EXPECTED_TOTAL_EVENTS
    assert _exam_event_count(ics) == EXPECTED_EXAM_EVENTS
    assert f"DTSTAMP:{PINNED_STAMP}" in ics


def test_v050_golden_bears_only_v050_artifacts() -> None:
    """直接对着固件字节跑 tripwire：让「在主检出用新代码/有台账重生成」的 golden 立即红。

    字节比较那条只在**渲染==固件**时给答案，分辨不了固件本身是被谁渲染的（这正是 D14 的
    自指困境）。这里不看渲染、只审固件：v0.5.0 从没写过 ``STATUS``，撤销事件带
    ``STATUS:CANCELLED``；撤销还会多塞 VEVENT、引入 VEVENT 级新属性。任一条被破坏，本用例
    先红，而不用等到有人肉去比对来历。全部断言路径无关、对「v0.5.0 代码合法重生成」恒绿。
    """
    golden = GOLDEN.read_text(encoding="utf-8")

    # 1) 无 v0.6 撤销泄漏：v0.5.0 的 render_ics 从不写 STATUS。
    assert "STATUS:" not in golden, "固件出现 STATUS：撤销事件混进了 v0.5.0 基线"

    # 2) DTSTAMP 钉在快照 mtime，且没有第二个（墙钟）值漏进来。
    dtstamps = [
        ln.split(":", 1)[1].strip() for ln in _logical_lines(golden) if ln.startswith("DTSTAMP:")
    ]
    assert dtstamps, "固件里没有 DTSTAMP：不可能是这份配方渲染的"
    assert set(dtstamps) == {PINNED_STAMP}, f"DTSTAMP 不止钉住的那个值：{sorted(set(dtstamps))}"

    # 3) 事件构成 = 2 课程 + 2 考试（撤销会让总数或考试数错位）。
    assert golden.count("BEGIN:VEVENT") == EXPECTED_TOTAL_EVENTS
    assert _exam_event_count(golden) == EXPECTED_EXAM_EVENTS

    # 4) 属性白名单：VEVENT 里不许出现 v0.5.0 从没发过的属性。
    leaked = _event_property_names(golden) - ALLOWED_V050_EVENT_PROPERTIES
    assert not leaked, (
        f"固件含 v0.5.0 未发过的 VEVENT 属性（疑似台账/撤销时代的产物）：{sorted(leaked)}"
    )


def test_pinned_stamp_is_derived_from_snapshot_mtime() -> None:
    """:data:`PINNED_STAMP` 必须由固件的 ``snapshot_mtime`` 推出——把「来历」写进断言而非注释。

    课程侧 golden 的 DTSTAMP 同源同值；若哪天有人改了固件的 mtime 却没重生成 golden，
    这条会先于字节比较说出「推导对不上」，而不是让 golden 红成一场莫名差异。
    """
    bundle = json.loads(INPUTS.read_text(encoding="utf-8"))
    mtime = int(bundle["snapshot_mtime"])
    assert mtime == SNAPSHOT_MTIME
    derived = datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    assert derived == PINNED_STAMP


def test_render_is_reproducible_without_a_ledger(tmp_path: Path) -> None:
    """同一输入连跑两次字节必须相同——撤销通路不许把挂钟带进产物。

    这条是上面那条的前提：`cancel_expiry_at` 默认取墙钟，若实现时让它影响了
    保留期**之外**的东西（比如每次都重排事件），两次就会分叉，本用例先红，
    而不是让 golden 以"莫名其妙变了"的形式红。
    """
    cfg, semester = lay_out_home(tmp_path)
    first = build_ics_for_semester(cfg, semester).ics
    second = build_ics_for_semester(cfg, semester).ics
    assert first == second


def test_v050_golden_fixture_keeps_its_crlf_bytes() -> None:
    """行尾契约（同 ``tests/test_legacy_course_export_golden.py`` 的最后一条）。

    ``.gitattributes`` 的例外是 glob ``tests/fixtures/*.ics -text``，固件**必须留在这个
    目录**；挪进子目录会被全局 ``* text=auto eol=lf`` 压平，新克隆、CI、本机工作树
    同时红成一场几千字节的假"课程侧回归"。
    """
    raw = GOLDEN.read_bytes()
    assert b"\r\n" in raw, "golden 固件里一个 CRLF 都没有：行尾归一化把它改了"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "固件里混进了裸 LF"
    assert raw.startswith(b"BEGIN:VCALENDAR\r\n")
