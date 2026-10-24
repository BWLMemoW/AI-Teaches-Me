"""学习台章节树（《项目文档》§11.4 ``LessonTree``、FR-05）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QTreeWidget, QTreeWidgetItem

from ...services import course_service

STATUS_MARK = {
    "not_started": "○",
    "in_progress": "▶",
    "completed": "✓",
    "review_needed": "↻",
}

# 章的展开箭头：直接用字符画在标题前，比默认的小三角更好认（窄栏也看得清）
ARROW_EXPANDED = "▾"
ARROW_COLLAPSED = "▸"
CHAPTER_TITLE_ROLE = int(Qt.ItemDataRole.UserRole) + 1
CHAPTER_ID_ROLE = int(Qt.ItemDataRole.UserRole) + 2


class LessonTreeWidget(QTreeWidget):
    lesson_selected = Signal(Path)
    #: 单击某个章 → 中间区切到「本章资料」页
    chapter_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["章节"])
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setToolTip("点章 = 查看/设置这一章的参考资料；双击章 = 收起 / 展开")
        self.itemClicked.connect(self._on_item_clicked)
        self.itemDoubleClicked.connect(self._on_item_double_clicked)
        self.itemExpanded.connect(self._sync_arrow)
        self.itemCollapsed.connect(self._sync_arrow)

    def load_course(
        self,
        course_dir: str | Path,
        *,
        icon_pixmap=None,
        expanded_state: dict[str, bool] | None = None,
    ) -> None:
        """按 ``outline.json`` + ``lesson.json`` 装载章节树；根节点带课程图标（M9）。"""
        self.clear()
        course_dir = Path(course_dir)
        meta = course_service.load_course(course_dir)[0] or {}
        root_item = QTreeWidgetItem([str(meta.get("name") or course_dir.name)])
        root_item.setFlags(root_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
        root_item.setData(0, Qt.ItemDataRole.UserRole, None)
        if icon_pixmap is not None and not icon_pixmap.isNull():
            root_item.setIcon(0, icon_pixmap)
        self.addTopLevelItem(root_item)

        for chapter in course_service.lesson_tree(course_dir):
            chapter_item = QTreeWidgetItem([str(chapter["title"])])
            chapter_item.setFlags(chapter_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            chapter_item.setData(0, CHAPTER_ID_ROLE, str(chapter["id"]))
            chapter_item.setData(0, CHAPTER_TITLE_ROLE, f"{chapter['id']} {chapter['title']}")
            root_item.addChild(chapter_item)
            for lesson in chapter["lessons"]:
                mark = STATUS_MARK.get(str(lesson.get("status")), "○")
                item = QTreeWidgetItem([f"{mark} {lesson['id']} {lesson['title']}"])
                item.setData(0, Qt.ItemDataRole.UserRole, lesson.get("path"))
                if lesson.get("status") == "completed":
                    item.setForeground(0, self.palette().text())
                chapter_item.addChild(item)
            chapter_item.setText(
                0,
                f"{ARROW_EXPANDED if self._want_expanded(chapter_item, expanded_state) else ARROW_COLLAPSED} "
                f"{chapter_item.data(0, CHAPTER_TITLE_ROLE)}",
            )
            chapter_item.setExpanded(self._want_expanded(chapter_item, expanded_state))
        root_item.setExpanded(True)

    @staticmethod
    def _want_expanded(chapter_item: QTreeWidgetItem, expanded_state: dict[str, bool] | None) -> bool:
        if not expanded_state:
            return True  # 默认全展开（和以前的观感一致）
        chapter_id = str(chapter_item.data(0, CHAPTER_ID_ROLE) or "")
        return bool(expanded_state.get(chapter_id, True))

    # --- 收起 / 展开 -----------------------------------------------------
    def chapter_items(self) -> list[QTreeWidgetItem]:
        items: list[QTreeWidgetItem] = []
        for root_index in range(self.topLevelItemCount()):
            root = self.topLevelItem(root_index)
            for chapter_index in range(root.childCount()):
                items.append(root.child(chapter_index))
        return items

    def expand_all(self) -> int:
        items = self.chapter_items()
        for item in items:
            item.setExpanded(True)
        for root_index in range(self.topLevelItemCount()):
            self.topLevelItem(root_index).setExpanded(True)
        return len(items)

    def collapse_all(self) -> int:
        items = self.chapter_items()
        for item in items:
            item.setExpanded(False)
        return len(items)

    def set_chapter_expanded(self, chapter_id: str, expanded: bool) -> bool:
        wanted = str(chapter_id)
        for item in self.chapter_items():
            if str(item.data(0, CHAPTER_ID_ROLE) or "") == wanted:
                item.setExpanded(bool(expanded))
                return True
        return False

    def chapter_states(self) -> dict[str, bool]:
        """各章当前的展开状态（刷新小节状态时用来复原，不会「一刷新就全展开」）。"""
        return {
            str(item.data(0, CHAPTER_ID_ROLE) or ""): bool(item.isExpanded())
            for item in self.chapter_items()
            if str(item.data(0, CHAPTER_ID_ROLE) or "")
        }

    def summary_text(self) -> str:
        lessons = len(self._lesson_items())
        return f"{len(self.chapter_items())} 章 · {lessons} 节"

    def _sync_arrow(self, item: QTreeWidgetItem) -> None:
        title = item.data(0, CHAPTER_TITLE_ROLE)
        if not title:
            return
        arrow = ARROW_EXPANDED if item.isExpanded() else ARROW_COLLAPSED
        item.setText(0, f"{arrow} {title}")

    def _lesson_items(self) -> list[QTreeWidgetItem]:
        """拍平所有小节课点（三层结构：课程 → 章 → 小节）。"""
        flat: list[QTreeWidgetItem] = []
        for root_index in range(self.topLevelItemCount()):
            root = self.topLevelItem(root_index)
            for chapter_index in range(root.childCount()):
                chapter = root.child(chapter_index)
                for lesson_index in range(chapter.childCount()):
                    flat.append(chapter.child(lesson_index))
        return flat

    def select_lesson(self, lesson_id: str, *, ensure_visible: bool = True) -> bool:
        for item in self._lesson_items():
            if str(lesson_id) in item.text(0):
                self.setCurrentItem(item)
                if ensure_visible:
                    self._reveal(item)
                return True
        return False

    @staticmethod
    def _reveal(item: QTreeWidgetItem) -> None:
        """把某节的父章展开，保证选中的一节真的看得见。"""
        parent = item.parent()
        if parent is not None:
            parent.setExpanded(True)

    def next_lesson(self) -> Path | None:
        return self._sibling(+1)

    def previous_lesson(self) -> Path | None:
        return self._sibling(-1)

    def _sibling(self, step: int) -> Path | None:
        flat = self._lesson_items()
        current = self.currentItem()
        if current not in flat:
            return None
        index = flat.index(current) + step
        if 0 <= index < len(flat):
            target = flat[index]
            self.setCurrentItem(target)
            self._reveal(target)
            return target.data(0, Qt.ItemDataRole.UserRole)
        return None

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        # 章单击 = 打开这一章的「章节资料」页（顺便展开，方便接着点小节）
        chapter_id = item.data(0, CHAPTER_ID_ROLE)
        if item.childCount() and chapter_id is not None:
            item.setExpanded(True)
            self.chapter_selected.emit(str(chapter_id))
            return
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.lesson_selected.emit(Path(path))

    def toggle_chapter(self, item: QTreeWidgetItem) -> None:
        """收起 / 展开某一章（单击留给「看这一章的资料」，所以放到双击）。"""
        if item.data(0, CHAPTER_ID_ROLE) is not None:
            item.setExpanded(not item.isExpanded())

    def _on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        self.toggle_chapter(item)

    def refresh_marks(self, course_dir: str | Path) -> None:
        current = self.currentItem()
        current_id = None
        if current is not None:
            parts = current.text(0).split()
            current_id = parts[1] if len(parts) > 1 else None
        states = self.chapter_states()
        self.load_course(course_dir, expanded_state=states)
        if current_id:
            # 不强行展开：用户刚收起的章不该因为一次状态刷新又被打开
            self.select_lesson(current_id, ensure_visible=False)
