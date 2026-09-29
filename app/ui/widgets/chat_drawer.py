"""追问抽屉（《项目文档》§4.7 FR-07、§11.4 ``ChatDrawer``）。

M23 起回答**渲染 Markdown**（标题/列表/表格/代码块），不再吐原始 NDJSON：
* 记录区用 ``QTextBrowser``（Qt 富文本，支持表格与等宽代码）；
* 模型流式吐 NDJSON → 解析成内容块 → 逐块转 Markdown → 追加渲染（块级流式）；
* 记录区只读且不可获焦；输入框在下方与发送按钮同行；支持「放大」到独立窗口；
* 同时维护一份 Markdown 源（``conversation_markdown``），供「放大」窗口保留结构地展示。
"""

from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ...core.models import ContentBlock
from ...llm.stream import BlockStreamParser


def markdown_to_html(text: str) -> str:
    """Markdown → HTML（Qt 富文本可渲染）；失败时降级为等宽原文。"""
    source = str(text or "")
    try:
        import markdown as markdown_module

        return markdown_module.markdown(source, extensions=["tables", "fenced_code", "sane_lists"])
    except Exception:  # noqa: BLE001
        return "<pre>" + escape(source) + "</pre>"


def block_to_markdown(block: dict) -> str:
    """单个内容块 → Markdown 文本（解析失败时退化为围栏代码块，避免丢内容）。"""
    try:
        return ContentBlock.model_validate(block).to_markdown()
    except Exception:  # noqa: BLE001
        return "```json\n" + str(block) + "\n```"


def blocks_to_html(blocks: list[dict]) -> str:
    parts: list[str] = []
    for block in blocks:
        markdown_text = block_to_markdown(block)
        if markdown_text.strip():
            parts.append(markdown_to_html(markdown_text))
    return "".join(parts)


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


class ChatDrawer(QWidget):
    send_requested = Signal(str, str)  # (问题, 选中片段)
    stop_requested = Signal()
    zoom_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumWidth(380)  # 太窄渲染不好看（M23）
        layout = QVBoxLayout(self)
        title = QLabel("追问")
        title.setProperty("role", "subtitle")
        layout.addWidget(title)

        self.history_label = QLabel("对话记录（只读；这里只展示，不能输入）")
        self.history_label.setProperty("role", "muted")
        self.history_label.setWordWrap(True)
        layout.addWidget(self.history_label)

        self.history = QTextBrowser()
        self.history.setOpenExternalLinks(False)
        self.history.setReadOnly(True)
        self.history.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.history.setObjectName("ChatHistory")
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

        # 回答流式解析状态 + Markdown 源（供放大窗口）
        self._parser = BlockStreamParser()
        self._rendered_blocks = 0
        self._transcript_md: list[str] = []

    # --- 加载与写入 -----------------------------------------------------
    selection = ""

    def focus_input(self) -> None:
        """把焦点给输入框（打开小节时调用）。"""
        self.input_edit.setFocus()

    def set_selection(self, text: str) -> None:
        self.selection = text
        self.selection_label.setText(f"选中：{text[:60]}" if text else "未选中片段")

    def clear(self) -> None:
        self.history.clear()
        self._parser = BlockStreamParser()
        self._rendered_blocks = 0
        self._transcript_md = []

    def _push_markdown(self, text: str) -> None:
        if text:
            self._transcript_md.append(text)

    def append_user(self, text: str) -> None:
        self.history.append(
            f'<p style="margin:8px 0 2px;"><b>我：</b>{escape(str(text))}</p>'
        )
        self._push_markdown(f"**我：** {text}\n\n")

    def begin_answer(self) -> None:
        """开始一段回答：重置解析器并写入「老师：」标签。"""
        self._parser = BlockStreamParser()
        self._rendered_blocks = 0
        self.history.append('<p style="margin:8px 0 2px;"><b>老师：</b></p>')
        self._push_markdown("**老师：**\n\n")

    def feed_answer_piece(self, piece: str) -> None:
        """流式喂入 NDJSON 片段：每完成一个内容块就渲染一段 Markdown。"""
        self._parser.feed(piece)
        self._flush_new_blocks()

    def end_answer(self) -> None:
        self._parser.finish()
        self._flush_new_blocks()
        if self._parser.protocol_broken:
            fallback = self._parser.fallback_markdown()
            self.history.append(markdown_to_html(fallback))
            self._push_markdown(fallback + "\n\n")

    def _flush_new_blocks(self) -> None:
        blocks = self._parser.blocks[self._rendered_blocks :]
        if not blocks:
            return
        self._rendered_blocks = len(self._parser.blocks)
        self.history.append(blocks_to_html(blocks))
        for block in blocks:
            self._push_markdown(block_to_markdown(block).rstrip() + "\n\n")
        scrollbar = self.history.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

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
                markdown_parts = [block_to_markdown(block) for block in parser.blocks]
                html = blocks_to_html(parser.blocks) or markdown_to_html(raw)
                markdown_source = "\n\n".join(part.strip() for part in markdown_parts if part.strip()) or raw
            else:
                html = markdown_to_html(raw)
                markdown_source = raw
            self.history.append('<p style="margin:8px 0 2px;"><b>老师：</b></p>' + html)
            self._push_markdown("**老师：**\n\n" + markdown_source.strip() + "\n\n")

    def conversation_markdown(self) -> str:
        """当前对话的 Markdown 源（供「放大」窗口保留结构地阅读）。"""
        return "".join(self._transcript_md)

    # --- 交互 -----------------------------------------------------------
    def _on_send(self) -> None:
        question = self.input_edit.toPlainText().strip()
        if not question:
            return
        self.input_edit.clear()
        self.append_user(question)
        self.send_requested.emit(question, self.selection)