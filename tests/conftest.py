"""pytest 全局配置。

保证 ``src/`` 位于导入路径上，使得不安装包时（例如直接 clone 后跑 pytest）
测试也能找到 ``xjtu_calendar``。
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
