"""QtWebEngine 打印为 PDF（《项目文档》§13.11：Markdown → HTML → QWebEngine 打印）。

放在 UI 层的原因：服务层不得导入 Qt（§1.5 红线 2），导出服务只接受注入的打印器。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer, QUrl


def print_pdf(html: str, target: str | Path, *, timeout_ms: int = 60_000) -> bool:
    """把 HTML 打印成 PDF；失败返回 False（调用方负责提示与降级）。"""
    from PySide6.QtWebEngineWidgets import QWebEnginePage

    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)

    page = QWebEnginePage()
    loop = QEventLoop()
    state = {"done": False, "ok": False}

    def on_pdf(data: bytes, _path: str = "") -> None:
        if data:
            destination.write_bytes(bytes(data))
            state["ok"] = True
        state["done"] = True
        loop.quit()

    def on_loaded(ok: bool) -> None:
        if not ok:
            state["done"] = True
            loop.quit()
            return
        page.printToPdf(on_pdf)

    page.loadFinished.connect(on_loaded)
    page.setHtml(html, QUrl("about:blank"))

    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(loop.quit)
    guard.start(timeout_ms)
    loop.exec()
    return bool(state["ok"] and destination.exists())
