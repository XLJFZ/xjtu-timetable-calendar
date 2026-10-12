"""课程侧独有的判据：数据可信（门 1）、比例熔断（门 2）、迁移候选。

通用撤销机制（台账、候选数学、撤销事件构建）在 `cancellation.py`，考试与课程共用。
本模块全是**纯函数**：不读文件、不发请求、不写盘。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from .cancellation import LedgerEntry
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


def upcoming_ledger_count(ledger: Mapping[str, LedgerEntry], now: datetime) -> int:
    """门 2 的分母：台账里**原定时刻还没过去**的条目数。

    分子 `deliverable` 只数未来条目（`cancel_candidates` 会把 `start < now` 筛掉），
    分母若用整本台账，学期中段"抓漏一半课表"的比例会被过期条目稀释到 30% 以下——
    熔断恰好在该响的时候不响。分子分母必须同域。
    """
    return sum(1 for entry in ledger.values() if entry.start >= now)


def mass_cancellation_check(*, candidates: int, ledger_total: int) -> MassCancellation | None:
    """候选数 ≥ 10 **且** 占门 2 分母（未过期台账条目，见 :func:`upcoming_ledger_count`）≥ 30% ⇒ 熔断（CD12）。

    双条件是为了不误伤两种形状：小规模调整（候选不足 10 条，无论比例）与大日历的
    局部异常（未过期 500 条里撤 20 条 ≈ 4%，比例不达标）。命中即由调用方拒绝发布——
    课程侧最坏的错位是"我们以为撤销过了，订阅端从没收到"（CD4）。
    """
    if ledger_total <= 0 or candidates < MASS_CANCELLATION_MIN_EVENTS:
        return None
    ratio = candidates / ledger_total
    if ratio < MASS_CANCELLATION_RATIO:
        return None
    return MassCancellation(candidates=candidates, ledger_total=ledger_total, ratio=ratio)


def migration_candidates(
    published: Mapping[str, LedgerEntry],
    live_uids: Collection[str],
    normal_cancel_uids: Collection[str],
    now: datetime,
) -> list[LedgerEntry]:
    """v1→v2 那一次的全量撤销候选（CD7/CD10）。

    ``published`` 是从**发布留底**解析出来的 `{UID: LedgerEntry}`——留底本身就是上次
    发出去的完整事件清单，所以旧 UID 不需要重算配方就能拿到（CD15），而且这些 UID
    天然都在 SEQUENCE 基线里，D20 那道筛自动全过。

    减去两个集合：本次 live（同 UID 是原地更新，不该撤）、本次常态撤销（避免同 UID
    双条目）。再过一遍 D2 过期筛：时刻已过的条目**不撤**，否则留底里那些"上次已撤销"
    的条目会因为不在任何台账里而永远出不去（CD10）。
    输出按 ``(start, uid)`` 全序，与 `cancel_candidates` 同口径。
    """
    picked = [
        entry
        for uid, entry in published.items()
        if uid not in live_uids and uid not in normal_cancel_uids and entry.start >= now
    ]
    return sorted(picked, key=lambda e: (e.start, e.uid))
