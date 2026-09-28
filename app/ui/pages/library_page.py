"""课程库页（FR-04 / FR-17：列表、检索、筛选、排序、标签、课程操作）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...services import course_service
from ...services.library_service import SORT_OPTIONS, VIEW_MODES, LibraryFilter, LibraryService


class LibraryPage(QWidget):
    open_course = Signal(object)
    go_settings = Signal()
    new_course_requested = Signal()
    rename_requested = Signal(object)
    delete_requested = Signal(object)
    complete_toggled = Signal(object, bool)
    open_in_explorer = Signal(object)

    def __init__(self, courses_dir: str | Path, *, service: LibraryService | None = None, parent=None) -> None:
        super().__init__(parent)
        self._courses_dir = Path(courses_dir)
        self._service = service
        self._summaries: list[course_service.CourseSummary] = []

        layout = QVBoxLayout(self)

        header = QHBoxLayout()
        title = QLabel("课程库")
        title.setProperty("role", "title")
        header.addWidget(title)
        header.addStretch(1)
        self.new_button = QPushButton("新建课程")
        self.new_button.setProperty("role", "primary")
        self.new_button.clicked.connect(self.new_course_requested.emit)
        header.addWidget(self.new_button)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self._on_refresh)
        header.addWidget(self.refresh_button)
        layout.addLayout(header)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索课程名 / 描述 / 标签 / 章节标题 / 资料名")
        self.search_edit.textChanged.connect(lambda *_: self.apply_filters())
        filters.addWidget(self.search_edit, 1)

        self.view_combo = QComboBox()
        for key, name in VIEW_MODES:
            self.view_combo.addItem(name, key)
        self.view_combo.currentIndexChanged.connect(lambda *_: self.apply_filters())
        filters.addWidget(self.view_combo)

        self.sort_combo = QComboBox()
        for key, name in SORT_OPTIONS:
            self.sort_combo.addItem("排序：" + name, key)
        self.sort_combo.currentIndexChanged.connect(lambda *_: self.apply_filters())
        filters.addWidget(self.sort_combo)
        layout.addLayout(filters)

        tag_row = QHBoxLayout()
        tag_row.addWidget(QLabel("标签"))
        self.tags_list = QListWidget()
        self.tags_list.setFixedHeight(78)
        self.tags_list.itemChanged.connect(lambda *_: self.apply_filters())
        tag_row.addWidget(self.tags_list, 1)

        right = QVBoxLayout()
        self.tag_mode_combo = QComboBox()
        self.tag_mode_combo.addItem("多标签：并集", "any")
        self.tag_mode_combo.addItem("多标签：交集", "all")
        self.tag_mode_combo.currentIndexChanged.connect(lambda *_: self.apply_filters())
        right.addWidget(self.tag_mode_combo)
        tag_input_row = QHBoxLayout()
        self.new_tag_edit = QLineEdit()
        self.new_tag_edit.setPlaceholderText("给选中课程加标签")
        tag_input_row.addWidget(self.new_tag_edit)
        self.add_tag_button = QPushButton("添加")
        self.add_tag_button.clicked.connect(self._on_add_tag)
        tag_input_row.addWidget(self.add_tag_button)
        right.addLayout(tag_input_row)
        tag_row.addLayout(right)
        layout.addLayout(tag_row)

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setProperty("role", "muted")
        layout.addWidget(self.banner)

        self.configure_button = QPushButton("去配置 API")
        self.configure_button.clicked.connect(self.go_settings.emit)
        self.configure_button.hide()
        layout.addWidget(self.configure_button)

        self.empty_label = QLabel(
            "课程库为空。三步开始：① 配置 API → ② 新建课程 → ③ 打开第一门课开始学。"
        )
        self.empty_label.setWordWrap(True)
        self.empty_label.setProperty("role", "muted")
        layout.addWidget(self.empty_label)

        self.course_list = QListWidget()
        self.course_list.itemActivated.connect(self._on_item_activated)
        self.course_list.itemDoubleClicked.connect(self._on_item_activated)
        self.course_list.currentRowChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.course_list, 1)

        actions = QHBoxLayout()
        self.open_button = QPushButton("打开")
        self.open_button.setProperty("role", "primary")
        self.open_button.clicked.connect(self._on_open_clicked)
        actions.addWidget(self.open_button)
        self.complete_button = QPushButton("标记完成")
        self.complete_button.clicked.connect(lambda: self._emit_complete(True))
        actions.addWidget(self.complete_button)
        self.uncomplete_button = QPushButton("取消完成")
        self.uncomplete_button.clicked.connect(lambda: self._emit_complete(False))
        actions.addWidget(self.uncomplete_button)
        self.rename_button = QPushButton("重命名")
        self.rename_button.clicked.connect(lambda: self._emit_selected(self.rename_requested))
        actions.addWidget(self.rename_button)
        self.explorer_button = QPushButton("在资源管理器中打开")
        self.explorer_button.clicked.connect(lambda: self._emit_selected(self.open_in_explorer))
        actions.addWidget(self.explorer_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(lambda: self._emit_selected(self.delete_requested))
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self._update_buttons()

    # --- 数据 -----------------------------------------------------------
    def refresh(self, *, needs_api_setup: bool = False) -> list[course_service.CourseSummary]:
        if self._service is not None:
            self._summaries = self._service.refresh()
        else:
            self._summaries = course_service.scan_courses(self._courses_dir)
        self._reload_tags()
        self.apply_filters()
        self.configure_button.setVisible(needs_api_setup)
        if needs_api_setup:
            self.banner.setText("尚未配置 API Key：课程相关操作会先引导你去设置页（AC-02）。")
        return self._summaries

    def _reload_tags(self) -> None:
        if self._service is None:
            return
        self.tags_list.blockSignals(True)
        checked = {
            self.tags_list.item(row).text()
            for row in range(self.tags_list.count())
            if self.tags_list.item(row).checkState() == Qt.CheckState.Checked
        }
        self.tags_list.clear()
        for tag in self._service.tags():
            item = QListWidgetItem(str(tag["name"]))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if str(tag["name"]) in checked else Qt.CheckState.Unchecked
            )
            self.tags_list.addItem(item)
        self.tags_list.blockSignals(False)

    def current_filter(self) -> LibraryFilter:
        selected_tags = tuple(
            self.tags_list.item(row).text()
            for row in range(self.tags_list.count())
            if self.tags_list.item(row).checkState() == Qt.CheckState.Checked
        )
        return LibraryFilter(
            query=self.search_edit.text(),
            view=str(self.view_combo.currentData() or "all"),
            tags=selected_tags,
            tag_mode=str(self.tag_mode_combo.currentData() or "any"),
            sort=str(self.sort_combo.currentData() or "recent"),
        )

    def apply_filters(self) -> list[course_service.CourseSummary]:
        filter_ = self.current_filter()
        if self._service is not None:
            items = self._service.search(filter_)
        else:
            items = course_service.scan_courses(self._courses_dir)
        self.set_courses(items)
        return items

    def set_courses(self, items: list[course_service.CourseSummary]) -> None:
        self.course_list.clear()
        for summary in items:
            item = QListWidgetItem(self._describe(summary))
            item.setData(Qt.ItemDataRole.UserRole, summary)
            self.course_list.addItem(item)
        self.empty_label.setVisible(not items)
        if not items:
            self.banner.setText("没有符合条件的课程（换个关键词或清空标签筛选）。")
        elif self.banner.text().startswith("没有符合条件"):
            self.banner.setText(f"共 {len(items)} 门课程。双击即可打开。")
        self._update_buttons()

    @staticmethod
    def _describe(summary: course_service.CourseSummary) -> str:
        parts = [summary.name]
        if summary.seed:
            parts.append("[示例]")
        if summary.pinned:
            parts.append("[置顶]")
        parts.append(f"{summary.chapters} 章 · {summary.lessons} 小节")
        parts.append(f"进度 {int(summary.progress_ratio * 100)}%")
        if summary.tags:
            parts.append("#" + " #".join(summary.tags))
        if summary.broken:
            parts.append("需要修复：" + "；".join(summary.issues))
        return "  " + "  ·  ".join(parts)

    def selected_course(self) -> course_service.CourseSummary | None:
        item = self.course_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item is not None else None

    # --- 交互 -----------------------------------------------------------
    def _update_buttons(self) -> None:
        has_selection = self.selected_course() is not None
        for button in (
            self.open_button,
            self.complete_button,
            self.uncomplete_button,
            self.rename_button,
            self.delete_button,
            self.explorer_button,
            self.add_tag_button,
        ):
            button.setEnabled(has_selection)

    def _emit_selected(self, signal) -> None:  # type: ignore[no-untyped-def]
        summary = self.selected_course()
        if summary is not None:
            signal.emit(summary)

    def _emit_complete(self, completed: bool) -> None:
        summary = self.selected_course()
        if summary is not None:
            self.complete_toggled.emit(summary, completed)

    def _on_add_tag(self) -> None:
        summary = self.selected_course()
        name = self.new_tag_edit.text().strip()
        if summary is None or not name or self._service is None:
            return
        self._service.add_tag(summary.id, name)
        self.new_tag_edit.clear()
        self.refresh()

    def _on_refresh(self) -> None:
        self.refresh()

    def _on_open_clicked(self) -> None:
        self._emit_selected(self.open_course)

    def _on_item_activated(self, item: QListWidgetItem) -> None:
        summary = item.data(Qt.ItemDataRole.UserRole)
        if summary is not None:
            self.open_course.emit(summary)

    def course_dir_name(self, raw_name: str) -> str:
        from ...core.paths import safe_component

        return safe_component(raw_name)
