"""0.9-P1-02 校历事件配置模型（先红用例）。

规格来自 `docs/design/2026-10-15-academic-calendar-source.md` §4 / §5 / §7 + §12.1–12.6。
这一笔只管**配置进来到能被判定**：解析、校验、身份、冲突记录，以及"预览可部分有效 /
正式发布整体拒绝"所依据的那份结构化错误清单。事件生成与 CLI 旗标是 P1-03。

三条最容易在实现时走样的口径，先在这里钉死：

1. **闭区间 → 半开区间只许换算一次**（§12.1）。配置写 `start..end`（含两端），
   `CalendarEvent` 与 ICS 一律半开，所以 `end` 进事件是 `end + 1 day`；
   指纹与基线读回来的也是那个不含的边界，两边同语义才不会漂移 SEQUENCE。
2. **`source_id` 必填、无降级链**（§12.3）。人工配置没有"服务端不给 ID"的客观理由，
   所以缺失/空/非法/重复一律是**配置错误**，不走考试侧那套 `WID → KSRWID → 内容组合`。
3. **错误只报序号与字段名**（§8 统一约束）。标题与来源 URL 的**值**不进错误文本，
   否则配置一贴到 issue 里就是个人数据（校历事件名可能写着"某某班补考"）。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

import pytest

from xjtu_calendar.academic_calendar import AcademicCalendar
from xjtu_calendar.calendar_events import (
    CALENDAR_EVENT_CATEGORIES,
    CalendarEventConflict,
    make_calendar_uid,
    parse_calendar_events,
)
from xjtu_calendar.errors import ParseError

# --------------------------------------------------------------------------- #
# 固件：全部是**合成**值。校历事件名刻意做成可识别的标记串，
# 好让"错误文本零原文"（§8）那条用例能真的抓到泄漏。
# --------------------------------------------------------------------------- #
MARK_TITLE = "标记-寒假标题"
MARK_URL = "https://due.xjtu.edu.cn/xxfw/标记-路径.htm"


def event_item(**over: Any) -> dict[str, Any]:
    """一条合法事件的原始 JSON 形态，允许逐字段覆盖（含把字段删掉：传 ``None`` 用 ``del_``）。"""
    item: dict[str, Any] = {
        "source_id": "2026-2027-1-winter",
        "category": "vacation",
        "title": MARK_TITLE,
        "start": "2027-01-18",
        "end": "2027-02-28",
        "source_url": MARK_URL,
    }
    remove = over.pop("remove", ())
    remove = (remove,) if isinstance(remove, str) else tuple(remove)
    item.update(over)
    for key in remove:
        item.pop(key, None)
    return item


def payload_with(
    events: list[dict[str, Any]], conflicts: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "semester": {
            "key": "2026-2027-1",
            "name": "测试学期",
            "first_week_monday": "2026-09-14",
            "total_weeks": 18,
        },
        "events": events,
    }
    if conflicts is not None:
        payload["conflicts"] = conflicts
    return payload


def fields(errors: list[Any]) -> set[tuple[int, str, str]]:
    """把错误清单压成 ``(序号, 字段名, 原因码)`` 集合——断言只该打这三样。"""
    return {(e.index, e.field, e.reason) for e in errors}


# --------------------------------------------------------------------------- #
# 1. 正常路径：一条好事件应当被完整接住
# --------------------------------------------------------------------------- #


def test_a_valid_event_parses_into_a_spec() -> None:
    result = parse_calendar_events([event_item()])

    assert result.errors == ()
    assert len(result.specs) == 1
    spec = result.specs[0]
    assert spec.source_id == "2026-2027-1-winter"
    assert spec.category == "vacation"
    assert spec.title == MARK_TITLE
    # 配置层保持**闭区间**原样：模型里存的就是人写的那两个日期，不预先加一天。
    assert spec.start == date(2027, 1, 18)
    assert spec.end_inclusive == date(2027, 2, 28)
    assert spec.source_url == MARK_URL
    assert spec.source_published_on is None
    assert spec.note is None


def test_optional_fields_are_accepted() -> None:
    result = parse_calendar_events(
        [event_item(source_published_on="2026-10-09", note="以校历图片为准")]
    )
    assert result.errors == ()
    assert result.specs[0].source_published_on == date(2026, 10, 9)
    assert result.specs[0].note == "以校历图片为准"


def test_missing_events_key_is_not_an_error() -> None:
    """没配 `events` 是常态（特性默认关）⇒ 空结果、零错误、零警告。"""
    result = parse_calendar_events(None)
    assert result.specs == ()
    assert result.conflicts == ()
    assert result.errors == ()


def test_categories_are_the_five_decided_in_section_4() -> None:
    """§4 的第一版边界：五类之外不做。枚举本身就是文档，漂了要看得见。"""
    assert set(CALENDAR_EVENT_CATEGORIES) == {
        "semester_start",
        "registration",
        "exam_period",
        "vacation",
        "holiday",
    }


# --------------------------------------------------------------------------- #
# 2. 必填与合法性：坏一条只拒那一条（预览口径），但必须留下机器可解析的错误
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("over", "expected_field", "reason"),
    [
        ({"remove": "source_id"}, "source_id", "missing"),
        ({"remove": "category"}, "category", "missing"),
        ({"remove": "title"}, "title", "missing"),
        ({"remove": "start"}, "start", "missing"),
        ({"remove": "end"}, "end", "missing"),
        ({"remove": "source_url"}, "source_url", "missing"),
        ({"source_id": ""}, "source_id", "blank"),
        ({"source_id": "   "}, "source_id", "blank"),
        ({"category": "commencement"}, "category", "unknown_category"),
        ({"title": "   "}, "title", "blank"),
        ({"start": "2027-1-18"}, "start", "not_full_iso_date"),
        ({"start": "20270118"}, "start", "not_full_iso_date"),
        ({"start": "2027-01-18T00:00"}, "start", "not_full_iso_date"),
        ({"end": "not-a-date"}, "end", "not_full_iso_date"),
        ({"start": "2027-02-28", "end": "2027-01-18"}, "end", "end_before_start"),
        ({"source_published_on": "2027-1-1"}, "source_published_on", "not_full_iso_date"),
        ({"sourse_id": "typo"}, "sourse_id", "unknown_field"),
    ],
)
def test_a_bad_item_is_reported_not_raised(
    over: dict[str, Any], expected_field: str, reason: str
) -> None:
    """逐条判定：**不许**在解析阶段抛异常——预览要能带着其余好事件继续出（§12.2）。"""
    result = parse_calendar_events([event_item(**over)])

    assert result.specs == (), "坏条目被静默接受了"
    assert (1, expected_field, reason) in fields(result.errors), result.errors


def test_single_day_event_is_legal_in_closed_interval() -> None:
    """§12.1：配置是闭区间，所以"报到 9/12 一天"写成 start == end，**不是**错误。"""
    result = parse_calendar_events(
        [event_item(start="2026-09-12", end="2026-09-12", category="registration")]
    )
    assert result.errors == ()
    assert result.specs[0].end_inclusive == result.specs[0].start


def test_good_items_survive_a_bad_neighbour() -> None:
    """一条坏配置只毙那一条，序号还得指对（第 2 条才是坏的）。"""
    result = parse_calendar_events(
        [event_item(), event_item(source_id="2026-2027-1-summer", category="nope")]
    )

    assert len(result.specs) == 1
    assert result.specs[0].source_id == "2026-2027-1-winter"
    assert fields(result.errors) == {(2, "category", "unknown_category")}


def test_event_list_must_be_an_array() -> None:
    """结构错位（写成对象/字符串）是**配置坏了**，不是"某一条坏了"⇒ 这时才允许抛。"""
    with pytest.raises(ParseError):
        AcademicCalendar.from_dict(
            {"semester": {"key": "k", "first_week_monday": "2026-09-14"}, "events": {"a": 1}}
        )


# --------------------------------------------------------------------------- #
# 3. 身份：source_id 必填 + 查重 + 独立命名空间（§5 / §12.3）
# --------------------------------------------------------------------------- #


def test_duplicate_source_id_is_a_config_error_on_the_later_item() -> None:
    """同学期重复 ⇒ 两条 UID 相撞，客户端只会留一条。判配置错误，不静默留前一条。"""
    result = parse_calendar_events([event_item(), event_item(title="第二条同 ID")])

    assert len(result.specs) == 1
    assert fields(result.errors) == {(2, "source_id", "duplicate_source_id")}


def test_source_id_characters_are_restricted() -> None:
    """`|` 是 UID 配方的分隔符，空白/控制字符会进摘要与文件名 ⇒ 非法字符算配置错误。"""
    for bad in ("a|b", "a b", "含中文的id", "a/b", "a\nb"):
        result = parse_calendar_events([event_item(source_id=bad)])
        assert fields(result.errors) == {(1, "source_id", "illegal_source_id")}, bad

    for ok in ("2026-2027-1-winter", "reg.1", "exam_period_3", "a-b_c"):
        assert parse_calendar_events([event_item(source_id=ok)]).errors == (), ok


def test_uid_recipe_is_semester_cal_source_id_and_nothing_else() -> None:
    """§12.3：配方只有 `semester_key | "cal" | source_id`——标题、日期都**不进** UID。

    改标题/改日期必须换 SEQUENCE 而不是换 UID（§5 那条"原地更新"的承诺）。
    """
    uid = make_calendar_uid("2026-2027-1", "2026-2027-1-winter")
    assert uid.endswith("@xjtu-timetable-calendar")
    assert uid == make_calendar_uid("2026-2027-1", "2026-2027-1-winter")
    assert uid != make_calendar_uid("2026-2027-2", "2026-2027-1-winter")
    assert uid != make_calendar_uid("2026-2027-1", "2026-2027-1-summer")

    # 与课程 / 考试命名空间互不相撞：同一条 payload 换个命名空间段就该是不同 UID。
    spec = parse_calendar_events([event_item()]).specs[0]
    assert spec.to_event("2026-2027-1").uid == uid


# --------------------------------------------------------------------------- #
# 4. 闭区间 → 半开区间：±1 只许发生一次（§12.1）
# --------------------------------------------------------------------------- #


def test_to_event_writes_half_open_end_for_a_multi_day_span() -> None:
    spec = parse_calendar_events([event_item()]).specs[0]
    event = spec.to_event("2026-2027-1")

    assert event.start == date(2027, 1, 18)
    # 2/28 是**含**的最后一天，所以事件里是不含的 3/1。
    assert event.end == date(2027, 3, 1)
    assert event.is_all_day is True
    assert event.summary == MARK_TITLE
    assert event.status is None, "校历事件不许带 STATUS：它不接撤销通路（§5）"


def test_to_event_single_day_span_advances_one_day() -> None:
    spec = parse_calendar_events(
        [event_item(start="2026-09-12", end="2026-09-12", category="registration")]
    ).specs[0]
    event = spec.to_event("2026-2027-1")
    assert (event.start, event.end) == (date(2026, 9, 12), date(2026, 9, 13))


def test_to_event_is_the_only_place_the_interval_shifts() -> None:
    """模型里存的仍是闭区间原值——换算发生在构造事件那一刻，不是解析那一刻。

    否则 `doctor` 打出来的日期会比用户手写的多一天，人工核对必然对不上（§7 的可核对性）。
    """
    spec = parse_calendar_events([event_item()]).specs[0]
    assert spec.start == date(2027, 1, 18)
    assert spec.end_inclusive == date(2027, 2, 28)
    assert spec.to_event("2026-2027-1").end == date(2027, 3, 1)
    # 反复换算必须幂等：不许出现 +1 两次。
    assert spec.to_event("2026-2027-1").end == spec.to_event("2026-2027-1").end


# --------------------------------------------------------------------------- #
# 5. 来源 URL：校历事件专用的官方域名清单（§12.5）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://due.xjtu.edu.cn/xxfw/xl.htm", "insecure_scheme"),
        ("ftp://due.xjtu.edu.cn/x.htm", "bad_scheme"),
        ("due.xjtu.edu.cn/xxfw/xl.htm", "bad_scheme"),
        ("/xxfw/xl.htm", "not_absolute"),
        ("https://due.xjtu.edu.cn", ""),  # 只到根，路径为空 ⇒ 不算 URL 错误
        ("https://www.xjtu.edu.cn/x.htm", ""),
        ("https://xjtu.edu.cn/x.htm", ""),
        ("https://jwc.xjtu.edu.cn/info/1095/10442.htm", ""),
        ("https://DUE.XJTU.EDU.CN/xxfw/xl.htm", ""),
    ],
)
def test_source_url_accepts_official_hosts_over_https(url: str, reason: str) -> None:
    result = parse_calendar_events([event_item(source_url=url)])
    if reason:
        assert fields(result.errors) == {(1, "source_url", reason)}, url
    else:
        assert result.errors == (), url


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://example.com/x.htm", "host_not_allowed"),
        # 后缀伪装：主机名以 xjtu.edu.cn **结尾**但不是它的子域。
        ("https://xjtu.edu.cn.evil.com/x.htm", "host_not_allowed"),
        ("https://evilxjtu.edu.cn/x.htm", "host_not_allowed"),
        ("https://due.xjtu.edu.cn.evil.net/x.htm", "host_not_allowed"),
        ("https://due.xjtu.edu.cn@evil.com/x.htm", "carries_credentials"),
        ("https://evil.com/?u=https://due.xjtu.edu.cn/x.htm", "host_not_allowed"),
        # 个人认证参数：URL 一旦贴出来就等于把凭据公开（订阅 token 那笔教训同型）。
        ("https://due.xjtu.edu.cn/x.htm?ticket=abc", "carries_credentials"),
        ("https://due.xjtu.edu.cn/x.htm?token=abc", "carries_credentials"),
        ("https://due.xjtu.edu.cn/x.htm?JSESSIONID=abc", "carries_credentials"),
        ("https://due.xjtu.edu.cn/x.htm?uid=2023123456", "carries_credentials"),
    ],
)
def test_source_url_refuses_non_official_and_credential_bearing_addresses(
    url: str, reason: str
) -> None:
    result = parse_calendar_events([event_item(source_url=url)])
    assert fields(result.errors) == {(1, "source_url", reason)}, url


def test_benign_query_parameters_are_allowed() -> None:
    """官方页常见的分页/栏目参数不是凭据，不许顺手拦掉——拦了人工录入就干脆不填来源。"""
    for url in (
        "https://due.xjtu.edu.cn/info/1095/10442.htm?cid=5",
        "https://jwc.xjtu.edu.cn/info/list.jsp?pn=2",
        "https://due.xjtu.edu.cn/xxfw/xl.htm#top",
    ):
        assert parse_calendar_events([event_item(source_url=url)]).errors == (), url


# --------------------------------------------------------------------------- #
# 6. 冲突记录：必须是机器可解析的结构（§12.6）
# --------------------------------------------------------------------------- #


def conflict_item(**over: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "topic": "winter_start",
        "source_id": "2026-2027-1-winter",
        "source_urls": [
            "https://due.xjtu.edu.cn/2026-2027.jpg",
            "https://jwc.xjtu.edu.cn/info/1095/10442.htm",
        ],
        "dates": ["2027-01-18", "2027-01-22"],
        "decided_on": "2026-10-15",
    }
    item.update(over)
    return item


def test_a_conflict_record_parses_into_structured_fields() -> None:
    result = parse_calendar_events([event_item()], [conflict_item()])

    assert result.errors == ()
    assert len(result.conflicts) == 1
    conflict = result.conflicts[0]
    assert isinstance(conflict, CalendarEventConflict)
    assert conflict.source_id == "2026-2027-1-winter"
    assert conflict.dates == (date(2027, 1, 18), date(2027, 1, 22))
    assert conflict.decided_on == datetime(2026, 10, 15).date()
    assert conflict.resolved is False, "缺 adjudicated_by 就是**未裁决**，默认必须是 False"
    assert result.has_unresolved_conflicts is True


def test_adjudicated_conflict_is_marked_resolved() -> None:
    result = parse_calendar_events(
        [event_item()], [conflict_item(adjudicated_by="以教务部校历图片为准，通知那份是转发")]
    )
    assert result.conflicts[0].resolved is True
    assert result.has_unresolved_conflicts is False


@pytest.mark.parametrize(
    ("over", "expected_field", "reason"),
    [
        ({"source_urls": ["https://due.xjtu.edu.cn/a.htm"]}, "source_urls", "needs_two_sources"),
        ({"source_urls": []}, "source_urls", "needs_two_sources"),
        (
            {"source_urls": ["https://due.xjtu.edu.cn/a.htm"] * 3},
            "source_urls",
            "needs_two_sources",
        ),
        ({"dates": ["2027-01-18"]}, "dates", "needs_two_dates"),
        ({"dates": ["2027-01-18", "2027-1-22"]}, "dates", "not_full_iso_date"),
        (
            {"source_urls": ["https://due.xjtu.edu.cn/a.htm", "https://example.com/b.htm"]},
            "source_urls",
            "host_not_allowed",
        ),
        ({"remove_source_id": True}, "source_id", "missing"),
        ({"decided_on": "2026-10-5"}, "decided_on", "not_full_iso_date"),
        ({"topic": "  "}, "topic", "blank"),
    ],
)
def test_malformed_conflict_is_a_config_error(
    over: dict[str, Any], expected_field: str, reason: str
) -> None:
    item = conflict_item()
    if over.pop("remove_source_id", False):
        item.pop("source_id")
    item.update(over)
    result = parse_calendar_events([event_item()], [item])
    assert (1, expected_field, reason) in fields(result.errors), result.errors


def test_conflict_must_point_at_a_declared_source_id() -> None:
    """冲突记录指不到任何事件 ⇒ 它拦不住任何东西，按配置错误处理（§12.6 的"机器可解析"要能落地）。"""
    result = parse_calendar_events([event_item()], [conflict_item(source_id="2026-2027-1-unknown")])
    assert fields(result.errors) == {(1, "source_id", "unknown_source_id")}


# --------------------------------------------------------------------------- #
# 6b. §12.2 的准入判据：预览可以宽容，发布不行
# --------------------------------------------------------------------------- #


def test_clean_config_is_not_publish_blocked() -> None:
    result = parse_calendar_events([event_item()], [conflict_item(adjudicated_by="以校历图片为准")])
    assert result.publish_blocked is False


def test_a_single_bad_event_blocks_the_whole_publish() -> None:
    """**整体拒绝**：一条坏配置不是"少一条"，而是这次发布不许发生。

    校历事件不接撤销通路 ⇒ "配置写错一条 ⇒ 线上少一条"是可感知却无法恢复的代价（§12.2 动机）。
    """
    assert parse_calendar_events([event_item(category="nope")]).publish_blocked is True


def test_an_unadjudicated_conflict_blocks_the_publish_and_names_its_event() -> None:
    result = parse_calendar_events([event_item()], [conflict_item()])
    assert result.publish_blocked is True
    assert result.has_unresolved_conflicts is True
    assert result.blocked_source_ids() == frozenset({"2026-2027-1-winter"})

    adjudicated = parse_calendar_events(
        [event_item()], [conflict_item(adjudicated_by="以教务部校历图片为准")]
    )
    assert adjudicated.publish_blocked is False
    assert adjudicated.blocked_source_ids() == frozenset()


# --------------------------------------------------------------------------- #
# 7. §8 口径：错误文本零原文
# --------------------------------------------------------------------------- #


def test_error_messages_never_carry_the_title_or_the_url_value() -> None:
    """标题与来源 URL 的**值**不进错误文本——这份清单会被打进日志、也会贴进 issue。"""
    result = parse_calendar_events(
        [
            event_item(category="nope"),
            event_item(
                source_id="2026-2027-1-summer", source_url="https://example.com/标记-url.htm"
            ),
            event_item(source_id="2026-2027-1-a", start="2027-13-01"),
        ],
        [
            conflict_item(
                source_id="2026-2027-1-a",
                source_urls=[
                    "https://due.xjtu.edu.cn/标记-a.htm",
                    "https://jwc.xjtu.edu.cn/标记-b.htm",
                ],
                dates=["2027-01-18", "2027-1-22"],
            )
        ],
    )
    assert result.errors, "固件本来就该触发若干错误"

    for error in result.errors:
        text = error.describe()
        assert MARK_TITLE not in text
        assert "标记-url" not in text
        assert "标记-a" not in text
        assert "标记-b" not in text
        assert MARK_URL not in text
        # 允许出现的只有：序号、字段名、原因码。
        assert str(error.index) in text


# --------------------------------------------------------------------------- #
# 8. 接进 AcademicCalendar：既有配置零改动、既有行为零改动
# --------------------------------------------------------------------------- #


def test_academic_calendar_exposes_events_and_conflicts() -> None:
    """数据落在**同一份学期配置**里（§4 的落点裁定），由 `AcademicCalendar` 一并交出去。"""
    cal = AcademicCalendar.from_dict(payload_with([event_item()], [conflict_item()]))
    assert len(cal.calendar_events.specs) == 1
    assert len(cal.calendar_events.conflicts) == 1
    assert cal.calendar_events.has_unresolved_conflicts is True


def test_config_without_events_still_loads_exactly_as_before() -> None:
    """没有 `events` 键的既有配置文件必须照常加载（默认关 ⇒ 产物字节不变的地基）。"""
    cal = AcademicCalendar.from_dict(
        {
            "semester": {"key": "2026-fall", "first_week_monday": "2026-09-07", "total_weeks": 16},
            "excluded_dates": ["2026-10-01"],
        }
    )
    assert cal.calendar_events.specs == ()
    assert cal.calendar_events.conflicts == ()
    assert cal.calendar_events.errors == ()
    assert cal.is_excluded(date(2026, 10, 1)) is True
