"""追问抽屉（《项目文档》§4.7 FR-07、§11.4 ``ChatDrawer``；《技术文档》§3.6 ``chat.jsonl``）。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


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

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        title = QLabel("追问")
        title.setProperty("role", "subtitle")
        layout.addWidget(title)

        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        self.history.setPlaceholderText("选中讲义里的句子后提问，回答会带上这一节的上下文。")
        layout.addWidget(self.history, 1)

        self.selection_label = QLabel("未选中片段")
        self.selection_label.setProperty("role", "muted")
        self.selection_label.setWordWrap(True)
        layout.addWidget(self.selection_label)

        self.input_edit = _InputBox()
        self.input_edit.setPlaceholderText("输入追问，Ctrl+Enter 发送")
        self.input_edit.setFixedHeight(72)
        # 明确保证「可以输入」：不被禁用、非只读、能拿焦点（曾出现「文字输不进去」）
        self.input_edit.setEnabled(True)
        self.input_edit.setReadOnly(False)
        self.input_edit.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.input_edit.submit.connect(self._on_send)
        layout.addWidget(self.input_edit)

        row = QHBoxLayout()
        self.send_button = QPushButton("发送")
        self.send_button.setProperty("role", "primary")
        self.send_button.clicked.connect(self._on_send)
        row.addWidget(self.send_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self.stop_requested.emit)
        row.addWidget(self.stop_button)
        row.addStretch(1)
        layout.addLayout(row)

    def focus_input(self) -> None:
        """把焦点给输入框（打开小节 / 点开抽屉时调用）。"""
        self.input_edit.setFocus()

    def set_selection(self, text: str) -> None:
        self.selection = text
        self.selection_label.setText(f"选中：{text[:60]}" if text else "未选中片段")

    selection = ""

    def _on_send(self) -> None:
        question = self.input_edit.toPlainText().strip()
        if not question:
            return
        self.input_edit.clear()
        self.append_user(question)
        self.send_requested.emit(question, self.selection)

    def append_user(self, text: str) -> None:
        self.history.appendPlainText(f"我：{text}\n")

    def append_assistant_start(self) -> None:
        self.history.appendPlainText("老师：")

    def append_stream(self, piece: str) -> None:
        cursor = self.history.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(piece)
        self.history.setTextCursor(cursor)
        self.history.ensureCursorVisible()

    def append_assistant_end(self) -> None:
        self.history.appendPlainText("")

    def load_records(self, records: list[dict]) -> None:
        self.history.clear()
        for record in records:
            role = "我" if record.get("role") == "user" else "老师"
            text = record.get("text") or record.get("md") or ""
            self.history.appendPlainText(f"{role}：{text}\n")
