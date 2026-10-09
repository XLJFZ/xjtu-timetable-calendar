"""``diff`` 子命令的 CLI 级 e2e：快照比对 + fetch 轮转的联动契约。

锁住三条：

1. 显式 ``--old/--new`` 两个 raw JSON -> 变化逐条可见（周次/教室）；
2. 全同 -> 明确「无变化」（而不是空输出让人怀疑没跑成功）；
3. 默认路径来自 ``fetch`` 的快照轮转：连续两次 fetch 后 ``diff --semester`` 直接可用；
   只有一次 fetch 时明确告知缺上一份快照以及怎么补救。

外加考试侧（设计文档 §6.6）：

4. **只有考试变化时不许被课程侧的短路吃掉**（旧代码在打印任何小节之前就
   ``if result.is_empty: print("无变化"); return``）；
5. 首次拿到考试快照（无 ``.prev``）时**全部按新增报告**，而不是静默跳过；
6. 没有考试快照时明说"本次未比对考试"，不用一句「无变化」蒙过去。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from exam_support import exam_payload, exam_row

from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings

SEMESTER = "2026-fall"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    return tmp_path


def _row(
    course: str,
    cid: str,
    weeks: str = "1-3周",
    room: str = "A-1001",
    teacher: str = "教师甲",
) -> dict[str, Any]:
    return {
        "KCM": course,
        "KCH": cid,
        "SKXQ": "5",
        "KSJC": "1",
        "JSJC": "2",
        "ZCMC": weeks,
        "JASMC": room,
        "SKJS": teacher,
    }


def _write_payload(path: Path, *rows: dict[str, Any]) -> Path:
    path.write_text(json.dumps({"kbList": list(rows)}, ensure_ascii=False), encoding="utf-8")
    return path


def test_diff_explicit_paths_reports_changes(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    old = _write_payload(
        tmp_path / "old.json", _row("示例课程甲", "D-1", weeks="1-4周", room="A-1001")
    )
    new = _write_payload(
        tmp_path / "new.json", _row("示例课程甲", "D-1", weeks="1-3周", room="A-2002")
    )

    code = main(["diff", "--old", str(old), "--new", str(new)])
    assert code == 0

    out = capsys.readouterr().out
    assert "周次" in out and "1-4" in out and "1-3" in out
    assert "教室" in out and "A-2002" in out


def test_diff_identical_snapsticks_says_no_change(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    row = _row("示例课程甲", "D-1")
    old = _write_payload(tmp_path / "old.json", row)
    new = _write_payload(tmp_path / "new.json", row)

    code = main(["diff", "--old", str(old), "--new", str(new)])
    assert code == 0
    assert "无变化" in capsys.readouterr().out


def test_diff_defaults_after_two_fetches(home: Path, tmp_path: Path) -> None:
    a = _write_payload(tmp_path / "a.json", _row("示例课程甲", "D-1", teacher="教师甲"))
    b = _write_payload(tmp_path / "b.json", _row("示例课程甲", "D-1", teacher="教师乙"))

    assert main(["fetch", "--from-file", str(a), "--semester", SEMESTER]) == 0
    assert main(["fetch", "--from-file", str(b), "--semester", SEMESTER]) == 0

    prev = home / "raw" / f"timetable-{SEMESTER}.prev.json"
    assert prev.is_file()  # fetch 轮转出了上一份快照

    code = main(["diff", "--semester", SEMESTER])
    assert code == 0


def test_diff_without_previous_snapshot_explains(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    a = _write_payload(tmp_path / "a.json", _row("示例课程甲", "D-1"))
    assert main(["fetch", "--from-file", str(a), "--semester", SEMESTER]) == 0

    code = main(["diff", "--semester", SEMESTER])
    assert code != 0
    err = capsys.readouterr().err
    assert "--old" in err  # 给出补救办法，而不是只说「文件不存在」


# --------------------------------------------------------------------------- #
# 考试小节（设计文档 §6.6）
# --------------------------------------------------------------------------- #
def _raw_dir(home: Path) -> None:
    Settings(home=home).ensure_dirs()


def _write_timetable_snapshots(home: Path, *rows: dict[str, Any]) -> None:
    """把「新」「旧」两份课表快照写成**同一份内容**：课程侧永远无变化。

    这样断言就能归因到考试侧：考试小节出不出现，只取决于考试比对，
    不再被课程侧短路（``if result.is_empty``）顺带吞掉。
    """
    _raw_dir(home)
    cfg = Settings(home=home)
    text = json.dumps({"kbList": list(rows)}, ensure_ascii=False)
    cfg.raw_timetable_path(SEMESTER).write_text(text, encoding="utf-8")
    cfg.raw_timetable_prev_path(SEMESTER).write_text(text, encoding="utf-8")


def _write_exam_snapshots(
    home: Path, new_rows: list[dict[str, str]], old_rows: list[dict[str, str]] | None
) -> None:
    """布置考试快照；``old_rows=None`` 表示**没有** ``.prev``（本学期首次拿到考试）。"""
    _raw_dir(home)
    cfg = Settings(home=home)
    cfg.raw_exams_path(SEMESTER).write_text(
        json.dumps(exam_payload(new_rows), ensure_ascii=False), encoding="utf-8"
    )
    if old_rows is not None:
        cfg.raw_exams_prev_path(SEMESTER).write_text(
            json.dumps(exam_payload(old_rows), ensure_ascii=False), encoding="utf-8"
        )


def test_exam_only_change_is_not_swallowed_by_the_course_short_circuit(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-shortcircuit：课程侧为空 + 考试侧有变更 -> 必须打出「考试变更」。

    写成 ``if course_empty or not exam_changes`` 的话这里直接打出「无变化」就 return 了
    ——座位重排、换考场正是学期中最常见的考试变更。
    """
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, [exam_row(ZWH="30")], [exam_row(ZWH="12")])

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "考试变更" in out
    assert "座位变更" in out
    assert "12" in out and "30" in out
    assert "无变化" not in out


