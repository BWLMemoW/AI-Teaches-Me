"""动画沙箱（《技术文档》§4.7 / §13.4；《项目文档》§8.4.2 与 AC-24 / AC-25）。

* 清洗：移除 ``<script src>`` / ``<link>`` / ``<iframe>`` / 远程地址 / fetch / XHR；
* 包裹：插入 CSP（禁网）+ 主题变量，交给 ``<iframe sandbox="allow-scripts">``；
* 熔断：宿主按 5s 超时切换到 ``fallback_md``。
"""

from __future__ import annotations

import re

FALLBACK_TIMEOUT_MS = 5000
CSP = (
    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "img-src data:; font-src data:; connect-src 'none'; frame-src 'none'"
)
REMOTE_SCRIPT_RE = re.compile(r"<\s*script[^>]*src\s*=\s*[\"'][^\"']+[\"'][^>]*>\s*<\s*/\s*script\s*>", re.IGNORECASE)
LINK_RE = re.compile(r"<\s*link[^>]*>", re.IGNORECASE)
IFRAME_RE = re.compile(r"<\s*iframe[\s\S]*?<\s*/\s*iframe\s*>", re.IGNORECASE)
NETWORK_CALL_RE = re.compile(r"\b(fetch|XMLHttpRequest|importScripts|WebSocket|EventSource)\s*\(", re.IGNORECASE)
REMOTE_URL_RE = re.compile(r"https?://[^\s\"')]+", re.IGNORECASE)


def sanitize_animation_html(raw_html: str) -> tuple[str, list[str]]:
    """清洗动画 HTML，返回（干净 HTML, 问题清单）。"""
    problems: list[str] = []
    html = raw_html or ""
    if not html.strip():
        return "", ["动画内容为空"]

    for pattern, label in ((REMOTE_SCRIPT_RE, "外部脚本"), (LINK_RE, "外部样式"), (IFRAME_RE, "嵌套 iframe")):
        if pattern.search(html):
            problems.append(f"已移除{label}")
            html = pattern.sub("", html)
    if NETWORK_CALL_RE.search(html):
        problems.append("已移除网络请求调用")
        html = NETWORK_CALL_RE.sub("(void 0)(", html)
    if REMOTE_URL_RE.search(html):
        problems.append("已移除远程地址")
        html = REMOTE_URL_RE.sub("", html)
    if "onbeforeunload" in html.lower():
        problems.append("已移除 onbeforeunload")
        html = re.sub(r"onbeforeunload\s*=\s*[\"'][^\"']*[\"']", "", html, flags=re.IGNORECASE)
    return html, problems


def prepare_animation_html(raw_html: str, theme: dict[str, str] | None = None) -> str:
    """清洗 + 注入 CSP 与主题变量（§13.4 的 ``prepare_animation_html``）。"""
    html, _ = sanitize_animation_html(raw_html)
    colors = theme or {}
    guard = (
        f'<meta http-equiv="Content-Security-Policy" content="{CSP}" />'
        "<style>:root{--atm-fg:%s;--atm-bg:%s;--atm-primary:%s;}"
        "html,body{margin:0;color:var(--atm-fg);background:var(--atm-bg);font-family:'Microsoft YaHei',sans-serif;}"
        "@media (prefers-reduced-motion: reduce){*{animation:none !important;transition:none !important;}}"
        "</style>"
        % (
            colors.get("--atm-text", "#222222"),
            colors.get("--atm-surface", "#FFFFFF"),
            colors.get("--atm-primary", "#E86A92"),
        )
    )
    if "<head>" in html:
        return html.replace("<head>", "<head>" + guard, 1)
    return "<!DOCTYPE html><html><head>" + guard + "</head><body>" + html + "</body></html>"
