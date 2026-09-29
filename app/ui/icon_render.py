"""课程图标 → QPixmap 渲染（UI 层；服务层不导入 Qt，见 §1.5 红线 2）。

用 QSvgRenderer 离屏渲染，纯本地、无外链；卡片底色用圆角矩形铺在图标下面。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QPainter, QPixmap

from ..core.models import CourseIcon
from ..services.icon_service import read_icon_svg


def svg_to_pixmap(svg_text: str, *, size: int = 96) -> QPixmap:
    """把 SVG 文本渲染成指定边长的透明底 QPixmap。"""
    from PySide6.QtSvg import QSvgRenderer

    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    renderer = QSvgRenderer(QByteArray(str(svg_text or "").encode("utf-8")))
    if not renderer.isValid():
        return pixmap
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        renderer.render(painter, QRectF(0, 0, size, size))
    finally:
        painter.end()
    return pixmap


def course_icon_pixmap(course_dir: str | Path, icon: CourseIcon | None, *, size: int = 96, padding: int = 14) -> QPixmap:
    """课程卡片用的圆角图标：底色方块 + 居中的单色 SVG 图标。"""
    target = icon or CourseIcon()
    svg = read_icon_svg(course_dir, target)
    canvas = QPixmap(size, size)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setBrush(QBrush(QColor(target.bg)))
        painter.setPen(Qt.PenStyle.NoPen)
        radius = size * 0.24
        painter.drawRoundedRect(QRectF(0, 0, size, size), radius, radius)
    finally:
        painter.end()

    inner = svg_to_pixmap(svg, size=max(16, size - padding * 2))
    painter = QPainter(canvas)
    try:
        painter.drawPixmap(padding, padding, inner)
    finally:
        painter.end()
    return canvas