@pytest.mark.parametrize(
    ("over", "expected"),
    [
        ({"KSSJMS": "2030-06-17 09:00-11:30(星期一)"}, "时间变更"),
        ({"JASMC": "B-2002"}, "教室变更"),
        ({"ZWH": "30"}, "座位变更"),
    ],
    ids=["time", "room", "seat"],
)
def test_each_exam_change_kind_reaches_the_output(
    home: Path, capsys: pytest.CaptureFixture[str], over: dict[str, str], expected: str
) -> None:
    """五类里三类「变更」各自可断言：把实现的比较项删掉就红。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, [exam_row(**over)], [exam_row()])

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert expected in out
    assert "无变化" not in out


def test_first_exam_snapshot_reports_every_row_as_added(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """R-G6：新快照在、``.prev`` 没有 -> **全部按新增报告**，不许静默跳过。

    按 brief 原文（任一侧缺失就打 info 返回空 diff）实现的话，学生第一次拿到考试时
    diff 会打出「无变化」，而日历里实实在在多出了一整批考试事件。
    """
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(
        home,
        [
            exam_row(ZWH="12"),
            exam_row(
                WID="WID-DEMO-2",
                KCM="示例课程乙",
                KSRQ="2030-06-20 00:00:00",
                KSSJMS="2030-06-20 09:00-11:30(星期四)",
            ),
        ],
        None,
    )
    assert not Settings(home=home).raw_exams_prev_path(SEMESTER).exists()

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "无上一份考试快照" in out
    assert "考试变更" in out
    assert out.count("新增：") == 2
    assert "无变化" not in out


def test_cancelled_exam_tells_the_user_the_client_may_keep_it(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """取消会随撤销通路下发（spec D13），但一次性导入型客户端可能要手动删：报告要说清。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, [], [exam_row()])

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "取消" in out
    assert "手动" in out


def test_identical_exam_snapshots_produce_no_exam_section(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = [exam_row(ZWH="12")]
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, rows, rows)

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "无变化" in out
    assert "考试变更" not in out
    assert "未比对考试" not in out  # 比过了，就别再打"没比"的提示


