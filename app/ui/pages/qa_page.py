"""小灶页（《项目文档》§4.16、§11.2；仅限当前课程）。

M33 起与**课内追问共用同一套方案**：``ChatHistoryView`` 气泡（用户在右 / 老师在左）+
``BlockStreamParser`` 增量解析 NDJSON + 同一套工具注册表（``agents/tooling.py``）。
因此小灶里也能调用 ``plot_function`` / ``model_3d``、图示与检索工具，而不再把原始
JSON 直接糊在文本框里。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

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
from ...llm.stream import BlockStreamParser
from ...services import course_service, lesson_service
from ...services.qa_service import CrossCourseRef, QaService
from ..widgets.chat_drawer import ChatHistoryView


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

    def __init__(
        self,
        *,
        courses_dir: str | Path,
        agent_factory=None,
        thread_pool: QThreadPool | None = None,
        tooling_factory: Callable[[Path], tuple[Any, Any] | None] | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._courses_dir = Path(courses_dir)
        self._agent_factory = agent_factory
        self._tooling_factory = tooling_factory
        self._tooling: tuple[Any, Any] | None = None
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._signals = _Signals()
        self._signals.chunk.connect(self._on_chunk)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(lambda message: self._on_failed(message))
        self._token = CancellationToken()
        self._service: QaService | None = None
        self._session_id: str | None = None
        self._lessons: list[Path] = []
        # 流式回答状态（与 ChatDrawer 同构：新块追加、未收完的一行原地刷新）
        self._parser = BlockStreamParser()
        self._current_bubble = None
        self._rendered_blocks = 0

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
        self.history = ChatHistoryView()  # 与课内追问同一套气泡视图（M33）
        self.history.empty_hint.setText("还没有答疑记录：先在左边新建会话、添加引用，然后在下面提问。")
        self.history.diagram_requested.connect(self._open_diagram)
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
        self.history.clear_bubbles()
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
        # 与课内追问共用同一套工具（M33）：课程切换后重新装配，工具被禁用时静默降级为纯对话
        self._tooling = None
        if self._tooling_factory is not None:
            try:
                self._tooling = self._tooling_factory(course_dir)
            except Exception as exc:  # noqa: BLE001 - 工具装配失败不该挡住答疑
                self.status_message.emit(f"工具不可用，已退回纯对话：{exc}")
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
        self.history.clear_bubbles()
        self._parser = BlockStreamParser()
        self._current_bubble = None
        self._rendered_blocks = 0
        refs = {str(item.get("id")): str(item.get("excerpt") or "") for item in self._service.refs(self._session_id)}
        for record in self._service.messages(self._session_id):
            if record.get("role") == "user":
                quote = " ／ ".join(
                    refs.get(str(identifier), "") for identifier in (record.get("refs") or [])
                )
                self.history.add_bubble(
                    "user", str(record.get("text") or ""), quote=quote.strip(" ／")
                )
                continue
            raw = str(record.get("md") or record.get("text") or "")
            blocks = self._parse_blocks(raw)
            if blocks:
                self.history.add_bubble("assistant", blocks=blocks)
            else:
                self.history.add_bubble("assistant", raw)
        self.history.scroll_to_bottom()

    @staticmethod
    def _parse_blocks(raw: str) -> list[dict[str, Any]]:
        """历史记录里存的是 NDJSON 原文；能解析成块就按块渲染，否则按 Markdown。"""
        text = str(raw or "")
        if not text.strip().startswith("{"):
            return []
        parser = BlockStreamParser()
        parser.feed(text)
        parser.finish()
        return [] if parser.protocol_broken else list(parser.blocks)

    def _quote_text(self) -> str:
        """把当前会话的引用卡片拼成气泡顶部的引用条。"""
        if self._service is None or self._session_id is None:
            return ""
        excerpts = [str(item.get("excerpt") or "").strip() for item in self._service.refs(self._session_id)]
        return " ／ ".join(item for item in excerpts if item)

    def _open_diagram(self, payload: dict) -> None:
        """双击气泡里的图 → 放大（与追问抽屉同一入口）。"""
        from ..widgets.diagram_dialog import view_diagram

        view_diagram(
            svg=str(payload.get("svg") or "") or None,
            pixmap=payload.get("pixmap"),
            title=str(payload.get("title") or "图示"),
            parent=self,
        )

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
        self.history.add_bubble("user", question, quote=self._quote_text())
        self._begin_answer()
        self._token = CancellationToken()
        service = self._service
        session_id = self._session_id
        executor, tools = self._tooling if self._tooling else (None, None)
        self._pool.start(
            _AskTask(
                lambda: service.ask(
                    session_id,
                    question,
                    refs=refs,
                    outline_summary=self._outline_summary(),
                    cancel=self._token,
                    persona_name=None,
                    executor=executor,
                    tools=tools,
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

    # --- 流式气泡（与 ChatDrawer 同构） ---------------------------------
    def _begin_answer(self) -> None:
        self._parser = BlockStreamParser()
        self._rendered_blocks = 0
        self._current_bubble = self.history.add_bubble("assistant", "")
        self._current_bubble.set_partial("（正在思考…）")

    def _flush_blocks(self) -> None:
        bubble = self._current_bubble
        if bubble is None:
            return
        blocks = list(self._parser.blocks)
        pending = blocks[self._rendered_blocks :]
        for block in pending:
            bubble.append_block(block)
        if pending:
            self._rendered_blocks = len(blocks)
        bubble.set_partial(self._parser.partial_text() or "")
        self.history.scroll_to_bottom()

    def _on_chunk(self, piece: str) -> None:
        self._parser.feed(piece)
        self._flush_blocks()

    def _on_done(self, _payload: object) -> None:
        bubble = self._current_bubble
        if bubble is not None:
            self._parser.finish()
            self._flush_blocks()
            if self._parser.protocol_broken:
                fallback = self._parser.fallback_markdown()
                if fallback.strip():
                    bubble.set_markdown(fallback)
            bubble.finish_stream()
            if not bubble.markdown.strip():
                bubble.set_markdown("（没有拿到内容，请重试一次。）")
        self._current_bubble = None
        self.history.scroll_to_bottom()
        self._reload_sessions()
        self.status_message.emit("答疑完成（已写入 qa/sessions.jsonl）")

    def _on_failed(self, message: str) -> None:
        bubble = self._current_bubble
        if bubble is not None:
            bubble.finish_stream()
            bubble.set_markdown(f"**答疑失败：** {message}\n\n（可以直接重发这个问题，或检查设置里的模型配置。）")
        self._current_bubble = None
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
