"""可编辑大纲树（《项目文档》§11.3 第 4 步：增删改、内联重命名、拖拽排序）。"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QAbstractItemView, QInputDialog, QTreeWidget, QTreeWidgetItem

from ...core.models import CourseOutline

CHAPTER_ROLE = Qt.ItemDataRole.UserRole
LESSON_ROLE = Qt.ItemDataRole.UserRole + 1


class OutlineTreeWidget(QTreeWidget):
    """章 → 小节两级可编辑树；知识点等细节保留在 item 数据里，提交时原样带出。"""

    outline_edited = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderLabels(["教学大纲（双击改名，或选中后点「重命名选中」；Ctrl+↑/↓ 可拖动）"])
        self.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked | QAbstractItemView.EditTrigger.EditKeyPressed)
        self.itemChanged.connect(lambda *_: self.outline_edited.emit())
        self._base: CourseOutline | None = None

    # --- 装载 -----------------------------------------------------------
    def set_outline(self, outline: CourseOutline) -> None:
        self._base = outline
        self.blockSignals(True)
        self.clear()
        for chapter in outline.chapters:
            chapter_item = QTreeWidgetItem([f"{chapter.id} {chapter.title}"])
            chapter_item.setData(0, CHAPTER_ROLE, {"id": chapter.id, "summary": chapter.summary})
            chapter_item.setFlags(chapter_item.flags() | Qt.ItemFlag.ItemIsEditable)
            self.addTopLevelItem(chapter_item)
            for lesson in chapter.lessons:
                lesson_item = QTreeWidgetItem([f"{lesson.id} {lesson.title}（{lesson.estimated_minutes} 分钟）"])
                lesson_item.setData(
                    0,
                    LESSON_ROLE,
                    {
                        "id": lesson.id,
                        "objectives": list(lesson.objectives),
                        "estimated_minutes": lesson.estimated_minutes,
                        "prerequisites": list(lesson.prerequisites),
                        "source_refs": list(lesson.source_refs),
                        "knowledge_points": [point.model_dump() for point in lesson.knowledge_points],
                    },
                )
                lesson_item.setFlags(lesson_item.flags() | Qt.ItemFlag.ItemIsEditable)
                chapter_item.addChild(lesson_item)
            chapter_item.setExpanded(True)
        self.blockSignals(False)

    # --- 编辑 -----------------------------------------------------------
    def add_chapter(self, title: str = "新章节") -> None:
        index = self.topLevelItemCount() + 1
        item = QTreeWidgetItem([f"C{index} {title}"])
        item.setData(0, CHAPTER_ROLE, {"id": f"C{index}", "summary": ""})
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.addTopLevelItem(item)
        self.outline_edited.emit()

    def add_lesson(self, title: str = "新小节") -> None:
        parent = self.currentItem()
        chapter = parent if parent is not None and parent.parent() is None else (parent.parent() if parent else None)
        if chapter is None and self.topLevelItemCount():
            chapter = self.topLevelItem(0)  # 没选中时默认加到第一章，避免操作「没反应」
        if chapter is None:
            return
        chapter_id = self._chapter_id(chapter)
        sequence = chapter.childCount() + 1
        item = QTreeWidgetItem([f"L{chapter_id.lstrip('C')}-{sequence} {title}（10 分钟）"])
        item.setData(
            0,
            LESSON_ROLE,
            {"id": f"L{chapter_id.lstrip('C')}-{sequence}", "objectives": [], "estimated_minutes": 10,
             "prerequisites": [], "source_refs": [], "knowledge_points": []},
        )
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        chapter.addChild(item)
        self.outline_edited.emit()

    def remove_selected(self) -> None:
        item = self.currentItem()
        if item is None:
            return
        if item.parent() is None:
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))
        else:
            item.parent().removeChild(item)
        self.outline_edited.emit()

    def rename_selected(self) -> bool:
        """只改标题，保留编号、时长与知识点。

        双击整行内联编辑时，用户很容易顺手删掉 ``L3-2`` 或 ``（35 分钟）``；
        这个入口用对话框只收集标题，写回时自动拼回编号与时长。
        """
        item = self.currentItem()
        if item is None:
            return False
        if item.parent() is None:
            data: dict[str, Any] = item.data(0, CHAPTER_ROLE) or {}
            identifier = str(data.get("id") or item.text(0).split(" ", 1)[0])
            title, ok = QInputDialog.getText(
                self, "重命名章节", "新章节名：", text=self._strip_prefix(item.text(0))
            )
            if not ok or not title.strip():
                return False
            item.setText(0, f"{identifier} {title.strip()}")
            return True

        data = item.data(0, LESSON_ROLE) or {}
        identifier = str(data.get("id") or "")
        minutes = int(data.get("estimated_minutes") or 10)
        title, ok = QInputDialog.getText(
            self, "重命名小节", "新小节名：", text=self._strip_prefix(item.text(0))
        )
        if not ok or not title.strip():
            return False
        if identifier:
            item.setText(0, f"{identifier} {title.strip()}（{minutes} 分钟）")
        else:
            item.setText(0, f"{title.strip()}（{minutes} 分钟）")
        return True

    # --- 导出 -----------------------------------------------------------
    def to_outline(self, base: CourseOutline | None = None) -> CourseOutline:
        """把树还原成 ``CourseOutline``（保留知识点、目标等细节）。"""
        from ...core.models import KnowledgePoint, OutlineChapter, OutlineLesson

        chapters: list[OutlineChapter] = []
        for chapter_index in range(self.topLevelItemCount()):
            chapter_item = self.topLevelItem(chapter_index)
            data: dict[str, Any] = chapter_item.data(0, CHAPTER_ROLE) or {}
            lessons: list[OutlineLesson] = []
            for lesson_index in range(chapter_item.childCount()):
                lesson_item = chapter_item.child(lesson_index)
                lesson_data: dict[str, Any] = lesson_item.data(0, LESSON_ROLE) or {}
                title = self._strip_prefix(lesson_item.text(0))
                lessons.append(
                    OutlineLesson(
                        id=str(lesson_data.get("id") or f"L{chapter_index + 1}-{lesson_index + 1}"),
                        title=title,
                        objectives=list(lesson_data.get("objectives") or []),
                        estimated_minutes=int(lesson_data.get("estimated_minutes") or 10),
                        prerequisites=list(lesson_data.get("prerequisites") or []),
                        source_refs=list(lesson_data.get("source_refs") or []),
                        knowledge_points=[
                            KnowledgePoint.model_validate(point)
                            for point in lesson_data.get("knowledge_points") or []
                        ],
                    )
                )
            chapters.append(
                OutlineChapter(
                    id=str(data.get("id") or f"C{chapter_index + 1}"),
                    title=self._strip_prefix(chapter_item.text(0)),
                    summary=str(data.get("summary") or ""),
                    lessons=lessons,
                )
            )
        reference = base or self._base or CourseOutline()
        return CourseOutline(
            schema_version=reference.schema_version,
            course_id=reference.course_id,
            learning_goals=list(reference.learning_goals),
            estimated_hours=reference.estimated_hours,
            chapters=chapters,
            assessment_plan=list(reference.assessment_plan),
        )

    # --- 内部 -----------------------------------------------------------
    @staticmethod
    def _strip_prefix(text: str) -> str:
        cleaned = text.strip()
        if " " in cleaned:
            head, tail = cleaned.split(" ", 1)
            if head and head[0] in "CL" and any(ch.isdigit() for ch in head):
                tail = tail.split("（")[0].strip()
                return tail or head
        return cleaned.split("（")[0].strip()

    @staticmethod
    def _chapter_id(item: QTreeWidgetItem) -> str:
        data: dict[str, Any] = item.data(0, CHAPTER_ROLE) or {}
        if data.get("id"):
            return str(data["id"])
        return item.text(0).split(" ", 1)[0]
