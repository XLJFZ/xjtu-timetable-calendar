"""校历事件的配置模型（v0.9-P1-02）。

规格来自 ``docs/design/2026-10-15-academic-calendar-source.md`` §4 / §5 / §7 + §12.1–12.6。
本模块只管**配置进来之后能被判定**：解析、逐条校验、身份配方、冲突记录，
以及"预览可部分有效 / 正式发布整体拒绝"（§12.2）所依据的那份结构化错误清单。
把事件接进产物与 CLI 旗标是 P1-03，端到端彩排是 P1-04。

三条容易被"顺手改宽"的口径，都在这里写死：

1. **配置是闭区间，产物是半开区间**（§12.1）。模型里存的是人手写的那两个日期
   （``end_inclusive`` 含端点），只有在 :meth:`CalendarEventSpec.to_event` 那一刻才
   ``+1 day`` 变成 ICS 的 ``DTEND``。加减只许发生这一处 —— 别处再加一次，
   基线回读的指纹就会和新产物不一致，每次重发布都凭空 ``SEQUENCE`` +1。
2. **``source_id`` 必填、没有降级链**（§12.3）。人工配置不存在"服务端不给 ID"，
   所以缺失 / 空 / 非法字符 / 同学期重复一律算**配置错误**，不走考试侧
   ``WID → KSRWID → 内容组合`` 那套。身份由人给，日期改了就原地更新（同 UID、``SEQUENCE`` +1）。
3. **错误只报序号、字段名、原因码**（§8 统一约束）。事件标题与来源 URL 的**值**
   不进错误文本，也不进日志 —— 这份清单会被原样打印，用户贴进 issue 就等于公开。

校历事件**不参与撤销通路**（§5）：:meth:`to_event` 永不写 ``status``，
所以 ``cancellation.render_ledger`` 那条"台账只收计时事件"的硬闸与它互不干涉。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import parse_qsl, urlsplit

from .errors import ParseError
from .models import CalendarEvent
from .naming import UID_DOMAIN

__all__ = [
    "CALENDAR_EVENT_CATEGORIES",
    "CalendarEventConfig",
    "CalendarEventConflict",
    "CalendarEventError",
    "CalendarEventSpec",
    "make_calendar_uid",
    "parse_calendar_events",
]

# --------------------------------------------------------------------------- #
# 枚举与常量
# --------------------------------------------------------------------------- #

#: §4 的第一版边界：只做这五类**有明确来源**的全天事件，范围之外一律不做。
#: 顺序即配置模板里的推荐顺序；判据只看成员关系，不看顺序。
CALENDAR_EVENT_CATEGORIES: tuple[str, ...] = (
    "semester_start",  # 学期开始 / 起课日
    "registration",  # 报到注册区间
    "exam_period",  # 学校公布的期末考试周（**不许**由 total_weeks 推）
    "vacation",  # 寒假 / 暑假
    "holiday",  # 学校公布的放假调休区间
)

#: UID 配方里代表"校历事件"这一命名空间的那一段（§5：与课程 / 事件分开）。
UID_NAMESPACE = "cal"

#: ``source_id`` 的合法字符。刻意**不含** ``|``（它是配方分隔符）、
#: 空白与控制字符（它们会渗进摘要、文件名与日志），也不含非 ASCII
#: （配置文件要能在终端里逐字核对，中文 ID 看不出撞了谁）。
_SOURCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: 完整 ISO 日期（``YYYY-MM-DD``）。放宽成"任意 ISO 变体"会让 `2027-1-18`
#: 与 `20270118` 都进得来，人工核对时看不出是抄错还是格式不同。
_FULL_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

#: 校历事件来源 URL 的官方域名清单（§12.5）。
#:
#: ⚠️ 这是**本模块自己的清单**，不复用任何既有校验：
#: ``notices.fetch_notice_html`` 实测只检查 http/https scheme，没有域名清单，
#: 而 ``fetcher`` 的清单是为"凭据往哪个域发送"设计的，语义不同。
#: 判据是"这个主机名属于西安交通大学"：根域或任意子域，且必须是**点边界**
#: （``evilxjtu.edu.cn`` 与 ``xjtu.edu.cn.evil.com`` 都不算）。
OFFICIAL_SOURCE_SUFFIX = "xjtu.edu.cn"

#: 地址里不许出现的参数名（§12.5"禁止带个人认证参数的地址"）。
#: 校历事件的来源应当是**公开页面**：一个永久链接不需要 ticket/token/身份参数。
#: 拦掉它们是因为这类串一旦进公开产物就等于把凭据或个人标识公开
#: （与订阅 token 那笔教训同型：公开仓库上"知道 URL 才能读"并不成立）。
_CREDENTIAL_PARAMS = frozenset(
    {
        "ticket",
        "st",
        "token",
        "access_token",
        "accesstoken",
        "id_token",
        "refresh_token",
        "code",
        "auth",
        "authorization",
        "session",
        "sessionid",
        "jsessionid",
        "phpsessid",
        "cookie",
        "apikey",
        "api_key",
        "key",
        "secret",
        "signature",
        "sig",
        "pwd",
        "password",
        "passwd",
        "uid",
        "userid",
        "user_id",
        "username",
        "account",
        "email",
        "phone",
        "mobile",
    }
)


# --------------------------------------------------------------------------- #
# 错误：机器可解析的最小记录（§12.2 / §12.6 的判据来源）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CalendarEventError:
    """一条配置错误：**只有** 序号 / 所属清单 / 字段名 / 原因码。

    刻意不存原值。要给人看的文案走 :meth:`describe`，它同样只许拼这四样。
    """

    index: int
    """在 ``events`` 或 ``conflicts`` 数组里的序号（从 1 起，与人工核对时数的那一行一致）。"""

    field: str
    """坏在哪个字段名；整条记录形态不对时是 ``_item``。"""

    reason: str
    """原因码（``missing`` / ``blank`` / ``not_full_iso_date`` / ``duplicate_source_id`` /
    ``host_not_allowed`` / ``carries_credentials`` / ``needs_two_sources`` …）。
    码本身是契约的一部分：CLI 与彩排都按它判定，别随意改名。"""

    section: str = "events"
    """来自哪份清单：``events`` 或 ``conflicts``。两者序号各自从 1 起。"""

    def describe(self) -> str:
        """一行可打印文案（§8：零原文）。"""
        where = "校历事件" if self.section == "events" else "校历冲突记录"
        return f"{where}第 {self.index} 条的 {self.field} 不合法（{self.reason}）"


# --------------------------------------------------------------------------- #
# 单条事件
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CalendarEventSpec:
    """配置里的一条校历事件，保持**人工录入的形态**（闭区间、原始字符串日期已经过校验）。

    ``index`` 是它在配置数组里的序号，只用于把错误指回用户能数到的那一行。
    """

    source_id: str
    category: str
    title: str
    start: date
    end_inclusive: date
    source_url: str
    source_published_on: date | None = None
    note: str | None = None
    index: int = 0

    @property
    def end_exclusive(self) -> date:
        """ICS ``DTEND`` 要的那个**不含**边界：闭区间末值 + 1 天（§12.1 唯一一次换算）。"""
        return self.end_inclusive + timedelta(days=1)

    def to_event(self, semester_key: str, description: str | None = None) -> CalendarEvent:
        """配置形态 → 产物形态。

        全天事件用 ``date`` 对（``VALUE=DATE``，见 P1-01 那套基础设施），
        ``end`` 是不含的边界；``status`` 永不设置 —— 校历事件不接撤销通路（§5）。
        """
        return CalendarEvent(
            uid=make_calendar_uid(semester_key, self.source_id),
            summary=self.title,
            start=self.start,
            end=self.end_exclusive,
            location=None,
            description=description,
        )


# --------------------------------------------------------------------------- #
# 冲突记录（§7 / §12.6）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CalendarEventConflict:
    """两个一手来源互相矛盾的**结构化**记录（§12.6：不许只有自由文本）。

    ``adjudicated_by`` 写了才算裁决过：它是"人怎么定的"一句话凭据，
    没有它 ⇒ :attr:`resolved` 为 ``False`` ⇒ 正式发布整体拒绝（§12.2）。
    """

    topic: str
    source_id: str
    source_urls: tuple[str, str]
    dates: tuple[date, date]
    decided_on: date
    adjudicated_by: str | None = None
    index: int = 0

    @property
    def resolved(self) -> bool:
        return bool(self.adjudicated_by)


# --------------------------------------------------------------------------- #
# 一次解析的全部产出
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class CalendarEventConfig:
    """``events`` + ``conflicts`` 的解析结果，P1-03 的准入判据全在这里。"""

    specs: tuple[CalendarEventSpec, ...] = ()
    conflicts: tuple[CalendarEventConflict, ...] = ()
    errors: tuple[CalendarEventError, ...] = ()

    @property
    def has_unresolved_conflicts(self) -> bool:
        return any(not conflict.resolved for conflict in self.conflicts)

    @property
    def publish_blocked(self) -> bool:
        """§12.2：正式发布**整体拒绝**的两个条件。预览不受此限制（可以带着好事件出）。"""
        return bool(self.errors) or self.has_unresolved_conflicts

    def blocked_source_ids(self) -> frozenset[str]:
        """未裁决冲突点名的那些事件身份 ⇒ 两边都不生成（§7）。"""
        return frozenset(c.source_id for c in self.conflicts if not c.resolved)


# --------------------------------------------------------------------------- #
# 身份
# --------------------------------------------------------------------------- #
def make_calendar_uid(semester_key: str, source_id: str) -> str:
    """校历事件的稳定 UID：``semester_key | "cal" | source_id``（§12.3 只有这一种配方）。

    与课程 / 考试用同一个摘要形状与域名后缀，但命名空间段是 ``cal`` ⇒
    三个来源的事件不会互相撞号。改标题、改日期都不换 UID ——
    身份是人给的 ``source_id``，客户端因此收到的是"同一事件的新版本"（``SEQUENCE`` +1）。
    """
    payload = "|".join([semester_key.strip(), UID_NAMESPACE, source_id.strip()])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"{digest}@{UID_DOMAIN}"


# --------------------------------------------------------------------------- #
# 解析入口
# --------------------------------------------------------------------------- #
def parse_calendar_events(
    raw_events: object,
    raw_conflicts: object = None,
) -> CalendarEventConfig:
    """解析学期配置里的 ``events`` 与 ``conflicts`` 两段。

    逐条判定、**不抛**：一条坏配置只毙它自己（预览要能带着其余好事件出，§12.2）。
    只有"整段形态不对"（``events`` 不是数组）才抛 :class:`~xjtu_calendar.errors.ParseError` ——
    那种情况下每条记录的序号都无从谈起，指不回用户写的那一行。
    """
    if raw_events is None:
        if raw_conflicts is not None and not isinstance(raw_conflicts, list):
            raise ParseError(
                "conflicts 必须是数组（每条含 topic / source_id / source_urls / dates / decided_on）"
            )
        conflicts, conflict_errors = _parse_conflicts(raw_conflicts, ())
        return CalendarEventConfig(specs=(), conflicts=conflicts, errors=tuple(conflict_errors))
    if not isinstance(raw_events, list):
        raise ParseError(
            "events 必须是数组（每条含 source_id / category / title / start / end / source_url）"
        )

    specs: list[CalendarEventSpec] = []
    errors: list[CalendarEventError] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_events, start=1):
        spec, item_errors = _parse_event_item(item, index, seen)
        errors.extend(item_errors)
        if spec is not None:
            specs.append(spec)
            seen.add(spec.source_id)

    conflicts, conflict_errors = _parse_conflicts(raw_conflicts, specs)
    errors.extend(conflict_errors)
    return CalendarEventConfig(specs=tuple(specs), conflicts=tuple(conflicts), errors=tuple(errors))


def _parse_event_item(
    item: object,
    index: int,
    seen: Iterable[str],
) -> tuple[CalendarEventSpec | None, tuple[CalendarEventError, ...]]:
    """校验一条事件。返回 ``(spec 或 None, 这条记录上的全部错误)``。

    只要有任何一条错误就**不收这条事件**：部分接受一条已知有病的记录，
    等于把"哪来的错"留给产物去暴露。
    """
    if not isinstance(item, Mapping):
        return None, (CalendarEventError(index, "_item", "not_an_object"),)

    already_seen = set(seen)
    errors: list[CalendarEventError] = []
    required = ("source_id", "category", "title", "start", "end", "source_url")

    for key in item:
        if not isinstance(key, str):
            errors.append(CalendarEventError(index, "_item", "key_not_a_string"))
        elif key.startswith("_"):
            continue  # `_readme` 这类注释键与既有配置同一口径（`_` 前缀一律不作数）
        elif key not in {*required, "source_published_on", "note"}:
            errors.append(CalendarEventError(index, key, "unknown_field"))

    source_id = _text(item, "source_id", index, errors, section="events")
    if source_id is not None and not source_id:
        errors.append(CalendarEventError(index, "source_id", "blank"))
        source_id = None
    if source_id is not None and not _SOURCE_ID_RE.match(source_id):
        errors.append(CalendarEventError(index, "source_id", "illegal_source_id"))
        source_id = None
    if source_id is not None and source_id in already_seen:
        errors.append(CalendarEventError(index, "source_id", "duplicate_source_id"))
        source_id = None

    category = _text(item, "category", index, errors, section="events")
    if category is not None and not category:
        errors.append(CalendarEventError(index, "category", "blank"))
        category = None
    if category is not None and category not in CALENDAR_EVENT_CATEGORIES:
        errors.append(CalendarEventError(index, "category", "unknown_category"))
        category = None

    title = _text(item, "title", index, errors, section="events")
    if title is not None and not title.strip():
        errors.append(CalendarEventError(index, "title", "blank"))
        title = None

    start = _date_field(item, "start", index, errors)
    end = _date_field(item, "end", index, errors)
    if start is not None and end is not None and end < start:
        errors.append(CalendarEventError(index, "end", "end_before_start"))

    url = _text(item, "source_url", index, errors, section="events")
    if url is not None:
        url_reason = _source_url_reason(url)
        if url_reason:
            errors.append(CalendarEventError(index, "source_url", url_reason))
            url = None

    published_raw = item.get("source_published_on")
    published: date | None = None
    if published_raw not in (None, ""):
        published = _date_field(item, "source_published_on", index, errors)

    note_raw = item.get("note")
    note: str | None = None
    if note_raw is not None:
        if not isinstance(note_raw, str):
            errors.append(CalendarEventError(index, "note", "not_a_string"))
        elif note_raw.strip():
            note = note_raw.strip()

    if errors or None in (source_id, category, title, start, end, url):
        return None, tuple(errors)

    assert source_id and category and title and start and end and url  # 上面已共同保证
    return (
        CalendarEventSpec(
            source_id=source_id,
            category=category,
            title=title.strip(),
            start=start,
            end_inclusive=end,
            source_url=url,
            source_published_on=published,
            note=note,
            index=index,
        ),
        (),
    )


def _text(
    item: Mapping[str, object],
    key: str,
    index: int,
    errors: list[CalendarEventError],
    *,
    section: str,
) -> str | None:
    """读一个必填字符串字段；缺失记 ``missing``、非字符串记 ``not_a_string``，都返回 ``None``。

    "值为空白"不在这里判定 —— 每个字段对空白的处置不同（标题算空、URL 算空、
    ``source_id`` 还要再看字符合法性），所以留给调用方。
    """
    if key not in item:
        errors.append(CalendarEventError(index, key, "missing", section))
        return None
    value = item[key]
    if not isinstance(value, str):
        errors.append(CalendarEventError(index, key, "not_a_string", section))
        return None
    return value.strip()


def _date_field(
    item: Mapping[str, object],
    key: str,
    index: int,
    errors: list[CalendarEventError],
    *,
    section: str = "events",
) -> date | None:
    """必填的完整 ISO 日期（``YYYY-MM-DD``）；缺失与格式不对分别记错误。"""
    if key not in item:
        errors.append(CalendarEventError(index, key, "missing", section))
        return None
    parsed = _full_iso_date(item[key])
    if parsed is None:
        errors.append(CalendarEventError(index, key, "not_full_iso_date", section))
    return parsed


def _source_url_reason(url: str) -> str | None:
    """来源 URL 的三项校验（协议 / 主机名 / 格式）+ 凭据参数禁令；返回原因码或 ``None``。"""
    text = url.strip()
    if not text:
        return "blank"
    try:
        parts = urlsplit(text)
        query_keys = {
            key.strip().lower() for key, _ in parse_qsl(parts.query, keep_blank_values=True)
        }
    except ValueError:
        return "malformed_url"

    if parts.scheme.lower() not in ("http", "https"):
        # 没写 scheme 但看得出是个域名（`due.xjtu.edu.cn/x.htm`）⇒ 提示"缺协议"比提示"不是绝对地址"更有用。
        if parts.scheme or parts.netloc:
            return "bad_scheme"
        first_segment = text.split("/", 1)[0]
        return "bad_scheme" if "." in first_segment else "not_absolute"
    if parts.scheme.lower() == "http":
        return "insecure_scheme"

    if parts.username or parts.password or "@" in (parts.netloc or ""):
        return "carries_credentials"

    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return "not_absolute"
    if host != OFFICIAL_SOURCE_SUFFIX and not host.endswith(f".{OFFICIAL_SOURCE_SUFFIX}"):
        return "host_not_allowed"

    if _CREDENTIAL_PARAMS & query_keys:
        return "carries_credentials"
    return None


def _parse_conflicts(
    raw_conflicts: object,
    specs: Sequence[CalendarEventSpec],
) -> tuple[tuple[CalendarEventConflict, ...], list[CalendarEventError]]:
    if raw_conflicts is None:
        return (), []
    if not isinstance(raw_conflicts, list):
        raise ParseError(
            "conflicts 必须是数组（每条含 topic / source_id / source_urls / dates / decided_on）"
        )

    known_ids = {spec.source_id for spec in specs}
    conflicts: list[CalendarEventConflict] = []
    errors: list[CalendarEventError] = []

    for index, item in enumerate(raw_conflicts, start=1):
        if not isinstance(item, Mapping):
            errors.append(CalendarEventError(index, "_item", "not_an_object", "conflicts"))
            continue

        item_errors: list[CalendarEventError] = []
        for key in item:
            if not isinstance(key, str):
                item_errors.append(
                    CalendarEventError(index, "_item", "key_not_a_string", "conflicts")
                )
            elif key.startswith("_"):
                continue
            elif key not in {
                "topic",
                "source_id",
                "source_urls",
                "dates",
                "decided_on",
                "adjudicated_by",
            }:
                item_errors.append(CalendarEventError(index, key, "unknown_field", "conflicts"))

        topic = _text(item, "topic", index, item_errors, section="conflicts")
        if topic is not None and not topic:
            item_errors.append(CalendarEventError(index, "topic", "blank", "conflicts"))
            topic = None

        source_id = _text(item, "source_id", index, item_errors, section="conflicts")
        if source_id is not None and source_id not in known_ids:
            # 指不到任何事件 ⇒ 它拦不住任何东西，只能算写错了。
            item_errors.append(
                CalendarEventError(index, "source_id", "unknown_source_id", "conflicts")
            )

        raw_urls = _pair_field(item, "source_urls", index, item_errors)
        urls: tuple[str, str] | None = None
        if raw_urls is not None:
            checked: list[str] = []
            for value in raw_urls:
                if not isinstance(value, str):
                    item_errors.append(
                        CalendarEventError(index, "source_urls", "not_a_string", "conflicts")
                    )
                    continue
                reason = _source_url_reason(value)
                if reason:
                    item_errors.append(
                        CalendarEventError(index, "source_urls", reason, "conflicts")
                    )
                else:
                    checked.append(value)
            if len(checked) == 2 and not any(e.field == "source_urls" for e in item_errors):
                urls = (checked[0], checked[1])

        raw_dates = _pair_field(item, "dates", index, item_errors)
        parsed_dates: tuple[date, date] | None = None
        if raw_dates is not None:
            first, second = _full_iso_date(raw_dates[0]), _full_iso_date(raw_dates[1])
            if first is not None and second is not None:
                parsed_dates = (first, second)
            else:
                item_errors.append(
                    CalendarEventError(index, "dates", "not_full_iso_date", "conflicts")
                )

        decided_on = _date_field(item, "decided_on", index, item_errors, section="conflicts")

        adjudicated_raw = item.get("adjudicated_by")
        adjudicated_by: str | None = None
        if adjudicated_raw is not None:
            if not isinstance(adjudicated_raw, str):
                item_errors.append(
                    CalendarEventError(index, "adjudicated_by", "not_a_string", "conflicts")
                )
            elif adjudicated_raw.strip():
                adjudicated_by = adjudicated_raw.strip()

        if item_errors or topic is None or source_id is None:
            errors.extend(item_errors)
            continue
        if urls is None or parsed_dates is None or decided_on is None:
            # 上面已各自记过错误；这里只是给类型收窄留一道明牌。
            errors.extend(item_errors)
            continue

        conflicts.append(
            CalendarEventConflict(
                topic=topic,
                source_id=source_id,
                source_urls=urls,
                dates=parsed_dates,
                decided_on=decided_on,
                adjudicated_by=adjudicated_by,
                index=index,
            )
        )
        errors.extend(item_errors)

    return tuple(conflicts), errors


def _full_iso_date(value: object) -> date | None:
    """把配置里的一个值解析成完整 ISO 日期；不合法返回 ``None``（调用方负责记错误）。"""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and _FULL_ISO_DATE_RE.match(value.strip()):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _pair_field(
    item: Mapping[str, object],
    key: str,
    index: int,
    errors: list[CalendarEventError],
) -> tuple[object, object] | None:
    """`source_urls` / `dates` 这类**必须恰好两项**的数组（§12.6 的"两条来源 + 两个日期值"）。"""
    if key not in item:
        errors.append(CalendarEventError(index, key, "missing", "conflicts"))
        return None
    value = item[key]
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        errors.append(CalendarEventError(index, key, "needs_two_values", "conflicts"))
        return None
    if len(value) != 2:
        errors.append(
            CalendarEventError(
                index,
                key,
                "needs_two_sources" if key == "source_urls" else "needs_two_dates",
                "conflicts",
            )
        )
        return None
    return (value[0], value[1])
