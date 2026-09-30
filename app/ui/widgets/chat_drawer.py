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
from collections.abc import Mapping, Sequence
from html import escape
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap, QTextDocument
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
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

    def __init__(self, role: str, parent=None) -> None:
        super().__init__(parent)
        self.role = role
        self.setObjectName("ChatBubble")
        self.setProperty("role", role)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(12, 9, 12, 9)
        self._layout.setSpacing(8)
        self.markdown = ""
        self.html_parts: list[str] = []
        self._plain_parts: list[str] = []
        self._partial_label: QLabel | None = None

    # --- 内容 -----------------------------------------------------------
    def clear_content(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self.html_parts = []
        self._plain_parts = []
        self._partial_label = None
        self.markdown = ""

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
        self._partial_label.setVisible(bool(clean_text.strip()))
        self._partial_label.setText(markdown_to_html(clean_text) if clean_text.strip() else "")

    def finish_stream(self) -> None:
        if self._partial_label is not None:
            self._layout.removeWidget(self._partial_label)
            self._partial_label.deleteLater()
            self._partial_label = None

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
    ) -> _Bubble:
        bubble = _Bubble(role)
        bubble.diagram_requested.connect(self.diagram_requested)
        if blocks is not None:
            bubble.set_blocks(blocks)
        else:
            bubble.set_markdown(markdown)
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

    def clear_bubbles(self) -> None:
        while self._layout.count() > 1:  # 保留末尾 stretch
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None and widget is not self.empty_hint:
                widget.deleteLater()
        self._bubbles = []
        self.empty_hint.setVisible(True)

    # --- 读取 -----------------------------------------------------------
    def toPlainText(self) -> str:  # noqa: N802 - 与旧接口保持一致
        return "\n\n".join(
            ROLE_PREFIX.get(bubble.role, "") + bubble.to_plain_text() for bubble in self._bubbles
        )

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
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(380)  # 太窄气泡换行难看（M23）
        layout = QVBoxLayout(self)
        title = QLabel("追问")
        title.setProperty("role", "subtitle")
        layout.addWidget(title)

        self.history_label = QLabel("对话记录（只读；这里只展示，不能输入）")
        self.history_label.setProperty("role", "muted")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)

        self.history = ChatHistoryView()
        self.history.diagram_requested.connect(self.diagram_requested)
        self.history.setMinimumHeight(240)
        layout.addWidget(self.history, 1)

        self.selection_label = QLabel("未选中片段")
        self.selection_label.setProperty("role", "muted")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)

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
    selection = ""

    def focus_input(self) -> None:
        """把焦点给输入框（打开小节时调用）。"""
        self.input_edit.setFocus()

    def set_selection(self, text: str) -> None:
        self.selection = text
        self.selection_label.setText(f"选中：{text[:60]}" if text else "未选中片段")

    def clear(self) -> None:
        self.history.clear_bubbles()
        self._parser = BlockStreamParser()
        self._current_bubble = None
        self._rendered_blocks = 0

    def append_user(self, text: str) -> None:
        self.history.add_bubble("user", str(text))

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
                self.append_user(record.get("text") or "")
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
        self.append_user(question)
        self.send_requested.emit(question, self.selection)