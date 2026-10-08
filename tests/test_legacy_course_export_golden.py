"""G1：把「课程侧字节与接入考试安排之前完全一致」这条承诺从人肉比对变成机器钉。

为什么现有的断言不够
--------------------
``test_exporter_exams.py`` 里 ``result.ics == courses.ics`` 那一类用例比较的是
**当前代码**开考试 / 关考试两种口径的产物。它们防不住「未来某次改动同时把课程侧
改坏」——两边一起坏，断言照样绿。接入前代码给出的字节是另一个主体，只能由**接入前
的代码**产出，于是这条用例把那个主体固化成一份被跟踪的产物：
:data:`GOLDEN`。将来任何改坏课程侧 UID / SEQUENCE / 字节的提交都会在这里变红。

golden 的确切来历（可复核）
--------------------------
- 旧代码：本仓库 ``main`` 分支的工作树（``git worktree list`` 的第一条，即主检出），
  提交 ``0bd9414681``——本分支的 merge-base，也是接入考试安排之前的最后一次发布。
- 运行方式：在**那个目录**下 ``PYTHONPATH=src <venv python> <生成脚本>``。本机的 venv
  是 editable 安装、指向主检出的 ``src/``，所以 ``PYTHONPATH=src`` 必须显式写：从工作树
  跑就是新代码了。两边靠 ``xjtu_calendar.__file__`` 区分，不靠版本号（``__version__``
  读的是已安装的 dist-info，两边都是旧号）。
- 输入：:data:`INPUTS`（全合成数据，见该文件的 ``_disclaimer``）。
- mtime：落盘后 ``os.utime`` 打成 ``1913025600``（= 2030-08-15T12:00:00Z），
  与 ``snapshot_mtime`` 同源。DTSTAMP 与所有新事件的 LAST-MODIFIED 都由它推导
  （:func:`xjtu_calendar.exporter._stamp_from_snapshot`），不钉住就没有可复现的 golden。
- 生成环境：CPython 3.13 + icalendar 7.3.0（VTIMEZONE 块由 icalendar 的
  ``add_missing_timezones()`` 产出，属**库的产物**而不是本项目的逻辑）。

重新生成（依赖升级等正当理由导致产物确实该变时）
----------------------------------------------
1. ``cd`` 到主检出（``main`` @ ``0bd9414681``），确认 ``git status`` 干净；
2. 照着 :func:`lay_out_home` 写一个一次性脚本（读 :data:`INPUTS` 布置 home、
   ``os.utime`` 钉 mtime、调用 ``build_ics_for_semester(cfg, semester)``——旧签名，
   没有考试相关参数、把 ``result.ics`` 以 ``newline=""`` 写出），在那儿用
   ``PYTHONPATH=src <venv python> <脚本>`` 跑。**脚本与产物都不提交**：脚本绑定
   本机的主检出位置，产物由本用例比对即可。
   本轮实际用过的那份脚本会先打印 ``xjtu_calendar.__file__`` 与 ``git rev-parse HEAD``
   再动手——基线的来历必须留在输出里，不然「跑的是旧代码」只是口头承诺。
3. 逐行检查新产物不含真实个人信息（``LOCATION`` / ``DESCRIPTION`` / ``X-WR-CALNAME``
   是真实数据会落进去的三个位置），再连同用例一起提交。

``icalendar`` / ``tzdata`` 升级可能让这条用例红
----------------------------------------------
``pyproject.toml`` 的约束是 ``icalendar>=6.1.0``（无上界），CI 又是 ``pip install -e .[dev]``
而不带 lockfile，所以依赖升级后 VTIMEZONE 块可能整体变化。那**不是**课程侧回归：红了先按
上面的流程用旧代码重跑一次，比对「只有 VTIMEZONE 变了」还是「VEVENT 也变了」，再决定是
重新生成 golden 还是修代码。本文件第一条用例的字节断言在最前面，就是为了不放过任一种。

可伪性
------
给 :func:`xjtu_calendar.exporter.make_uid` 的 ``payload`` 追加任一字段（例如
``str(meeting.weekday)``），或改 ``SUMMARY`` 的拼装（例如加一个后缀），本文件前两条用例
立即红；第三条 :func:`test_course_side_rendering_is_reproducible` 反而会**保持绿**——它比的是
同一段代码连跑两次的自洽性，专门用来区分「产物里有挂钟」与「产物与接入前不同」这两件事。

行尾：这条用例依赖 ``.gitattributes`` 的 ``tests/fixtures/*.ics -text``
--------------------------------------------------------------------
本仓库全局是 ``* text=auto eol=lf``，而 .ics 按 RFC 5545 是 CRLF。若那条例外被删掉，
固件一进仓库就压成 LF，前两条字节用例会红成一场 2000 行的差异，看上去像课程侧回归。
:func:`test_golden_fixture_keeps_its_crlf_bytes` 先在那之前把原因说清楚。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from xjtu_calendar.config import Settings
from xjtu_calendar.exporter import build_ics_for_semester

FIXTURES = Path(__file__).resolve().parent / "fixtures"
INPUTS = FIXTURES / "legacy_course_inputs.json"
GOLDEN = FIXTURES / "legacy_course_export.ics"

#: 钉住的快照 mtime（UTC epoch 秒）换算出来的 DTSTAMP / LAST-MODIFIED 文本。
#: 单独断言它，是为了让「golden 红了」和「mtime 推导变了」在失败信息里分得开。
PINNED_STAMP = "20300815T120000Z"

#: golden 里的课程事件数（3 门课：6 + 5 + 3 次上课）。为 0 的 golden 等于没有钉任何东西。
EXPECTED_COURSE_EVENTS = 14


def lay_out_home(tmp_path: Path) -> tuple[Settings, str]:
    """按 :data:`INPUTS` 在 ``tmp_path`` 布置一个离线 home，并钉好快照 mtime。

    与生成 golden 的脚本共用同一份输入文件（不是同一份代码拷贝），所以两边不可能
    因为「改了一处忘了另一处」而错开：改输入就等于改 golden，必须重新生成产物。
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
    raw = cfg.raw_timetable_path(semester)
    raw.write_text(json.dumps(bundle["timetable_payload"], ensure_ascii=False), encoding="utf-8")
    stamp = float(bundle["snapshot_mtime"])
    os.utime(raw, (stamp, stamp))
    return cfg, semester


