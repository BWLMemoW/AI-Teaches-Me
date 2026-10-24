"""课程库页（FR-04 / FR-17：虚拟文件夹、列表、检索、筛选、排序、标签、课程操作）。"""

from __future__ import annotations

import html
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...services import course_service, icon_service
from ...services.folder_service import FolderNode, FolderService
from ...services.library_service import (
    SORT_OPTIONS,
    UNFILED_FOLDER_ID,
    VIEW_MODES,
    LibraryFilter,
    LibraryService,
)
from ..icon_render import course_icon_pixmap
from ..widgets.course_card import CardStyle, CourseCard, load_progress
from ..widgets.folder_tree import ALL_FOLDERS_ID, COURSE_MIME, FolderTree

_USE_CURRENT = object()


class CardHost(QWidget):
    """卡片网格空白区：接收课程拖拽，移动到当前文件夹。"""

    course_dropped = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME):
            text = bytes(event.mimeData().data(COURSE_MIME)).decode("utf-8")
            for course_id in text.splitlines():
                if course_id.strip():
                    self.course_dropped.emit(course_id.strip())
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class CourseListWidget(QListWidget):
    """列表视图：课程行可拖到文件夹树或当前文件夹空白处。"""

    course_dropped = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDragDropMode(QListWidget.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    def mimeData(self, items) -> QMimeData:  # type: ignore[no-untyped-def]
        mime = QMimeData()
        ids: list[str] = []
        for item in items:
            data = item.data(Qt.ItemDataRole.UserRole)
            if isinstance(data, course_service.CourseSummary):
                ids.append(data.id)
        mime.setData(COURSE_MIME, "\n".join(ids).encode("utf-8"))
        return mime

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME):
            text = bytes(event.mimeData().data(COURSE_MIME)).decode("utf-8")
            for course_id in text.splitlines():
                if course_id.strip():
                    self.course_dropped.emit(course_id.strip())
            event.acceptProposedAction()
            return
        super().dropEvent(event)


