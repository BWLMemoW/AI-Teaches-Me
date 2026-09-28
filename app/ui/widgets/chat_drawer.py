"""追问抽屉（《项目文档》§4.7 FR-07、§11.4 ``ChatDrawer``；《技术文档》§3.6 ``chat.jsonl``）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


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

        self.input_edit = QPlainTextEdit()
        self.input_edit.setPlaceholderText("输入追问，Ctrl+Enter 发送")
        self.input_edit.setFixedHeight(72)
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
