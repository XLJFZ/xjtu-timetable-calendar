"""集中配置管理。

原则：**所有容易变化的学校信息都不散落在代码里。**

集中在这里的东西：

- eHall 域名与 appId
- 用户数据目录（登录态、学期配置、作息表）
- 默认学期标识

可用环境变量覆盖：

===========================  ==========================================
变量                          作用
===========================  ==========================================
``XJTU_EHALL_BASE``          eHall 域名，默认 ``https://ehall.xjtu.edu.cn``
``XJTU_CALENDAR_HOME``       用户数据目录，默认 ``~/.xjtu-timetable-calendar``
``XJTU_CALENDAR_BROWSER``    强制指定浏览器可执行文件路径
``XJTU_SEMESTER``            默认学期标识
===========================  ==========================================
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

__all__ = ["Settings", "find_browser", "settings"]

#: eHall 课表应用的 appId（来自用户提供的入口 URL，属公开的学校应用标识）
DEFAULT_APP_ID = "4770397878132218"

#: eHall 域名
DEFAULT_EHALL_BASE = "https://ehall.xjtu.edu.cn"

#: 用户数据目录名
DEFAULT_HOME_NAME = ".xjtu-timetable-calendar"


@dataclass
class Settings:
    """运行期配置。

    Attributes
    ----------
    ehall_base:
        eHall 根地址，不含末尾斜杠。
    app_id:
        课表应用标识。
    home:
        用户数据目录。
    semester_key:
        默认学期标识，可被 CLI ``--semester`` 覆盖。
    """

    ehall_base: str = DEFAULT_EHALL_BASE
    app_id: str = DEFAULT_APP_ID
    home: Path = field(default_factory=lambda: Path.home() / DEFAULT_HOME_NAME)
    semester_key: str | None = None

    #: 网络请求约束（见 README「安全与网络行为」）
    request_timeout: float = 30.0
    max_retries: int = 3
    retry_backoff_base: float = 1.5

    @classmethod
    def load(cls) -> Settings:
        """从环境变量加载配置。"""
        base = (os.environ.get("XJTU_EHALL_BASE") or DEFAULT_EHALL_BASE).rstrip("/")
        home_env = os.environ.get("XJTU_CALENDAR_HOME")
        home = Path(home_env).expanduser() if home_env else Path.home() / DEFAULT_HOME_NAME
        return cls(
            ehall_base=base,
            app_id=os.environ.get("XJTU_EHALL_APP_ID") or DEFAULT_APP_ID,
            home=home,
            semester_key=os.environ.get("XJTU_SEMESTER") or None,
        )

    # ------------------------------------------------------------------ #
    # URL 构造
    # ------------------------------------------------------------------ #
    @property
    def host(self) -> str:
        return urlparse(self.ehall_base).hostname or "ehall.xjtu.edu.cn"

    @property
    def select_role_url(self) -> str:
        """课表应用入口（用户手动进入时的页面）。"""
        return f"{self.ehall_base}/portal/html/select_role.html?appId={self.app_id}"

    # ------------------------------------------------------------------ #
    # 目录
    # ------------------------------------------------------------------ #
    @property
    def session_dir(self) -> Path:
        """登录态与浏览器 profile 存放处（**已在 .gitignore 中排除**）。"""
        return self.home / "session"

    @property
    def semesters_dir(self) -> Path:
        """各学期的教学日历（校历）配置。"""
        return self.home / "semesters"

    @property
    def schedules_dir(self) -> Path:
        """作息表配置。"""
        return self.home / "schedules"

    @property
    def raw_dir(self) -> Path:
        """原始课表 JSON 缓存（含个人信息，**不提交**）。"""
        return self.home / "raw"

    def ensure_dirs(self) -> None:
        """创建全部数据目录。"""
        for directory in (
            self.home,
            self.session_dir,
            self.semesters_dir,
            self.schedules_dir,
            self.raw_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # 文件定位
    # ------------------------------------------------------------------ #
    def state_path(self) -> Path:
        """Playwright storage_state 文件路径。"""
        return self.session_dir / "storage_state.json"

    def profile_dir(self) -> Path:
        """持久化浏览器 profile 目录。"""
        return self.session_dir / "profile"

    def semester_config_path(self, semester_key: str) -> Path:
        return self.semesters_dir / f"{semester_key}.json"

    def schedule_config_path(self) -> Path:
        return self.schedules_dir / "schedule.json"

    def raw_timetable_path(self, semester_key: str) -> Path:
        return self.raw_dir / f"timetable-{semester_key}.json"


def find_browser() -> str | None:
    """自动探测可用的 Chromium 系浏览器。

    顺序：环境变量 ``XJTU_CALENDAR_BROWSER`` → Edge → Chrome → ``None``。
    返回 ``None`` 时由调用方回退到 Playwright 自带的 Chromium。
    """
    forced = os.environ.get("XJTU_CALENDAR_BROWSER")
    if forced and Path(forced).is_file():
        return forced

    candidates: list[str] = []
    if sys.platform == "win32":
        # Windows 的环境变量名大小写不敏感：CPython 在 nt 平台上把 os.environ 的键
        # 统一转成大写存储，键查找同样会被转成大写，故 PROGRAMFILES 与 ProgramFiles
        # 在这里完全等价（已实测两种拼写取到同一值）。统一用大写以通过 SIM112。
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        candidates = [
            str(Path(pf86) / r"Microsoft\Edge\Application\msedge.exe"),
            str(Path(pf) / r"Microsoft\Edge\Application\msedge.exe"),
            str(Path(local) / r"Microsoft\Edge\Application\msedge.exe"),
            str(Path(pf) / r"Google\Chrome\Application\chrome.exe"),
            str(Path(pf86) / r"Google\Chrome\Application\chrome.exe"),
            str(Path(local) / r"Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        candidates = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        ]
    else:
        for exe in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            found = shutil.which(exe)
            if found:
                candidates.append(found)

    for path in candidates:
        if path and Path(path).is_file():
            return path
    return None


#: 模块级默认配置（惰性加载）
settings = Settings.load()
