"""日志配置。

红线（见 README「日志」）：

- 绝不输出 Cookie、Authorization、ticket、token、学号、姓名
- 绝不输出完整统一身份认证 URL 的参数

本模块提供一个 :func:`redact` 工具，供需要记录 URL/字典时脱敏。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse, urlunparse

__all__ = ["get_logger", "is_sensitive_key", "redact", "redact_by_key", "redact_url", "setup_logging"]

LOGGER_NAME = "xjtu_calendar"

#: 敏感键名（小写匹配）
_SENSITIVE_KEYS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "auth",
        "token",
        "access_token",
        "refresh_token",
        "ticket",
        "password",
        "passwd",
        "pwd",
        "secret",
        "session",
        "sessionid",
        "jsessionid",
        "sid",
        "student_id",
        "studentid",
        "xh",          # 学号
        "name",        # 姓名
        "xm",
        # ---- 教务系统（eHall/金智）真实响应中出现过的个人字段 ----
        "sfzjh",       # 身份证件号（含末尾 X，长数字规则打不掉）
        "zjhm",        # 证件号码
        "ksh",         # 考生号
        "jtdz",        # 家庭地址
        "jtdzqh",
        "zstxdz",      # 通信地址
        "csrq",        # 出生日期
        "dzxx",        # 电子邮箱
        "sjh",         # 手机号
        "yhh",         # 用户号/手机号
        "qqh",         # QQ 号
        "jtyb",        # 家庭邮编
        "bjdm",        # 班级代码
        "bjmc",        # 班级名称
        "zymc",        # 专业名称
        "username",    # eHall 门户 userName（姓名）
        "usersex",     # 性别
    }
)

#: URL 查询参数里需要脱敏的键
_SENSITIVE_QUERY_KEYS = _SENSITIVE_KEYS | {"service", "ticket", "st", "code"}


def setup_logging(verbose: bool = False, quiet: bool = False) -> logging.Logger:
    """配置根日志器。

    Parameters
    ----------
    verbose:
        ``True`` 时输出 DEBUG 级别（含详细异常）。
    quiet:
        ``True`` 时只输出 WARNING 及以上。
    """
    if quiet:
        level = logging.WARNING
    elif verbose:
        level = logging.DEBUG
    else:
        level = logging.INFO

    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(level)

    # 幂等：重复调用不叠加 handler
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def redact_url(url: str) -> str:
    """把 URL 里敏感的查询参数替换为 ``***``。

    >>> redact_url("https://ehall.xjtu.edu.cn/x?ticket=abc123&appId=9")
    'https://ehall.xjtu.edu.cn/x?ticket=***&appId=9'
    """
    try:
        parsed = urlparse(url)
    except ValueError:  # pragma: no cover
        return "<invalid-url>"

    if not parsed.query:
        return url

    pairs = []
    for key, value in (item.split("=", 1) if "=" in item else (item, "") for item in parsed.query.split("&")):
        if key.lower() in _SENSITIVE_QUERY_KEYS:
            pairs.append(f"{key}=***")
        else:
            pairs.append(f"{key}={value}")

    return urlunparse(parsed._replace(query="&".join(pairs)))


def redact(value: Any) -> Any:
    """递归脱敏字典/列表中的敏感字段。

    用于在 DEBUG 日志里打印响应结构而不泄露凭据。
    """
    if isinstance(value, Mapping):
        return {
            key: ("***" if str(key).lower() in _SENSITIVE_KEYS else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, str) and re.fullmatch(r"\d{10,}", value):
        # 长数字串很可能是学号
        return "***"
    return value


def is_sensitive_key(key: Any) -> bool:
    """判断字段名是否属于敏感键（大小写不敏感）。

    .. warning::
        :func:`redact` 的按键打码**只在整字典递归时生效**。
        如果你先把值从字典里取出来再单独展示（例如结构骨架生成器），
        键的上下文已经丢失，必须改用 :func:`redact_by_key`。
    """
    return str(key).lower() in _SENSITIVE_KEYS


def redact_by_key(key: Any, value: Any) -> Any:
    """在**键上下文仍然在场**时脱敏单个值。

    典型场景：结构骨架生成器逐字段取值展示。此时 ``{"XM": "张三"}``
    里的 ``"张三"`` 已经脱离了字典，:func:`redact` 看不到键名，
    无法识别它是姓名。教务系统真实响应里存在大量**非纯数字的敏感值**
    （中文姓名、含 X 的身份证号、家庭住址），仅靠「长数字串」规则
    会把它们原样漏出去。
    """
    if is_sensitive_key(key):
        return "***"
    if isinstance(value, str) and re.fullmatch(r"\d{10,}", value):
        return "***"
    return value
