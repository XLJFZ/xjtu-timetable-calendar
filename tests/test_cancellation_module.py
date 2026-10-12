"""CD9：通用机制搬到 cancellation.py 后，考试侧必须还是同一批对象（不是复制品）。"""

from __future__ import annotations


def test_exams_reexports_the_same_objects() -> None:
    import functools

    from xjtu_calendar import cancellation, exams

    assert exams.render_exam_ledger is cancellation.render_ledger
    # 预飞裁定：`load_exam_ledger` 是 `partial(load_ledger, label="考试台账")` 而不是
    # lambda，所以它**不是** `load_ledger` 本身；断言底函数与关键字，证明没有复制品。
    assert isinstance(exams.load_exam_ledger, functools.partial)
    assert exams.load_exam_ledger.func is cancellation.load_ledger
    assert exams.load_exam_ledger.keywords == {"label": "考试台账"}
    assert exams.cancel_candidates is cancellation.cancel_candidates
    assert exams.build_cancellation_events is cancellation.build_cancellation_events
    assert exams.LedgerEntry is cancellation.LedgerEntry
    assert exams.CANCELLATION_SUMMARY == cancellation.CANCELLATION_SUMMARY == "已取消的活动"
    assert exams.EXAM_STATUS_CANCELLED == cancellation.STATUS_CANCELLED == "CANCELLED"
