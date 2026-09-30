"""整张图示预览窗口（双击图打开；《项目文档》§11.1 阅读体验）。

为什么需要它：学习台右栏 / 讲义区里的图受栏宽限制，复杂图会显得小或需要滚动。
双击任意图 → 在这里按可用屏幕放大呈现，支持滚轮缩放、适应窗口、另存为 PNG。

渲染仍然**全部在本地**：SVG 走 QtSvg 离屏栅格化（渲染前过 svg_sanitizer）。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
)

from ...infra.svg_renderer import render_svg_png
from ...infra.svg_sanitizer import clean, is_acceptable

BASE_RENDER_WIDTH = 1400  # 大图预览的栅格化宽度（比栏宽高得多，缩放才不糊）


class DiagramDialog(QDialog):
    """一张图的完整视图。"""

    def __init__(
        self,
        *,
        svg: str | None = None,
        pixmap: QPixmap | None = None,
        title: str = "图示",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title or "图示")
        self.resize(1100, 820)
        self._pixmap = QPixmap()
        self._scale = 1.0
        self._fit = True

        if pixmap is not None and not pixmap.isNull():
            self._pixmap = pixmap
        elif svg:
            self._pixmap = _render_svg(svg)

        layout = QVBoxLayout(self)
        header = QLabel(title or "图示")
        header.setProperty("role", "subtitle")
        layout.addWidget(header)

        self.canvas = QLabel()
        self.canvas.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.canvas.setStyleSheet("background: #FFFFFF;")
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll.setWidget(self.canvas)
        layout.addWidget(self.scroll, 1)

        row = QHBoxLayout()
        self.zoom_out_button = QPushButton("缩小")
        self.zoom_out_button.clicked.connect(lambda: self.zoom_by(1 / 1.25))
        row.addWidget(self.zoom_out_button)
        self.zoom_in_button = QPushButton("放大")
        self.zoom_in_button.clicked.connect(lambda: self.zoom_by(1.25))
        row.addWidget(self.zoom_in_button)
        self.reset_button = QPushButton("适应窗口")
        self.reset_button.clicked.connect(self.fit_to_window)
        row.addWidget(self.reset_button)
        self.actual_button = QPushButton("原始大小")
        self.actual_button.clicked.connect(self.show_actual_size)
        row.addWidget(self.actual_button)
        row.addStretch(1)
        self.save_button = QPushButton("另存为 PNG")
        self.save_button.clicked.connect(self.save_as)
        row.addWidget(self.save_button)
        self.copy_button = QPushButton("复制图片")
        self.copy_button.clicked.connect(self.copy_to_clipboard)
        row.addWidget(self.copy_button)
        self.close_button = QPushButton("关闭")
        self.close_button.setProperty("role", "primary")
        self.close_button.clicked.connect(self.accept)
        row.addWidget(self.close_button)
        layout.addLayout(row)

        self._apply_scale()

    # --- 缩放 -----------------------------------------------------------
    def loaded(self) -> bool:
        return not self._pixmap.isNull()

    def zoom_by(self, factor: float) -> None:
        self._fit = False
        self._scale = max(0.05, min(8.0, self._scale * factor))
        self._apply_scale()

    def fit_to_window(self) -> None:
        self._fit = True
        self._apply_scale()

    def show_actual_size(self) -> None:
        self._fit = False
        self._scale = 1.0
        self._apply_scale()

    def _apply_scale(self) -> None:
        if self._pixmap.isNull():
            self.canvas.setText("（这张图没能渲染出来，已保留源码在正文里）")
            return
        if self._fit:
            available = self.scroll.viewport().size()
            width = max(64, available.width() - 24)
            height = max(64, available.height() - 24)
            scale = min(width / self._pixmap.width(), height / self._pixmap.height())
            self._scale = max(0.05, min(8.0, scale))
        target = self._pixmap.size() * self._scale
        self.canvas.setPixmap(
            self._pixmap.scaled(
                target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        if self._fit:
            self._apply_scale()

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_by(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)
            return
        super().wheelEvent(event)

    # --- 导出 -----------------------------------------------------------
    def copy_to_clipboard(self) -> None:
        if not self._pixmap.isNull():
            QApplication.clipboard().setPixmap(self._pixmap)

    def save_as(self) -> None:
        if self._pixmap.isNull():
            return
        target, _ = QFileDialog.getSaveFileName(self, "另存为 PNG", "图示.png", "PNG 图片 (*.png)")
        if not target:
            return
        path = Path(target)
        if path.suffix.lower() != ".png":
            path = path.with_suffix(".png")
        if not self._pixmap.save(str(path), "PNG"):
            QMessageBox.warning(self, "保存失败", f"无法写入 {path}")


def _render_svg(svg: str) -> QPixmap:
    """SVG → 位图；清洗不通过或渲染失败时返回空 QPixmap。"""
    text = str(svg or "")
    if not text.strip() or not is_acceptable(clean(text)):
        return QPixmap()
    try:
        data = render_svg_png(text, width=BASE_RENDER_WIDTH, scale=1.0)
    except Exception:  # noqa: BLE001 - 渲染失败就显示提示文案
        return QPixmap()
    pixmap = QPixmap()
    if not data or not pixmap.loadFromData(data, "PNG"):
        return QPixmap()
    return pixmap


def view_diagram(*, svg: str | None = None, pixmap: QPixmap | None = None, title: str = "图示", parent=None) -> DiagramDialog:
    """打开大图窗口（模态）。"""
    dialog = DiagramDialog(svg=svg, pixmap=pixmap, title=title, parent=parent)
    dialog.exec()
    return dialog