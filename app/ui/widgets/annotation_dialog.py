"""标注详情窗口（M28）：写评论 / 引用到追问 / 删除。

讲义里点黄色划线、或点右侧标注列表的「改备注」，都走这一个窗口：
上面是原文，中间是评论框，下面三个动作，所见即所得。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

ACTION_CLOSE = "close"
ACTION_SAVE = "save"
ACTION_CITE = "cite"
ACTION_DELETE = "delete"


class AnnotationDialog(QDialog):
    def __init__(
        self,
        *,
        quote: str,
        note: str = "",
        index: int = 0,
        total: int = 0,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("讲义标注")
        self.resize(580, 400)
        self.action = ACTION_CLOSE

        layout = QVBoxLayout(self)

        caption = QLabel("这一条标注的原文（讲义里显示为黄色划线）")
        caption.setProperty("role", "muted")
        layout.addWidget(caption)

        quote_box = QLabel(str(quote or ""))
        quote_box.setObjectName("AnnotationQuote")
        quote_box.setWordWrap(True)
        quote_box.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(quote_box)

        if total:
            position = QLabel(f"第 {index} / {total} 条标注")
            position.setProperty("role", "muted")
            layout.addWidget(position)

        note_caption = QLabel("我的评论（写好后会随引用一起发给老师）")
        note_caption.setProperty("role", "muted")
        layout.addWidget(note_caption)

        self.note_edit = QPlainTextEdit()
        self.note_edit.setPlaceholderText("写点自己的理解、疑问或提醒……")
        self.note_edit.setPlainText(str(note or ""))
        layout.addWidget(self.note_edit, 1)

        row = QHBoxLayout()
        self.cite_button = QPushButton("引用到追问")
        self.cite_button.setProperty("role", "primary")
        self.cite_button.setToolTip("把这段原文 + 评论带到右侧追问")
        self.cite_button.clicked.connect(self._on_cite)
        row.addWidget(self.cite_button)
        self.save_button = QPushButton("保存评论")
        self.save_button.clicked.connect(self._on_save)
        row.addWidget(self.save_button)
        self.delete_button = QPushButton("删除标注")
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(self._on_delete)
        row.addWidget(self.delete_button)
        row.addStretch(1)
        self.close_button = QPushButton("关闭")
        self.close_button.setProperty("role", "ghost")
        self.close_button.clicked.connect(self.reject)
        row.addWidget(self.close_button)
        layout.addLayout(row)

    # --- 动作 -----------------------------------------------------------
    def current_note(self) -> str:
        return self.note_edit.toPlainText().strip()

    def _on_save(self) -> None:
        self.action = ACTION_SAVE
        self.accept()

    def _on_cite(self) -> None:
        self.action = ACTION_CITE
        self.accept()

    def _on_delete(self) -> None:
        self.action = ACTION_DELETE
        self.accept()
