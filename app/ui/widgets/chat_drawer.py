"""追问抽屉（《项目文档》§4.7 FR-07、§11.4 ``ChatDrawer``）。

气泡式对话：
* 用户的追问靠右（主色浅底），老师的回答靠左（面板色底）；
* 老师回答**按内容块增量渲染**：已完成的块一次性追加进气泡，未收完的那一行
  只在末尾的临时标签里原地刷新——**不重建整个气泡**（否则会一闪一闪）；
* `svg` / `diagram(kind=svg)` / `image` 块离屏栅格化成图片真的画出来，
  **双击**任意图 → 弹出整张大图窗口；
* 记录区只读、不可获焦；输入框在下方与发送按钮同行；支持「放大」到独立窗口。
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ...core.models import ContentBlock
from ...infra.svg_renderer import render_svg_png
from ...infra.svg_sanitizer import clean, is_acceptable
from ...llm.stream import BlockStreamParser

CODE_FONT = "'JetBrains Mono', Consolas, 'Courier New', monospace"
CODE_BG = "#F2F4F7"
ROLE_PREFIX = {"user": "我：", "assistant": "老师："}
DIAGRAM_SOURCE_WIDTH = 1040  # SVG 栅格化宽度（2× 显示宽度，保证清晰）
DIAGRAM_DISPLAY_WIDTH = 460  # 气泡里图的显示宽度上限
_IMAGE_CACHE: dict[str, QPixmap] = {}

# 引用文本里带的本地图片路径（讲义里「💬 引用到追问」会写成「…（文件：D:\...\x.png）」）
_ASSET_RE = re.compile(r"（文件：(.+?)）")
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def find_local_image(text: str) -> str:
    """从引用文本里挑出真实存在的本地图片路径（没有就返回空串）。"""
    images = find_local_images(text)
    return images[0] if images else ""


def find_local_images(text: str) -> list[str]:
    """引用文本里所有真实存在的本地图片路径（去重、保序）。"""
    found: list[str] = []
    for match in _ASSET_RE.finditer(str(text or "")):
        candidate = match.group(1).strip().strip('"')
        path = Path(candidate)
        if path.suffix.lower() in _IMAGE_SUFFIXES and path.exists() and str(path) not in found:
            found.append(str(path))
    return found


def markdown_to_html(text: str) -> str:
    """Markdown → HTML（Qt 富文本可渲染）；失败时降级为等宽原文。"""
    source = str(text or "")
    try:
        import markdown as markdown_module

        html = markdown_module.markdown(source, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:  # noqa: BLE001
        return "<pre>" + escape(source) + "</pre>"
    # Qt 富文本不认 <style> 选择器，代码的等宽字体与底色只能内联（§11.1 代码块样式）
    html = html.replace(
        "<pre>", f'<pre style="font-family:{CODE_FONT}; background-color:{CODE_BG}; padding:6px;">'
    )
    html = html.replace("<code>", f'<code style="font-family:{CODE_FONT};">')
    return html


def block_to_markdown(block: Mapping[str, Any]) -> str:
    """单个内容块 → Markdown 文本（解析失败时退化为围栏代码块，避免丢内容）。"""
    payload = dict(block)
    try:
        text = ContentBlock.model_validate(payload).to_markdown()
    except Exception:  # noqa: BLE001
        return "```json\n" + str(payload) + "\n```"
    kind = str(payload.get("type") or "")
    if kind == "diagram" and str(payload.get("kind") or "svg") != "svg" and text.strip():
        return "（这张图当前以源码展示，暂不支持离线绘制）\n\n" + text
    return text


def blocks_to_markdown(blocks: Sequence[Mapping[str, Any]]) -> str:
    return "\n\n".join(
        part.strip() for part in (block_to_markdown(block) for block in blocks) if part.strip()
    )


def html_to_plain(html: str) -> str:
    """富文本 → 纯文本（用 Qt 自己的解析器，结果与界面所见一致）。"""
    document = QTextDocument()
    document.setHtml(html)
    return document.toPlainText()


def block_svg(block: Mapping[str, Any]) -> str:
    """取出块里的内联 SVG 源码（没有就返回空串）。"""
    kind = str(block.get("type") or "")
    if kind == "svg":
        return str(block.get("svg") or block.get("content") or "")
    if kind == "diagram" and str(block.get("kind") or "svg") == "svg":
        return str(block.get("content") or block.get("svg") or "")
    return ""


def block_pixmap(block: Mapping[str, Any]) -> QPixmap | None:
    """把 ``svg`` / ``diagram(kind=svg)`` / ``image`` 块变成可显示的位图。

    纯本地：内联 SVG 用 QtSvg 离屏栅格化并缓存（同一张图流式刷新时不再重复渲染），
    本地图片直接读文件；失败返回 ``None``，由调用方退回源码展示。
    """
    svg_text = block_svg(block)
    if svg_text.strip():
        key = hashlib.sha1(svg_text.encode("utf-8")).hexdigest()
        cached = _IMAGE_CACHE.get(key)
        if cached is not None:
            return cached
        # 模型给的 SVG 属于不可信内容：渲染前必须过一遍清洗（§13.3 / §13.4）
        if not is_acceptable(clean(svg_text)):
            return None
        try:
            data = render_svg_png(svg_text, width=DIAGRAM_SOURCE_WIDTH, scale=1.0)
        except Exception:  # noqa: BLE001 - 渲染失败就退回源码
            return None
        pixmap = QPixmap()
        if not data or not pixmap.loadFromData(data, "PNG") or pixmap.isNull():
            return None
        _IMAGE_CACHE[key] = pixmap
        return pixmap
    if str(block.get("type") or "") == "image":
        path = str(block.get("asset") or block.get("path") or block.get("src") or "")
        if path and Path(path).exists():
            pixmap = QPixmap(path)
            if not pixmap.isNull():
                return pixmap
    return None


class _InputBox(QPlainTextEdit):
    """追问输入框：Ctrl+Enter 发送（控件级事件，比 QShortcut 更可靠）。"""

    submit = Signal()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if (
            event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            and event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self.submit.emit()
            return
        super().keyPressEvent(event)


class _ImageLabel(QLabel):
    """可双击的图（双击 = 看整张图）。"""

    doubleClicked = Signal()

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.doubleClicked.emit()
        super().mouseDoubleClickEvent(event)


class _Bubble(QFrame):
    """一个对话气泡：``role`` 为 ``user``（靠右）或 ``assistant``（靠左）。

    增量渲染：``append_block`` 追加已完成的块；``set_partial`` 只刷新末尾那一行；
    ``finish_stream`` 收掉临时行。全程**不重建已有控件**，所以不会闪。
    """

    diagram_requested = Signal(dict)

    def __init__(self, role: str, parent=None, *, persona: tuple[str, str] | None = None) -> None:
        super().__init__(parent)
        self.role = role
        self.setObjectName("ChatBubble")
        self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 9, 12, 9)
        self._layout.setSpacing(8)
        # 表头：老师的回答标注「谁在讲」（人设）；用户气泡没有表头
        self._header_label = QLabel()
        self._header_label.setObjectName("ChatBubbleHeader")
        self._header_label.setTextFormat(Qt.TextFormat.RichText)
        self._header_label.setWordWrap(True)
        self._header_label.setVisible(False)
        self._layout.addWidget(self._header_label)
        self._persona_label = ""
        self._streaming = False
        if persona is not None:
            self.set_persona(*persona)
        self.markdown = ""
        self.html_parts: list[str] = []
        self._plain_parts: list[str] = []
        self._partial_label: QLabel | None = None
        self.quote = ""

    # --- 表头（人设） ---------------------------------------------------
    def set_persona(self, name: str, avatar: str = "") -> None:
        self._persona_label = " ".join(
            part for part in (str(avatar or "").strip(), str(name or "").strip()) if part
        )
        self._refresh_header()

    def _refresh_header(self, *, streaming: bool | None = None) -> None:
        """表头只写「谁在讲」；**流式回答期间**才挂一个「正在回答」后缀（M33）。

        之前后缀是常驻的，于是历史记录里的旧回答也一直显示「正在回答」。
        """
        if streaming is not None:
            self._streaming = bool(streaming)
        label = self._persona_label
        if not label:
            self._header_label.setVisible(False)
            self._header_label.setText("")
            return
        suffix = ""
        if self._streaming:
            suffix = '<span style="font-size:11px; color:#8B949E;"> · 正在回答</span>'
        self._header_label.setText(
            f'<span style="font-size:13px; font-weight:600;">{escape(label)}</span>' + suffix
        )
        self._header_label.setVisible(True)

    # --- 引用（标注 / 选中片段） ----------------------------------------
    def set_quote(self, quote: str) -> None:
        """在气泡顶部放引用区：引用的原文 + 引用到的图片缩略图（可多张）。"""
        text = " ".join(str(quote or "").split())
        self.quote = text
        if not text:
            return
        offset = 1
        for asset in find_local_images(text):
            pixmap = QPixmap(asset)
            if pixmap.isNull():
                continue
            thumb = _ImageLabel()
            thumb.setObjectName("ChatBubbleImage")
            thumb.setPixmap(
                pixmap.scaledToWidth(
                    min(pixmap.width(), 240), Qt.TransformationMode.SmoothTransformation
                )
            )
            thumb.setAlignment(Qt.AlignmentFlag.AlignLeft)
            thumb.setCursor(Qt.CursorShape.PointingHandCursor)
            thumb.setToolTip("引用的讲义图片（双击看整张）")
            thumb.doubleClicked.connect(
                lambda _pixmap=pixmap: self.diagram_requested.emit(
                    {"svg": "", "pixmap": _pixmap, "title": "引用的讲义图片"}
                )
            )
            self._layout.insertWidget(offset, thumb)
            self._plain_parts.append("（引用了讲义里的一张图）")
            offset += 1
        label = QLabel(f"引用讲义：{text}")
        label.setObjectName("ChatQuoteChip")
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._layout.insertWidget(offset, label)  # 0 是表头，引用紧随其后
        self._plain_parts.append(f"（引用讲义：{text}）")

    # --- 内容 -----------------------------------------------------------
    def clear_content(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is None:
                continue
            if widget is self._header_label:  # 表头不属于内容，清空时保留
                continue
            if widget is not None:
                widget.deleteLater()
        self._layout.insertWidget(0, self._header_label)
        self.html_parts = []
        self._plain_parts = []
        self._partial_label = None
        self.markdown = ""
        self.quote = ""

    def _add_html(self, html: str) -> None:
        if not html.strip():
            return
        label = QLabel()
        label.setObjectName("ChatBubbleText")
        label.setTextFormat(Qt.TextFormat.RichText)
        label.setWordWrap(True)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        label.setText(html)
        self._layout.addWidget(label)
        self.html_parts.append(html)
        self._plain_parts.append(html_to_plain(html))

    def _add_image(self, pixmap: QPixmap, caption: str, svg: str = "") -> None:
        label = _ImageLabel()
        label.setObjectName("ChatBubbleImage")
        width = min(pixmap.width(), DIAGRAM_DISPLAY_WIDTH)
        label.setPixmap(pixmap.scaledToWidth(width, Qt.TransformationMode.SmoothTransformation))
        label.setAlignment(Qt.AlignmentFlag.AlignLeft)
        label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        label.setCursor(Qt.CursorShape.PointingHandCursor)
        label.setToolTip(f"{caption or '图示'}（双击看整张图）")
        label.doubleClicked.connect(
            lambda: self.diagram_requested.emit(
                {"svg": svg, "pixmap": pixmap, "title": caption or "图示"}
            )
        )
        self._layout.addWidget(label)
        self.html_parts.append(f"<p>{escape(caption or '图示')}</p>")
        self._plain_parts.append(f"（图示：{caption}）" if caption else "（图示）")

    def append_block(self, block: Mapping[str, Any]) -> None:
        """追加一个**已完成**的内容块（先摘掉末尾的临时行，保持它在最后）。"""
        self._detach_partial()
        pixmap = block_pixmap(block)
        if pixmap is not None:
            caption = str(block.get("title") or block.get("caption") or "图示")
            self._add_image(pixmap, caption, svg=block_svg(block))
        else:
            self._add_html(markdown_to_html(block_to_markdown(block)))
        addition = block_to_markdown(block).strip()
        if addition:
            self.markdown = (self.markdown + "\n\n" + addition).strip() if self.markdown else addition
        self._attach_partial()

    def set_markdown(self, markdown: str) -> None:
        """整块 Markdown（用户提问、历史记录里的 md、协议错乱回退）。"""
        self.clear_content()
        self.markdown = str(markdown or "")
        self._add_html(markdown_to_html(self.markdown))

    def set_blocks(self, blocks: Sequence[Mapping[str, Any]]) -> None:
        self.clear_content()
        for block in blocks:
            self.append_block(block)

    # --- 流式临时行 -----------------------------------------------------
    def set_partial(self, text: str) -> None:
        """末尾的「还没收完那一行」：原地刷新同一个标签，不重建控件。"""
        clean_text = str(text or "")
        if self._partial_label is None:
            label = QLabel()
            label.setObjectName("ChatBubbleText")
            label.setProperty("role", "muted")
            label.setTextFormat(Qt.TextFormat.RichText)
            label.setWordWrap(True)
            label.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
            self._partial_label = label
            self._layout.addWidget(label)
            self._refresh_header(streaming=True)  # 这一轮开始流式输出
        self._partial_label.setVisible(bool(clean_text.strip()))
        self._partial_label.setText(markdown_to_html(clean_text) if clean_text.strip() else "")

    def finish_stream(self) -> None:
        if self._partial_label is not None:
            self._layout.removeWidget(self._partial_label)
            self._partial_label.deleteLater()
            self._partial_label = None
        self._refresh_header(streaming=False)

    def _detach_partial(self) -> None:
        if self._partial_label is not None:
            self._layout.removeWidget(self._partial_label)

    def _attach_partial(self) -> None:
        if self._partial_label is not None:
            self._layout.addWidget(self._partial_label)

    def to_plain_text(self) -> str:
        return "\n".join(part for part in self._plain_parts if part)


class ChatHistoryView(QScrollArea):
    """气泡记录区：只读、不可获焦，仅用于展示。"""

    diagram_requested = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ChatHistory")
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # 整块不可获焦：这里不是输入框
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._host = QWidget()
        self._host.setObjectName("ChatHistoryHost")
        self._layout = QVBoxLayout(self._host)
        self._layout.setContentsMargins(10, 10, 10, 10)
        self._layout.setSpacing(10)
        self._bubbles: list[_Bubble] = []
        self._persona: tuple[str, str] = ("老师", "")
        # 是否"粘底"：内容变长时自动跟到底部；用户自己往上滚过就不再拽他（M37）
        self._stick_to_bottom = True
        # 定时器挂在控件上：控件销毁时一起销毁，避免「延后滚动」在窗口关掉后才触发
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(0)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom_now)
        scrollbar = self.verticalScrollBar()
        scrollbar.valueChanged.connect(self._on_scrolled)
        scrollbar.rangeChanged.connect(self._on_range_changed)

        self.empty_hint = QLabel("还没有追问记录：选中讲义里的片段提问，或在下面直接输入。")
        self.empty_hint.setProperty("role", "muted")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setWordWrap(True)
        self._layout.addWidget(self.empty_hint)
        self._layout.addStretch(1)
        self.setWidget(self._host)

    # --- 增删 -----------------------------------------------------------
    def add_bubble(
        self,
        role: str,
        markdown: str = "",
        *,
        blocks: Sequence[Mapping[str, Any]] | None = None,
        quote: str = "",
    ) -> _Bubble:
        bubble = _Bubble(role, persona=self._persona if role == "assistant" else None)
        bubble.diagram_requested.connect(self.diagram_requested)
        if blocks is not None:
            bubble.set_blocks(blocks)
        else:
            bubble.set_markdown(markdown)
        # 引用必须在内容之后：set_markdown/set_blocks 会清空内容区
        if quote:
            bubble.set_quote(quote)
        row = QWidget()
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(0)
        if role == "user":
            box.addStretch(1)
            box.addWidget(bubble)
        else:
            box.addWidget(bubble)
            box.addStretch(1)
        self._layout.insertWidget(self._layout.count() - 1, row)
        self._bubbles.append(bubble)
        self.empty_hint.setVisible(False)
        self._apply_bubble_width()
        self.scroll_to_bottom()
        return bubble

    def set_persona(self, name: str, avatar: str = "") -> None:
        """人设变了：已有的老师气泡表头一起更新（不用重开会话）。"""
        self._persona = (str(name or "老师"), str(avatar or ""))
        for bubble in self._bubbles:
            if bubble.role == "assistant":
                bubble.set_persona(*self._persona)

    @property
    def role_prefix(self) -> str:
        name = self._persona[0] or "老师"
        return f"{name}："

    def clear_bubbles(self) -> None:
        while self._layout.count() > 1:  # 保留末尾 stretch
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty_hint:
                widget.deleteLater()
        self._bubbles = []
        # ``takeAt`` 会把空提示一起摘出布局：不塞回去的话，它会停在旧位置与新气泡重叠（M35 修）
        if self._layout.indexOf(self.empty_hint) < 0:
            self._layout.insertWidget(0, self.empty_hint)
        self.empty_hint.setVisible(True)

    # --- 读取 -----------------------------------------------------------
    def toPlainText(self) -> str:  # noqa: N802 - 与旧接口保持一致
        parts: list[str] = []
        for bubble in self._bubbles:
            if bubble.role == "assistant":
                prefix = self.role_prefix
            else:
                prefix = ROLE_PREFIX.get(bubble.role, "")
            parts.append(prefix + bubble.to_plain_text())
        return "\n\n".join(parts)

    def conversation_markdown(self) -> str:
        parts: list[str] = []
        for bubble in self._bubbles:
            body = bubble.markdown.strip()
            if not body:
                continue
            if bubble.role == "user":
                parts.append(f"**我：** {body}")
            else:
                parts.append(f"**老师：**\n\n{body}")
        return "\n\n".join(parts)

    def scroll_to_bottom(self) -> None:
        """把记录区滚到底（**延到事件循环下一拍**再滚）。

        刚插入的气泡还没完成布局，立刻读 ``maximum()`` 拿到的是旧值——表现就是「答案在往下长，
        可视图停在上面，看不到最新内容」（M37 修：追问 / 小灶都受影响）。
        """
        self._stick_to_bottom = True
        self._scroll_timer.start()

    def _scroll_to_bottom_now(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def _on_scrolled(self, value: int) -> None:
        """用户自己往上滚 → 解开粘底；滚回底部 → 重新粘上。"""
        bar = self.verticalScrollBar()
        self._stick_to_bottom = value >= bar.maximum() - 8

    def _on_range_changed(self, _minimum: int, maximum: int) -> None:
        """内容变长（气泡长大 / 新块追加）时跟到底部。"""
        if self._stick_to_bottom:
            self.verticalScrollBar().setValue(maximum)

    # --- 布局 -----------------------------------------------------------
    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._apply_bubble_width()

    def _apply_bubble_width(self) -> None:
        """气泡最宽占可视宽度的 ~84%，保证长段落换行而不是横向溢出。"""
        width = max(200, int(self.viewport().width() * 0.84))
        for bubble in self._bubbles:
            bubble.setMaximumWidth(width)


class ChatDrawer(QWidget):
    send_requested = Signal(str, str)  # (问题, 选中片段)
    stop_requested = Signal()
    zoom_requested = Signal()
    diagram_requested = Signal(dict)  # {svg|pixmap, title}：双击图看整张
    session_changed = Signal(str)  # 切换会话
    new_session_requested = Signal()
    rename_session_requested = Signal(str)
    delete_session_requested = Signal(str)
    annotation_edit_requested = Signal(str)  # 改某条标注的备注
    annotation_delete_requested = Signal(str)
    annotations_clear_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(380)  # 太窄气泡换行难看（M23）
        self._annotations: list[dict] = []
        self._citations: list[dict] = []
        layout = QVBoxLayout(self)
        title_row = QHBoxLayout()
        title = QLabel("追问")
        title.setProperty("role", "subtitle")
        title_row.addWidget(title)
        self.persona_hint = QLabel("")
        self.persona_hint.setProperty("role", "muted")
        self.persona_hint.setToolTip("当前教学人设：讲义与回答都由这个人设来表达")
        title_row.addWidget(self.persona_hint)
        title_row.addStretch(1)
        layout.addLayout(title_row)

        # 会话：每个小节可以有多个追问会话（M27）
        session_row = QHBoxLayout()
        session_row.setSpacing(6)
        session_label = QLabel("会话")
        session_label.setProperty("role", "muted")
        session_row.addWidget(session_label)
        self.session_combo = QComboBox()
        self.session_combo.setMinimumWidth(150)
        self.session_combo.setToolTip("切换这个小节的追问会话")
        self.session_combo.currentIndexChanged.connect(self._on_session_picked)
        session_row.addWidget(self.session_combo, 1)
        self.new_session_button = QPushButton("＋ 新建会话")
        self.new_session_button.setProperty("role", "primary")
        self.new_session_button.setToolTip("开一个全新的追问会话，互不串上下文")
        self.new_session_button.clicked.connect(self.new_session_requested.emit)
        session_row.addWidget(self.new_session_button)
        self.rename_session_button = QPushButton("重命名")
        self.rename_session_button.setProperty("role", "ghost")
        self.rename_session_button.clicked.connect(
            lambda: self.rename_session_requested.emit(self.current_session_id())
        )
        session_row.addWidget(self.rename_session_button)
        self.delete_session_button = QPushButton("删除")
        self.delete_session_button.setProperty("role", "ghost")
        self.delete_session_button.clicked.connect(
            lambda: self.delete_session_requested.emit(self.current_session_id())
        )
        session_row.addWidget(self.delete_session_button)
        layout.addLayout(session_row)

        # 讲义标注（划词标注）：一条标注 = 一段原文（可选备注），可一键引用到追问
        annot_row = QHBoxLayout()
        annot_row.setSpacing(6)
        self.annotation_label = QLabel("讲义标注（0）")
        self.annotation_label.setProperty("role", "muted")
        annot_row.addWidget(self.annotation_label)
        annot_row.addStretch(1)
        self.cite_annotation_button = QPushButton("引用到追问")
        self.cite_annotation_button.setProperty("role", "primary")
        self.cite_annotation_button.setToolTip("把选中的标注原文带进下面的提问框")
        self.cite_annotation_button.clicked.connect(lambda: self.cite_annotation(self.current_annotation()))
        annot_row.addWidget(self.cite_annotation_button)
        self.edit_annotation_button = QPushButton("改备注")
        self.edit_annotation_button.setProperty("role", "ghost")
        self.edit_annotation_button.clicked.connect(
            lambda: self.annotation_edit_requested.emit(self.current_annotation_id())
        )
        annot_row.addWidget(self.edit_annotation_button)
        self.delete_annotation_button = QPushButton("删除")
        self.delete_annotation_button.setProperty("role", "ghost")
        self.delete_annotation_button.clicked.connect(
            lambda: self.annotation_delete_requested.emit(self.current_annotation_id())
        )
        annot_row.addWidget(self.delete_annotation_button)
        self.clear_annotations_button = QPushButton("清空")
        self.clear_annotations_button.setProperty("role", "ghost")
        self.clear_annotations_button.clicked.connect(self.annotations_clear_requested.emit)
        annot_row.addWidget(self.clear_annotations_button)
        layout.addLayout(annot_row)

        self.annotation_list = QListWidget()
        self.annotation_list.setObjectName("AnnotationList")
        self.annotation_list.setFixedHeight(118)
        self.annotation_list.setToolTip("在讲义里选中文字 → 点「✏️ 标注」；双击这里 = 引用到追问")
        self.annotation_list.itemDoubleClicked.connect(
            lambda _item: self.cite_annotation(self.current_annotation())
        )
        layout.addWidget(self.annotation_list)
        self.empty_annotation_hint = QLabel("还没有标注：在中间讲义里选中一段文字，点浮出的「✏️ 标注」。")
        self.empty_annotation_hint.setProperty("role", "muted")
        self.empty_annotation_hint.setWordWrap(True)
        layout.addWidget(self.empty_annotation_hint)

        self.history_label = QLabel("对话记录（只读；这里只展示，不能输入）")
        self.history_label.setProperty("role", "muted")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)

        self.history = ChatHistoryView()
        self.history.diagram_requested.connect(self.diagram_requested)
        self.history.setMinimumHeight(240)
        layout.addWidget(self.history, 1)

        # 引用区：可以同时引用「一句话 + 一张图 + ……」，提问时一起发给老师
        citation_row = QHBoxLayout()
        citation_row.setSpacing(6)
        self.selection_label = QLabel("引用（0）：可以同时引用多段文字和多张图")
        self.selection_label.setProperty("role", "muted")
        self.selection_label.setWordWrap(True)
        citation_row.addWidget(self.selection_label, 1)
        self.remove_citation_button = QPushButton("移除选中")
        self.remove_citation_button.setProperty("role", "ghost")
        self.remove_citation_button.clicked.connect(self.remove_current_citation)
        citation_row.addWidget(self.remove_citation_button)
        self.clear_selection_button = QPushButton("清空引用")
        self.clear_selection_button.setProperty("role", "ghost")
        self.clear_selection_button.clicked.connect(self.clear_citations)
        citation_row.addWidget(self.clear_selection_button)
        layout.addLayout(citation_row)

        self.citation_list = QListWidget()
        self.citation_list.setObjectName("CitationList")
        self.citation_list.setFixedHeight(78)
        self.citation_list.setToolTip("这些会随下一次提问一起发给老师（含每条的评论）")
        self.citation_list.setVisible(False)
        layout.addWidget(self.citation_list)
        for button in (self.remove_citation_button, self.clear_selection_button):
            button.setVisible(False)

        input_label = QLabel("你的追问")
        input_label.setProperty("role", "muted")
        layout.addWidget(input_label)

        self.input_edit = _InputBox()
        self.input_edit.setPlaceholderText("在这里输入追问（Ctrl+Enter 发送）")
        self.input_edit.setFixedHeight(84)
        self.input_edit.setEnabled(True)
        self.input_edit.setReadOnly(False)
        self.input_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.input_edit.submit.connect(self._on_send)

        composer = QHBoxLayout()
        composer.addWidget(self.input_edit, 1)
        buttons = QVBoxLayout()
        self.send_button = QPushButton("发送")
        self.send_button.setProperty("role", "primary")
        self.send_button.clicked.connect(self._on_send)
        buttons.addWidget(self.send_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        buttons.addWidget(self.stop_button)
        self.zoom_button = QPushButton("放大")
        self.zoom_button.setProperty("role", "ghost")
        self.zoom_button.clicked.connect(self.zoom_requested.emit)
        buttons.addWidget(self.zoom_button)
        composer.addLayout(buttons)
        layout.addLayout(composer)

        # 流式回答状态
        self._parser = BlockStreamParser()
        self._current_bubble: _Bubble | None = None
        self._rendered_blocks = 0

    # --- 加载与写入 -----------------------------------------------------
    # 引用区状态：每项 {"kind": "text"|"image", "quote", "note", "asset"}
    def citations(self) -> list[dict]:
        return [dict(item) for item in self._citations]

    def citation_assets(self) -> list[str]:
        """引用到的本地图片路径（去重、保序）。"""
        found: list[str] = []
        for item in self._citations:
            asset = str(item.get("asset") or "")
            if item.get("kind") == "image" and asset and asset not in found:
                found.append(asset)
        return found

    def selection_text(self) -> str:
        """引用区 → 发给老师的文本（每条的评论一起带上）。"""
        lines: list[str] = []
        for index, item in enumerate(self._citations, start=1):
            quote = str(item.get("quote") or "").strip()
            note = str(item.get("note") or "").strip()
            if item.get("kind") == "image":
                line = f"{index}. [图片] {quote}"
            else:
                line = f"{index}. 「{quote}」"
            if note:
                line += f"（我的评论：{note}）"
            lines.append(line)
        return "\n".join(lines)

    @property
    def selection(self) -> str:  # noqa: D401 - 兼容旧接口
        return self.selection_text()

    def add_citation(self, *, kind: str = "text", quote: str, note: str = "", asset: str = "") -> dict | None:
        """往引用区加一个对象（可以同时引用多段文字、多张图）。"""
        text = " ".join(str(quote or "").split())
        if not text:
            return None
        item = {"kind": str(kind or "text"), "quote": text, "note": str(note or "").strip(), "asset": str(asset or "")}
        for existing in self._citations:
            if (existing["kind"], existing["quote"], existing["asset"]) == (item["kind"], item["quote"], item["asset"]):
                if item["note"]:
                    existing["note"] = item["note"]
                    self._refresh_citations()
                return existing
        self._citations.append(item)
        self._refresh_citations()
        return item

    def clear_citations(self) -> None:
        self._citations = []
        self._refresh_citations()

    def remove_current_citation(self) -> None:
        row = self.citation_list.currentRow()
        if 0 <= row < len(self._citations):
            self._citations.pop(row)
            self._refresh_citations()

    def _refresh_citations(self) -> None:
        self.citation_list.clear()
        for item in self._citations:
            quote = str(item.get("quote") or "")
            note = str(item.get("note") or "")
            if item.get("kind") == "image":
                label = f"🖼 图片：{quote[:34]}"
            else:
                label = f"📄 “{quote[:30]}{'…' if len(quote) > 30 else ''}”"
            if note:
                label += f"  · 评论：{note[:14]}"
            row = QListWidgetItem(label)
            row.setData(Qt.ItemDataRole.UserRole, item)
            row.setToolTip(quote + (f"\n评论：{note}" if note else ""))
            self.citation_list.addItem(row)
        count = len(self._citations)
        self.selection_label.setText(
            f"引用（{count}）：可以同时引用多段文字和多张图" if count else "引用（0）：可以同时引用多段文字和多张图"
        )
        self.citation_list.setVisible(count > 0)
        self.clear_selection_button.setVisible(count > 0)
        self.remove_citation_button.setVisible(count > 0)
        if count:
            self.citation_list.setCurrentRow(0)

    def focus_input(self) -> None:
        """把焦点给输入框（打开小节时调用）。"""
        self.input_edit.setFocus()

    # --- 会话 -----------------------------------------------------------
    def set_sessions(self, sessions: list[dict], *, current: str = "") -> None:
        """填充会话下拉（静默切换，不触发 session_changed）。"""
        self.session_combo.blockSignals(True)
        self.session_combo.clear()
        for item in sessions:
            self.session_combo.addItem(
                str(item.get("title") or "新会话"), str(item.get("id") or "")
            )
        index = self.session_combo.findData(current)
        if index < 0 and self.session_combo.count():
            index = self.session_combo.count() - 1
        if index >= 0:
            self.session_combo.setCurrentIndex(index)
        self.session_combo.blockSignals(False)

    def current_session_id(self) -> str:
        return str(self.session_combo.currentData() or "")

    def _on_session_picked(self, _index: int) -> None:
        session_id = self.current_session_id()
        if session_id:
            self.session_changed.emit(session_id)

    # --- 讲义标注（划词 → 引用到追问） ----------------------------------
    def set_annotations(self, items: Sequence[Mapping[str, Any]]) -> None:
        """填充标注列表；不改变当前引用（引用由用户点「引用到追问」显式发生）。"""
        self._annotations = [dict(item) for item in items if item.get("quote")]
        selected = self.current_annotation_id()
        self.annotation_list.clear()
        for item in self._annotations:
            quote = " ".join(str(item.get("quote") or "").split())
            note = " ".join(str(item.get("note") or "").split())
            label = f"“{quote[:28]}{'…' if len(quote) > 28 else ''}”"
            if note:
                label += f"  · 备注：{note[:16]}"
            row = QListWidgetItem(label)
            row.setData(Qt.ItemDataRole.UserRole, str(item.get("id") or ""))
            row.setToolTip(quote + (f"\n备注：{note}" if note else ""))
            self.annotation_list.addItem(row)
        count = len(self._annotations)
        self.annotation_label.setText(f"讲义标注（{count}）")
        self.empty_annotation_hint.setVisible(count == 0)
        self.annotation_list.setVisible(count > 0)
        for index in range(self.annotation_list.count()):
            if self.annotation_list.item(index).data(Qt.ItemDataRole.UserRole) == selected:
                self.annotation_list.setCurrentRow(index)
                break
        if count and self.annotation_list.currentRow() < 0:
            self.annotation_list.setCurrentRow(0)

    def current_annotation_id(self) -> str:
        item = self.annotation_list.currentItem()
        return str(item.data(Qt.ItemDataRole.UserRole) or "") if item is not None else ""

    def current_annotation(self) -> dict | None:
        wanted = self.current_annotation_id()
        if not wanted:
            return None
        return next((item for item in self._annotations if item.get("id") == wanted), None)

    def cite_annotation(self, item: Mapping[str, Any] | None) -> bool:
        """把一条标注引用到追问（原文 + 评论一起带过去，作为本次提问的依据）。"""
        if not item:
            return False
        quote = " ".join(str(item.get("quote") or "").split())
        if not quote:
            return False
        note = " ".join(str(item.get("note") or "").split())
        self.cite_quote(quote, note=note)
        return True

    def cite_quote(self, quote: str, note: str = "", *, kind: str = "text", asset: str = "") -> None:
        """引用一个讲义对象（一句话 / 一张图）：加进引用区，随下一次提问一起发送。"""
        text = " ".join(str(quote or "").split())
        if not text:
            return
        asset = str(asset or "") or find_local_image(text)
        if asset and kind == "text":
            kind = "image"  # 引用文本里带着本地图片路径 → 按「图片」引用处理，图片才会真发出去
        self.add_citation(kind=kind, quote=text, note=note, asset=asset)
        self.focus_input()

    # --- 人设（回答必须看得出是谁在讲） --------------------------------
    def set_persona(self, persona: Mapping[str, Any] | None) -> None:
        name = str((persona or {}).get("name") or "老师")
        avatar = str((persona or {}).get("avatar") or "")
        self.history.set_persona(name, avatar)
        self.persona_hint.setText(" · ".join(part for part in (f"{avatar} {name}".strip(), "在回答") if part))

    def set_selection(self, text: str) -> None:
        """兼容旧接口：用一段文本整体替换引用区。"""
        self.clear_citations()
        flat = " ".join(str(text or "").split())
        if flat:
            asset = find_local_image(flat)
            self.add_citation(kind="image" if asset else "text", quote=flat, asset=asset)

    def clear(self) -> None:
        self.history.clear_bubbles()
        self._parser = BlockStreamParser()
        self._current_bubble = None
        self._rendered_blocks = 0

    def append_user(self, text: str, quote: str = "") -> None:
        self.history.add_bubble("user", str(text), quote=quote)

    def begin_answer(self) -> None:
        """开始一段回答：新建一个左侧气泡，后续内容增量追加进去。"""
        self._parser = BlockStreamParser()
        self._rendered_blocks = 0
        self._current_bubble = self.history.add_bubble("assistant", "")
        self._current_bubble.set_partial("（正在思考…）")

    def feed_answer_piece(self, piece: str) -> None:
        """流式喂入 NDJSON 片段：新块追加、未收完的一行原地刷新。"""
        self._parser.feed(piece)
        self._refresh_current_bubble()

    def end_answer(self) -> None:
        self._parser.finish()
        self._refresh_current_bubble()
        bubble = self._current_bubble
        if bubble is not None:
            if self._parser.protocol_broken:
                fallback = self._parser.fallback_markdown()
                if fallback.strip():
                    bubble.set_markdown(fallback)
            bubble.finish_stream()
            if not bubble.markdown.strip():
                bubble.set_markdown("（没有拿到内容，请重试一次。）")
        self._current_bubble = None
        self.history.scroll_to_bottom()

    def _refresh_current_bubble(self) -> None:
        """只做增量：把新完成的块追加进去，其余原地刷新（避免整块重建导致闪烁）。"""
        bubble = self._current_bubble
        if bubble is None:
            return
        blocks = list(self._parser.blocks)
        new_blocks = blocks[self._rendered_blocks :]
        if new_blocks:
            for block in new_blocks:
                bubble.append_block(block)
            self._rendered_blocks = len(blocks)
            self.history._apply_bubble_width()
        bubble.set_partial(self._parser.partial_text() or "")
        self.history.scroll_to_bottom()

    def append_stream(self, piece: str) -> None:  # 兼容旧调用
        self.feed_answer_piece(piece)

    def append_assistant_start(self) -> None:
        self.begin_answer()

    def append_assistant_end(self) -> None:
        self.end_answer()

    def load_records(self, records: list[dict]) -> None:
        self.clear()
        for record in records:
            if record.get("role") == "user":
                self.append_user(
                    record.get("text") or "", quote=str(record.get("selection") or "")
                )
                continue
            raw = str(record.get("md") or "")
            if raw.strip().startswith("{"):
                parser = BlockStreamParser()
                parser.feed(raw)
                parser.finish()
                if parser.blocks:
                    self.history.add_bubble("assistant", blocks=parser.blocks)
                    continue
            self.history.add_bubble("assistant", raw)
        self.history.scroll_to_bottom()

    def conversation_markdown(self) -> str:
        """当前对话的 Markdown 源（供「放大」窗口保留结构地阅读）。"""
        return self.history.conversation_markdown()

    # --- 交互 -----------------------------------------------------------
    def _on_send(self) -> None:
        question = self.input_edit.toPlainText().strip()
        if not question:
            return
        self.input_edit.clear()
        # 引用区里的对象（文字 / 图片，含评论）随这次提问一起发出去，气泡里也留一份
        quoted = self.selection_text()
        self.append_user(question, quote=quoted)
        self.send_requested.emit(question, quoted)
        # 发完就清空引用区：下一问重新选，避免把上次的图又带一遍
        self.clear_citations()