def test_missing_exam_snapshot_says_exams_were_not_compared(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有考试快照（未排考／从未抓过）时不能一句「无变化」糊过去（fail-closed 口径）。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "未比对考试" in out
    assert "考试变更" not in out
    assert Settings(home=home).raw_exams_path(SEMESTER).is_file() is False


def test_explicit_paths_without_semester_still_reports_no_exam_comparison(
    home: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--old/--new`` 只对课表生效（§6.6:333-336）：无法定位学期快照时要明说没比对。"""
    row = _row("示例课程甲", "D-1")
    old = _write_payload(tmp_path / "old.json", row)
    new = _write_payload(tmp_path / "new.json", _row("示例课程甲", "D-1", teacher="教师乙"))

    assert main(["diff", "--old", str(old), "--new", str(new)]) == 0
    out = capsys.readouterr().out
    assert "未比对考试" in out
    assert "考试变更" not in out


def _write_exam_snapshot(home: Path, payload_text: str, *, prev: bool = False) -> None:
    """把考试快照的**原始文本**直接落盘（用来布置坏 JSON）。"""
    _raw_dir(home)
    cfg = Settings(home=home)
    path = cfg.raw_exams_prev_path(SEMESTER) if prev else cfg.raw_exams_path(SEMESTER)
    path.write_text(payload_text, encoding="utf-8")


def test_corrupt_exam_snapshot_does_not_kill_the_course_report(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """评审 F1（Important）：坏考试快照是**增量侧**，读它失败只能跳过考试比对。

    课程侧这里就有变化（换教室）。旧写法在打印任何课程小节之前，于
    ``_diff_exam_snapshots()``（先于短路调用）里抛 ``XjtuCalendarError`` → 非零退出、
    课程变更一个字都不出，与同函数上面「缺快照 → 打一行说明、继续比课程」自相矛盾。
    改后：考试读/解析失败走既有的 ``exam_skip_note`` 通道，课程照常报告、exit 0。
    """
    # 课程两份快照**不同**（换教室）——断言因此能归因到"课程照常报告"。
    cfg = Settings(home=home)
    _raw_dir(home)
    cfg.raw_timetable_prev_path(SEMESTER).write_text(
        json.dumps({"kbList": [_row("示例课程甲", "D-1", room="A-2002")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps({"kbList": [_row("示例课程甲", "D-1", room="A-1001")]}, ensure_ascii=False),
        encoding="utf-8",
    )
    # 新考试快照坏掉，旧考试快照正常。
    _write_exam_snapshot(home, "{坏掉的 JSON")
    _write_exam_snapshot(
        home, json.dumps(exam_payload([exam_row()]), ensure_ascii=False), prev=True
    )

    assert main(["diff", "--semester", SEMESTER]) == 0
    captured = capsys.readouterr()  # 只读一次：读两次的话第一下就把两条流排干了
    out = captured.out
    # 课程照常报告（换教室这条变更必须出现在输出里）。
    assert "教室" in out and "A-1001" in out and "A-2002" in out
    # 考试被跳过，且说明里点名是**考试**快照坏掉。
    assert "本次未比对考试" in out
    assert "考试快照" in out
    assert "无变化" not in out  # 课程有变更，绝不能被当成"无变化"


def test_corrupt_previous_exam_snapshot_is_also_skipped(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """评审 F1 的另一半：坏的是 ``.prev`` 那一侧同样不许杀掉课程报告（原来只测了新侧）。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))  # 课程全同
    cfg = Settings(home=home)
    _raw_dir(home)
    cfg.raw_exams_path(SEMESTER).write_text(
        json.dumps(exam_payload([exam_row(ZWH="30")]), ensure_ascii=False), encoding="utf-8"
    )
    _write_exam_snapshot(home, "{坏掉的 .prev", prev=True)

    assert main(["diff", "--semester", SEMESTER]) == 0
    captured = capsys.readouterr()
    assert "本次未比对考试" in captured.out
    assert "考试快照" in captured.out
    assert "考试变更" not in captured.out  # 跳过 = 没比对，不假装产出了变更


def test_diff_cancel_note_points_at_the_new_cancel_path(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D13：取消现在会自动下发；"一次性导入型客户端仍需手动删"这半句不许丢。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, [], [exam_row(WID="WID-GONE")])  # 新快照为空 ⇒ 一条取消

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "考试变更" in out and "取消" in out
    assert "不会从已订阅的日历里自动消失" not in out, "旧断言还挂着（spec D13）"
    assert "STATUS:CANCELLED" in out, "要告诉用户取消会真的下发"
    assert "手动删除" in out, "一次性导入型客户端那半句事实没变，不许顺手删掉"
    # 修复轮 Important 1：主谓装反的病句必须绝迹。本仓库术语（README「已知边界」）里
    # "不再回源"恰是一次性导入客户端的特征——它们**不会**自动移除；会自动移除的是
    # 会定期回源拉取的订阅客户端。整句与分小句两个层面都钉死。
    assert "不再回源的订阅客户端会自动移除" not in out, "病句回归（把主语装反了）"
    note_line = next(line for line in out.splitlines() if line.startswith("注意："))
    clauses = re.split("[；，]", note_line)
    assert not any("不再回源" in c and "移除" in c for c in clauses), (
        "「不再回源⇄自动移除」配对不许出现"
    )
    assert any("移除" in c and "不再回源" not in c for c in clauses), (
        "自动移除必须归给会回源的客户端"
    )
    assert any("仍需手动删除" in c and "一次性导入" in c for c in clauses), (
        "手动删除那半句要挂在一次性导入上"
    )


def test_diff_summary_line_previews_the_cancellation_count(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D13 后半句：diff 要打出「本次将撤销 N 条」摘要行，N 只数取消、不吃其它变更。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    # 两条变更：一条座位变更（12→30）+ 一条取消（WID-GONE 消失）——N 必须是 1 不是 2。
    _write_exam_snapshots(home, [exam_row(ZWH="30")], [exam_row(), exam_row(WID="WID-GONE")])

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "考试变更（2 项）" in out
    assert "本次将撤销：1 条" in out, "摘要行缺失，或把 2 项变更全算成了撤销"
    assert "已撤销" not in out  # 这是预览：下次发布才会发生，不许说成已完成


def test_diff_omits_the_cancellation_summary_when_nothing_is_cancelled(
    home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """N == 0 时整行不打印——无条件 print 混不过这条（座位变更不是撤销）。"""
    _write_timetable_snapshots(home, _row("示例课程甲", "D-1"))
    _write_exam_snapshots(home, [exam_row(ZWH="30")], [exam_row(ZWH="12")])

    assert main(["diff", "--semester", SEMESTER]) == 0
    out = capsys.readouterr().out
    assert "考试变更" in out and "座位变更" in out
    assert "将撤销" not in out
