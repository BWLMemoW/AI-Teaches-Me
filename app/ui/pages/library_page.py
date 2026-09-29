"""课程库页（FR-04 / FR-17：列表、检索、筛选、排序、标签、课程操作）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QGridLayout,
    QListView,
    QMenu,
    QScrollArea,
    QWidget,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...services import course_service, icon_service
from ..icon_render import course_icon_pixmap
from ..widgets.course_card import CardStyle, CourseCard, load_progress
from ...services.library_service import SORT_OPTIONS, VIEW_MODES, LibraryFilter, LibraryService


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
        self._summaries: list[course_service.CourseSummary] = []
        self._icon_cache: dict[str, object] = {}
        self._progress_cache: dict[str, dict] = {}
        self._selected: object | None = None

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

        self.view_mode_combo = QComboBox()
        self.view_mode_combo.addItem("网格", "grid")
        self.view_mode_combo.addItem("列表", "list")
        self.view_mode_combo.currentIndexChanged.connect(self._apply_view_mode)
        filters.addWidget(self.view_mode_combo)
        layout.addLayout(filters)

        tag_row = QHBoxLayout()
        self.tag_label = QLabel("标签")
        tag_row.addWidget(self.tag_label)
        self.tags_list = QListWidget()
        # 紧凑的胶囊条：横向流动、自动换行、固定一行高度；没有标签时整行隐藏
        self.tags_list.setFlow(QListView.Flow.LeftToRight)
        self.tags_list.setWrapping(True)
        self.tags_list.setResizeMode(QListView.ResizeMode.Adjust)
        self.tags_list.setFixedHeight(34)
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
        self.course_list.setIconSize(QSize(72, 72))
        self.course_list.setGridSize(QSize(180, 168))
        self.course_list.setSpacing(8)
        self.course_list.setWordWrap(True)
        self.course_list.itemActivated.connect(self._on_item_activated)
        self.course_list.itemDoubleClicked.connect(self._on_item_activated)
        self.course_list.currentRowChanged.connect(lambda *_: self._update_buttons())
        layout.addWidget(self.course_list, 1)

        # 卡片网格（M9）：与列表视图二选一显示
        self.card_style = CardStyle.from_theme()
        self.card_scroll = QScrollArea()
        self.card_scroll.setWidgetResizable(True)
        self.card_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        self.card_host = QWidget()
        self.card_grid = QGridLayout(self.card_host)
        self.card_grid.setContentsMargins(4, 4, 4, 4)
        self.card_grid.setSpacing(12)
        self.card_grid.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.card_scroll.setWidget(self.card_host)
        layout.addWidget(self.card_scroll, 1)
        self.card_cards: list[CourseCard] = []

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
        layout.addLayout(actions)
        self._update_buttons()

    # --- 数据 -----------------------------------------------------------
    def refresh(self, *, needs_api_setup: bool = False) -> list[course_service.CourseSummary]:
        if self._service is not None:
            self._summaries = self._service.refresh()
        else:
            self._summaries = course_service.scan_courses(self._courses_dir)
        self._reload_tags()
        self._apply_view_mode()
        self.configure_button.setVisible(needs_api_setup)
        if needs_api_setup:
            self.banner.setText("尚未配置 API Key：课程相关操作会先引导你去设置页（AC-02）。")
        return self._summaries

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
        return LibraryFilter(
            query=self.search_edit.text(),
            view=str(self.view_combo.currentData() or "all"),
            tags=selected_tags,
            tag_mode=str(self.tag_mode_combo.currentData() or "any"),
            sort=str(self.sort_combo.currentData() or "recent"),
        )

    # --- 卡片网格（M9） --------------------------------------------------
    def set_card_style(self, style: CardStyle) -> None:
        """主题切换时刷新卡片配色。"""
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
                widget.deleteLater()
        self.card_cards = []
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
        self._reflow_cards()
        self._sync_card_selection()

    def _reflow_cards(self) -> None:
        """按可用宽度决定列数（自适应网格）。"""
        if not self.card_cards:
            return
        while self.card_grid.count():
            self.card_grid.takeAt(0)
        width = max(self.card_scroll.viewport().width(), CourseCard.CARD_WIDTH)
        columns = max(1, (width - 8) // (CourseCard.CARD_WIDTH + 12))
        for index, card in enumerate(self.card_cards):
            self.card_grid.addWidget(card, index // columns, index % columns)

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
        entries = (
            ("打开", lambda: self.open_course.emit(summary)),
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
            menu.addAction(label).triggered.connect(handler)
        menu.exec(global_pos)

    def current_cards(self) -> list:
        return list(self.card_cards)
    def apply_filters(self) -> list[course_service.CourseSummary]:
        filter_ = self.current_filter()
        if self._service is not None:
            items = self._service.search(filter_)
        else:
            items = course_service.scan_courses(self._courses_dir)
        self.set_courses(items)
        return items

    def _icon_for(self, summary: course_service.CourseSummary):
        """课程图标位图（带缓存；服务层只给 SVG，渲染在 UI 层）。"""
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
        grid = str(self.view_mode_combo.currentData() or "grid") == "grid"
        self.course_list.clear()
        for summary in items:
            item = QListWidgetItem(self._card_text(summary) if grid else self._describe(summary))
            item.setData(Qt.ItemDataRole.UserRole, summary)
            if grid:
                item.setIcon(self._icon_for(summary))
                item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
                item.setToolTip(self._describe(summary))
            self.course_list.addItem(item)
        self._rebuild_cards()
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
        if self._selected is not None:
            return self._selected  # type: ignore[return-value]
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
            self.backup_button,
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
