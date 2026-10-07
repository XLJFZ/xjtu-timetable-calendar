"""文件写入工具：原子替换，可选属主-only 权限。

动机
----
v0.2 的 ``notices`` 写回学期配置时已有「同目录临时文件 + ``os.replace``」
的原子替换实现，但 ``auth.ensure_login`` 写 ``storage_state.json``
（文件自注「等价于登录凭据」）却用普通 ``write_text``——非原子，
且 POSIX 下默认 0644 组/其他可读。

本模块把原子写入提取为公共设施，两处共用同一份实现：

- **原子性**：同一文件系统内 ``os.replace`` 是原子操作，
  进程中途崩溃只会留下「旧内容」或「新内容」，不会有截断的半成品；
- **异常清理**：失败路径删除临时文件，不留下 ``*.tmp`` 垃圾；
- **``private=True``**：写临时文件后显式 ``chmod 0600`` 再替换，
  凭据等价文件不应放宽权限（Windows 无 POSIX 权限位，chmod 尽力而为，
  失败只记 debug 日志）。
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from .logging_setup import get_logger

__all__ = ["atomic_write_text"]

logger = get_logger()


def atomic_write_text(path: Path, text: str, *, private: bool = False) -> None:
    """原子替换写入文本（UTF-8，LF 行尾）。

    Parameters
    ----------
    path:
        目标文件路径；其父目录必须已存在。
    text:
        完整文件内容。
    private:
        ``True`` 时将权限收紧为 0600（用于等价于凭据的文件）。
    """
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        if private:
            try:
                os.chmod(tmp_path, 0o600)
            except OSError as exc:  # pragma: no cover - Windows 等平台权限语义有限
                logger.debug("无法将 %s 权限收紧为 0600：%s", tmp_path, exc)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
