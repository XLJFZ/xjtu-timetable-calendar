"""项目异常体系。

设计目标：CLI 层能够针对不同失败原因给出**可读的中文提示**，
而不是把 Python traceback 直接甩给用户。

约定
----
- 所有业务异常继承 :class:`XjtuCalendarError`。
- ``AuthenticationRequired`` 与 ``AuthenticationExpired`` 语义不同：
  前者是「从未登录」，后者是「登录过但已失效」。CLI 对两者的引导语不同。
- 401/403 一旦出现即视为终态，**不做重试**。
"""

from __future__ import annotations

__all__ = [
    "AuthenticationExpired",
    "AuthenticationRequired",
    "CalendarExportError",
    "EndpointNotConfigured",
    "ParseError",
    "PeriodError",
    "PermissionDenied",
    "ScheduleNotConfigured",
    "SemesterNotConfigured",
    "TimetableFetchError",
    "WeekError",
    "XjtuCalendarError",
]


class XjtuCalendarError(Exception):
    """所有业务异常的基类。"""

    #: 建议的进程退出码，供 CLI 使用
    exit_code: int = 1

    #: 面向用户的修复建议（可为空）
    hint: str = ""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if hint is not None:
            self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - 简单拼装
        return self.message


# --------------------------------------------------------------------------- #
# 认证 / 权限
# --------------------------------------------------------------------------- #
class AuthenticationRequired(XjtuCalendarError):
    """本地不存在可用会话，需要用户先完成一次登录。"""

    exit_code = 2
    hint = "请先运行：python -m xjtu_calendar login"


class AuthenticationExpired(XjtuCalendarError):
    """会话存在但已失效（服务端返回 401）。"""

    exit_code = 3
    hint = "登录状态已失效，请重新运行：python -m xjtu_calendar login"


class PermissionDenied(XjtuCalendarError):
    """当前账号无权访问目标资源（服务端返回 403）。"""

    exit_code = 4
    hint = "当前账号没有访问该资源的权限，请确认登录的是本人账号。"


# --------------------------------------------------------------------------- #
# 抓取 / 解析
# --------------------------------------------------------------------------- #
class TimetableFetchError(XjtuCalendarError):
    """网络请求或响应结构异常。"""


class EndpointNotConfigured(XjtuCalendarError):
    """课表接口端点尚未由真实探测确认。

    与 :class:`TimetableFetchError` 的区别：这不是「请求失败了」，
    而是「我们根本还不知道该请求哪里」。此时**绝不允许猜测路径**，
    必须回到浏览器拦截路径或先跑接口探测。
    """

    exit_code = 1
    hint = (
        "接口路径必须来自真实网络观测，不得猜测。"
        "请运行 `python scripts/probe_ehall.py` 完成登录后进入「我的课表」页面，"
        "再用观测到的路径填写 config/ehall_endpoints.json。"
    )


class ParseError(XjtuCalendarError):
    """解析失败（原始数据与预期结构不符）。"""


class WeekError(XjtuCalendarError):
    """教学周次解析失败。"""


class PeriodError(XjtuCalendarError):
    """节次解析失败。"""


# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #
class SemesterNotConfigured(XjtuCalendarError):
    """请求的学期没有对应的校历配置。"""

    hint = "请在配置目录中补充该学期的教学日历（参见 examples/academic_calendar.example.json）。"


class ScheduleNotConfigured(XjtuCalendarError):
    """作息表缺少该日期所属区间的定义。"""

    hint = "请检查作息表配置中的 periods 区间是否已覆盖该学期。"


class CalendarExportError(XjtuCalendarError):
    """iCalendar 生成失败。"""


class UnsupportedAdjustmentError(XjtuCalendarError):
    """校历里声明了本工具无法表达的调课，且该调课落在导出范围内。

    这是**故意的 fail-closed**：与其生成一份「看起来完整、实则缺了
    若干调课时段」的日历，不如拒绝并把这些条目原样报给用户。
    用户确认接受缺失后，可用 ``--allow-unsupported-adjustments``
    带着显著警告继续导出。
    """

    hint = (
        "核对上面列出的调课是否影响你；确认可以接受缺失后，"
        "加 --allow-unsupported-adjustments 重新导出。"
    )
