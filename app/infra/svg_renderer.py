"""SVG → PNG 离屏渲染（《技术文档》§4.7：QtSvg 渲染自检图与首屏占位图）。"""

from __future__ import annotations

from pathlib import Path


def render_svg_png(svg_text: str, *, width: int = 820, scale: float = 2.0, background: str | None = None) -> bytes:
    """把 SVG 渲染成 PNG 字节；失败返回空字节（调用方降级为文字说明）。"""
    if not svg_text.strip():
        return b""
    try:
        from PySide6.QtCore import QByteArray, QSize
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
    except Exception:  # noqa: BLE001 - 没有 QtSvg 就放弃渲染
        return b""

    renderer = QSvgRenderer(QByteArray(svg_text.encode("utf-8")))
    if not renderer.isValid():
        return b""
    size = renderer.defaultSize()
    if size.isEmpty():
        size = QSize(width, int(width * 0.6))
    height = max(1, int(size.height() * (width / max(1, size.width()))))
    image = QImage(int(width * scale), int(height * scale), QImage.Format.Format_ARGB32)
    image.fill(background or "#FFFFFF00")
    painter = QPainter(image)
    try:
        renderer.render(painter)
    finally:
        painter.end()

    from PySide6.QtCore import QBuffer, QIODevice

    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def render_to_file(svg_text: str, target: str | Path, **kwargs: object) -> Path | None:
    payload = render_svg_png(svg_text, **kwargs)  # type: ignore[arg-type]
    if not payload:
        return None
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path
