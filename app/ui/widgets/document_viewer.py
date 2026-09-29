"""文档阅读器（M17）：课程大纲 / 课程资料等 Markdown 文件的查看窗口。

用只读富文本展示（Qt 自带 Markdown 渲染），并提供「用系统默认程序打开」「复制全文」。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)


class DocumentViewerDialog(QDialog):
    """只读展示一个 Markdown / 文本文件。"""

    def __init__(self, *, path: str | Path | None = None, text: str | None = None, title: str | None = None, parent=None) -> None:
        super().__init__(parent)
        self.path = Path(path) if path is not None else Path("（未落盘文本）")
        self.setWindowTitle(title or self.path.name)
        self.resize(880, 660)

        layout = QVBoxLayout(self)
        header_text = title if text is not None else f"{self.path.parent.name} / {self.path.name}"
        header = QLabel(header_text)
        header.setProperty("role", "muted")
        layout.addWidget(header)

        self.viewer = QTextBrowser()
        self.viewer.setOpenExternalLinks(False)
        if text is None:
            try:
                body = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                body = f"（读取失败：{exc}）"
        else:
            body = text
        if text is not None:
            self.viewer.setMarkdown(body)
        elif self.path.suffix.lower() == ".md":
            self.viewer.setMarkdown(body)
        else:
            self.viewer.setPlainText(body)
        layout.addWidget(self.viewer, 1)

        row = QHBoxLayout()
        self.copy_button = QPushButton("复制全文")
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.viewer.toPlainText()))
        row.addWidget(self.copy_button)
        self.open_button = QPushButton("用系统默认程序打开")
        self.open_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path))))
        row.addWidget(self.open_button)
        self.folder_button = QPushButton("打开所在文件夹")
        self.folder_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))
        )
        row.addWidget(self.folder_button)
        row.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.clicked.connect(self.accept)
        row.addWidget(self.close_button)
        layout.addLayout(row)
