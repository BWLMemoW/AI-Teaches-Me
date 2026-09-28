"""笔记中心（《项目文档》FR-10 / §11.2；导出入 MP4 之外的 Markdown / PDF）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...services import course_service, export_service, lesson_service, notes_service


class NotesPage(QWidget):
    status_message = Signal(str)

    def __init__(self, *, courses_dir: str | Path, notes_agent=None, pdf_printer=None, parent=None) -> None:
        super().__init__(parent)
        self._courses_dir = Path(courses_dir)
        self._notes_agent = notes_agent
        self._pdf_printer = pdf_printer
        self._course_dir: Path | None = None
        self._lessons: list[Path] = []

        layout = QVBoxLayout(self)
        title = QLabel("笔记中心")
        title.setProperty("role", "title")
        layout.addWidget(title)

        row = QHBoxLayout()
        self.course_combo = QComboBox()
        self.course_combo.currentIndexChanged.connect(self._on_course_changed)
        row.addWidget(QLabel("课程"))
        row.addWidget(self.course_combo, 1)
        self.refresh_button = QPushButton("刷新")
        self.refresh_button.clicked.connect(self.refresh)
        row.addWidget(self.refresh_button)
        layout.addLayout(row)

        body = QHBoxLayout()
        left = QVBoxLayout()
        self.lesson_list = QListWidget()
        self.lesson_list.currentRowChanged.connect(self._on_lesson_changed)
        left.addWidget(self.lesson_list, 1)

        actions = QVBoxLayout()
        self.generate_button = QPushButton("生成笔记（选中）")
        self.generate_button.setProperty("role", "primary")
        self.generate_button.clicked.connect(lambda: self.generate_for_selected(incomplete=False))
        actions.addWidget(self.generate_button)
        self.generate_incomplete_button = QPushButton("生成笔记（未完成）")
        self.generate_incomplete_button.clicked.connect(lambda: self.generate_for_selected(incomplete=True))
        actions.addWidget(self.generate_incomplete_button)
        self.export_md_button = QPushButton("导出 Markdown")
        self.export_md_button.clicked.connect(self.export_markdown)
        actions.addWidget(self.export_md_button)
        self.export_pdf_button = QPushButton("导出 PDF")
        self.export_pdf_button.clicked.connect(self.export_pdf)
        actions.addWidget(self.export_pdf_button)
        actions.addStretch(1)
        left.addLayout(actions)
        body.addLayout(left, 2)

        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        body.addWidget(self.preview, 3)
        layout.addLayout(body, 1)

        self.refresh()

    # --- 数据 -----------------------------------------------------------
    def refresh(self) -> None:
        courses = course_service.scan_courses(self._courses_dir)
        current = self.course_combo.currentData()
        self.course_combo.blockSignals(True)
        self.course_combo.clear()
        for summary in courses:
            self.course_combo.addItem(summary.name, str(summary.path))
        if current:
            index = self.course_combo.findData(current)
            self.course_combo.setCurrentIndex(index if index >= 0 else 0)
        self.course_combo.blockSignals(False)
        self._load_lessons()

    def _load_lessons(self) -> None:
        data = self.course_combo.currentData()
        self._course_dir = Path(str(data)) if data else None
        self.lesson_list.clear()
        self._lessons = []
        if self._course_dir is None:
            return
        for lesson_dir in lesson_service.iter_lesson_dirs(self._course_dir):
            content = lesson_service.load_lesson(lesson_dir)
            note_file = notes_service.note_path_for(self._course_dir, lesson_dir)
            mark = "📝" if note_file.exists() else "·"
            status = {"completed": "已完成", "in_progress": "进行中"}.get(content.status, "未开始")
            self.lesson_list.addItem(QListWidgetItem(f"{mark} {content.lesson_id} {content.title}（{status}）"))
            self._lessons.append(lesson_dir)

    def _selected_lesson(self) -> Path | None:
        row = self.lesson_list.currentRow()
        if 0 <= row < len(self._lessons):
            return self._lessons[row]
        return None

    # --- 生成 / 导出 ----------------------------------------------------
    def generate_for_selected(self, *, incomplete: bool) -> None:
        lesson_dir = self._selected_lesson()
        if lesson_dir is None or self._course_dir is None:
            self.status_message.emit("请先选择一门课的一个小节")
            return
        content = lesson_service.load_lesson(lesson_dir)
        incomplete = incomplete or content.status != lesson_service.STATUS_COMPLETED
        result = notes_service.generate_notes(
            self._course_dir, lesson_dir, agent=self._notes_agent, incomplete=incomplete
        )
        self._load_lessons()
        self.preview.setPlainText(result.text)
        self.status_message.emit(
            f"笔记已写入 {result.path.name}"
            + ("（本小节尚未学完）" if incomplete else "")
            + ("；" + "；".join(result.issues) if result.issues else "")
        )

    def export_markdown(self, *, scope: str = "all") -> None:
        if self._course_dir is None:
            return
        result = export_service.export_markdown(self._course_dir, scope=scope)
        self.status_message.emit(f"已导出 Markdown：{result.path.name}（{result.size_bytes} 字节）")

    def export_pdf(self) -> None:
        if self._course_dir is None:
            return
        printer = self._pdf_printer
        if printer is None:
            from ..export_helper import print_pdf

            printer = print_pdf
        try:
            result = export_service.export_pdf(self._course_dir, printer=printer, scope="all")
        except Exception as exc:  # noqa: BLE001
            self.status_message.emit(f"导出 PDF 失败：{exc}")
            return
        self.status_message.emit(f"已导出 PDF：{result.path.name}（{result.size_bytes} 字节）")

    # --- 交互 -----------------------------------------------------------
    def _on_course_changed(self) -> None:
        self._load_lessons()

    def _on_lesson_changed(self) -> None:
        if self._course_dir is None:
            return
        lesson_dir = self._selected_lesson()
        if lesson_dir is None:
            return
        note_file = notes_service.note_path_for(self._course_dir, lesson_dir)
        self.preview.setPlainText(
            note_file.read_text(encoding="utf-8") if note_file.exists() else "（这一节还没有笔记，点左侧按钮生成）"
        )
