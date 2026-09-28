"""小灶页（《项目文档》§4.16、§11.2；仅限当前课程）。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ...core.cancel import CancellationToken
from ...core.models import RefCard
from ...services import course_service, lesson_service
from ...services.qa_service import CrossCourseRef, QaService


class _Signals(QObject):
    chunk = Signal(str)
    done = Signal(object)
    failed = Signal(str)


class _AskTask(QRunnable):
    def __init__(self, factory, signals: _Signals) -> None:
        super().__init__()
        self._factory = factory
        self._signals = signals

    @Slot()
    def run(self) -> None:
        import asyncio

        async def consume() -> None:
            async for piece in self._factory():
                self._signals.chunk.emit(piece)

        try:
            asyncio.run(consume())
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(None)


class QaPage(QWidget):
    status_message = Signal(str)

    def __init__(self, *, courses_dir: str | Path, agent_factory=None, thread_pool: QThreadPool | None = None, parent=None) -> None:
        super().__init__(parent)
        self._courses_dir = Path(courses_dir)
        self._agent_factory = agent_factory
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._signals = _Signals()
        self._signals.chunk.connect(self._on_chunk)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(lambda message: self._on_failed(message))
        self._token = CancellationToken()
        self._service: QaService | None = None
        self._session_id: str | None = None
        self._lessons: list[Path] = []

        layout = QVBoxLayout(self)
        title = QLabel("小灶 · 课内自由答疑")
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
        left.addWidget(QLabel("会话"))
        self.session_list = QListWidget()
        self.session_list.currentRowChanged.connect(self._on_session_changed)
        left.addWidget(self.session_list, 1)
        self.new_session_button = QPushButton("新建会话")
        self.new_session_button.clicked.connect(self._on_new_session)
        left.addWidget(self.new_session_button)

        left.addWidget(QLabel("引用卡片（仅限本课程）"))
        self.ref_list = QListWidget()
        self.ref_list.setFixedHeight(96)
        left.addWidget(self.ref_list)
        ref_row = QHBoxLayout()
        self.ref_lesson_combo = QComboBox()
        ref_row.addWidget(self.ref_lesson_combo, 1)
        self.ref_excerpt_edit = QLineEdit()
        self.ref_excerpt_edit.setPlaceholderText("引用摘录（选中讲义片段）")
        ref_row.addWidget(self.ref_excerpt_edit, 2)
        self.add_ref_button = QPushButton("添加引用")
        self.add_ref_button.clicked.connect(self._on_add_ref)
        ref_row.addWidget(self.add_ref_button)
        left.addLayout(ref_row)
        body.addLayout(left, 2)

        right = QVBoxLayout()
        self.history = QPlainTextEdit()
        self.history.setReadOnly(True)
        right.addWidget(self.history, 1)
        self.input_edit = QPlainTextEdit()
        self.input_edit.setFixedHeight(72)
        self.input_edit.setPlaceholderText("基于引用提问，例如：这两处讲的矩阵乘法是同一回事吗？")
        right.addWidget(self.input_edit)
        actions = QHBoxLayout()
        self.ask_button = QPushButton("发送")
        self.ask_button.setProperty("role", "primary")
        self.ask_button.clicked.connect(self._on_ask)
        actions.addWidget(self.ask_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(lambda: self._token.cancel())
        actions.addWidget(self.stop_button)
        self.save_course_note_button = QPushButton("存为课程笔记")
        self.save_course_note_button.clicked.connect(lambda: self._save_note("course"))
        actions.addWidget(self.save_course_note_button)
        self.save_lesson_note_button = QPushButton("存进小节笔记")
        self.save_lesson_note_button.clicked.connect(lambda: self._save_note("lesson"))
        actions.addWidget(self.save_lesson_note_button)
        actions.addStretch(1)
        right.addLayout(actions)
        body.addLayout(right, 3)
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
        self._on_course_changed()

    def _on_course_changed(self) -> None:
        data = self.course_combo.currentData()
        self._service = None
        self._session_id = None
        self.ref_list.clear()
        self.history.clear()
        self.session_list.clear()
        self._lessons = []
        self.ref_lesson_combo.clear()
        if not data:
            return
        course_dir = Path(str(data))
        meta = lesson_service.read_json(course_dir / "course.json", default={}) or {}
        course_id = str(meta.get("id") or course_dir.name)
        agent = self._agent_factory() if self._agent_factory else None
        self._service = QaService(session_path=course_dir / "qa" / "sessions.jsonl", course_id=course_id, agent=agent)
        for chapter in course_service.lesson_tree(course_dir):
            for lesson in chapter["lessons"]:
                if lesson.get("path"):
                    self._lessons.append(Path(lesson["path"]))
                    self.ref_lesson_combo.addItem(f"{lesson['id']} {lesson['title']}", lesson["id"])
        self._reload_sessions()

    def _reload_sessions(self) -> None:
        if self._service is None:
            return
        self.session_list.blockSignals(True)
        self.session_list.clear()
        for session in self._service.sessions():
            self.session_list.addItem(QListWidgetItem(f"{session.title}（{session.messages} 条 · {session.refs} 引用）"))
        self.session_list.blockSignals(False)
        if self.session_list.count():
            self.session_list.setCurrentRow(0)

    def _on_new_session(self) -> None:
        if self._service is None:
            return
        title, ok = self._prompt_title()
        if not ok:
            return
        session = self._service.create_session(title)
        self._reload_sessions()
        self.status_message.emit(f"已创建会话：{session.title}")

    @staticmethod
    def _prompt_title() -> tuple[str, bool]:
        from PySide6.QtWidgets import QInputDialog

        return QInputDialog.getText(None, "新建会话", "会话标题：")

    def _on_session_changed(self) -> None:
        if self._service is None:
            return
        sessions = self._service.sessions()
        row = self.session_list.currentRow()
        if 0 <= row < len(sessions):
            self._session_id = sessions[row].id
            self.ref_list.clear()
            for ref in self._service.refs(self._session_id):
                self.ref_list.addItem(QListWidgetItem(str(ref.get("excerpt") or ref.get("id"))[:120]))
            self._reload_history()

    def _reload_history(self) -> None:
        if self._service is None or self._session_id is None:
            return
        self.history.clear()
        for record in self._service.messages(self._session_id):
            role = "我" if record.get("role") == "user" else "老师"
            text = str(record.get("text") or record.get("md") or "")
            self.history.appendPlainText(f"{role}：{text}\n")

    def _on_add_ref(self) -> None:
        if self._service is None or self._session_id is None:
            self.status_message.emit("请先新建一个会话")
            return
        excerpt = self.ref_excerpt_edit.text().strip()
        lesson_id = str(self.ref_lesson_combo.currentData() or "")
        if not excerpt:
            self.status_message.emit("请填写引用摘录")
            return
        try:
            ref_id = self._service.add_ref(
                self._session_id,
                RefCard(type="lesson_text", lesson_id=lesson_id, excerpt=excerpt),
            )
        except CrossCourseRef as exc:
            self.status_message.emit(str(exc))
            return
        self.ref_excerpt_edit.clear()
        self._on_session_changed()
        self.status_message.emit(f"已添加引用 {ref_id}")

    def _on_ask(self) -> None:
        if self._service is None or self._session_id is None or self._service.agent is None:
            self.status_message.emit("请先新建会话并配置模型")
            return
        question = self.input_edit.toPlainText().strip()
        if not question:
            return
        self.input_edit.clear()
        refs = self._service.refs(self._session_id)
        self.history.appendPlainText(f"我：{question}\n老师：")
        self._token = CancellationToken()
        service = self._service
        session_id = self._session_id
        self._pool.start(
            _AskTask(
                lambda: service.ask(
                    session_id,
                    question,
                    refs=refs,
                    outline_summary=self._outline_summary(),
                    cancel=self._token,
                    persona_name=None,
                ),
                self._signals,
            )
        )

    def _outline_summary(self) -> str:
        data = self.course_combo.currentData()
        if not data:
            return ""
        outline = lesson_service.read_json(Path(str(data)) / "outline.json", default={}) or {}
        lines = [f"{chapter.get('id')} {chapter.get('title')}" for chapter in outline.get("chapters", [])]
        return "\n".join(lines)

    def _on_chunk(self, piece: str) -> None:
        self.history.moveCursor(self.history.textCursor().MoveOperation.End)
        self.history.insertPlainText(piece)
        self.history.ensureCursorVisible()

    def _on_done(self, _payload: object) -> None:
        self.history.appendPlainText("")
        self._reload_sessions()
        self.status_message.emit("答疑完成（已写入 qa/sessions.jsonl）")

    def _on_failed(self, message: str) -> None:
        self.status_message.emit(f"答疑失败：{message}")

    def _save_note(self, target: str) -> None:
        if self._service is None or self._session_id is None:
            return
        lesson_dir = self._lessons[0] if (target == "lesson" and self._lessons) else None
        path = self._service.save_to_notes(self._session_id, target=target, lesson_dir=lesson_dir)
        self.status_message.emit(f"已沉淀为笔记：{path.name}")


def build_agent_factory(client) -> object:
    """小灶默认使用答疑顾问 Agent（引用限本课程的约束由 QaService 保证）。"""

    def factory() -> object:
        from ...agents.qa import QaAgent

        return QaAgent(client)

    return factory