def test_course_side_bytes_equal_pre_feature_export(tmp_path: Path) -> None:
    """课程侧产物逐字节等于接入前的产物（``include_exams=False`` 口径）。"""
    cfg, semester = lay_out_home(tmp_path)
    ics = build_ics_for_semester(cfg, semester, include_exams=False).ics

    assert ics.encode("utf-8") == GOLDEN.read_bytes()
    # 下面两条是「红了以后一眼看懂红在哪」的前置断言，不参与字节比较。
    assert ics.count("BEGIN:VEVENT") == EXPECTED_COURSE_EVENTS
    assert f"DTSTAMP:{PINNED_STAMP}" in ics


def test_default_path_without_exam_snapshot_keeps_course_bytes(tmp_path: Path) -> None:
    """没抓过考试时，**默认口径**（``include_exams=True``）的字节也必须与接入前一致。

    课程侧的承诺面向真实用户，而真实用户第一次 export 时本地就没有考试快照：
    降级分支（``TimetableFetchError`` → 只记一条 INFO）必须一个字节都不动。
    只在 ``include_exams=False`` 上断言的话，这条分支就漏在钉子外面。

    与上面那条同理由，``test_exam_snapshot_lag_days`` 那套「考试滞后提示」也不许动字节：
    滞后块在 ``cmd_export`` 里，只打日志，不回到渲染管线（本用例直接调
    :func:`build_ics_for_semester`，覆盖的是管线侧；CLI 侧的跳过逻辑由
    ``test_exams_fetch.py`` 的用例覆盖）。
    """
    cfg, semester = lay_out_home(tmp_path)
    assert not cfg.raw_exams_path(semester).exists()  # 前置：本用例测的就是「没有考试」

    assert build_ics_for_semester(cfg, semester).ics.encode("utf-8") == GOLDEN.read_bytes()


def test_course_side_rendering_is_reproducible(tmp_path: Path) -> None:
    """同一输入连跑两次字节必须相同——课程侧产物里不许有挂钟。

    这条是 golden 用例成立的前提，也是它自己的钉：`DTSTAMP` 走快照 mtime
    （见 :func:`xjtu_calendar.exporter._stamp_from_snapshot` 的说明），若将来有人
    把 ``now_local()`` 之类塞回渲染路径，两次跑就会分叉，本用例先红，
    而不会让上面两条以「golden 莫名其妙变了」的形式红。
    """
    cfg, semester = lay_out_home(tmp_path)
    first = build_ics_for_semester(cfg, semester, include_exams=False).ics
    second = build_ics_for_semester(cfg, semester, include_exams=False).ics
    assert first == second


def test_golden_fixture_keeps_its_crlf_bytes() -> None:
    """golden 固件在**仓库里**仍是 CRLF：`.gitattributes` 的免归一化规则不许被删。

    本仓库的全局行尾契约是 ``* text=auto eol=lf``，而 RFC 5545 要求 CRLF。上面两条
    字节用例比的是「渲染字符串」与「固件字节」，一旦 Git 把固件压成 LF，两边就永远
    对不上——而且是新克隆、CI、本机工作树**同时**红，红成一个 2000 行的字节差异，
    看上去像课程侧回归，实际只是行尾被"顺手转换"了。

    所以这里单独钉一次契约（对应 ``.gitattributes`` 的 ``tests/fixtures/*.ics -text``）：
    破坏红 = 删掉那条规则后重新检出固件，本用例立即红并直接说出原因。
    """
    raw = GOLDEN.read_bytes()
    assert b"\r\n" in raw, "golden 固件里一个 CRLF 都没有：行尾归一化把它改了"
    assert raw.count(b"\n") == raw.count(b"\r\n"), "golden 固件里混进了裸 LF"
    assert raw.startswith(b"BEGIN:VCALENDAR\r\n")
