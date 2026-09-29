"""学习台（《项目文档》FR-05 / FR-06 / FR-07 / FR-18；《技术文档》§4.2–§4.4）。

布局：左侧章节树 ｜ 中间渲染内核（含讲义生成/重讲）｜ 右侧追问抽屉。
网络与文件读写在**工作线程**（§1.5 红线 3），随时可取消；渲染交给 app/web 的 renderer.js。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ...agents.illustrator import AnimatorAgent, IllustratorAgent
from ...agents.notes import NoteKeeperAgent
from ...agents.teacher import TeacherAgent
from ...agents.tools import build_registry
from ...core.cancel import CancellationToken
from ...infra.tool_executor import CallBudget, ToolExecutor
from ...services import course_service, lesson_service
from ...services.persona_service import PersonaService
from ...services.progress_service import ProgressService, resolve_assessment
from ..widgets.chat_drawer import ChatDrawer
from ..widgets.lesson_tree import LessonTreeWidget

DEPTH_CHOICES: tuple[tuple[str, str], ...] = (
    ("minimal", "深度：极简"),
    ("standard", "深度：标准"),
    ("deep", "深度：深入"),
)
MASTERY_CHOICES: tuple[tuple[int, str], ...] = ((3, "懂了"), (2, "一般"), (1, "没懂"))


def default_html_path() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "lesson.html"


class LessonRenderer:
    """渲染内核接口（默认实现是 QWebEngineView，可注入假实现做无界面测试）。"""

    def widget(self) -> QWidget: ...
    def load_page(self, html_path: Path) -> None: ...
    def set_theme(self, css_vars: dict) -> None: ...
    def render_blocks(self, blocks: list[dict]) -> None: ...
    def render_markdown(self, text: str) -> None: ...


class WebEngineRenderer:
    def __init__(self, parent: QWidget | None = None) -> None:
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._view = QWebEngineView(parent)
        self._ready = False
        self._queue: list[str] = []
        self.last_load_ok: bool | None = None
        self._view.loadFinished.connect(self._on_load_finished)

    @property
    def ready(self) -> bool:
        return self._ready

    def widget(self) -> QWidget:
        return self._view

    def load_page(self, html_path: Path) -> None:
        self._ready = False
        self._queue.clear()
        self._view.load(QUrl.fromLocalFile(str(html_path)))

    @Slot(bool)
    def _on_load_finished(self, ok: bool) -> None:
        self.last_load_ok = bool(ok)
        self._ready = bool(ok)
        pending, self._queue = self._queue, []
        for script in pending:
            self._view.page().runJavaScript(script)
        if ok:
            self._view.page().runJavaScript("window.ATM.init();")

    def evaluate(self, script: str, callback: Callable[[object], None] | None = None) -> None:
        self._view.page().runJavaScript(script, callback)

    def _run(self, script: str) -> None:
        if self._ready:
            self._view.page().runJavaScript(script)
        else:
            self._queue.append(script)

    def set_theme(self, css_vars: dict) -> None:
        self._run(f"window.ATM.setTheme({json.dumps(css_vars, ensure_ascii=False)});")

    def render_blocks(self, blocks: list[dict]) -> None:
        self._run(f"window.ATM.renderBlocks({json.dumps(list(blocks), ensure_ascii=False)});")

    def render_markdown(self, text: str) -> None:
        self._run(
            f"window.ATM.renderFallbackMarkdown({json.dumps(text, ensure_ascii=False)}, "
            "'缺少结构化块，已按整段 Markdown 渲染。');"
        )


class _Signals(QObject):
    chunk = Signal(str)
    done = Signal(object)
    failed = Signal(str)


class _AsyncGenTask(QRunnable):
    """在工作线程里消费一个 async 生成器，把片段通过信号发回主线程。"""

    def __init__(self, factory: Callable[[], AsyncIterator[str]], signals: _Signals) -> None:
        super().__init__()
        self._factory = factory
        self._signals = signals

    @Slot()
    def run(self) -> None:
        async def consume() -> None:
            async for piece in self._factory():
                self._signals.chunk.emit(piece)

        try:
            asyncio.run(consume())
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(None)


class _AsyncCallTask(QRunnable):
    def __init__(self, factory: Callable[[], Awaitable[Any]], signals: _Signals) -> None:
        super().__init__()
        self._factory = factory
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            result = asyncio.run(self._factory())
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(result)


class StudyPage(QWidget):
    back_requested = Signal()
    lesson_completed = Signal(str)
    status_message = Signal(str)
    course_opened = Signal(object)

    def __init__(
        self,
        *,
        services: Any = None,
        renderer: LessonRenderer | None = None,
        html_path: Path | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._services = services
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._renderer = renderer if renderer is not None else WebEngineRenderer()
        self._signals = _Signals()
        self._signals.chunk.connect(self._on_chunk)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._token = CancellationToken()
        self._lesson_dir: Path | None = None
        self._course_dir: Path | None = None
        self._context: dict[str, Any] = {}
        self._css_vars: dict = {}
        self._mode = ""  # "" / "generate" / "ask"
        self.last_content: lesson_service.LessonContent | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.back_button = QPushButton("← 课程库")
        self.back_button.clicked.connect(self.back_requested.emit)
        bar.addWidget(self.back_button)
        self.title_label = QLabel("尚未打开小节")
        self.title_label.setProperty("role", "subtitle")
        bar.addWidget(self.title_label, 1)

        self.depth_combo = QComboBox()
        for key, label in DEPTH_CHOICES:
            self.depth_combo.addItem(label, key)
        self.depth_combo.currentIndexChanged.connect(self._on_depth_changed)
        bar.addWidget(self.depth_combo)

        self.persona_combo = QComboBox()
        self.persona_combo.currentIndexChanged.connect(self._on_persona_changed)
        bar.addWidget(self.persona_combo)

        self.generate_button = QPushButton("生成讲义")
        self.generate_button.setProperty("role", "primary")
        self.generate_button.clicked.connect(lambda: self.generate_script())
        bar.addWidget(self.generate_button)
        self.reteach_button = QPushButton("换种讲法")
        self.reteach_button.clicked.connect(lambda: self.generate_script(revision_reason="换种讲法"))
        bar.addWidget(self.reteach_button)
        self.latest_button = QPushButton("查最新")
        self.latest_button.clicked.connect(self.refresh_latest)
        bar.addWidget(self.latest_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.clicked.connect(self._on_stop)
        bar.addWidget(self.stop_button)
        layout.addLayout(bar)

        tool_row = QHBoxLayout()
        self.prev_button = QPushButton("上一节")
        self.prev_button.clicked.connect(lambda: self._goto_sibling(-1))
        tool_row.addWidget(self.prev_button)
        self.next_button = QPushButton("下一节")
        self.next_button.clicked.connect(lambda: self._goto_sibling(+1))
        tool_row.addWidget(self.next_button)
        self.complete_button = QPushButton("标记完成")
        self.complete_button.setEnabled(False)
        self.complete_button.clicked.connect(self._on_mark_complete)
        tool_row.addWidget(self.complete_button)
        self.mastery_combo = QComboBox()
        for value, label in MASTERY_CHOICES:
            self.mastery_combo.addItem(f"自评：{label}", value)
        self.mastery_combo.setCurrentIndex(0)
        tool_row.addWidget(self.mastery_combo)
        self.favorite_button = QPushButton("收藏")
        self.favorite_button.setCheckable(True)
        self.favorite_button.clicked.connect(self._on_toggle_favorite)
        tool_row.addWidget(self.favorite_button)
        self.quiz_toggle = QComboBox()
        for value, label in (("inherit", "测验：跟随"), ("on", "测验：开"), ("off", "测验：关")):
            self.quiz_toggle.addItem(label, value)
        self.quiz_toggle.currentIndexChanged.connect(self._on_assessment_changed)
        tool_row.addWidget(self.quiz_toggle)
        self.self_toggle = QComboBox()
        for value, label in (("inherit", "自评：跟随"), ("on", "自评：开"), ("off", "自评：关")):
            self.self_toggle.addItem(label, value)
        self.self_toggle.currentIndexChanged.connect(self._on_assessment_changed)
        tool_row.addWidget(self.self_toggle)
        tool_row.addStretch(1)
        layout.addLayout(tool_row)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        splitter = QSplitter()
        self.lesson_tree = LessonTreeWidget()
        self.lesson_tree.lesson_selected.connect(self.load_lesson)
        splitter.addWidget(self.lesson_tree)
        splitter.addWidget(self._renderer.widget())
        self.chat_drawer = ChatDrawer()
        self.chat_drawer.send_requested.connect(self.ask)
        self.chat_drawer.stop_requested.connect(self._on_stop)
        splitter.addWidget(self.chat_drawer)
        splitter.setSizes([240, 700, 320])
        layout.addWidget(splitter, 1)

        self._reload_personas()
        self._renderer.load_page(html_path or default_html_path())

    # --- 课程 / 小节装载 -------------------------------------------------
    def open_course(self, course_dir: str | Path, lesson_dir: str | Path | None = None) -> None:
        self._course_dir = Path(course_dir)
        from ...services.icon_service import IconService
        from ..icon_render import course_icon_pixmap

        icon = IconService.load(self._course_dir)
        self.lesson_tree.load_course(
            self._course_dir, icon_pixmap=course_icon_pixmap(self._course_dir, icon, size=40, padding=6)
        )
        self.course_opened.emit(self._course_dir)
        if lesson_dir is not None:
            self.load_lesson(Path(lesson_dir))
        else:
            for chapter in course_service.lesson_tree(self._course_dir):
                for lesson in chapter["lessons"]:
                    if lesson.get("path"):
                        self.load_lesson(Path(lesson["path"]))
                        return
            self.status_label.setText("这门课还没有可读的小节。")

    def load_lesson(self, lesson_dir: str | Path) -> None:
        self._lesson_dir = Path(lesson_dir)
        content = lesson_service.load_lesson(self._lesson_dir)
        self.last_content = content
        lesson_service.mark_in_progress(self._lesson_dir)
        self.title_label.setText(f"{content.lesson_id} · {content.title}")
        self.status_label.setText("" if not content.issues else "提示：" + "；".join(content.issues))
        self._loaded_blocks = list(content.blocks)
        if content.has_blocks:
            self._renderer.render_blocks(list(content.blocks))
        elif content.markdown:
            self._renderer.render_markdown(content.markdown)
        self._renderer.set_theme(self._css_vars)
        self.complete_button.setEnabled(True)
        self._load_assessment_state(content)
        self._load_favorite_state(content)
        self._sync_depth(content)
        self._sync_persona(content)
        self.chat_drawer.load_records(lesson_service.load_chat(self._lesson_dir))
        self._load_context()
        self._hint_staleness(content)
        self.status_message.emit(f"已打开 {content.lesson_id}")


    def _hint_staleness(self, content: lesson_service.LessonContent) -> None:
        """未开启联网时，对时效敏感内容主动提示（§4.19「未开启时」）。"""
        from ...services.search_service import SearchService

        service = getattr(self._services, "search", None)
        if service is None or getattr(service, "enabled", False):
            return
        topics = [content.title] + [
            str(point.get("title") or "") for point in (self._context.get("knowledge_points") or [])
        ]
        if any(SearchService.should_trigger(topic) for topic in topics):
            self.status_label.setText(self.status_label.text() + "  ⚠️ 该内容可能已更新，建议点「查最新」开启联网校验。")
    def apply_theme_vars(self, css_vars: dict) -> None:
        self._css_vars = dict(css_vars or {})
        self._renderer.set_theme(self._css_vars)

    @property
    def css_vars(self) -> dict:
        return dict(self._css_vars)

    @property
    def renderer(self) -> LessonRenderer:
        return self._renderer

    # --- 上下文与生成 ---------------------------------------------------
    def _load_context(self) -> None:
        if self._lesson_dir is None:
            self._context = {}
            return
        content = lesson_service.load_lesson(self._lesson_dir)
        course_dir = self._lesson_dir.parents[3]
        course_meta = lesson_service.read_json(course_dir / "course.json", default={}) or {}
        outline = lesson_service.read_json(course_dir / "outline.json", default={}) or {}
        chapter: dict[str, Any] = {}
        lesson_spec: dict[str, Any] = {}
        for item in outline.get("chapters", []):
            for candidate in item.get("lessons", []):
                if candidate.get("id") == content.lesson_id:
                    chapter = item
                    lesson_spec = candidate
        lesson_meta = content.lesson
        course_learning = course_meta.get("learning") or {}
        lesson_assessment = lesson_meta.get("assessment") or {}
        global_config = getattr(getattr(self._services, "config", None), "generation", None)
        toggles = resolve_assessment(
            lesson_quiz=str(lesson_assessment.get("quiz") or "inherit"),
            lesson_self=str(lesson_assessment.get("self_assessment") or "inherit"),
            course_quiz=str(course_learning.get("quiz") or "inherit"),
            course_self=str(course_learning.get("self_assessment") or "inherit"),
            global_quiz=bool(getattr(global_config, "quiz_enabled", True)),
            global_self=bool(getattr(global_config, "self_assessment_enabled", True)),
        )
        snippets: list[dict[str, Any]] = []
        index = getattr(self._services, "index", None)
        if index is not None and lesson_spec.get("knowledge_points"):
            for point in lesson_spec["knowledge_points"][:3]:
                snippets.extend(index.search_chunks(str(point.get("title") or ""), k=2))
        self._context = {
            "lesson": lesson_spec or {"id": content.lesson_id, "title": content.title},
            "chapter": {"id": chapter.get("id", ""), "title": chapter.get("title", ""), "summary": chapter.get("summary", "")},
            "course": course_meta,
            "knowledge_points": lesson_spec.get("knowledge_points", []),
            "material_snippets": snippets,
            "depth": str(lesson_meta.get("depth") or "standard"),
            "quiz_enabled": toggles["quiz"],
            "self_assessment_enabled": toggles["self_assessment"],
        }

    def _build_teacher(self) -> TeacherAgent | None:
        client = getattr(self._services, "client", None)
        if client is None:
            return None
        return TeacherAgent(client)

    def generate_script(self, revision_reason: str = "") -> None:
        teacher = self._build_teacher()
        if teacher is None or self._lesson_dir is None:
            self.status_message.emit("还没有可用的模型配置，请先在设置里配置 API")
            return
        self._load_context()
        persona = self._current_persona()
        depth = str(self.depth_combo.currentData() or "standard")
        lesson_dir = self._lesson_dir
        context = dict(self._context)
        token = self._new_token()
        executor, tools = self._build_tooling()

        def factory() -> Awaitable[Any]:
            return lesson_service.generate_script(
                lesson_dir,
                teacher=teacher,
                context=context,
                persona_body=persona.effective_body if persona else None,
                depth=depth,
                cancel=token,
                executor=executor,
                tools=tools,
                lesson_id=str((context.get("lesson") or {}).get("id") or ""),
                course_id=str((context.get("course") or {}).get("id") or ""),
                revision_reason=revision_reason,
                on_block=lambda block: self.status_message.emit(
                    f"已生成内容块：{block.get('type')}"
                ),
            )

        self._mode = "generate"
        self.generate_button.setEnabled(False)
        self.status_label.setText("正在生成讲义…（在工作线程里，可随时点「停止」）")
        self._pool.start(_AsyncCallTask(factory, self._signals))

    def ask(self, question: str, selection: str = "") -> None:
        teacher = self._build_teacher()
        if teacher is None or self._lesson_dir is None:
            self.status_message.emit("还没有可用的模型配置")
            return
        self._load_context()
        persona = self._current_persona()
        depth = str(self.depth_combo.currentData() or "standard")
        lesson_dir = self._lesson_dir
        context = dict(self._context)
        token = self._new_token()
        executor, tools = self._build_tooling()
        self.chat_drawer.append_assistant_start()

        def factory() -> AsyncIterator[str]:
            return lesson_service.ask_question(
                lesson_dir,
                teacher=teacher,
                question=question,
                context=context,
                selection=selection,
                persona_body=persona.effective_body if persona else None,
                depth=depth,
                cancel=token,
                executor=executor,
                tools=tools,
            )

        self._mode = "ask"
        self._pool.start(_AsyncGenTask(factory, self._signals))

    def _build_tooling(self) -> tuple[ToolExecutor | None, list[dict[str, Any]] | None]:
        """装配工具：图示 / 动画 / 读图 / 笔记 / 联网 + **人设工具白名单交集**（§10.6）。"""
        if self._services is None or self._lesson_dir is None:
            return None, None
        client = getattr(self._services, "client", None)
        persona = self._current_persona()
        vision = None
        illustrator = None
        animator = None
        notes_agent = None
        if client is not None:
            try:
                supports_vision = bool(self._services.config.require_profile().capabilities.vision)
            except Exception:  # noqa: BLE001
                supports_vision = True

            def vision(png: bytes, prompt: str) -> str:
                return asyncio.run(client.review_image(png, prompt=prompt))

            vision = vision if supports_vision else None
            illustrator = IllustratorAgent(client, vision=vision)
            animator = AnimatorAgent(client)
            notes_agent = NoteKeeperAgent(client)

        registry = build_registry(
            outline=(self._context.get("outline") or None),
            course_dir=self._lesson_dir.parents[3],
            lesson_dir=self._lesson_dir,
            index=getattr(self._services, "index", None),
            progress=self._context.get("progress"),
            client=client,
            search_service=getattr(self._services, "search", None),
            illustrator=illustrator,
            animator=animator,
            vision=vision,
            notes_agent=notes_agent,
        )
        allowed = persona.tools if (persona is not None and persona.tools) else registry.names()
        tools = registry.schemas(allowed)
        if not tools:
            tools = registry.schemas()
        return ToolExecutor(registry, budget=CallBudget()), tools

    # --- 时效性校验（FR-19 辅助路径：系统预取） --------------------------
    def refresh_latest(self) -> None:
        """点「查最新」：检索时效性信息，命中则渲染 ref(web) 卡片（§4.19）。"""
        service = getattr(self._services, "search", None)
        if service is None or self._lesson_dir is None:
            self.status_message.emit("联网搜索不可用")
            return
        topic = " ".join(
            [str((self._context.get("lesson") or {}).get("title") or "")]
            + [str(point.get("title") or "") for point in (self._context.get("knowledge_points") or [])]
        ).strip()
        self._mode = "latest"
        self.latest_button.setEnabled(False)
        self.status_label.setText("正在联网校验时效性…")

        def factory():
            return service.search(topic or "课程内容 最新")

        self._pool.start(_AsyncCallTask(factory, self._signals))

    def _render_latest(self, result: Any) -> None:
        if getattr(result, "stopped", False):
            self.status_label.setText(f"未能校验时效性：{getattr(result, 'message', '')}")
            return
        blocks: list[dict[str, Any]] = list(getattr(self, "_loaded_blocks", []))
        for hit in getattr(result, "hits", []):
            blocks.append(
                {
                    "type": "ref",
                    "url": hit.url,
                    "title": hit.title,
                    "domain": hit.domain,
                    "accessed_at": hit.accessed_at,
                    "quote": hit.snippet,
                }
            )
        if not blocks:
            self.status_label.setText("未能校验时效性：没有可用结果")
            return
        self._renderer.render_blocks(blocks)
        self._renderer.set_theme(self._css_vars)
        suffix = "（命中缓存）" if getattr(result, "cached", False) else ""
        self.status_label.setText(f"已补充 {len(getattr(result, 'hits', []))} 条网络来源{suffix}，请自行判断时效性")

    # --- 交互 -----------------------------------------------------------
    def _new_token(self) -> CancellationToken:
        self._token = CancellationToken()
        return self._token

    def _on_stop(self) -> None:
        self._token.cancel()
        self.status_message.emit("已请求停止（已生成内容保留）")

    def _on_chunk(self, piece: str) -> None:
        if self._mode == "ask":
            self.chat_drawer.append_stream(piece)
        else:
            self.status_label.setText(("…" + piece[-120:]).replace("\n", " "))

    def _on_done(self, payload: object) -> None:
        mode, self._mode = self._mode, ""
        self.generate_button.setEnabled(True)
        self.latest_button.setEnabled(True)
        if mode == "latest":
            self._render_latest(payload)
            self.status_message.emit("时效性校验完成")
            return
        if mode == "ask":
            self.chat_drawer.append_assistant_end()
            self.status_message.emit("追问完成（已写入 chat.jsonl）")
            return
        if self.last_content is not None and self._lesson_dir is not None:
            content = lesson_service.load_lesson(self._lesson_dir)
            self.last_content = content
            self._renderer.render_blocks(list(content.blocks)) if content.has_blocks else self._renderer.render_markdown(content.markdown)
        if self._course_dir is not None:
            self.lesson_tree.refresh_marks(self._course_dir)
        outcome = payload
        missing = list(getattr(outcome, "missing_knowledge_points", []) or [])
        total_points = len(self._context.get("knowledge_points") or [])
        ratio = 100 if total_points == 0 else int((total_points - len(missing)) / total_points * 100)
        message = f"讲义已生成，知识点覆盖 {ratio}%"
        if missing:
            message += "（仍缺：" + "、".join(missing) + "）"
        self.status_label.setText(message)
        self.status_message.emit(message)
        self.lesson_completed.emit(str((self._context.get("lesson") or {}).get("id") or ""))

    def _on_failed(self, message: str) -> None:
        self._mode = ""
        self.generate_button.setEnabled(True)
        self.status_label.setText(f"失败：{message}")
        self.status_message.emit(f"失败：{message}")

    def _goto_sibling(self, step: int) -> None:
        target = self.lesson_tree.next_lesson() if step > 0 else self.lesson_tree.previous_lesson()
        if target is not None:
            self.load_lesson(target)

    def _on_mark_complete(self) -> None:
        if self._lesson_dir is None:
            return
        mastery = int(self.mastery_combo.currentData() or 3)
        course_dir = self._lesson_dir.parents[3]
        ProgressService(course_dir).update_lesson(
            self._lesson_dir, status=lesson_service.STATUS_COMPLETED, mastery=mastery, minutes=1
        )
        if self._course_dir is not None:
            self.lesson_tree.refresh_marks(self._course_dir)
        self.lesson_completed.emit(str((self._context.get("lesson") or {}).get("id") or ""))
        self.status_message.emit("已标记完成（进度与复习队列已更新）")

    def _on_toggle_favorite(self) -> None:
        if self._lesson_dir is None:
            return
        value = course_service.toggle_favorite(self._lesson_dir, self.favorite_button.isChecked())
        self.status_message.emit("已收藏本节" if value else "已取消收藏")

    def _load_favorite_state(self, content: lesson_service.LessonContent) -> None:
        self.favorite_button.setChecked(bool(content.lesson.get("favorite")))

    def _load_assessment_state(self, content: lesson_service.LessonContent) -> None:
        assessment = content.lesson.get("assessment") or {}
        for combo, key in ((self.quiz_toggle, "quiz"), (self.self_toggle, "self_assessment")):
            value = str(assessment.get(key) or "inherit")
            index = combo.findData(value)
            combo.blockSignals(True)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    def _sync_persona(self, content: lesson_service.LessonContent) -> None:
        """把下拉同步到小节记录的人设（避免「看起来没切」）。"""
        recorded = str((content.lesson.get("persona") or {}).get("id") or "default-teacher")
        index = self.persona_combo.findData(recorded)
        if index < 0:
            return
        self.persona_combo.blockSignals(True)
        self.persona_combo.setCurrentIndex(index)
        self.persona_combo.blockSignals(False)

    def _sync_depth(self, content: lesson_service.LessonContent) -> None:
        index = self.depth_combo.findData(str(content.lesson.get("depth") or "standard"))
        self.depth_combo.blockSignals(True)
        self.depth_combo.setCurrentIndex(index if index >= 0 else 1)
        self.depth_combo.blockSignals(False)

    def _on_depth_changed(self) -> None:
        if self._lesson_dir is None:
            return
        depth = str(self.depth_combo.currentData() or "standard")
        lesson = lesson_service.read_json(self._lesson_dir / "lesson.json", default={}) or {}
        lesson["depth"] = depth
        from ...infra import file_repository

        file_repository.write_json(self._lesson_dir / "lesson.json", lesson)
        self._context["depth"] = depth
        self.status_message.emit(f"讲解深度已切换为「{depth}」（只影响之后重新生成的讲义）")

    def _on_assessment_changed(self) -> None:
        if self._lesson_dir is None:
            return
        lesson = lesson_service.read_json(self._lesson_dir / "lesson.json", default={}) or {}
        lesson["assessment"] = {
            "quiz": str(self.quiz_toggle.currentData() or "inherit"),
            "self_assessment": str(self.self_toggle.currentData() or "inherit"),
        }
        from ...infra import file_repository

        file_repository.write_json(self._lesson_dir / "lesson.json", lesson)
        self.status_message.emit("本小节的测验 / 自评开关已更新（只影响之后生成的内容）")

    def _reload_personas(self) -> None:
        if self._services is None:
            return
        persona_service = PersonaService(
            user_dir=self._services.layout.personas, index=getattr(self._services, "index", None)
        )
        self.persona_combo.blockSignals(True)
        self.persona_combo.clear()
        for persona in persona_service.list_personas():
            self.persona_combo.addItem(f"{persona.avatar} {persona.name}", persona.id)
        self.persona_combo.blockSignals(False)

    def _current_persona(self):
        if self._services is None:
            return None
        persona_service = PersonaService(
            user_dir=self._services.layout.personas, index=getattr(self._services, "index", None)
        )
        return persona_service.get(str(self.persona_combo.currentData() or "default-teacher"))

    def _on_persona_changed(self) -> None:
        persona = self._current_persona()
        if persona is None:
            return
        if self._lesson_dir is not None:
            lesson = lesson_service.read_json(self._lesson_dir / "lesson.json", default={}) or {}
            lesson["persona"] = {"id": persona.id, "version": persona.version}
            from ...infra import file_repository

            file_repository.write_json(self._lesson_dir / "lesson.json", lesson)
        self.status_message.emit(
            f"已切换人设：{persona.name}（只改表达层；已生成的小节可点「换种讲法」用新风格重讲）"
        )
