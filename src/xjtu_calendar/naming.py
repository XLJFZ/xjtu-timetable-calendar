"""日历标题：从课表数据推导「大X-上/下」后缀。

设计口径（2026-10-08 与用户确认）：

- 标题格式为 ``<基础名> · <年级>-<学期>``，例如「西安交通大学课表 · 大三-上」；
- 年级来自课表里的入学年级（教务字段 ``NJDM``），按 ``学年起始年 − 入学年 + 1``
  换算，只在 1..5（大一…大五，含五年制）范围内显示；
- 学期来自 ``YYYY-YYYY-N`` 形式的学期代码，``N=1`` 记「上」、``N=2`` 记「下」；
- **任何不确定的情况一律回退基础名**：学期 key 是用户自定义的（README 举例
  ``2026-fall``）、年级缺失/非数字/越界、多个年级票数并列 —— 宁可少信息，
  也不要给用户一个错的年级；
- 跨年级（插班、跨年级选课、留级）时取多数，并记一条 info 日志。日志走
  ``logger`` 而非 ``print``：CLI 的输出流可能被重定向到非 UTF-8 编码，
  print 中文/符号会让进程崩掉（见 v0.4.1 修复）。

本模块是纯函数，不依赖包内其他模块，便于独立测试。
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable

logger = logging.getLogger("xjtu_calendar.naming")

#: 日历标题的基础名，也是 ``--name`` 未显式给出时使用的默认值。
DEFAULT_CALENDAR_NAME = "西安交通大学课表"

#: 形如 2026-2027-1 / 2026-2027-2 的学期代码；第 3 学期（短学期制）刻意不匹配。
_SEMESTER_CODE = re.compile(r"^(\d{4})-(\d{4})-([12])$")

_CN_ORDINALS = ("一", "二", "三", "四", "五")


def _grade_label(start_year: int, enrollment_year: int) -> str | None:
    """把入学年份换算成「大一…」；越界返回 ``None`` 表示不显示。"""
    n = start_year - enrollment_year + 1
    if 1 <= n <= len(_CN_ORDINALS):
        return f"大{_CN_ORDINALS[n - 1]}"
    return None


def calendar_title(
    semester_key: str,
    grade_years: Iterable[str | None],
    *,
    base: str = DEFAULT_CALENDAR_NAME,
) -> str:
    """生成日历标题：能确定就追加「 · 大X-上/下」，不能确定就返回基础名。

    Parameters
    ----------
    semester_key:
        学期标识，例如教务返回的 ``"2026-2027-1"``。非该格式（例如用户自定义的
        ``"2026-fall"``）时不追加后缀。
    grade_years:
        每条课程安排的入学年级文本（``NJDM``），允许缺失（``None`` / 空串）。
    base:
        基础名，默认 :data:`DEFAULT_CALENDAR_NAME`。
    """
    match = _SEMESTER_CODE.match((semester_key or "").strip())
    if match is None:
        return base
    term = "上" if match.group(3) == "1" else "下"

    tokens = [str(year).strip() for year in grade_years if str(year or "").strip()]
    if not tokens:
        return base

    counts = Counter(tokens)
    top, top_count = counts.most_common(1)[0]
    if sum(1 for count in counts.values() if count == top_count) > 1:
        logger.info("课表年级分布并列（%s），日历标题不追加年级后缀", dict(counts))
        return base
    if len(counts) > 1:
        logger.info(
            "课表有 %d 条课程安排与多数年级 %s 不一致，标题按多数显示",
            len(tokens) - top_count,
            top,
        )

    if not (len(top) == 4 and top.isdigit()):
        return base
    grade = _grade_label(int(match.group(1)), int(top))
    if grade is None:
        return base
    return f"{base} · {grade}-{term}"
