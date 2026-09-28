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


class LessonTreeWidget(QTreeWidget):
    lesson_selected = Signal(Path)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["章节"])
        self.itemClicked.connect(self._on_item_clicked)

    def load_course(self, course_dir: str | Path) -> None:
        """按 ``outline.json`` + ``lesson.json`` 装载章节树。"""
        self.clear()
        for chapter in course_service.lesson_tree(course_dir):
            chapter_item = QTreeWidgetItem([f"{chapter['id']} {chapter['title']}"])
            chapter_item.setFlags(chapter_item.flags() & ~Qt.ItemFlag.ItemIsSelectable)
            self.addTopLevelItem(chapter_item)
            for lesson in chapter["lessons"]:
                mark = STATUS_MARK.get(str(lesson.get("status")), "○")
                item = QTreeWidgetItem([f"{mark} {lesson['id']} {lesson['title']}"])
                item.setData(0, Qt.ItemDataRole.UserRole, lesson.get("path"))
                if lesson.get("status") == "completed":
                    item.setForeground(0, self.palette().text())
                chapter_item.addChild(item)
            chapter_item.setExpanded(True)

    def select_lesson(self, lesson_id: str) -> bool:
        for chapter_index in range(self.topLevelItemCount()):
            chapter = self.topLevelItem(chapter_index)
            for lesson_index in range(chapter.childCount()):
                item = chapter.child(lesson_index)
                if str(lesson_id) in item.text(0):
                    self.setCurrentItem(item)
                    return True
        return False

    def next_lesson(self) -> Path | None:
        return self._sibling(+1)

    def previous_lesson(self) -> Path | None:
        return self._sibling(-1)

    def _sibling(self, step: int) -> Path | None:
        flat: list[QTreeWidgetItem] = []
        for chapter_index in range(self.topLevelItemCount()):
            chapter = self.topLevelItem(chapter_index)
            for lesson_index in range(chapter.childCount()):
                flat.append(chapter.child(lesson_index))
        current = self.currentItem()
        if current not in flat:
            return None
        index = flat.index(current) + step
        if 0 <= index < len(flat):
            target = flat[index]
            self.setCurrentItem(target)
            return target.data(0, Qt.ItemDataRole.UserRole)
        return None

    def _on_item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        path = item.data(0, Qt.ItemDataRole.UserRole)
        if path:
            self.lesson_selected.emit(Path(path))

    def refresh_marks(self, course_dir: str | Path) -> None:
        current = self.currentItem()
        current_id = None
        if current is not None:
            parts = current.text(0).split()
            current_id = parts[1] if len(parts) > 1 else None
        self.load_course(course_dir)
        if current_id:
            self.select_lesson(current_id)
