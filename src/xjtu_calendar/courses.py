"""课程侧独有的判据：数据可信（门 1）、比例熔断（门 2）、迁移候选。

通用撤销机制（台账、候选数学、撤销事件构建）在 `cancellation.py`，考试与课程共用。
本模块全是**纯函数**：不读文件、不发请求、不写盘。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .models import CourseMeeting


@dataclass(frozen=True)
class CoursesTrust:
    """门 1 的结论。`reason` 只允许是类别词，**不得**携带课程名/教师/教室（规格 §8）。"""

    trusted: bool
    reason: str | None = None


def assess_courses(
    *,
    total_candidates: int,
    skipped: Sequence[str],
    meetings: Sequence[CourseMeeting],
) -> CoursesTrust:
    """只有"响应里有课记录、每一行都解析成功、且至少解析出一门课"才允许算撤销（CD13）。

    这是考试侧 D18 的同构判据，但课程侧的后果是**整学期**：`live = ∅` 在撤销语义下
    等于"取消整学期"，所以必须是白名单（默认不放开）。
    `--from-file` 本身**不**取消信任——离线导入是常态操作，判据看的是数据完整性，
    不是它从哪来。
    """
    if total_candidates == 0:
        return CoursesTrust(False, "响应中没有任何课程记录")
    if skipped:
        return CoursesTrust(False, f"课表有 {len(skipped)} 行解析不了")
    if not meetings:
        return CoursesTrust(False, "零门课程安排")
    return CoursesTrust(True)


#: 熔断判据的两个常量（CD12）。**刻意不做成配置项**：可调的保险丝等于没有保险丝，
#: 真要改数值走规格，不走命令行。
MASS_CANCELLATION_MIN_EVENTS = 10
MASS_CANCELLATION_RATIO = 0.30


@dataclass(frozen=True)
class MassCancellation:
    candidates: int
    ledger_total: int
    ratio: float


def mass_cancellation_check(*, candidates: int, ledger_total: int) -> MassCancellation | None:
    """候选数 ≥ 10 **且** 占台账事件总数 ≥ 30% ⇒ 熔断（CD12）。

    双条件是为了不误伤两种形状：小规模调整（候选不足 10 条，无论比例）与大日历的
    局部异常（500 条里撤 20 条 ≈ 4%，比例不达标）。命中即由调用方拒绝发布——
    课程侧最坏的错位是"我们以为撤销过了，订阅端从没收到"（CD4）。
    """
    if ledger_total <= 0 or candidates < MASS_CANCELLATION_MIN_EVENTS:
        return None
    ratio = candidates / ledger_total
    if ratio < MASS_CANCELLATION_RATIO:
        return None
    return MassCancellation(candidates=candidates, ledger_total=ledger_total, ratio=ratio)
