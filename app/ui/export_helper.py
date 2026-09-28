"""PDF 打印（《项目文档》§13.11：Markdown → HTML → 打印为 PDF）。

两条路径：

1. **首选** QtWebEngine ``printToPdf``：保留 CSS 排版与公式渲染；
2. **兜底** ``QTextDocument`` + ``QPrinter(PdfFormat)``：在无 GPU / 离屏环境也能出 PDF
   （表格、标题、代码块可读，公式降级为原文）。

服务层不导入 Qt（§1.5 红线 2），导出服务只接受这里注入的打印器。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEventLoop, QTimer, QUrl
from PySide6.QtGui import QTextDocument
from PySide6.QtPrintSupport import QPrinter


def _print_with_webengine(html: str, destination: Path, timeout_ms: int) -> bool:
    try:
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage  # Qt6：Page 在 Core 模块
        except ImportError:  # pragma: no cover - 老版本 Qt
            from PySide6.QtWebEngineWidgets import QWebEnginePage

        page = QWebEnginePage()
        loop = QEventLoop()
        state = {"ok": False}

        def on_pdf(data: object) -> None:
            payload = bytes(data) if data else b""
            if payload:
                destination.write_bytes(payload)
                state["ok"] = True
            loop.quit()

        def on_loaded(ok: bool) -> None:
            if not ok:
                loop.quit()
                return
            try:
                page.printToPdf(on_pdf)
            except Exception:  # noqa: BLE001
                loop.quit()

        page.loadFinished.connect(on_loaded)
        page.setHtml(html, QUrl("about:blank"))
        guard = QTimer()
        guard.setSingleShot(True)
        guard.timeout.connect(loop.quit)
        guard.start(timeout_ms)
        loop.exec()
        return state["ok"] and destination.exists()
    except Exception:  # noqa: BLE001 - 没有 WebEngine 就走兜底
        return False


def _print_with_text_document(html: str, destination: Path) -> bool:
    try:
        document = QTextDocument()
        document.setHtml(html)
        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setOutputFormat(QPrinter.OutputFormat.PdfFormat)
        printer.setOutputFileName(str(destination))
        document.print_(printer)
        return destination.exists() and destination.stat().st_size > 0
    except Exception:  # noqa: BLE001
        return False


def print_pdf(html: str, target: str | Path, *, timeout_ms: int = 20_000) -> bool:
    """把 HTML 打印成 PDF；成功返回 True。"""
    destination = Path(target)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if _print_with_webengine(html, destination, timeout_ms):
        return True
    return _print_with_text_document(html, destination)
