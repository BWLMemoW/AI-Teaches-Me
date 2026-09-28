"""课程库页（FR-04 的 M0 最小版：列出 Courses/ 下的课程并打开）。

M1 会在此基础上加创建向导、标签、搜索与筛选。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.paths import safe_component
from ...services import course_service


class LibraryPage(QWidget):
    """课程卡片列表（M0 用列表展示，M1 起换成卡片网格）。"""

    open_course = Signal(object)
    go_settings = Signal()
    new_course_requested = Signal()
    refresh_requested = Signal()

    def __init__(self, courses_dir: str | Path, parent=None) -> None:
        super().__init__(parent)
        self._courses_dir = Path(courses_dir)

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("课程库")
        title.setProperty("role", "title")
        header.addWidget(title)
        header.addStretch(1)
        self.new_button = QPushButton("新建课程")
        self.new_button.clicked.connect(self.new_course_requested.emit)
        header.addWidget(self.new_button)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self._on_refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setProperty("role", "muted")
        layout.addWidget(self.banner)

        self.configure_button = QPushButton("去配置 API")
        self.configure_button.clicked.connect(self.go_settings.emit)
        self.configure_button.hide()
        layout.addWidget(self.configure_button)

        self.empty_label = QLabel(
            "还没有课程。\n三步开始：① 配置 API → ② 新建课程 → ③ 打开第一门课开始学。"
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setProperty("role", "muted")
        layout.addWidget(self.empty_label)

        self.course_list = QListWidget()
        self.course_list.itemActivated.connect(self._on_item_activated)
        self.course_list.itemDoubleClicked.connect(self._on_item_activated)
        layout.addWidget(self.course_list, 1)

        bottom = QHBoxLayout()
        self.open_button = QPushButton("打开")
        self.open_button.setProperty("role", "primary")
        self.open_button.clicked.connect(self._on_open_clicked)
        bottom.addWidget(self.open_button)
        bottom.addStretch(1)
        layout.addLayout(bottom)

    # --- 数据 -----------------------------------------------------------
    def refresh(self, *, needs_api_setup: bool = False) -> list[course_service.CourseSummary]:
        """重新扫描课程库并刷新界面，返回扫描结果。"""
        courses = course_service.scan_courses(self._courses_dir)
        self.course_list.clear()
        for summary in courses:
            item = QListWidgetItem(self._describe(summary))
            item.setData(0x0100, summary)  # Qt.UserRole
            self.course_list.addItem(item)

        self.empty_label.setVisible(not courses)
        self.configure_button.setVisible(needs_api_setup)
        if needs_api_setup:
            self.banner.setText("尚未配置 API Key：课程相关操作会先引导你去设置页（AC-02）。")
        elif not courses:
            self.banner.setText("课程库为空（示例课程会在首次启动时自动加入）。")
        else:
            self.banner.setText(f"共 {len(courses)} 门课程。双击卡片即可打开。")
        return courses

    @staticmethod
    def _describe(summary: course_service.CourseSummary) -> str:
        parts = [summary.name]
        if summary.seed:
            parts.append("[示例]")
        parts.append(f"{summary.chapters} 章 · {summary.lessons} 小节")
        parts.append(f"进度 {int(summary.progress_ratio * 100)}%")
        if summary.tags:
            parts.append("#" + " #".join(summary.tags))
        if summary.broken:
            parts.append("需要修复：" + "；".join(summary.issues))
        return "  " + "  ·  ".join(parts)

    # --- 交互 -----------------------------------------------------------
    def selected_course(self) -> course_service.CourseSummary | None:
        item = self.course_list.currentItem()
        if item is None:
            return None
        return item.data(0x0100)

    def _on_refresh(self) -> None:
        self.refresh_requested.emit()

    def _on_open_clicked(self) -> None:
        summary = self.selected_course()
        if summary is not None:
            self.open_course.emit(summary)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        summary = item.data(0x0100)
        if summary is not None:
            self.open_course.emit(summary)

    def course_dir_name(self, raw_name: str) -> str:
        """课程名 → 目录名（写路径前必须过 safe_component，§13.6）。"""
        return safe_component(raw_name)
