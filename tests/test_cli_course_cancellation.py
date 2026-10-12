"""迁移的三条时序铁律（规格 §7、CD7/CD11/D11）：预告在前、回写在后、失败一律不动。

`tests/test_cli_cancellation.py` 已经把**考试侧**台账的时序钉住（D11/D24）；这个文件只管
课程侧新增的四件事：迁移预告的打印时机、`uid_recipe` 标记的回写时机、门 2 熔断的零写盘、
CD3（`export` 不参与迁移）。

固件刻意把撤销保留期的墙钟钉在教学周开始之前：`subscribe push` 不注入
`cancel_expiry_at`，而撤销候选要过 D2 过期筛（`start < now` 直接退场）——跟着真实挂钟走的话
2026 秋季的固件课次迟早全部"过期"，预告与熔断的断言会悄悄变成空话。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest
from exam_support import exam_payload, exam_row
from subscribe_support import SEMESTER, make_home, payload_row

from xjtu_calendar import cli, subscribe
from xjtu_calendar.cli import main
from xjtu_calendar.config import Settings
from xjtu_calendar.errors import XjtuCalendarError
from xjtu_calendar.exporter import UID_RECIPE_VERSION, build_ics_for_semester
from xjtu_calendar.fetcher import save_raw
from xjtu_calendar.schedules import combine

#: 升级前的老用户手上有三样东西：状态文件（没有 `uid_recipe`）、发布留底、考试台账。
#: 保留期墙钟：固件第 1 教学周周一是 2026-09-07 ⇒ 取 9-01 时全部课次「未过期」
#: （与 `tests/test_exporter_course_cancellation.py` 的 `NOW` 同一口径）。
FIXED_NOW = combine(date(2026, 9, 1), "00:00")

#: 迁移用例的规模：留底 3 门课（6 个事件），升级后本次只剩 1 门 ⇒ 候选 4 条。
ARCHIVE_COURSES = ("示例课程甲", "示例课程乙", "示例课程丙")


def _rows(*names: str) -> list[dict[str, str]]:
    picked = names or ARCHIVE_COURSES
    return [payload_row(KCM=name, KCH=f"D-{i}") for i, name in enumerate(picked, start=1)]


def _rewrite_snapshot(cfg: Settings, rows: list[dict[str, str]]) -> None:
    """模拟「下次 fetch 之后课变了」：直接覆写 raw 课表快照。"""
    cfg.raw_timetable_path(SEMESTER).write_text(
        json.dumps({"kbList": rows}, ensure_ascii=False), encoding="utf-8"
    )


def _archive_path(cfg: Settings) -> Path:
    return subscribe.subscribe_dir(cfg) / f"last-{SEMESTER}.ics"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """离线 home：学期用 `subscribe_support` 的 2026 秋季，撤销保留期墙钟钉在 9-01。"""
    identity = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@e",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@e",
    }
    for key, value in identity.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("XJTU_CALENDAR_HOME", str(tmp_path))
    monkeypatch.delenv("XJTU_SEMESTER", raising=False)
    # 只钉撤销保留期那一次取钟（`cancel_expiry_at or now_local()`）：DTSTAMP 仍由快照
    # mtime 推导，与本用例的字节比对无关（`test_cli_cancellation.py` 的 R2 说明）。
    monkeypatch.setattr("xjtu_calendar.exporter.now_local", lambda: FIXED_NOW)
    return make_home(tmp_path, _rows("示例课程甲"))


def _register(cfg: Settings, *, uid_recipe: int | None = None) -> None:
    """跳过 `subscribe init`（它要真远端），直接落一份订阅状态。

    ``uid_recipe=None`` 就是「从 v0.7.x 升级上来的老用户」——迁移分支的唯一判据（CD7）。
    """
    subscribe.save_state(
        cfg,
        subscribe.SubscriptionState(
            semester=SEMESTER,
            repo_url="https://example.invalid/calendar.git",
            branch="cal",
            token="deadbeef",
            url_base="https://me.github.io/timetable/",
            uid_recipe=uid_recipe,
        ),
    )


def _publish_archive(
    cfg: Settings, rows: list[dict[str, str]], *, after: list[dict[str, str]] | None = None
) -> Path:
    """把「升级前最后一次发布」落成留底：先按这批行渲染一次，产物**原样**写盘。

    刻意不在测试里重算 UID 配方（抄第二份配方 = 看守作废，与
    `tests/test_exporter_course_cancellation.py` 同一个理由）；留底 UID 与本次 live 集
    的差集就是迁移候选。``after`` 是渲染之后留在快照里的行，默认「升级后只剩 1 门课」。
    """
    _rewrite_snapshot(cfg, rows)
    result = build_ics_for_semester(cfg, SEMESTER)
    archive = _archive_path(cfg)
    archive.parent.mkdir(parents=True, exist_ok=True)
    archive.write_text(result.ics, encoding="utf-8", newline="")
    _rewrite_snapshot(cfg, _rows("示例课程甲") if after is None else after)
    return archive


def _stub_publish(
    monkeypatch: pytest.MonkeyPatch,
    order: list[str],
    capsys: pytest.CaptureFixture[str] | None = None,
    *,
    fail: bool = False,
) -> dict[str, object]:
    """真 publish 要动 git；换成记录调用顺序的桩，并在调用那一刻抓走 stdout。

    ``captured["out"]`` 是**进入 publish 时**已经打印的内容 ⇒「预告早于发布」的取证点；
    抓走即清空，所以这条用例后面不能再读同一句预告。
    ``order`` 里的 `state:<值>` 是一次真实的 `save_state` 调用（标记回写的取证点）。
    """
    captured: dict[str, object] = {}

    def fake_publish(_cfg: Settings, _state: object, _ics: str) -> subscribe.PublishResult:
        if capsys is not None:
            captured["out"] = capsys.readouterr().out
        order.append("publish")
        if fail:
            raise XjtuCalendarError("远端拒绝")
        return subscribe.PublishResult(
            outcome=subscribe.PublishOutcome.PUSHED,
            url="https://e.invalid/x.ics",
            content_sha256="0" * 64,
        )

    monkeypatch.setattr(subscribe, "publish", fake_publish)
    real_exam = cli._write_exam_ledger
    real_course = cli._write_course_ledger
    real_state = subscribe.save_state

    def exam(cfg: Settings, semester: str, text: str) -> None:
        # 台账一定晚于留底：进这里时留底必须已经是本次真正发出去的那份文本。
        captured["archive_at_exam_ledger"] = _archive_path(cfg).read_text(encoding="utf-8")
        order.append("exam-ledger")
        real_exam(cfg, semester, text)

    def course(cfg: Settings, semester: str, text: str) -> None:
        captured["archive_at_course_ledger"] = _archive_path(cfg).read_text(encoding="utf-8")
        order.append("course-ledger")
        real_course(cfg, semester, text)

    def save(cfg: Settings, state: subscribe.SubscriptionState) -> None:
        order.append(f"state:{state.uid_recipe}")
        real_state(cfg, state)

    monkeypatch.setattr(cli, "_write_exam_ledger", exam)
    monkeypatch.setattr(cli, "_write_course_ledger", course)
    monkeypatch.setattr(subscribe, "save_state", save)
    return captured


def _sha_of_all_side_effects(cfg: Settings) -> dict[str, str]:
    """熔断的零写盘面：留底 + 两份台账 + 状态文件，一个字节都不许动（CD4）。"""
    paths = [
        _archive_path(cfg),
        cfg.exam_ledger_path(SEMESTER),
        cfg.course_ledger_path(SEMESTER),
        subscribe.state_path(cfg, SEMESTER),
    ]
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "<absent>"
        for p in paths
    }


# --------------------------------------------------------------------------- #
# 预告的时机：必须早于 publish（CD11「自动但不静默」）
# --------------------------------------------------------------------------- #
def test_push_prints_migration_notice_before_publishing(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = home
    _register(cfg)
    _publish_archive(cfg, _rows())
    order: list[str] = []
    captured = _stub_publish(monkeypatch, order, capsys)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0

    out = str(captured["out"])
    assert "v1 UID 配方" in out and "预期行为" in out and "不需要额外操作" in out
    assert "通常会" in out, "客户端行为未经真机演练，措辞不许说满（红线）"
    assert "已取消的活动" in out
    assert "撤销 4 条" in out
    assert order[0] == "publish", "预告必须早于发布调用"


def test_notice_is_absent_when_the_recipe_is_already_recorded(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = home
    _register(cfg, uid_recipe=UID_RECIPE_VERSION)
    _publish_archive(cfg, _rows())
    _stub_publish(monkeypatch, [])

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "v1 UID 配方" not in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# 回写的时机：留底 → 考试台账 → 课程台账 → 标记，全在 publish 成功之后（D11/CD7）
# --------------------------------------------------------------------------- #
def test_write_back_order_after_a_successful_publish(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = home
    _register(cfg)
    save_raw(exam_payload([exam_row()]), cfg, SEMESTER, kind="exams")
    _publish_archive(cfg, _rows())
    order: list[str] = []
    captured = _stub_publish(monkeypatch, order)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0

    assert order == ["publish", "exam-ledger", "course-ledger", f"state:{UID_RECIPE_VERSION}"]
    assert subscribe.load_state(cfg, SEMESTER).uid_recipe == UID_RECIPE_VERSION
    # 两份台账都读得到已经落地的留底（同一时刻更新，D11）。
    assert captured["archive_at_exam_ledger"] == _archive_path(cfg).read_text(encoding="utf-8")
    assert captured["archive_at_course_ledger"] == captured["archive_at_exam_ledger"]
    # 台账文本不是产物文本：D10 的台账永远没有 SEQUENCE。
    assert "SEQUENCE" not in cfg.course_ledger_path(SEMESTER).read_text(encoding="utf-8")


def test_second_push_does_not_migrate_again(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = home
    _register(cfg)
    _publish_archive(cfg, _rows())
    _stub_publish(monkeypatch, [])

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "v1 UID 配方" in capsys.readouterr().out

    order: list[str] = []
    _stub_publish(monkeypatch, order)
    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert "v1 UID 配方" not in capsys.readouterr().out
    # 标记已经写上 ⇒ 状态文件不再被第二次回写（`state:<值>` 一条都不该出现）。
    assert not [item for item in order if item.startswith("state")]
    assert subscribe.load_state(cfg, SEMESTER).uid_recipe == UID_RECIPE_VERSION


# --------------------------------------------------------------------------- #
# 失败不动：发布失败与熔断都一个字节都不许动（D11/CD4）
# --------------------------------------------------------------------------- #
def test_failed_publish_leaves_everything_untouched(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = home
    _register(cfg)
    _publish_archive(cfg, _rows())
    before = _sha_of_all_side_effects(cfg)
    order: list[str] = []
    _stub_publish(monkeypatch, order, fail=True)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 1

    assert subscribe.load_state(cfg, SEMESTER).uid_recipe is None
    assert not cfg.course_ledger_path(SEMESTER).exists()
    assert _sha_of_all_side_effects(cfg) == before
    assert order == ["publish"]


def test_mass_cancellation_exit_code_is_six_and_writes_nothing(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CD4：熔断必须**零写盘**——留底、两份台账、状态文件字节全不变。"""
    cfg = home
    _register(cfg, uid_recipe=UID_RECIPE_VERSION)  # 标记已在 ⇒ 轮到常态分支与门 2
    many = [payload_row(KCM=f"示例课程{i}", KCH=f"D-{i}") for i in range(1, 7)]
    archive = _publish_archive(cfg, many, after=many)  # 留底 12 个事件
    full = build_ics_for_semester(cfg, SEMESTER)
    assert full.course_ledger_text is not None
    ledger = cfg.course_ledger_path(SEMESTER)
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text(full.course_ledger_text, encoding="utf-8", newline="")
    _rewrite_snapshot(cfg, many[:1])  # 12 条里只剩 2 条 ⇒ 候选 10、占比 0.83 ⇒ 门 2 触发

    before = _sha_of_all_side_effects(cfg)
    order: list[str] = []
    _stub_publish(monkeypatch, order)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 6
    assert _sha_of_all_side_effects(cfg) == before
    assert order == [], "熔断必须发生在任何写盘之前，publish 一次都不许调"
    assert archive.is_file()


