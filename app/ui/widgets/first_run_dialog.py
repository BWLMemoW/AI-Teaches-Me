"""首启动引导（《项目文档》FR-01 / AC-02、R-10：空状态要有引导）。"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


class FirstRunDialog(QDialog):
    """三步上手：① 配置 API → ② 新建课程 → ③ 打开示例课程先熟悉界面。"""

    go_settings = Signal()
    go_new_course = Signal()
    open_seed_course = Signal()
    go_backup = Signal()

    def __init__(self, *, has_seed_course: bool = True, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("欢迎使用 AI Teaches Me")
        self.resize(560, 320)

        layout = QVBoxLayout(self)
        title = QLabel("三步开始")
        title.setProperty("role", "title")
        layout.addWidget(title)

        steps = QLabel(
            "① 配置 AI 供应商与密钥（设置 → 测试连接）\n"
            "② 新建课程：只填课程名，AI 会拆成章 → 小节 → 知识点\n"
            "③ 打开课程逐节学习：可换人设、调深度、追问、生成笔记\n\n"
            "不想立刻配置也行：先读内置的《ATM 使用指南》，它自带预置讲义、离线可读。"
        )
        steps.setWordWrap(True)
        layout.addWidget(steps)

        hint = QLabel("提示：课程与配置都放在你的工作区里，随时可备份为 zip（课程库 → 备份课程）。")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        self.seed_button = QPushButton("先读示例课程")
        self.seed_button.clicked.connect(self._on_seed)
        self.seed_button.setEnabled(has_seed_course)
        row.addWidget(self.seed_button)
        row.addStretch(1)
        self.backup_button = QPushButton("备份课程库")
        self.backup_button.clicked.connect(self._on_backup)
        row.addWidget(self.backup_button)
        self.new_course_button = QPushButton("新建课程")
        self.new_course_button.clicked.connect(self._on_new_course)
        row.addWidget(self.new_course_button)
        self.settings_button = QPushButton("去配置 API")
        self.settings_button.setProperty("role", "primary")
        self.settings_button.clicked.connect(self._on_settings)
        row.addWidget(self.settings_button)
        layout.addLayout(row)

    def _close(self) -> None:
        self.accept()

    def _on_settings(self) -> None:
        self.go_settings.emit()
        self._close()

    def _on_new_course(self) -> None:
        self.go_new_course.emit()
        self._close()

    def _on_seed(self) -> None:
        self.open_seed_course.emit()
        self._close()

    def _on_backup(self) -> None:
        self.go_backup.emit()
        self._close()