class LibraryPage(QWidget):
    open_course = Signal(object)
    go_settings = Signal()
    new_course_requested = Signal()
    rename_requested = Signal(object)
    delete_requested = Signal(object)
    complete_toggled = Signal(object, bool)
    open_in_explorer = Signal(object)
    backup_requested = Signal(object)
    icon_requested = Signal(object)
    color_requested = Signal(object)
    restore_requested = Signal()

    def __init__(self, courses_dir: str | Path, *, service: LibraryService | None = None, parent=None) -> None:
        super().__init__(parent)
        self._courses_dir = Path(courses_dir)
        self._service = service
        self._folders = service.folders if service is not None else FolderService(self._courses_dir)
        self._all_summaries: list[course_service.CourseSummary] = []
        self._summaries: list[course_service.CourseSummary] = []
        self._icon_cache: dict[str, object] = {}
        self._progress_cache: dict[str, dict] = {}
        self._selected: object | None = None
        self._folder_totals: dict[str, int] = {}
        self._unfiled_count = 0
        self._needs_api_setup = False
        self._current_folder_id = self._folders.last_folder() or ALL_FOLDERS_ID
        self._history: list[str] = []

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

        split = QSplitter(Qt.Orientation.Horizontal)
        layout.addWidget(split, 1)

        folder_panel = QWidget()
        folder_layout = QVBoxLayout(folder_panel)
        folder_layout.setContentsMargins(0, 0, 8, 0)
        folder_layout.setSpacing(6)
        folder_header = QHBoxLayout()
        folder_title = QLabel("文件夹")
        folder_title.setProperty("role", "muted")
        folder_header.addWidget(folder_title)
        folder_header.addStretch(1)
        self.new_folder_button = QPushButton("＋")
        self.new_folder_button.setToolTip("新建文件夹（Ctrl+Shift+N）")
        self.new_folder_button.setFixedWidth(34)
        self.new_folder_button.clicked.connect(lambda: self._prompt_new_folder(_USE_CURRENT))
        folder_header.addWidget(self.new_folder_button)
        self.rename_folder_button = QPushButton("改名")
        self.rename_folder_button.setToolTip("重命名选中的文件夹（F2）")
        self.rename_folder_button.clicked.connect(self._rename_current_folder)
        folder_header.addWidget(self.rename_folder_button)
        self.delete_folder_button = QPushButton("删除")
        self.delete_folder_button.setProperty("role", "ghost")
        self.delete_folder_button.setToolTip("删除选中的文件夹（不会删除课程）")
        self.delete_folder_button.clicked.connect(self._delete_current_folder)
        folder_header.addWidget(self.delete_folder_button)
        folder_layout.addLayout(folder_header)
        self.folder_tree = FolderTree()
        folder_layout.addWidget(self.folder_tree, 1)
        split.addWidget(folder_panel)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(6)

        crumb_row = QHBoxLayout()
        self.back_button = QPushButton("←")
        self.back_button.setToolTip("返回")
        self.back_button.setFixedWidth(32)
        self.back_button.clicked.connect(self.go_back)
        crumb_row.addWidget(self.back_button)
        self.up_button = QPushButton("↑")
        self.up_button.setToolTip("上一级")
        self.up_button.setFixedWidth(32)
        self.up_button.clicked.connect(self.go_up)
        crumb_row.addWidget(self.up_button)
        self.breadcrumb_label = QLabel("全部课程")
        self.breadcrumb_label.setTextFormat(Qt.TextFormat.RichText)
        self.breadcrumb_label.linkActivated.connect(self._on_breadcrumb)
        self.breadcrumb_label.setWordWrap(True)
        crumb_row.addWidget(self.breadcrumb_label, 1)
        right_layout.addLayout(crumb_row)

        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("在当前文件夹中搜索：课程名 / 描述 / 标签 / 章节 / 资料名")
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

        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItem("网格", "grid")
        self.view_mode_combo.addItem("列表", "list")
        self.view_mode_combo.currentIndexChanged.connect(self._apply_view_mode)
        filters.addWidget(self.view_mode_combo)
        right_layout.addLayout(filters)

        tag_row = QHBoxLayout()
        self.tag_label = QLabel("标签")
        tag_row.addWidget(self.tag_label)
        self.tags_list = QListWidget()
        self.tags_list.setFlow(QListView.Flow.LeftToRight)
        self.tags_list.setWrapping(True)
        self.tags_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.tags_list.setFixedHeight(34)
        self.tags_list.itemChanged.connect(lambda *_: self.apply_filters())
        tag_row.addWidget(self.tags_list, 1)

        tag_tools = QVBoxLayout()
        self.tag_mode_combo = QComboBox()
        self.tag_mode_combo.addItem("多标签：并集", "any")
        self.tag_mode_combo.addItem("多标签：交集", "all")
        self.tag_mode_combo.currentIndexChanged.connect(lambda *_: self.apply_filters())
        tag_tools.addWidget(self.tag_mode_combo)
        tag_input_row = QHBoxLayout()
        self.new_tag_edit = QLineEdit()
        self.new_tag_edit.setPlaceholderText("给选中课程加标签")
        tag_input_row.addWidget(self.new_tag_edit)
        self.add_tag_button = QPushButton("添加")
        self.add_tag_button.clicked.connect(self._on_add_tag)
        tag_input_row.addWidget(self.add_tag_button)
        tag_tools.addLayout(tag_input_row)
        tag_row.addLayout(tag_tools)
        right_layout.addLayout(tag_row)

        self.banner = QLabel("")
        self.banner.setWordWrap(True)
        self.banner.setProperty("role", "muted")
        right_layout.addWidget(self.banner)

        self.configure_button = QPushButton("去配置 API")
        self.configure_button.clicked.connect(self.go_settings.emit)
        self.configure_button.hide()
        right_layout.addWidget(self.configure_button)

        self.empty_label = QLabel("这个文件夹还是空的。可以把课程拖进来，或点「新建课程」。")
        self.empty_label.setWordWrap(True)
        self.empty_label.setProperty("role", "muted")
        right_layout.addWidget(self.empty_label)

        self.course_list = CourseListWidget()
        self.course_list.setIconSize(QSize(72, 72))
        self.course_list.setGridSize(QSize(180, 168))
        self.course_list.setSpacing(8)
        self.course_list.setWordWrap(True)
        self.course_list.itemActivated.connect(self._on_item_activated)
        self.course_list.itemDoubleClicked.connect(self._on_item_activated)
        self.course_list.currentRowChanged.connect(lambda *_: self._update_buttons())
        self.course_list.course_dropped.connect(self._on_course_dropped_to_current)
        right_layout.addWidget(self.course_list, 1)

        self.card_style = CardStyle.from_theme()
        self.card_scroll = QScrollArea()
        self.card_scroll.setWidgetResizable(True)
        self.card_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.card_host = CardHost()
        self.card_host.course_dropped.connect(self._on_course_dropped_to_current)
        self.card_grid = QGridLayout(self.card_host)
        self.card_grid.setContentsMargins(4, 4, 4, 4)
        self.card_grid.setSpacing(12)
        self.card_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.card_scroll.setWidget(self.card_host)
        right_layout.addWidget(self.card_scroll, 1)
        self.card_cards: list[CourseCard] = []
        self._grid_widgets: list[QWidget] = []

        actions = QHBoxLayout()
        self.open_button = QPushButton("打开")
        self.open_button.setProperty("role", "primary")
        self.open_button.clicked.connect(self._on_open_clicked)
        actions.addWidget(self.open_button)
        self.complete_button = QPushButton("标记完成")
        self.complete_button.setProperty("role", "ghost")
        self.complete_button.clicked.connect(lambda: self._emit_complete(True))
        actions.addWidget(self.complete_button)
        self.uncomplete_button = QPushButton("取消完成")
        self.uncomplete_button.setProperty("role", "ghost")
        self.uncomplete_button.clicked.connect(lambda: self._emit_complete(False))
        actions.addWidget(self.uncomplete_button)
        self.rename_button = QPushButton("重命名")
        self.rename_button.clicked.connect(lambda: self._emit_selected(self.rename_requested))
        actions.addWidget(self.rename_button)
        self.explorer_button = QPushButton("在资源管理器中打开")
        self.explorer_button.setProperty("role", "ghost")
        self.explorer_button.clicked.connect(lambda: self._emit_selected(self.open_in_explorer))
        actions.addWidget(self.explorer_button)
        self.delete_button = QPushButton("删除")
        self.delete_button.setProperty("role", "danger")
        self.delete_button.clicked.connect(lambda: self._emit_selected(self.delete_requested))
        actions.addWidget(self.delete_button)
        self.backup_button = QPushButton("备份课程")
        self.backup_button.setProperty("role", "ghost")
        self.backup_button.clicked.connect(lambda: self._emit_selected(self.backup_requested))
        actions.addWidget(self.backup_button)
        self.restore_button = QPushButton("恢复备份")
        self.restore_button.setProperty("role", "ghost")
        self.restore_button.clicked.connect(self.restore_requested.emit)
        actions.addWidget(self.restore_button)
        actions.addStretch(1)
        right_layout.addLayout(actions)
        split.addWidget(right)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([230, 860])

        self.folder_tree.folder_selected.connect(self._on_folder_selected)
        self.folder_tree.new_folder_requested.connect(self._prompt_new_folder)
        self.folder_tree.rename_requested.connect(self._prompt_rename_folder)
        self.folder_tree.delete_requested.connect(self._confirm_delete_folder)
        self.folder_tree.course_dropped.connect(self._on_course_dropped)
        self.folder_tree.folder_dropped.connect(self._on_folder_dropped)
        self.folder_tree.build(
            self._folders,
            all_count=0,
            total_counts={},
            unfiled_count=0,
            current_id=self._current_folder_id,
        )
        self._update_breadcrumb()

        self._shortcut_f2 = QShortcut(QKeySequence("F2"), self)
        self._shortcut_f2.activated.connect(self._rename_focused)
        self._shortcut_delete = QShortcut(QKeySequence("Delete"), self)
        self._shortcut_delete.activated.connect(self._delete_focused)
        self._shortcut_new_folder = QShortcut(QKeySequence("Ctrl+Shift+N"), self)
        self._shortcut_new_folder.activated.connect(
            lambda: self._prompt_new_folder(_USE_CURRENT)
        )
        self._shortcut_enter = QShortcut(QKeySequence("Return"), self)
        self._shortcut_enter.activated.connect(self._open_focused)
        self._apply_view_mode()
        self._update_buttons()

    # --- 数据 -----------------------------------------------------------
    def refresh(self, *, needs_api_setup: bool = False) -> list[course_service.CourseSummary]:
        self._needs_api_setup = needs_api_setup
        self._all_summaries = self._load_all_summaries()
        self._reload_tags()
        valid = {ALL_FOLDERS_ID, UNFILED_FOLDER_ID} | {
            node.id for node in self._folders.folders()
        }
        if self._current_folder_id not in valid:
            self._current_folder_id = ALL_FOLDERS_ID
        self._recompute_folder_totals()
        self.folder_tree.build(
            self._folders,
            all_count=len(self._all_summaries),
            total_counts=self._folder_totals,
            unfiled_count=self._unfiled_count,
            current_id=self._current_folder_id,
        )
        self._update_breadcrumb()
        self._update_folder_buttons()
        self.apply_filters()
        self.configure_button.setVisible(needs_api_setup)
        if needs_api_setup:
            self.banner.setText("尚未配置 API Key：课程相关操作会先引导你去设置页（AC-02）。")
        return self._all_summaries

    def _load_all_summaries(self) -> list[course_service.CourseSummary]:
        if self._service is not None:
            return self._service.refresh()
        summaries = course_service.scan_courses(self._courses_dir)
        self._folders.remove_missing_courses({summary.id for summary in summaries})
        return [
            replace(summary, folder_id=self._folders.folder_of(summary.id) or "")
            for summary in summaries
        ]

    def _reload_tags(self) -> None:
        if self._service is None:
            self.tags_list.setVisible(False)
            self.tag_label.setVisible(False)
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
        has_tags = self.tags_list.count() > 0
        self.tags_list.setVisible(has_tags)
        self.tag_label.setVisible(has_tags)

    def current_filter(self) -> LibraryFilter:
        selected_tags = tuple(
            self.tags_list.item(row).text()
            for row in range(self.tags_list.count())
            if self.tags_list.item(row).checkState() == Qt.CheckState.Checked
        )
        current = self._current_folder_id
        folder_id = None if current == ALL_FOLDERS_ID else current
        return LibraryFilter(
            query=self.search_edit.text(),
            view=str(self.view_combo.currentData() or "all"),
            tags=selected_tags,
            tag_mode=str(self.tag_mode_combo.currentData() or "any"),
            sort=str(self.sort_combo.currentData() or "recent"),
            folder_id=folder_id,
            include_descendants=bool(self.search_edit.text().strip()),
        )

    def apply_filters(self) -> list[course_service.CourseSummary]:
        filter_ = self.current_filter()
        if self._service is not None:
            items = self._service.search(filter_, summaries=self._all_summaries)
        else:
            items = self._filter_fallback(filter_)
        self.set_courses(items)
        return items

    def _filter_fallback(self, filter_: LibraryFilter) -> list[course_service.CourseSummary]:
        items = list(self._all_summaries)
        if filter_.folder_id == UNFILED_FOLDER_ID:
            items = [item for item in items if not item.folder_id]
        elif filter_.folder_id:
            allowed = {filter_.folder_id}
            if filter_.include_descendants:
                allowed |= self._folders.descendant_ids(filter_.folder_id)
            items = [item for item in items if item.folder_id in allowed]
        query = filter_.query.strip().lower()
        if query:
            items = [
                item
                for item in items
                if query in item.name.lower()
                or query in item.description.lower()
                or any(query in tag.lower() for tag in item.tags)
            ]
        if filter_.sort == "name":
            items.sort(key=lambda item: item.name)
        return items

    # --- 文件夹树 / 导航 -------------------------------------------------
    def _recompute_folder_totals(self) -> None:
        self._folder_totals = {node.id: 0 for node in self._folders.folders()}
        self._unfiled_count = 0
        for summary in self._all_summaries:
            folder_id = self._folders.folder_of(summary.id)
            if not folder_id:
                self._unfiled_count += 1
                continue
            for node in self._folders.path_of(folder_id):
                self._folder_totals[node.id] = self._folder_totals.get(node.id, 0) + 1

    def _on_folder_selected(self, folder_id: str) -> None:
        if folder_id != self._current_folder_id:
            self.navigate(folder_id)

    def navigate(self, folder_id: str | None, *, record: bool = True) -> None:
        target = folder_id or ALL_FOLDERS_ID
        valid = {ALL_FOLDERS_ID, UNFILED_FOLDER_ID} | {
            node.id for node in self._folders.folders()
        }
        if target not in valid:
            target = ALL_FOLDERS_ID
        if record and target != self._current_folder_id:
            self._history.append(self._current_folder_id)
            self._history = self._history[-50:]
        self._current_folder_id = target
        self._selected = None
        self._folders.set_last_folder(
            target if target not in (ALL_FOLDERS_ID, UNFILED_FOLDER_ID) else None
        )
        self.folder_tree.blockSignals(True)
        self.folder_tree.select_folder(target)
        self.folder_tree.blockSignals(False)
        self._update_breadcrumb()
        self._update_folder_buttons()
        self.apply_filters()

    def go_back(self) -> None:
        if not self._history:
            return
        target = self._history.pop()
        self.navigate(target, record=False)

    def go_up(self) -> None:
        current = self._current_folder_id
        if current == ALL_FOLDERS_ID:
            return
        if current == UNFILED_FOLDER_ID:
            self.navigate(ALL_FOLDERS_ID)
            return
        node = self._folders.get(current)
        self.navigate(node.parent_id if node and node.parent_id else ALL_FOLDERS_ID)

    def _on_breadcrumb(self, href: str) -> None:
        self.navigate(href)

    def _breadcrumb_link(self, label: str, target: str) -> str:
        return (
            f'<a href="{html.escape(target, quote=True)}" '
            f'style="color:#E86A92; text-decoration:none;">{html.escape(label)}</a>'
        )

    def _update_breadcrumb(self) -> None:
        parts = [self._breadcrumb_link("全部课程", ALL_FOLDERS_ID)]
        if self._current_folder_id == UNFILED_FOLDER_ID:
            parts.append("未分类")
        elif self._current_folder_id != ALL_FOLDERS_ID:
            for node in self._folders.path_of(self._current_folder_id):
                parts.append(self._breadcrumb_link(node.name, node.id))
        self.breadcrumb_label.setText(" / ".join(parts))
        self.back_button.setEnabled(bool(self._history))
        self.up_button.setEnabled(self._current_folder_id != ALL_FOLDERS_ID)

    def _update_folder_buttons(self) -> None:
        is_folder = self.folder_tree.current_folder_node_id() is not None
        self.rename_folder_button.setEnabled(is_folder)
        self.delete_folder_button.setEnabled(is_folder)

    # --- 文件夹操作 -----------------------------------------------------
    def create_folder(self, name: str, parent_id=_USE_CURRENT) -> FolderNode:
        if parent_id is _USE_CURRENT:
            parent = self.folder_tree.current_folder_node_id()
        else:
            parent = str(parent_id) if parent_id else None
        node = self._folders.create(name, parent)
        self.refresh()
        self.navigate(node.id, record=True)
        return node

    def rename_folder(self, folder_id: str, name: str) -> FolderNode:
        node = self._folders.rename(folder_id, name)
        self.refresh()
        self.navigate(folder_id, record=False)
        return node

    def delete_folder(self, folder_id: str) -> None:
        node = self._folders.get(folder_id)
        if node is None:
            return
        parent_id = node.parent_id
        self._folders.delete(folder_id)
        self.refresh()
        self.navigate(parent_id or ALL_FOLDERS_ID, record=False)

    def _prompt_new_folder(self, parent_id=_USE_CURRENT) -> None:
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称：")
        if not ok or not name.strip():
            return
        try:
            self.create_folder(name.strip(), parent_id)
        except Exception as exc:  # noqa: BLE001 - 名称非法或写入失败，界面提示即可
            QMessageBox.warning(self, "新建文件夹失败", str(exc))

    def _prompt_rename_folder(self, folder_id: str) -> None:
        node = self._folders.get(folder_id)
        if node is None:
            return
        name, ok = QInputDialog.getText(
            self, "重命名文件夹", "新名称：", text=node.name
        )
        if not ok or not name.strip():
            return
        try:
            self.rename_folder(folder_id, name.strip())
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "重命名失败", str(exc))

    def _confirm_delete_folder(self, folder_id: str) -> None:
        node = self._folders.get(folder_id)
        if node is None:
            return
        answer = QMessageBox.question(
            self,
            "删除文件夹",
            f"删除文件夹「{node.name}」？\n\n"
            "子文件夹和课程会移到上一级，课程文件不会被删除。",
        )
        if answer == QMessageBox.StandardButton.Yes:
            self.delete_folder(folder_id)

    def _rename_current_folder(self) -> None:
        folder_id = self.folder_tree.current_folder_node_id()
        if folder_id:
            self._prompt_rename_folder(folder_id)

    def _delete_current_folder(self) -> None:
        folder_id = self.folder_tree.current_folder_node_id()
        if folder_id:
            self._confirm_delete_folder(folder_id)

    def move_course_to_folder(self, course_id: str, folder_id: str | None) -> None:
        target = None if folder_id in (None, "", ALL_FOLDERS_ID, UNFILED_FOLDER_ID) else str(folder_id)
        try:
            self._folders.assign_course(course_id, target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "移动失败", str(exc))
            return
        self.refresh()

    def move_folder_to(self, folder_id: str, new_parent_id: str | None) -> bool:
        target = None if new_parent_id in (None, "", ALL_FOLDERS_ID, UNFILED_FOLDER_ID) else str(new_parent_id)
        try:
            self._folders.move(folder_id, target)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "移动文件夹失败", str(exc))
            return False
        self.refresh()
        self.navigate(folder_id, record=False)
        return True

    def target_folder_for_new_course(self) -> str | None:
        return self.folder_tree.current_folder_node_id()

    def assign_new_course(self, course_id: str) -> str | None:
        target = self.target_folder_for_new_course()
        try:
            self._folders.assign_course(course_id, target)
        except Exception:  # noqa: BLE001 - 新课程归属失败不该挡住课程创建
            return target
        self.refresh()
        return target

    def _on_course_dropped(self, course_id: str, folder_id: str | None = None) -> None:
        self.move_course_to_folder(course_id, folder_id)

    def _on_course_dropped_to_current(self, course_id: str) -> None:
        self.move_course_to_folder(course_id, self.folder_tree.current_folder_node_id())

    def _on_folder_dropped(self, folder_id: str, parent_id: str | None) -> None:
        self.move_folder_to(folder_id, parent_id)

    # --- 卡片网格 -------------------------------------------------------
    def set_card_style(self, style: CardStyle) -> None:
        self.card_style = style
        self._rebuild_cards()

    def _icon_pixmap(self, summary):
        cached = self._icon_cache.get(summary.id)
        if cached is not None:
            return cached
        icon = icon_service.IconService.load(summary.path)
        pixmap = course_icon_pixmap(summary.path, icon, size=112, padding=18)
        self._icon_cache[summary.id] = pixmap
        return pixmap

    def _progress_of(self, summary) -> dict:
        cached = self._progress_cache.get(summary.id)
        if cached is None:
            cached = load_progress(summary.path)
            self._progress_cache[summary.id] = cached
        return cached

    def _rebuild_cards(self) -> None:
        while self.card_grid.count():
            item = self.card_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self.card_cards = []
        self._grid_widgets = []
        for summary in self._summaries:
            card = CourseCard(
                summary,
                icon_pixmap=self._icon_pixmap(summary),
                style=self.card_style,
                progress=self._progress_of(summary),
            )
            card.activated.connect(self._on_card_activated)
            card.context_requested.connect(self._on_card_context)
            self.card_cards.append(card)
            self._grid_widgets.append(card)
        self._reflow_cards()
        self._sync_card_selection()

    def _reflow_cards(self) -> None:
        if not self._grid_widgets:
            return
        while self.card_grid.count():
            self.card_grid.takeAt(0)
        width = max(self.card_scroll.viewport().width(), CourseCard.CARD_WIDTH)
        columns = max(1, (width - 8) // (CourseCard.CARD_WIDTH + 12))
        for index, widget in enumerate(self._grid_widgets):
            self.card_grid.addWidget(widget, index // columns, index % columns)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().resizeEvent(event)
        self._reflow_cards()

    def _sync_card_selection(self) -> None:
        for card in self.card_cards:
            card.set_selected(card.summary is self._selected)

    def _on_card_activated(self, summary) -> None:
        self._selected = summary
        self._sync_card_selection()
        self._update_buttons()
        self.open_course.emit(summary)

    def _on_card_context(self, summary, global_pos) -> None:
        self._selected = summary
        self._sync_card_selection()
        self._update_buttons()
        menu = QMenu(self)
        self._menu_action(menu, "打开", lambda: self.open_course.emit(summary))
        move_menu = menu.addMenu("移动到文件夹")
        self._menu_action(
            move_menu,
            "未分类",
            lambda: self.move_course_to_folder(summary.id, None),
        )
        for node in self._folders.folders():
            path = " / ".join(item.name for item in self._folders.path_of(node.id))
            self._menu_action(
                move_menu,
                path,
                lambda checked=False, fid=node.id: self.move_course_to_folder(summary.id, fid),
            )
        menu.addSeparator()
        entries = (
            ("换图标…", lambda: self.icon_requested.emit(summary)),
            ("换颜色…", lambda: self.color_requested.emit(summary)),
            ("标记完成", lambda: self.complete_toggled.emit(summary, True)),
            ("取消完成", lambda: self.complete_toggled.emit(summary, False)),
            ("重命名…", lambda: self.rename_requested.emit(summary)),
            ("在资源管理器中打开", lambda: self.open_in_explorer.emit(summary)),
            ("备份课程…", lambda: self.backup_requested.emit(summary)),
            ("删除…", lambda: self.delete_requested.emit(summary)),
        )
        for label, handler in entries:
            self._menu_action(menu, label, handler)
        menu.exec(global_pos)

    @staticmethod
    def _menu_action(menu: QMenu, label: str, handler):
        action = menu.addAction(label)
        action.triggered.connect(handler)
        return action

    def current_cards(self) -> list:
        return list(self.card_cards)

    # --- 列表 / 视图 ----------------------------------------------------
    def _icon_for(self, summary: course_service.CourseSummary):
        cached = self._icon_cache.get(summary.id)
        if cached is not None:
            return cached
        icon = icon_service.IconService.load(summary.path)
        pixmap = course_icon_pixmap(summary.path, icon, size=96)
        self._icon_cache[summary.id] = pixmap
        return pixmap

    def _apply_view_mode(self) -> None:
        grid = str(self.view_mode_combo.currentData() or "grid") == "grid"
        self.card_scroll.setVisible(grid)
        self.course_list.setVisible(not grid)
        self.set_courses(self._summaries)
        if grid:
            self._reflow_cards()

    def _card_text(self, summary: course_service.CourseSummary) -> str:
        lines = [summary.name]
        badge = "示例 · " if summary.seed else ""
        lines.append(f"{badge}{summary.lessons} 小节 · {int(summary.progress_ratio * 100)}%")
        if summary.tags:
            lines.append("#" + " #".join(summary.tags[:3]))
        return "\n".join(lines)

    def set_courses(self, items: list[course_service.CourseSummary]) -> None:
        self._summaries = list(items)
        if self._selected is not None and not any(
            summary is self._selected for summary in items
        ):
            self._selected = None
        grid = str(self.view_mode_combo.currentData() or "grid") == "grid"
        self.course_list.blockSignals(True)
        self.course_list.clear()
        for summary in items:
            item = QListWidgetItem(self._card_text(summary) if grid else self._describe(summary))
            item.setData(Qt.ItemDataRole.UserRole, summary)
            if grid:
                item.setIcon(self._icon_for(summary))
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                item.setToolTip(self._describe(summary))
            self.course_list.addItem(item)
        self.course_list.blockSignals(False)
        self._rebuild_cards()
        empty = not items
        self.empty_label.setVisible(empty)
        if not self._needs_api_setup:
            if items:
                self.banner.setText(f"共 {len(items)} 门课程。双击课程即可打开。")
            elif self.search_edit.text().strip():
                self.banner.setText("没有符合条件的课程（换个关键词或清空标签筛选）。")
            elif self._current_folder_id == ALL_FOLDERS_ID:
                self.banner.setText("课程库为空。")
            else:
                self.banner.setText("这个文件夹还没有课程，可从左侧进入子文件夹。")
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
        if self._selected is not None:
            return self._selected  # type: ignore[return-value]
        item = self.course_list.currentItem()
        if item is None:
            return None
        data = item.data(Qt.ItemDataRole.UserRole)
        return data if isinstance(data, course_service.CourseSummary) else None

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
            self.backup_button,
            self.add_tag_button,
        ):
            button.setEnabled(has_selection)
        self._update_folder_buttons()

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
        data = item.data(Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and len(data) == 2 and data[0] == "folder":
            self.navigate(str(data[1]))
            return
        if isinstance(data, course_service.CourseSummary):
            self._selected = data
            self.open_course.emit(data)

    def _text_focus(self) -> bool:
        focus = QApplication.focusWidget()
        return isinstance(focus, QLineEdit)

    def _rename_focused(self) -> None:
        if self._text_focus():
            return
        if self.folder_tree.hasFocus() and self.folder_tree.current_folder_node_id():
            self._rename_current_folder()
        else:
            self._emit_selected(self.rename_requested)

    def _delete_focused(self) -> None:
        if self._text_focus():
            return
        if self.folder_tree.hasFocus() and self.folder_tree.current_folder_node_id():
            self._delete_current_folder()
        else:
            self._emit_selected(self.delete_requested)

    def _open_focused(self) -> None:
        if self._text_focus():
            return
        if not self.folder_tree.hasFocus():
            self._emit_selected(self.open_course)

    def course_dir_name(self, raw_name: str) -> str:
        from ...core.paths import safe_component

        return safe_component(raw_name)