# --------------------------------------------------------------------------- #
# CD3：迁移撤销只在 subscribe push，export 一律不参与
# --------------------------------------------------------------------------- #
def test_export_never_triggers_the_migration_sweep(
    home: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = home
    _register(cfg)
    _publish_archive(cfg, _rows())
    output = cfg.home / "out.ics"

    assert main(["export", "--semester", SEMESTER, "-o", str(output)]) == 0

    out = capsys.readouterr().out
    assert "v1 UID 配方" not in out
    assert "STATUS:CANCELLED" not in output.read_text(encoding="utf-8"), (
        "CD3：留底里缺席的旧 UID 不许被 export 撤销——手工导入的人会把它们当新事件收进去"
    )
    assert subscribe.load_state(cfg, SEMESTER).uid_recipe is None, "export 不读也不写标记（CD3）"


# --------------------------------------------------------------------------- #
# rotate：只读探针不产生迁移副作用，预告早于换 token（D24/D12）
# --------------------------------------------------------------------------- #
def test_rotate_prints_migration_notice_before_swapping_the_token(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg = home
    _register(cfg)
    archive = _publish_archive(cfg, _rows())
    archive_bytes = archive.read_bytes()
    order: list[str] = []
    captured = _stub_publish(monkeypatch, order)

    real_rotate = subscribe.rotate_token

    def spy_rotate(cfg: Settings, state: subscribe.SubscriptionState):
        order.append("rotate-token")
        # 换 token 那一刻：预告已经打印，而探针**什么都没写**
        # （D24②「探针只算不写」的课程侧对应物）。
        captured["out_at_rotate"] = capsys.readouterr().out
        captured["archive_at_rotate"] = archive.read_bytes()
        captured["course_ledger_at_rotate"] = cfg.course_ledger_path(SEMESTER).is_file()
        captured["state_at_rotate"] = subscribe.load_state(cfg, SEMESTER).uid_recipe
        return real_rotate(cfg, state)

    monkeypatch.setattr(subscribe, "rotate_token", spy_rotate)

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0

    assert "v1 UID 配方" in str(captured["out_at_rotate"])
    assert captured["archive_at_rotate"] == archive_bytes
    assert captured["course_ledger_at_rotate"] is False
    assert captured["state_at_rotate"] is None
    # 补发成功之后才回写标记，顺序与 push 同形（探针的 state:None 是 rotate_token 写的）。
    assert order == [
        "rotate-token",
        "state:None",
        "publish",
        "course-ledger",
        f"state:{UID_RECIPE_VERSION}",
    ]
    assert subscribe.load_state(cfg, SEMESTER).uid_recipe == UID_RECIPE_VERSION


def test_rotate_without_migration_keeps_its_old_behaviour(
    home: Settings, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """回归护栏：标记已在的订阅走 rotate，不许打印迁移预告、也不许再回写标记。"""
    cfg = home
    _register(cfg, uid_recipe=UID_RECIPE_VERSION)
    _publish_archive(cfg, _rows())
    order: list[str] = []
    _stub_publish(monkeypatch, order)

    assert main(["subscribe", "rotate", "--semester", SEMESTER]) == 0
    assert "v1 UID 配方" not in capsys.readouterr().out
    # 唯一一次 state 落盘是 `rotate_token` 自己写的（标记原样带着）；publish 之后不许再写。
    assert order == [f"state:{UID_RECIPE_VERSION}", "publish", "course-ledger"]
    assert subscribe.load_state(cfg, SEMESTER).uid_recipe == UID_RECIPE_VERSION


# --------------------------------------------------------------------------- #
# 课程台账本身的写盘契约（与考试台账同等待遇，CD8）
# --------------------------------------------------------------------------- #
def test_course_ledger_is_written_privately(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = home
    _register(cfg, uid_recipe=UID_RECIPE_VERSION)
    _publish_archive(cfg, _rows())
    _stub_publish(monkeypatch, [])
    seen: dict[str, object] = {}
    real = cli.atomic_write_text

    def spy(path, text, **kwargs):
        seen[Path(path).name] = kwargs.get("private")
        return real(path, text, **kwargs)

    monkeypatch.setattr("xjtu_calendar.cli.atomic_write_text", spy)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert seen[f"last-courses-{SEMESTER}.ics"] is True


def test_course_ledger_write_failure_does_not_change_push_exit_code(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D24① 的课程侧对应物：台账写不进去只是撤销依据的增量，不改 push 的成败。"""
    cfg = home
    _register(cfg, uid_recipe=UID_RECIPE_VERSION)
    _publish_archive(cfg, _rows())
    _stub_publish(monkeypatch, [])

    def boom(*args, **kwargs):
        raise OSError("disk is full")

    monkeypatch.setattr("xjtu_calendar.cli.atomic_write_text", boom)

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0


def test_untrusted_snapshot_writes_no_course_ledger(
    home: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """门 1（CD13）⇒ `course_ledger_text is None` ⇒ 既不创建也不改写文件（§6.6 空值口径）。"""
    cfg = home
    _register(cfg, uid_recipe=UID_RECIPE_VERSION)
    _publish_archive(cfg, _rows())
    # 一行看不懂周次、另一行正常：全坏的话课程侧现有的解析护栏会直接拒绝导出（§4:109），
    # 门 1 要防的正是"半份课表"这种形状。
    corrupt = _rows("示例课程甲", "示例课程乙")
    corrupt[1]["ZCMC"] = "看不懂周次"
    _rewrite_snapshot(cfg, corrupt)
    _stub_publish(monkeypatch, [])

    assert main(["subscribe", "push", "--semester", SEMESTER]) == 0
    assert not cfg.course_ledger_path(SEMESTER).exists()
