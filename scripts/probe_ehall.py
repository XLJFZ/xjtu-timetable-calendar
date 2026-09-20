"""eHall 接口探测脚本（Phase 1 分析工具，非运行期依赖）。

目的：**不做任何猜测**，用真实浏览器走一遍用户正常登录 + 进入课表页面的过程，
记录前端自己调用的结构化接口。

.. important::
   本脚本是 ``config/ehall_endpoints.json`` 里接口路径的**唯一合法来源**。
   任何没有在本脚本输出中出现过的 endpoint，都不得写进正式配置。

上一版缺陷（导致捕获数为 0）
----------------------------
1. 只监听初始 ``page`` 的 ``response`` 事件。eHall 的课表应用常在新标签页或
   iframe 里发起请求，初始 page 收不到 → 全部漏掉。
2. 只接受 ``content-type`` 含 ``json`` 的响应。eHall 部分接口返回
   ``text/plain`` 或``text/html`` 包裹的 JSON 字符串 → 被无条件丢弃。
3. 不记录请求方法之外的任何请求信息（请求体、参数名）→ 无法确定学期参数格式。
4. 不保存 ``storage_state`` → 探测结束也无法让 ``login`` 复用会话。
5. 无法判断「用户到底登录成功没有」→ 静默产出空报告，看起来像「接口不存在」。

当前版修复
----------
- 监听 ``context`` 上的**每一个** page（含后续新建的标签页/弹窗）
- 不看 content-type，直接尝试 ``json.loads``，失败则单独记为「非 JSON」
- 记录 method / URL / 请求头键名 / 请求体键名（值一律脱敏）
- 结束时保存 ``storage_state``，供 ``python -m xjtu_calendar login`` 复用
- 明确报告登录检测结果（eHall 域名下是否有会话 cookie）

安全约束（本脚本严格遵守）
--------------------------
- 不绕过统一身份认证：等用户在浏览器里自己完成登录
- 不枚举学号 / 不请求他人课表 / 不高并发
- 报告只记录 endpoint / method / 参数名 / 响应**结构骨架**，值一律脱敏
- 原始捕获写入 ``~/.xjtu-timetable-calendar/probe/``（仓库外、.gitignore 已排除）

用法::

    python scripts/probe_ehall.py
    python scripts/probe_ehall.py --url "https://ehall.xjtu.edu.cn/..."   # 直接打开指定页面
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Iterable
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from xjtu_calendar.config import Settings, find_browser  # noqa: E402
from xjtu_calendar.logging_setup import redact_by_key, redact_url  # noqa: E402

OUT_DIR = ROOT / "_notes"

#: 与课表相关的关键词，用于**排序**（不再用于丢弃），让目标接口排在前面
TIMETABLE_HINTS = (
    "timetable", "schedule", "course", "kcb", "kb", "lesson", "class",
    "semester", "term", "xnm", "xqm", "week", "student", "pkb", "kbxx",
)

#: 只记录键名、不记录值的请求头（真正需要的通常是这几个）
INTERESTING_REQ_HEADERS = (
    "content-type", "accept", "x-requested-with", "referer",
    "x-auth-token", "token", "csrf", "x-csrf-token",
)

#: 判定「看起来像登录页」的 HTML 特征
LOGIN_HTML_MARKERS = (
    "统一身份认证", "统一身份", "账号登录", "用户登录", "login.xjtu.edu.cn",
    "cas.xjtu.edu.cn", "请输入账号", "请输入密码", "ids.xjtu.edu.cn",
)


# --------------------------------------------------------------------------- #
# 脱敏与骨架
# --------------------------------------------------------------------------- #
def skeleton(value: Any, depth: int = 0, max_depth: int = 6, key: Any = None) -> Any:
    """把 JSON 压成「结构骨架」：保留键名与层级，值只留类型/长度。

    列表保留第一个元素的骨架 + 长度，避免报告过大。

    .. warning::
        脱敏**必须带键上下文**（:func:`redact_by_key`）。真实教务响应里
        存在中文姓名、含字母 X 的身份证号、家庭住址等非纯数字敏感值，
        只按「长数字串」规则打码会把它们原样漏进报告。
    """
    if depth >= max_depth:
        return f"<{type(value).__name__}>"

    if isinstance(value, dict):
        return {
            str(k): skeleton(v, depth + 1, max_depth, key=k)
            for k, v in value.items()
        }
    if isinstance(value, list):
        if not value:
            return {"__type__": "list", "__len__": 0}
        return {
            "__type__": "list",
            "__len__": len(value),
            "__item__": skeleton(value[0], depth + 1, max_depth, key=key),
        }
    if isinstance(value, str):
        return {
            "__type__": "str",
            "__len__": len(value),
            "__sample__": redact_by_key(key, value),
        }
    if isinstance(value, bool):
        return {"__type__": "bool", "__sample__": value}
    if isinstance(value, (int, float)):
        return {
            "__type__": type(value).__name__,
            "__sample__": redact_by_key(key, str(value)),
        }
    return {"__type__": type(value).__name__}


def _body_keys(post_data: str | None, content_type: str) -> dict[str, Any] | None:
    """从请求体里提取**键名**（值一律丢弃）。"""
    if not post_data:
        return None
    text = post_data.strip()
    if not text:
        return None

    # 先看内容像不像 JSON，不依赖 content-type（eHall 常见用 text/plain 传 JSON）
    if text[:1] in "{[":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {"__type__": "json", "__parse_error__": True, "__len__": len(text)}
        if isinstance(parsed, dict):
            return {"__type__": "json", "__keys__": sorted(str(k) for k in parsed)}
        return {"__type__": "json", "__item_type__": type(parsed).__name__}

    try:
        pairs = parse_qs(text, keep_blank_values=True)
    except Exception:
        return {"__type__": "raw", "__len__": len(text)}
    # 只留键名；值里可能有学号，绝不保留
    return {"__type__": "form", "__keys__": sorted(str(k) for k in pairs)}


def _query_keys(url: str) -> list[str]:
    """URL 查询参数的**键名**列表（不含值）。"""
    try:
        return sorted(parse_qs(urlsplit(url).query, keep_blank_values=True).keys())
    except Exception:
        return []


def _score(url: str) -> int:
    lowered = url.lower()
    return sum(1 for hint in TIMETABLE_HINTS if hint in lowered)


def _looks_like_login_html(text: str) -> bool:
    head = text[:4000].lower()
    return any(marker.lower() in head for marker in LOGIN_HTML_MARKERS)


def _stdin_is_tty() -> bool:
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except Exception:
        return False


def _auto_wait_loop(context: Any, captured: list[dict[str, Any]], max_wait: float) -> None:
    """轮询等待：登录成功**且**捕获到课表相关 JSON 后自动结束。

    没有这一步，非交互终端里 ``input()`` 会瞬间 EOF，浏览器刚打开就被关掉，
    用户根本来不及登录——这是上一轮「捕获 0」最可能的事故路径。
    """
    deadline = time.monotonic() + max_wait
    last_report = 0.0

    while time.monotonic() < deadline:
        try:
            cookies = context.cookies()
            login_ok = any(
                "ehall" in str(c.get("domain", "")).lower() for c in cookies
            )
        except Exception:
            login_ok = False

        found = any(
            e["body_kind"] == "json" and e["score"] >= 2 for e in captured
        )

        now = time.monotonic()
        if now - last_report >= 10:
            remaining = int(deadline - now)
            print(
                f"[…] 等待中：eHall 登录={'是' if login_ok else '否'}，"
                f"课表相关 JSON={'已捕获' if found else '未捕获'}，"
                f"总请求 {len(captured)}，剩余 {remaining}s",
                flush=True,
            )
            last_report = now

        if login_ok and found:
            print("[✓] 已捕获到课表相关 JSON，自动结束采集", flush=True)
            return

        time.sleep(2)

    print("[!] 达到最长等待时间，按当前已捕获内容结束", flush=True)


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def _attach(context: Any, captured: list[dict[str, Any]], seen: set[str]) -> None:
    """给 context 上已存在的和将来出现的每一个 page 挂上监听器。"""

    def on_response(response: Any) -> None:
        try:
            request = response.request
            url = response.url
            if url in seen:
                return
            seen.add(url)

            headers = {k.lower(): v for k, v in (request.headers or {}).items()}
            ctype = (response.headers or {}).get("content-type", "")

            # 不看 content-type，直接尝试解析；失败则按内容归类
            payload: Any = None
            body_kind = "empty"
            try:
                body = response.text()
            except Exception:
                body = ""
            if body:
                try:
                    payload = json.loads(body)
                    body_kind = "json"
                except json.JSONDecodeError:
                    payload = None
                    body_kind = (
                        "html" if body.lstrip()[:20].lower().startswith("<") else "text"
                    )

            captured.append(
                {
                    "url": url,
                    "path": url.split("?")[0],
                    "method": request.method,
                    "status": response.status,
                    "score": _score(url),
                    "request_content_type": headers.get("content-type", ""),
                    "request_header_keys": sorted(
                        h for h in INTERESTING_REQ_HEADERS if h in headers
                    ),
                    "query_keys": _query_keys(url),
                    "body_keys": _body_keys(
                        getattr(request, "post_data", None),
                        headers.get("content-type", ""),
                    ),
                    "response_content_type": ctype,
                    "body_kind": body_kind,
                    "payload": payload,
                }
            )
        except Exception:
            # 单条捕获失败不能中断整个探测
            return

    def attach_page(page: Any) -> None:
        page.on("response", on_response)

    for page in context.pages:
        attach_page(page)
    # 课表应用常在新标签页/弹窗里发起请求，必须监听后续新建的 page
    context.on("page", attach_page)


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="探测 eHall 课表真实接口")
    parser.add_argument("--url", default=None, help="直接打开指定页面（默认课表应用入口）")
    parser.add_argument("--settle", type=float, default=0.0,
                        help="按 Enter 结束后额外等待并采集的秒数（默认 0）")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（仅用于**自动化冒烟测试**；"
                             "需要本人输入账号密码登录时请勿使用）")
    parser.add_argument("--wait-seconds", type=float, default=None,
                        help="自动等待模式的最长等待秒数（默认 900）。"
                             "浏览器保持打开，**捕获到课表相关 JSON 后自动结束**，"
                             "无需回终端按 Enter。适合非交互终端或忘记按 Enter 的场景。")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright", file=sys.stderr)
        return 1

    settings = Settings.load()
    settings.ensure_dirs()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    probe_dir = settings.home / "probe"
    probe_dir.mkdir(parents=True, exist_ok=True)

    executable = find_browser()
    interactive = _stdin_is_tty()
    auto_wait = args.wait_seconds is not None or not interactive
    max_wait = float(args.wait_seconds) if args.wait_seconds is not None else 900.0

    print(f"[i] eHall: {settings.ehall_base}")
    print(f"[i] 入口: {args.url or settings.select_role_url}")
    print(f"[i] 浏览器: {executable or 'playwright 自带 chromium'}")
    print(f"[i] profile: {settings.profile_dir()}")
    print()
    print("请在打开的浏览器窗口里完成：")
    print("  1) 由你本人输入账号密码完成统一身份认证（本工具不参与、不记录凭据）")
    print("  2) 进入「我的课表」页面，切到有课的学期，等课表真正显示出来")
    print("  3) 可再点几次学期/周次切换，让前端多打几个接口")
    if auto_wait:
        print(f"  4) 结束条件：捕获到课表相关 JSON 后**自动结束**；最长等 {int(max_wait)}s")
    else:
        print("  4) 回到本终端按 Enter 结束采集（Ctrl+C 也会保留已捕获内容）")
    print()

    captured: list[dict[str, Any]] = []
    seen: set[str] = set()
    login_cookie_count = 0
    final_url = ""

    with sync_playwright() as pw:
        launch_kwargs: dict[str, Any] = {
            "user_data_dir": str(settings.profile_dir()),
            "headless": bool(args.headless),
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if executable:
            launch_kwargs["executable_path"] = executable

        context = pw.chromium.launch_persistent_context(**launch_kwargs)
        _attach(context, captured, seen)

        page = context.pages[0] if context.pages else context.new_page()
        # 启动 URL 不带 #/，避免 SPA 卡死
        page.goto(args.url or settings.select_role_url,
                  wait_until="domcontentloaded", timeout=60_000)

        try:
            if auto_wait:
                # 非交互终端下 input() 会立刻 EOF 并把浏览器关掉——
                # 这是上一轮「探测捕获 0」最可能的人为触发方式。
                # 改为轮询：登录成功且捕获到课表相关 JSON 即自动结束。
                _auto_wait_loop(context, captured, max_wait)
            else:
                try:
                    input(">>> 完成登录与课表浏览后，按 Enter 结束采集... ")
                except KeyboardInterrupt:
                    print("\n[i] 收到 Ctrl+C，保留已捕获内容继续")
        except KeyboardInterrupt:
            print("\n[i] 收到 Ctrl+C，保留已捕获内容继续")

        if args.settle > 0:
            page.wait_for_timeout(int(args.settle * 1000))

        try:
            final_url = page.url
        except Exception:
            final_url = ""

        try:
            all_cookies = context.cookies()
            # 只看 ehall 域自己的 cookie。
            # 注意：login.xjtu.edu.cn 的 CAS cookie 在**未登录**时也存在，
            # 用它判断会把「跳到了登录页」误判成「已登录」。
            ehall_cookies = [
                c for c in all_cookies if "ehall" in str(c.get("domain", "")).lower()
            ]
            login_cookie_count = len(ehall_cookies)

            # **只在确认存在 eHall 会话 cookie 时才落盘会话。**
            # 否则会把未登录的 CAS 临时 cookie 写进 storage_state，
            # 让 `status` / `fetch` 误以为已登录。
            if ehall_cookies:
                settings.state_path().write_text(
                    json.dumps(
                        {
                            "cookies": all_cookies,
                            "origins": [],
                            "_saved_at": datetime.now().astimezone().isoformat(),
                            "_note": "本文件等价于登录凭据，请勿提交或分享。",
                        },
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )
                print(f"[✓] 登录态已保存到 {settings.state_path()}")
            else:
                print("[!] eHall 域下无会话 cookie，不保存会话文件（未登录不落盘）。")
        except Exception as exc:
            print(f"[!] 保存会话失败：{exc}", file=sys.stderr)

        context.close()

    # ---------------- 原始捕获（仓库外，供后续分析） ---------------- #
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    raw_path = probe_dir / f"capture-{stamp}.json"
    raw_path.write_text(
        json.dumps(
            [{"url": redact_url(e["url"]), **{k: v for k, v in e.items() if k != "url"}}
             for e in captured],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    # ---------------- 生成报告 ---------------- #
    deduped: dict[str, dict[str, Any]] = {}
    for item in captured:
        key = f"{item['method']} {item['path']}"
        if key not in deduped or item["score"] > deduped[key]["score"]:
            deduped[key] = item

    ranked = sorted(deduped.values(), key=lambda x: (-x["score"], x["path"]))
    json_entries = [e for e in ranked if e["body_kind"] == "json"]

    lines: list[str] = []
    lines.append("# eHall 课表接口探测报告（自动生成，已脱敏）")
    lines.append("")
    lines.append(f"- eHall 根地址：`{settings.ehall_base}`")
    lines.append(f"- 入口：`{args.url or settings.select_role_url}`")
    lines.append(f"- 采集时间：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"- 捕获请求总数：{len(captured)}（去重后 {len(ranked)}）")
    lines.append(f"- 其中 JSON 响应：{len(json_entries)}")
    lines.append("")
    on_auth_host = any(
        marker in final_url.lower()
        for marker in ("login.xjtu.edu.cn", "cas.xjtu.edu.cn", "org.xjtu.edu.cn")
    )
    logged_in = login_cookie_count > 0 and not on_auth_host

    if not logged_in:
        lines.append("> ⚠️ **未确认登录成功。**")
        lines.append("")
        lines.append(f"> - 结束时的页面：`{redact_url(final_url) or '(未知)'}`")
        lines.append(f"> - eHall 域下的会话 cookie 数：{login_cookie_count}")
        if on_auth_host:
            lines.append("> - 结束时仍停留在统一身份认证页面 → **登录未完成**。")
        lines.append(">")
        lines.append("> 这几乎可以肯定是「登录未完成」而非「接口不存在」。")
        lines.append("> 请重新运行本脚本，在浏览器里真正完成统一身份认证后，")
        lines.append("> **进入「我的课表」页面并等课表显示出来**，再回来按 Enter。")
        lines.append("")
    else:
        lines.append(f"> ✅ 检测到 {login_cookie_count} 个 eHall 会话 cookie，登录态已保存。")
        lines.append("")
    lines.append("> 本报告不含 Cookie / token / 学号 / 姓名 / 原始个人课表。")
    lines.append("")

    for item in json_entries:
        lines.append(f"## `{item['method']} {item['path']}`")
        lines.append("")
        lines.append(f"- 状态码：{item['status']}")
        lines.append(f"- 课表相关度评分：{item['score']}")
        lines.append(f"- 完整 URL（参数已脱敏）：`{redact_url(item['url'])}`")
        if item["query_keys"]:
            lines.append(f"- 查询参数**键名**：`{', '.join(item['query_keys'])}`")
        if item["body_keys"]:
            lines.append(f"- 请求体（仅键名）：`{json.dumps(item['body_keys'], ensure_ascii=False)}`")
        if item["request_header_keys"]:
            lines.append(f"- 相关请求头：`{', '.join(item['request_header_keys'])}`")
        lines.append("")
        lines.append("响应结构骨架：")
        lines.append("")
        lines.append("```json")
        lines.append(json.dumps(skeleton(item["payload"]), ensure_ascii=False, indent=2))
        lines.append("```")
        lines.append("")

    if len(json_entries) < len(ranked):
        lines.append("## 非 JSON 响应（按相关度排序，可能含登录页/重定向）")
        lines.append("")
        for item in ranked:
            if item["body_kind"] == "json":
                continue
            note = ""
            lines.append(
                f"- `{item['method']} {item['path']}` → {item['status']} "
                f"({item['body_kind']}){note}"
            )
        lines.append("")

    report_path = OUT_DIR / "ehall-probe.md"
    report_path.write_text("\n".join(lines), encoding="utf-8")

    print()
    print(f"[✓] 报告已写入 {report_path}")
    print(f"[i] 原始捕获（含结构骨架，仓库外）：{raw_path}")
    print(f"[i] 捕获 {len(captured)} 个请求，其中 JSON {len(json_entries)} 个")
    if not logged_in:
        print("[!] 未确认登录成功 —— 登录很可能未完成，报告不可用于填写端点配置。")
        return 2
    if not json_entries:
        print("[!] 登录态存在但未捕获到 JSON 接口 —— 请确认已进入「我的课表」页面。")
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
