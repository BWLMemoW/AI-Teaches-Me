"""学习台（《项目文档》FR-05 / FR-06 / FR-07 / FR-18；《技术文档》§4.2–§4.4）。

布局：左侧章节树 ｜ 中间渲染内核（含讲义生成/重讲）｜ 右侧追问抽屉。
网络与文件读写在**工作线程**（§1.5 红线 3），随时可取消；渲染交给 app/web 的 renderer.js。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, QUrl, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...agents.tooling import build_visual_support, tools_allowed
from ...agents.qa import QaAgent
from ...agents.teacher import TeacherAgent
from ...agents.tools import build_registry
from ...core.cancel import CancellationToken
from ...infra.tool_executor import CallBudget, ToolExecutor
from ...services import course_service, lesson_service
from ...services.material_service import (
    describe_reference_refs,
    material_manifest_index,
    parse_source_ref,
    resolve_reference_digest,
    resolve_reference_snippets,
)
from ...services.persona_service import PersonaService
from ...services.progress_service import ProgressService, resolve_assessment
from ..widgets.chat_drawer import ChatDrawer, find_local_image
from ..widgets.lesson_tree import LessonTreeWidget
from ..widgets.annotation_dialog import (
    ACTION_CITE,
    ACTION_DELETE,
    ACTION_SAVE,
    AnnotationDialog,
)

DEPTH_CHOICES: tuple[tuple[str, str], ...] = (
    ("minimal", "深度：极简"),
    ("standard", "深度：标准"),
    ("deep", "深度：深入"),
)
MASTERY_CHOICES: tuple[tuple[int, str], ...] = ((3, "懂了"), (2, "一般"), (1, "没懂"))


def default_html_path() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "lesson.html"


def _decode_json_payload(raw: object) -> object:
    """解码讲义页回传的 JSON 字符串。

    QtWebEngine 的 ``runJavaScript`` **只把基本类型回传 Python**：JS 数组 / 对象会变成空字符串，
    所以渲染内核里凡是「取值」的接口都返回 JSON 字符串，这里统一解回来。
    """
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "ignore")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def decode_renderer_events(raw: object) -> list[dict]:
    """把 ``window.ATM.drainEvents()`` 的返回值解成事件列表（坏数据一律忽略）。"""
    if isinstance(raw, (list, tuple)):
        return [item for item in raw if isinstance(item, dict)]
    data = _decode_json_payload(raw)
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    return []


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
        # 注意：runJavaScript 不接受 None 作为回调（PySide 会直接抛 TypeError）
        self._view.page().runJavaScript(script, callback or (lambda _value: None))

    def _run(self, script: str) -> None:
        if self._ready:
            self._view.page().runJavaScript(script)
        else:
            self._queue.append(script)

    def set_theme(self, css_vars: dict) -> None:
        self._run(f"window.ATM.setTheme({json.dumps(css_vars, ensure_ascii=False)});")

    def render_blocks(self, blocks: list[dict]) -> None:
        self._run(f"window.ATM.renderBlocks({json.dumps(list(blocks), ensure_ascii=False)});")

    def stream_start(self) -> None:
        """开始一段实时预览（打字机，M21）。"""
        self._run("window.ATM.streamStart();")

    def stream_update(self, text: str) -> None:
        """原地更新实时预览文本（不重建 DOM）。"""
        self._run(f"window.ATM.streamUpdate({json.dumps(text, ensure_ascii=False)});")

    def stream_end(self) -> None:
        """结束实时预览（正式块接手）。"""
        self._run("window.ATM.streamEnd();")
    def append_block(self, block: dict) -> None:
        """流式追加：单个内容块到达即渲染（M20：边生成边输出）。"""
        payload = json.dumps(dict(block), ensure_ascii=False)
        self._run(f"window.ATM.appendBlock({payload});")

    def render_markdown(self, text: str) -> None:
        self._run(
            f"window.ATM.renderFallbackMarkdown({json.dumps(text, ensure_ascii=False)}, "
            "'缺少结构化块，已按整段 Markdown 渲染。');"
        )

    def set_persona(self, persona: dict) -> None:
        """讲义顶部的人设徽章（谁在讲）。"""
        self._run(f"window.ATM.setPersona({json.dumps(dict(persona or {}), ensure_ascii=False)});")

    def set_annotations(self, items: list[dict]) -> None:
        """把标注高亮画回讲义正文。"""
        self._run(f"window.ATM.setAnnotations({json.dumps(list(items), ensure_ascii=False)});")

    def drain_events(self, callback: Callable[[object], None]) -> None:
        """取走讲义页里产生的事件（划词标注 / 引用追问）。

        页面还没加载完时回调空列表，调用方（QTimer）不用关心时序。
        """
        if not self._ready:
            callback([])
            return
        self._view.page().runJavaScript(
            "window.ATM.drainEvents();",
            lambda raw: callback(decode_renderer_events(raw)),
        )

    def selected_text(self, callback: Callable[[dict], None]) -> None:
        """读讲义页当前选中的片段（{quote, block_type, block_index}）。

        这是「标注 / 追问」的第二条通路：走 Qt 按钮时不用等事件轮询，立刻就能拿到选区。
        """
        script = (
            "(function(){var s=window.getSelection?window.getSelection():null;"
            "var t=s?String(s.toString()):'';"
            "var out={quote:t.replace(/\\s+/g,' ').trim(),block_type:'',block_index:null};"
            "if(!out.quote){return JSON.stringify(out);}"
            "var n=s.anchorNode;n=n&&n.nodeType===1?n:(n?n.parentElement:null);"
            "var b=n&&n.closest?n.closest('.atm-block'):null;"
            "if(b){out.block_type=b.getAttribute('data-block-type')||'';"
            "var all=document.querySelectorAll('.atm-block');"
            "for(var i=0;i<all.length;i+=1){if(all[i]===b){out.block_index=i;break;}}}"
            "return JSON.stringify(out);})()"
        )
        if not self._ready:
            callback({"quote": "", "block_type": "", "block_index": None})
            return

        def _deliver(raw: object) -> None:
            data = _decode_json_payload(raw)
            payload = {
                "quote": str((data or {}).get("quote") or "") if isinstance(data, dict) else "",
                "block_type": str((data or {}).get("block_type") or "") if isinstance(data, dict) else "",
                "block_index": (data or {}).get("block_index") if isinstance(data, dict) else None,
            }
            callback(payload)

        self._view.page().runJavaScript(script, _deliver)


class _Signals(QObject):
    chunk = Signal(str)
    block = Signal(object)
    partial = Signal(str)
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
        self._signals.block.connect(self._on_block_ready)
        self._signals.partial.connect(self._on_partial)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._token = CancellationToken()
        self._lesson_dir: Path | None = None
        self._course_dir: Path | None = None
        self._context: dict[str, Any] = {}
        self._css_vars: dict = {}
        self._mode = ""  # "" / "generate" / "ask"
        self.last_content: lesson_service.LessonContent | None = None
        self._annotations: list[dict] = []
        self._panel_collapsed = False
        # 讲义页（JS）→ 主线程的事件队列：划词标注、引用追问
        self._event_timer = QTimer(self)
        self._event_timer.setInterval(250)
        self._event_timer.timeout.connect(self._poll_renderer_events)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.back_button = QPushButton("← 课程库")
        self.back_button.setProperty("role", "ghost")
        self.back_button.clicked.connect(self.back_requested.emit)
        bar.addWidget(self.back_button)
        self.title_label = QLabel("尚未打开小节")
        self.title_label.setProperty("role", "subtitle")
        bar.addWidget(self.title_label, 1)

        self.depth_combo = QComboBox()
        self.depth_combo.addItem("深度：跟随设置", "inherit")
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
        self.reteach_button.setProperty("role", "ghost")
        self.reteach_button.clicked.connect(lambda: self.generate_script(revision_reason="换种讲法"))
        bar.addWidget(self.reteach_button)
        self.outline_button = QPushButton("课程大纲")
        self.outline_button.setProperty("role", "ghost")
        self.outline_button.clicked.connect(lambda: self._open_document("outline"))
        bar.addWidget(self.outline_button)
        self.materials_button = QPushButton("课程资料")
        self.materials_button.setProperty("role", "ghost")
        self.materials_button.clicked.connect(lambda: self._open_document("materials"))
        bar.addWidget(self.materials_button)
        self.reference_button = QPushButton("本节参考")
        self.reference_button.setProperty("role", "ghost")
        self.reference_button.setToolTip("指定这一节（或本章默认）要参考的课件 / 教材与页码；生成讲义时优先依据它们")
        self.reference_button.clicked.connect(self._open_reference_dialog)
        bar.addWidget(self.reference_button)
        self.import_button = QPushButton("导入课件")
        self.import_button.setProperty("role", "ghost")
        self.import_button.setToolTip("给这门课补传 PPT / PDF / 教材；导入后可在「本节参考」里指定给某一节")
        self.import_button.clicked.connect(self._import_materials)
        bar.addWidget(self.import_button)
        self.latest_button = QPushButton("查最新")
        self.latest_button.clicked.connect(self.refresh_latest)
        bar.addWidget(self.latest_button)
        self.stop_button = QPushButton("停止")
        self.stop_button.setProperty("role", "ghost")
        self.stop_button.clicked.connect(self._on_stop)
        bar.addWidget(self.stop_button)
        layout.addLayout(bar)

        tool_row = QHBoxLayout()
        self.prev_button = QPushButton("上一节")
        self.prev_button.setProperty("role", "ghost")
        self.prev_button.clicked.connect(lambda: self._goto_sibling(-1))
        tool_row.addWidget(self.prev_button)
        self.next_button = QPushButton("下一节")
        self.next_button.setProperty("role", "ghost")
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
        self.favorite_button.setProperty("role", "ghost")
        self.favorite_button.setCheckable(True)
        self.favorite_button.clicked.connect(self._on_toggle_favorite)
        tool_row.addWidget(self.favorite_button)
        # 讲义标注的两条通路之一：Qt 按钮（另一条是讲义里划词后浮出的工具条）
        self.annotate_selection_button = QPushButton("标注选中")
        self.annotate_selection_button.setProperty("role", "ghost")
        self.annotate_selection_button.setToolTip("先在中间讲义里选中一段文字，再点这里记成本节标注")
        self.annotate_selection_button.clicked.connect(self.annotate_selection)
        tool_row.addWidget(self.annotate_selection_button)
        self.ask_selection_button = QPushButton("追问选中")
        self.ask_selection_button.setProperty("role", "ghost")
        self.ask_selection_button.setToolTip("把讲义里选中的那段原文引用到右侧追问")
        self.ask_selection_button.clicked.connect(self.ask_selection)
        tool_row.addWidget(self.ask_selection_button)
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
        self.apply_settings_button = QPushButton("应用到全课程")
        self.apply_settings_button.setProperty("role", "ghost")
        self.apply_settings_button.setToolTip(
            "把当前老师、深度、测验与自评设置一次应用到本课程所有小节；之后切小节不会再各用一套"
        )
        self.apply_settings_button.clicked.connect(self._apply_settings_to_course)
        tool_row.addWidget(self.apply_settings_button)
        tool_row.addStretch(1)
        layout.addLayout(tool_row)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        splitter = QSplitter()
        self.lesson_tree = LessonTreeWidget()
        self.lesson_tree.lesson_selected.connect(self.load_lesson)
        self.lesson_tree.chapter_selected.connect(self.open_chapter)
        self._splitter = splitter
        self._panel_sizes: list[int] = []
        self.collapsed_bar = self._build_collapsed_bar()
        self.left_panel = self._build_left_panel()
        splitter.addWidget(self.collapsed_bar)
        splitter.addWidget(self.left_panel)
        # 中间区两页：0 = 讲义渲染内核，1 = 「本章资料」页（点左侧的章切过去）
        from ..widgets.chapter_materials_panel import ChapterMaterialsPanel

        self.chapter_panel = ChapterMaterialsPanel()
        self.chapter_panel.save_requested.connect(self._save_chapter_refs)
        self._stack = QStackedWidget()
        self._stack.addWidget(self._renderer.widget())
        self._stack.addWidget(self.chapter_panel)
        splitter.addWidget(self._stack)
        self.chat_drawer = ChatDrawer()
        self._chat_session_id = ""
        self.chat_drawer.send_requested.connect(self.ask)
        self.chat_drawer.stop_requested.connect(self._on_stop)
        self.chat_drawer.zoom_requested.connect(self._open_chat_window)
        self.chat_drawer.diagram_requested.connect(self._open_diagram)
        self.chat_drawer.session_changed.connect(self._on_chat_session_changed)
        self.chat_drawer.new_session_requested.connect(self._on_new_chat_session)
        self.chat_drawer.rename_session_requested.connect(self._on_rename_chat_session)
        self.chat_drawer.delete_session_requested.connect(self._on_delete_chat_session)
        self.chat_drawer.annotation_edit_requested.connect(self._on_annotation_edit)
        self.chat_drawer.annotation_delete_requested.connect(self._on_annotation_delete)
        self.chat_drawer.annotations_clear_requested.connect(self._on_annotations_clear)
        splitter.addWidget(self.chat_drawer)
        splitter.setSizes([0, 260, 640, 440])  # 右栏加宽：追问要渲染 Markdown/代码块
        layout.addWidget(splitter, 1)
        self.collapsed_bar.setVisible(False)
        self._panel_sizes = [0, 260, 640, 440]

        self._reload_personas()
        self._renderer.load_page(html_path or default_html_path())

    def _build_collapsed_bar(self) -> QWidget:
        """面板收起后：最左边只留一个凸起的小标签，写「›」表示可以展开。"""
        bar = QWidget()
        bar.setObjectName("CollapsedChapterBar")
        bar.setFixedWidth(30)
        column = QVBoxLayout(bar)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addStretch(1)
        self.panel_expand_button = QPushButton("›\n展\n开")
        self.panel_expand_button.setObjectName("PanelTabButton")
        self.panel_expand_button.setFixedSize(28, 104)
        self.panel_expand_button.setToolTip("展开章节面板")
        self.panel_expand_button.clicked.connect(self.toggle_chapter_panel)
        column.addWidget(self.panel_expand_button)
        column.addStretch(1)
        return bar

    def _build_left_panel(self) -> QWidget:
        """左侧栏 = 章节卡片 + 卡片右侧凸出的小标签（写「‹」，点它收起整块面板）。

        面板的收起 / 展开只由这两个凸起标签负责，和「章标题折叠」是两件事。
        """
        panel = QWidget()
        row = QHBoxLayout(panel)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        card = QWidget()
        card.setObjectName("ChapterCard")
        column = QVBoxLayout(card)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(6)

        bar = QHBoxLayout()
        tree_title = QLabel("章节")
        tree_title.setProperty("role", "muted")
        bar.addWidget(tree_title)
        self.chapter_count_label = QLabel("")
        self.chapter_count_label.setProperty("role", "muted")
        bar.addWidget(self.chapter_count_label)
        bar.addStretch(1)
        column.addLayout(bar)

        column.addWidget(self.lesson_tree, 1)
        hint = QLabel("点章标题即可折叠 / 展开这一章")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        column.addWidget(hint)

        row.addWidget(card, 1)

        # 卡片右侧凸出的标签：竖着写「‹」，点它就把整块面板收起来
        self.panel_tab_column = QWidget()
        self.panel_tab_column.setObjectName("PanelTabColumn")
        self.panel_tab_column.setFixedWidth(30)
        tab_column = QVBoxLayout(self.panel_tab_column)
        tab_column.setContentsMargins(0, 0, 0, 0)
        tab_column.setSpacing(0)
        tab_column.addStretch(1)
        self.panel_collapse_tab = QPushButton("‹\n收\n起")
        self.panel_collapse_tab.setObjectName("PanelTabButton")
        self.panel_collapse_tab.setFixedSize(28, 104)
        self.panel_collapse_tab.setToolTip("收起章节面板")
        self.panel_collapse_tab.clicked.connect(self.toggle_chapter_panel)
        tab_column.addWidget(self.panel_collapse_tab)
        tab_column.addStretch(1)
        row.addWidget(self.panel_tab_column)
        return panel

    # --- 章节收起 / 展开（FR-05 增强） -----------------------------------
    def _expand_all_chapters(self) -> None:
        self.lesson_tree.expand_all()
        self.status_message.emit("已展开全部章节")

    def _collapse_all_chapters(self) -> None:
        self.lesson_tree.collapse_all()
        self.status_message.emit("已折叠全部章节（点章标题可再展开）")

    # --- 收起 / 展开整个章节面板 -----------------------------------------
    def toggle_chapter_panel(self, *_args: object) -> None:
        self.set_chapter_panel_collapsed(not self._panel_collapsed)

    def set_chapter_panel_collapsed(self, collapsed: bool) -> None:
        """把整个左侧章节面板收起来 / 放回去（不是折叠某一章）。"""
        collapsed = bool(collapsed)
        if collapsed and not self._panel_collapsed:
            self._panel_sizes = list(self._splitter.sizes())
        self._panel_collapsed = collapsed
        self._apply_panel_state()
        self.status_message.emit("已收起章节面板（点 ‹ 或顶部按钮可展开）" if collapsed else "已展开章节面板")

    def _apply_panel_state(self) -> None:
        collapsed = self._panel_collapsed
        self.left_panel.setVisible(not collapsed)
        self.collapsed_bar.setVisible(collapsed)
        saved = list(self._panel_sizes) if len(self._panel_sizes) == self._splitter.count() else [0, 260, 640, 440]
        if collapsed:
            self._splitter.setSizes([30, 0, saved[2], saved[3]])
        else:
            self._splitter.setSizes(saved)

    # --- 标注 / 追问选中片段（Qt 按钮通路） -------------------------------
    def _read_selection(self, handler: Callable[[dict], None]) -> None:
        reader = getattr(self._renderer, "selected_text", None)
        if not callable(reader):
            self.status_message.emit("当前渲染内核不支持读取选中片段")
            return
        reader(handler)

    def annotate_selection(self) -> None:
        """顶部「标注选中」按钮：把讲义里选中的文字记成本节标注。"""

        def handle(info: dict) -> None:
            quote = str(info.get("quote") or "")
            if not quote:
                self.status_message.emit("先在中间讲义里选中一段文字，再点「标注选中」")
                return
            self._add_annotation(
                quote,
                block_index=info.get("block_index"),
                block_type=str(info.get("block_type") or ""),
            )

        self._read_selection(handle)

    def ask_selection(self) -> None:
        """顶部「追问选中」按钮：把讲义里选中的文字引用到右侧追问。"""

        def handle(info: dict) -> None:
            quote = str(info.get("quote") or "")
            if not quote:
                self.status_message.emit("先在中间讲义里选中一段文字，再点「追问选中」")
                return
            self.chat_drawer.cite_quote(quote)
            self.status_message.emit("已把讲义片段引用到追问，接着写下你的问题吧")

        self._read_selection(handle)

    # --- 讲义标注（划词 → 引用追问） -------------------------------------
    def _poll_renderer_events(self) -> None:
        """定时取走讲义页产生的事件（WebEngine 没有现成的反向通道）。"""
        drain = getattr(self._renderer, "drain_events", None)
        if not callable(drain):
            self._event_timer.stop()
            return
        drain(self._on_renderer_events)

    def _on_renderer_events(self, events: object) -> None:
        if not isinstance(events, (list, tuple)):
            return
        for event in events:
            if not isinstance(event, dict):
                continue
            action = str(event.get("action") or "")
            quote = str(event.get("quote") or "")
            if action == "annotate":
                self._add_annotation(
                    quote,
                    block_index=event.get("block_index"),
                    block_type=str(event.get("block_type") or ""),
                )
            elif action == "ask_quote":
                self.chat_drawer.cite_quote(quote)
                self.status_message.emit("已把讲义片段引用到追问，接着写下你的问题吧")
            elif action == "cite_block":
                asset = str(event.get("asset") or "")
                self.chat_drawer.cite_quote(quote, kind="image" if asset else "text", asset=asset)
                self.status_message.emit("已把这张图加进引用区，可以再引用别的内容，也可以直接提问")
            elif action in ("open_annotation", "comment_annotation"):
                self._open_annotation_dialog(str(event.get("annotation_id") or ""))
            elif action == "cite_annotation":
                current = self._find_annotation(str(event.get("annotation_id") or ""))
                if current is None:
                    return
                self.chat_drawer.cite_annotation(current)
                note = str(current.get("note") or "").strip()
                self.status_message.emit(
                    "已把这句话加进引用区" + ("（评论也一起发给老师）" if note else "（还没写评论，鼠标停在划线上可以补）")
                )

    def _find_annotation(self, annotation_id: str) -> dict | None:
        if self._lesson_dir is None or not annotation_id:
            return None
        return next(
            (item for item in lesson_service.load_annotations(self._lesson_dir) if item["id"] == annotation_id),
            None,
        )

    def _add_annotation(
        self,
        quote: str,
        *,
        note: str = "",
        block_index: object = None,
        block_type: str = "",
    ) -> dict | None:
        if self._lesson_dir is None:
            self.status_message.emit("请先在课程库里打开一门课")
            return None
        try:
            item = lesson_service.add_annotation(
                self._lesson_dir,
                quote=quote,
                note=note,
                block_index=block_index if isinstance(block_index, int) else None,
                block_type=block_type,
            )
        except ValueError:
            self.status_message.emit("选中的内容是空的，没有记下标注")
            return None
        self._refresh_annotations()
        self.status_message.emit(
            "已标注：讲义里出现黄色划线；鼠标停在划线上可以写评论，或引用到右侧追问"
        )
        return item

    def _open_annotation_dialog(self, annotation_id: str) -> None:
        """打开标注详情：写评论 / 引用到追问 / 删除（讲义点划线、右侧「改备注」都走这里）。"""
        if self._lesson_dir is None or not annotation_id:
            return
        items = lesson_service.load_annotations(self._lesson_dir)
        index = next((i for i, item in enumerate(items) if item["id"] == annotation_id), -1)
        if index < 0:
            return
        current = items[index]
        dialog = AnnotationDialog(
            quote=str(current.get("quote") or ""),
            note=str(current.get("note") or ""),
            index=index + 1,
            total=len(items),
            parent=self,
        )
        dialog.exec()
        if dialog.action in (ACTION_SAVE, ACTION_CITE):
            lesson_service.update_annotation(self._lesson_dir, annotation_id, note=dialog.current_note())
            self._refresh_annotations()
        if dialog.action == ACTION_CITE:
            self.chat_drawer.cite_quote(str(current.get("quote") or ""), note=dialog.current_note())
            self.status_message.emit("已引用到追问（原文 + 评论），写下你的问题吧")
        elif dialog.action == ACTION_SAVE:
            self.status_message.emit("评论已保存")
        elif dialog.action == ACTION_DELETE:
            lesson_service.delete_annotation(self._lesson_dir, annotation_id)
            self._refresh_annotations()
            self.status_message.emit("已删除标注")

    def _refresh_annotations(self) -> None:
        """刷新标注列表 + 讲义里的高亮。"""
        items = lesson_service.load_annotations(self._lesson_dir) if self._lesson_dir else []
        self._annotations = items
        self.chat_drawer.set_annotations(items)
        setter = getattr(self._renderer, "set_annotations", None)
        if callable(setter):
            setter(items)

    @Slot(str)
    def _on_annotation_edit(self, annotation_id: str) -> None:
        self._open_annotation_dialog(annotation_id)

    @Slot(str)
    def _on_annotation_delete(self, annotation_id: str) -> None:
        if self._lesson_dir is None or not annotation_id:
            return
        answer = QMessageBox.question(self, "删除标注", "确定删除这条标注吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        if lesson_service.delete_annotation(self._lesson_dir, annotation_id):
            self._refresh_annotations()
            self.status_message.emit("已删除标注")

    @Slot()
    def _on_annotations_clear(self) -> None:
        if self._lesson_dir is None or not self._annotations:
            return
        answer = QMessageBox.question(self, "清空标注", "确定清空本节的全部标注吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        removed = lesson_service.clear_annotations(self._lesson_dir)
        self._refresh_annotations()
        self.status_message.emit(f"已清空 {removed} 条标注")

    # --- 人设可见性（讲义徽章 + 追问表头） --------------------------------
    def _persona_payload(self, persona: object) -> dict:
        if persona is None:
            return {}
        return {
            "id": str(getattr(persona, "id", "") or ""),
            "name": str(getattr(persona, "name", "") or ""),
            "avatar": str(getattr(persona, "avatar", "") or ""),
            "tone": list(getattr(persona, "tone", ()) or ()),
        }

    @staticmethod
    def _persona_label(persona: object) -> str | None:
        """给模型的「身份名」：只给正文时模型不知道自己是谁，人设容易退化。"""
        if persona is None:
            return None
        name = str(getattr(persona, "name", "") or "").strip()
        if not name:
            return None
        avatar = str(getattr(persona, "avatar", "") or "").strip()
        return f"{avatar} {name}".strip()

    def _apply_persona_ui(self) -> None:
        """把当前人设推给讲义徽章与追问抽屉（人设必须看得见）。"""
        payload = self._persona_payload(self._current_persona())
        if not payload:
            return
        setter = getattr(self._renderer, "set_persona", None)
        if callable(setter):
            setter(payload)
        self.chat_drawer.set_persona(payload)

    # --- 课程 / 小节装载 -------------------------------------------------
    def open_course(self, course_dir: str | Path, lesson_dir: str | Path | None = None) -> None:
        self._course_dir = Path(course_dir)
        lesson_service.migrate_legacy_lesson_settings(self._course_dir)
        from ...services.icon_service import IconService
        from ..icon_render import course_icon_pixmap

        icon = IconService.load(self._course_dir)
        self.lesson_tree.load_course(
            self._course_dir, icon_pixmap=course_icon_pixmap(self._course_dir, icon, size=40, padding=6)
        )
        self.chapter_count_label.setText(self.lesson_tree.summary_text())
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
        self._stack.setCurrentIndex(0)  # 从「本章资料」页切回讲义
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
        else:
            # 空小节（还没生成讲义）：给一个明确的下一步，而不是一片空白
            self._renderer.render_blocks(
                [
                    {"type": "callout", "style": "tip",
                     "md": f"**这一节还没有讲义。** 点上方「生成讲义」开始（需要已在设置里配置 API）；"
                           f"生成过程会逐块落盘，可随时停止。"},
                    {"type": "next_preview", "md": "生成后这里会出现正文、图示、小结与下节预告。"},
                ]
            )
            self.status_label.setText("本节尚未生成讲义：点上方「生成讲义」开始。")
        self._renderer.set_theme(self._css_vars)
        self.complete_button.setEnabled(True)
        self._load_assessment_state(content)
        self._load_favorite_state(content)
        self._sync_depth(content)
        self._sync_persona(content)
        self._apply_persona_ui()
        self._refresh_annotations()
        self._refresh_chat_sessions()
        if callable(getattr(self._renderer, "drain_events", None)):
            self._event_timer.start()  # 划词标注 / 引用追问的事件轮询
        focus = getattr(self.chat_drawer, "focus_input", None)
        if callable(focus):
            focus()
        self.chat_drawer.input_edit.setEnabled(True)
        self.chat_drawer.input_edit.setReadOnly(False)
        self._load_context()
        self._refresh_reference_button()
        self._hint_reference_coverage()
        self._hint_staleness(content)
        self.status_message.emit(f"已打开 {content.lesson_id}")

    def _hint_reference_coverage(self) -> None:
        """参考范围过大时明说「已经抽样」，避免主人以为整本书都进了提示词。"""
        if not self._context.get("reference_digest_truncated"):
            return
        total = int(self._context.get("reference_digest_total") or 0)
        shown = len(self._context.get("reference_digest") or [])
        if total <= 0 or shown <= 0:
            return
        self.status_label.setText(
            self.status_label.text()
            + f"  ℹ️ 本节参考范围较大（{total} 页要点），已等距抽样 {shown} 条进提示词；"
              "想讲得更细可以点「本节参考」把页码范围填小。"
        )


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
        sequence: list[dict[str, Any]] = []
        for chapter_index, item in enumerate(outline.get("chapters", []) or [], start=1):
            for lesson_index, candidate in enumerate(item.get("lessons", []) or [], start=1):
                sequence.append(
                    {
                        **dict(candidate),
                        "chapter_id": str(item.get("id") or ""),
                        "chapter_title": str(item.get("title") or ""),
                        "chapter_index": chapter_index,
                        "lesson_index": lesson_index,
                    }
                )
        current_index = next(
            (index for index, item in enumerate(sequence) if item.get("id") == content.lesson_id),
            -1,
        )
        chapter_total = len(outline.get("chapters", []) or [])
        current_entry = sequence[current_index] if current_index >= 0 else {}
        covered_entries = sequence[:current_index] if current_index > 0 else []
        future_entries = sequence[current_index + 1 :] if current_index >= 0 else []
        covered_lessons = [self._lesson_brief(item) for item in covered_entries[-16:]]
        future_lessons = [self._lesson_brief(item) for item in future_entries[:12]]
        previous_summary = ""
        if covered_entries:
            previous_summary = self._previous_lesson_summary(course_dir, covered_entries[-1])
        next_entry = future_entries[0] if future_entries else {}
        course_position = (
            {
                "chapter_index": int(current_entry.get("chapter_index") or 0),
                "chapter_total": chapter_total,
                "lesson_index": int(current_entry.get("lesson_index") or 0),
                "overall_index": current_index + 1,
                "overall_total": len(sequence),
            }
            if current_entry
            else {}
        )
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
        course_id = str((course_meta or {}).get("id") or "")
        # 指定参考优先：小节自己指定的资料（否则继承本章）是硬依据，不再关键词乱搜
        lesson_refs = [str(item).strip() for item in (lesson_spec.get("source_refs") or []) if str(item).strip()]
        chapter_refs = [str(item).strip() for item in (chapter.get("source_refs") or []) if str(item).strip()]
        refs = lesson_refs or chapter_refs
        bound: list[dict[str, Any]] = []
        digest: list[dict[str, Any]] = []
        digest_info: dict[str, Any] = {}
        if refs:
            # 全貌用「逐页要点」（覆盖整份范围、篇幅可控），原文只摘一小截备用；
            # 真要看某一页原话时，由教师用 read_material_slice 工具按需取（A + B）。
            digest_info = resolve_reference_digest(course_dir, refs)
            digest = list(digest_info.get("items") or [])
            bound = resolve_reference_snippets(course_dir, refs, budget_chars=3000, per_material_chars=1200)
        if not refs and index is not None and lesson_spec.get("knowledge_points"):
            for point in lesson_spec["knowledge_points"][:3]:
                # 只在本课程的资料里检索，避免引用到别的课的资料（跨课程串味）
                snippets.extend(
                    index.search_chunks(
                        str(point.get("title") or ""), k=2, course_id=course_id or None
                    )
                )
        self._context = {
            "outline": outline,
            "lesson": lesson_spec or {"id": content.lesson_id, "title": content.title},
            "chapter": {"id": chapter.get("id", ""), "title": chapter.get("title", ""), "summary": chapter.get("summary", "")},
            "course": course_meta,
            "course_position": course_position,
            "covered_lessons": covered_lessons,
            "covered_lesson_count": len(covered_entries),
            "future_lessons": future_lessons,
            "future_lesson_count": len(future_entries),
            "previous_summary": previous_summary,
            "next_lesson": (
                f"{next_entry.get('id', '')} {next_entry.get('title', '')}".strip()
                if next_entry
                else ""
            ),
            "knowledge_points": lesson_spec.get("knowledge_points", []),
            "material_snippets": bound or snippets,
            "bound_references": bound,
            "reference_digest": digest,
            "reference_digest_total": int(digest_info.get("total_items") or 0),
            "reference_digest_truncated": bool(digest_info.get("truncated")),
            "reference_refs": refs,
            "reference_summary": describe_reference_refs(course_dir, refs) if refs else "",
            "reference_scope": "lesson" if lesson_refs else ("chapter" if chapter_refs else ""),
            "depth": self._effective_depth(),
            "quiz_enabled": toggles["quiz"],
            "self_assessment_enabled": toggles["self_assessment"],
        }

    @staticmethod
    def _lesson_brief(item: Mapping[str, Any]) -> dict[str, Any]:
        """课程顺序里的一节：只带定位与知识点标题，避免整份讲义撑爆提示词。"""
        points = [
            str(point.get("title") or "").strip()
            for point in (item.get("knowledge_points") or [])
            if str(point.get("title") or "").strip()
        ]
        objectives = [
            str(value).strip()
            for value in (item.get("objectives") or [])
            if str(value).strip()
        ]
        return {
            "id": str(item.get("id") or ""),
            "title": str(item.get("title") or ""),
            "chapter": str(item.get("chapter_title") or ""),
            "knowledge_points": points[:8],
            "objectives": objectives[:3],
        }

    @staticmethod
    def _previous_lesson_summary(course_dir: Path, previous: Mapping[str, Any]) -> str:
        """上一节「已经讲过什么」：优先用已生成讲义的 summary 块。"""
        lesson_dir = course_service.find_lesson_dir(course_dir, str(previous.get("id") or ""))
        points: list[str] = []
        if lesson_dir is not None:
            content = lesson_service.load_lesson(lesson_dir)
            for block in content.blocks:
                if str(block.get("type") or "") == "summary":
                    points.extend(str(item).strip() for item in (block.get("points") or []) if str(item).strip())
        if not points:
            points.extend(str(item).strip() for item in (previous.get("objectives") or []) if str(item).strip())
        if not points:
            points.extend(
                str(point.get("title") or "").strip()
                for point in (previous.get("knowledge_points") or [])
                if str(point.get("title") or "").strip()
            )
        return "；".join(points)[:1200]

    def _build_teacher(self) -> TeacherAgent | None:
        client = getattr(self._services, "client", None)
        if client is None:
            return None
        return TeacherAgent(client)

    def _build_qa(self):
        """答疑 Agent（与教师共用客户端，但用「只回答问题」的骨架）。"""
        client = getattr(self._services, "client", None)
        if client is None:
            return None
        return QaAgent(client)

    def generate_script(self, revision_reason: str = "") -> None:
        teacher = self._build_teacher()
        if teacher is None or self._lesson_dir is None:
            self.status_message.emit("还没有可用的模型配置，请先在设置里配置 API")
            return
        self._load_context()
        if not self._open_generate_dialog():
            self.status_message.emit("已取消生成")
            return
        persona = self._current_persona()
        depth = self._effective_depth()
        lesson_dir = self._lesson_dir
        context = dict(self._context)
        token = self._new_token()
        executor, tools = self._build_tooling(for_lecture=True)

        def factory() -> Awaitable[Any]:
            return lesson_service.generate_script(
                lesson_dir,
                teacher=teacher,
                context=context,
                persona_body=persona.effective_body if persona else None,
                persona_label=self._persona_label(persona),
                depth=depth,
                cancel=token,
                executor=executor,
                tools=tools,
                lesson_id=str((context.get("lesson") or {}).get("id") or ""),
                course_id=str((context.get("course") or {}).get("id") or ""),
                revision_reason=revision_reason,
                on_block=lambda block: self._signals.block.emit(block),
                on_partial=lambda text: self._signals.partial.emit(text),
            )

        self._mode = "generate"
        self.generate_button.setEnabled(False)
        self.status_label.setText("正在生成讲义…（边生成边显示，可随时点「停止」）")
        # 先给阅读区一个占位提示，避免长等待期间一片空白
        self._renderer.render_blocks([
            {"type": "callout", "style": "tip",
             "md": "**正在生成讲义…** 内容会边生成边出现；需要时点「停止」会保留已生成的部分。"}
        ])
        self._pool.start(_AsyncCallTask(factory, self._signals))

    def ask(self, question: str, selection: str = "") -> None:
        teacher = self._build_teacher()
        if teacher is None or self._lesson_dir is None:
            self.status_message.emit("还没有可用的模型配置")
            return
        # 引用区里的图片在发问这一刻就取出来（发完引用区会清空）
        attachments = self._question_attachments()
        self._load_context()
        persona = self._current_persona()
        depth = self._effective_depth()
        lesson_dir = self._lesson_dir
        context = dict(self._context)
        token = self._new_token()
        executor, tools = self._build_tooling()
        self.chat_drawer.append_assistant_start()

        def factory() -> AsyncIterator[str]:
            return lesson_service.ask_question(
                lesson_dir,
                teacher=teacher,
                qa=self._build_qa(),  # 答疑走专用骨架：只回答，不复述讲义
                question=question,
                context=context,
                selection=selection,
                persona_body=persona.effective_body if persona else None,
                persona_name=persona.name if persona else None,
                persona_label=self._persona_label(persona),
                depth=depth,
                cancel=token,
                executor=executor,
                tools=tools,
                session_id=self._chat_session_id or None,
                attachments=attachments,
            )

        self._mode = "ask"
        self._pool.start(_AsyncGenTask(factory, self._signals))

    def _question_attachments(self) -> list[dict]:
        """引用区里若有图片，就把图片本体一起带上（档案不支持读图时只发文字）。"""
        paths = self.chat_drawer.citation_assets()
        if not paths:
            return []
        try:
            supports_vision = bool(self._services.config.require_profile().capabilities.vision)
        except Exception:  # noqa: BLE001 - 档案读不出来时按「不支持」处理，不影响提问
            supports_vision = False
        if not supports_vision:
            return []
        items: list[dict] = []
        for path in paths:
            attachment = lesson_service.build_image_attachment(path)
            if attachment:
                items.append(attachment)
        return items

    def _build_tooling(self, *, for_lecture: bool = False) -> tuple[ToolExecutor | None, list[dict[str, Any]] | None]:
        """装配工具：图示 / 动画 / 读图 / 笔记 / 联网 + **人设工具白名单交集**（§10.6）。"""
        if self._services is None or self._lesson_dir is None:
            return None, None
        # 工具循环默认关闭（见生成偏好）：有些端点不支持 tools，带上就会被 422 拒绝
        # 能力驱动（不猜模型世代）：档案声明支持 function calling 且总开关打开时才带 tools
        if not tools_allowed(getattr(self._services, "config", None)):
            return None, None
        client = getattr(self._services, "client", None)
        persona = self._current_persona()
        # 视觉 / 笔记支持与小灶共用同一装配（agents/tooling.py，M33 起）
        support = build_visual_support(client, getattr(self._services, "config", None))
        vision, illustrator, animator, notes_agent = (
            support.vision,
            support.illustrator,
            support.animator,
            support.notes_agent,
        )

        # 资料范围：限定本课程；本节指定了参考就再收窄到那几份（B）
        course_dir = self._lesson_dir.parents[3]
        course_meta = lesson_service.read_json(course_dir / "course.json", default={}) or {}
        scoped_ids: list[str] = []
        for ref in self._context.get("reference_refs") or []:
            parsed = parse_source_ref(str(ref))
            identifier = str((parsed or {}).get("material_id") or "")
            if identifier and identifier not in scoped_ids:
                scoped_ids.append(identifier)
        registry = build_registry(
            outline=(self._context.get("outline") or None),
            course_dir=course_dir,
            lesson_dir=self._lesson_dir,
            index=getattr(self._services, "index", None),
            material_ids=scoped_ids or None,
            course_id=str(course_meta.get("id") or "") or None,
            progress=self._context.get("progress"),
            client=client,
            search_service=getattr(self._services, "search", None),
            illustrator=illustrator,
            animator=animator,
            vision=vision,
            notes_agent=notes_agent,
            theme=dict(self._css_vars) or None,  # 三维交互页跟随当前主题（明暗模式都能看清）
        )
        allowed = persona.tools if (persona is not None and persona.tools) else registry.names()
        tools = registry.schemas(allowed)
        if for_lecture:
            # 讲义上下文里已经有本小节、知识点和检索结果。再把这些「取上下文」工具
            # 暴露给模型，实测会让它先空转 1–2 轮工具调用，长讲义迟迟不出正文。
            skip = {
                "read_outline",
                "get_lesson_kps",
                "get_progress",
                "search_materials",
                "cite_from_lesson",
                "make_notes",
                "save_note",
                "make_quiz",
            }
            tools = [
                item
                for item in tools
                if str((item.get("function") or {}).get("name") or "") not in skip
            ]
        if not tools and not for_lecture:
            tools = registry.schemas()
        return ToolExecutor(registry, budget=CallBudget()), tools

    # --- 时效性校验（FR-19 辅助路径：系统预取） --------------------------
    def _open_document(self, kind: str) -> None:
        """打开课程大纲 / 课程资料文档（M17）。"""
        if self._course_dir is None:
            self.status_message.emit("请先在课程库里打开一门课程")
            return
        if kind == "outline":
            candidates = [self._course_dir / "outline.md", self._course_dir / "outline.json"]
            label = "课程大纲"
        else:
            candidates = [
                self._course_dir / "materials" / "课程资料.md",
                self._course_dir / "outline.md",
            ]
            label = "课程资料"
        path = next((item for item in candidates if item.exists()), None)
        if path is None:
            self.status_message.emit(f"还没有{label}文档：创建课程时会自动生成")
            return
        from ..widgets.document_viewer import DocumentViewerDialog

        DocumentViewerDialog(path=path, title=label, parent=self).exec()

    # --- 指定参考（M30：一节一份课件；本章默认作为兜底） ------------------
    def open_chapter(self, chapter_id: str) -> None:
        """点左侧的章 → 中间区显示这一章的参考资料页（不再只是收起/展开）。"""
        if self._course_dir is None:
            return
        outline = lesson_service.read_json(self._course_dir / "outline.json", default={}) or {}
        chapter = next(
            (
                item
                for item in (outline.get("chapters") or [])
                if str(item.get("id") or "") == str(chapter_id)
            ),
            None,
        )
        if chapter is None:
            return
        self.chapter_panel.set_sources(importer=self._make_import_runner(), material_loader=self._material_entries)
        self.chapter_panel.set_chapter(
            chapter_id=str(chapter_id),
            chapter_title=str(chapter.get("title") or ""),
            materials=self._material_entries(),
            refs=[str(item) for item in (chapter.get("source_refs") or [])],
        )
        self._stack.setCurrentIndex(1)
        self.title_label.setText(f"{chapter_id} {chapter.get('title', '')} · 章节资料".strip())
        self.status_label.setText(
            "给这一章挑参考：勾选资料 + 填页码范围，然后点「保存本章资料」；"
            "没单独指定参考的小节都会用它。点左侧的小节回到讲义。"
        )

    def _save_chapter_refs(self) -> None:
        """保存「本章资料」页的选择（中间区那一页的保存按钮）。"""
        if self._course_dir is None:
            return
        chapter_id = self.chapter_panel.chapter_id
        if not chapter_id:
            return
        refs = self.chapter_panel.chosen_refs()
        try:
            course_service.set_chapter_source_refs(self._course_dir, chapter_id, refs)
        except KeyError as exc:
            self.chapter_panel.set_status(f"保存失败：{exc}")
            return
        self.chapter_panel.set_status(f"已保存本章默认 {len(refs)} 份；没单独指定参考的小节会继承它。")
        self.status_message.emit(f"已保存 {chapter_id} 的本章资料 {len(refs)} 份")
        # 当前打开的小节如果属于这一章，立刻刷新它的参考状态（按钮上的「参考资料·章」）
        if self._lesson_dir is not None and str(self._reference_context().get("chapter_id") or "") == chapter_id:
            self._load_context()
            self._refresh_reference_button()

    def _open_reference_dialog(self) -> None:
        """设置本节参考 / 本章默认（同一个窗口，两块各自独立）。"""
        if self._course_dir is None or self._lesson_dir is None:
            self.status_message.emit("请先在课程库里打开一门课，再点进一个小节")
            return
        info = self._reference_context()
        dialog = self._build_materials_dialog(info, ok_text="保存")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        summary = self._apply_reference_choice(info, dialog)
        if summary is None:
            return  # 具体原因已由 _apply_reference_choice 报出
        self._load_context()
        self._refresh_reference_button()
        self.status_message.emit(f"参考已保存（{summary}）；生成讲义会优先依据它们" if summary else "已清空参考设置")

    def _build_materials_dialog(self, info: dict[str, Any], *, ok_text: str, show_skip: bool = False):
        """参考资料窗口（本节 + 本章同屏）；两个入口共用，避免两套交互。"""
        from ..widgets.materials_dialog import MaterialsDialog

        return MaterialsDialog(
            materials=self._material_entries(),
            lesson_refs=info["lesson_refs"],
            chapter_refs=info["chapter_refs"],
            lesson_label=str(info["lesson_label"]),
            chapter_label=str(info["chapter_label"]),
            importer=self._make_import_runner(),
            material_loader=self._material_entries,
            ok_text=ok_text,
            show_skip=show_skip,
            parent=self,
        )

    def _apply_reference_choice(self, info: dict[str, Any], dialog: Any) -> str | None:
        """把窗口里的两块选择写回 outline.json；失败返回 ``None``。"""
        lesson = list(dialog.lesson_refs())
        chapter = list(dialog.chapter_refs())
        try:
            if self._course_dir is not None:
                course_service.set_lesson_source_refs(self._course_dir, str(info["lesson_id"]), lesson)
                if info["chapter_id"]:
                    course_service.set_chapter_source_refs(self._course_dir, str(info["chapter_id"]), chapter)
        except KeyError as exc:
            self.status_message.emit(f"保存参考失败：{exc}")
            return None
        parts: list[str] = []
        if lesson:
            parts.append(f"本节 {len(lesson)} 份")
        if chapter:
            parts.append(f"本章默认 {len(chapter)} 份")
        return "、".join(parts)

    def _reference_context(self) -> dict[str, Any]:
        """当前小节在参考这件事上的全部上下文（小节 / 所在章 / 标签）。"""
        content = lesson_service.load_lesson(self._lesson_dir)  # type: ignore[arg-type]
        course_dir = Path(self._course_dir)  # type: ignore[arg-type]
        outline = lesson_service.read_json(course_dir / "outline.json", default={}) or {}
        info: dict[str, Any] = {
            "lesson_id": content.lesson_id,
            "lesson_label": f"{content.lesson_id} {content.title}".strip(),
            "lesson_refs": [],
            "chapter_id": "",
            "chapter_label": "",
            "chapter_refs": [],
        }
        for chapter in outline.get("chapters", []) or []:
            lessons = chapter.get("lessons", []) or []
            if not any(str(item.get("id") or "") == content.lesson_id for item in lessons):
                continue
            info["chapter_id"] = str(chapter.get("id") or "")
            info["chapter_label"] = f"{info['chapter_id']} {chapter.get('title', '')}".strip()
            info["chapter_refs"] = [str(item) for item in (chapter.get("source_refs") or [])]
            for item in lessons:
                if str(item.get("id") or "") == content.lesson_id:
                    info["lesson_refs"] = [str(value) for value in (item.get("source_refs") or [])]
        return info

    def _material_entries(self) -> list[dict]:
        """本课程已导入、可作参考的资料。"""
        if self._course_dir is None:
            return []
        return sorted(
            material_manifest_index(self._course_dir).values(),
            key=lambda item: str(item.get("filename") or ""),
        )

    def _refresh_reference_button(self) -> None:
        """两个按钮都带上状态：这节依据什么、生成时会用什么，一眼可见。"""
        refs = list(self._context.get("reference_refs") or [])
        scope = str(self._context.get("reference_scope") or "")
        self.reteach_button.setToolTip("换种讲法会沿用同一份参考依据")
        if not refs:
            self.reference_button.setText("参考资料")
            self.reference_button.setToolTip("设置本节参考与本章默认（课件 / 教材 + 页码范围）；生成讲义时优先依据它们")
            self.generate_button.setText("生成讲义")
            self.generate_button.setToolTip(
                "本节还没有指定参考课件：生成时会退回「用知识点标题在本课程资料里关键词检索」，"
                "讲的内容不一定与你手上的课件对得上。点左边「参考资料」可以先指定。"
            )
            return
        where = "章" if scope == "chapter" else "节"
        self.reference_button.setText(f"参考资料·{where} {len(refs)}")
        tooltip = "已指定参考：" + "、".join(refs)
        summary = str(self._context.get("reference_summary") or "")
        total = int(self._context.get("reference_digest_total") or 0)
        shown = len(self._context.get("reference_digest") or [])
        if total:
            tooltip += f"\n逐页要点：覆盖 {total} 页"
            if self._context.get("reference_digest_truncated"):
                tooltip += f"，已等距抽样 {shown} 条进提示词（范围越大越稀疏，可把页码范围填小）"
        self.reference_button.setToolTip(tooltip)
        self.generate_button.setText("生成讲义")
        self.generate_button.setToolTip(f"生成时将依据：{summary}" if summary else tooltip)

    def _open_generate_dialog(self) -> bool:
        """点「生成讲义」时的准备页：上传资料 / 选择资料（本节 + 本章）/ 直接生成。

        返回 ``False`` = 主人取消了，本次不生成。
        """
        if self._course_dir is None or self._lesson_dir is None:
            return True
        generation = getattr(getattr(self._services, "config", None), "generation", None)
        if not bool(getattr(generation, "ask_reference_when_missing", True)):
            return True  # 主人关掉过这个窗口：按当前设置直接生成
        info = self._reference_context()
        dialog = self._build_materials_dialog(info, ok_text="开始生成", show_skip=True)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        summary = self._apply_reference_choice(info, dialog)
        if summary is None:
            return False
        if dialog.skip_next_time():
            self._remember_reference_prompt(False)
        self._load_context()
        self._refresh_reference_button()
        if summary:
            self.status_message.emit(f"参考已更新（{summary}），开始生成讲义")
        return True

    def _remember_reference_prompt(self, ask: bool) -> None:
        """把「以后不再提醒」写进生成偏好；写不进去也不该挡住生成。"""
        settings = getattr(self._services, "settings", None)
        if settings is None:
            return
        try:
            setter = getattr(settings, "set_ask_reference_when_missing", None)
            if callable(setter):
                setter(ask)
            settings.save()
        except Exception:  # noqa: BLE001 - 偏好写失败不影响本次生成
            pass

    # --- 补传课件（M30：资料原来只能在建课时导入） ------------------------
    def _make_import_runner(self):
        """给「生成前准备页」用的同步导入函数（它会在工作线程里调，避免卡界面）。"""
        if self._course_dir is None:
            return None
        course_dir = Path(self._course_dir)
        course_meta = lesson_service.read_json(course_dir / "course.json", default={}) or {}
        course_id = str(course_meta.get("id") or course_dir.name)

        def runner(files: Sequence[str]) -> object:
            return self._import_files_sync(files, course_dir=course_dir, course_id=course_id)

        return runner

    def _import_files_sync(self, files: Sequence[str], *, course_dir: Path, course_id: str) -> object:
        """同步导入资料（解析 + 识图 + 落盘 + 建索引）；调用方负责放到工作线程。"""
        from ...services.material_service import MaterialService

        token = self._new_token()
        outline = getattr(self._services, "outline", None)
        vision = outline.default_vision(cancel=token) if outline is not None else None
        vision_batch = outline.default_vision_batch(cancel=token) if outline is not None else None
        service = MaterialService(
            course_dir=course_dir,
            index=getattr(self._services, "index", None),
            cache_dir=self._services.layout.cache,
            vision=vision,
            vision_batch=vision_batch,
            cancel=token,
            confirm=lambda _count, _names: {"proceed": True, "include_all_images": False},
        )
        return service.import_files(list(files), course_id=course_id)

    def _import_materials(self) -> None:
        """给当前课程补传课件 / 教材；导入后就能在「本节参考」里指定给某一节。"""
        if self._course_dir is None:
            self.status_message.emit("请先在课程库里打开一门课程")
            return
        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择课件 / 教材",
            "",
            "资料 (*.pdf *.pptx *.ppt *.docx *.txt *.md *.png *.jpg *.jpeg *.webp)",
        )
        if not files:
            return
        if (
            QMessageBox.question(
                self,
                "导入课件",
                "导入时会读取资料里的文字、表格与演讲者备注；资料中的知识图会一并识别"
                "（装饰图本地过滤，识别图片会产生模型调用费用）。要开始吗？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        runner = self._make_import_runner()
        if runner is None:
            return

        async def factory() -> object:
            return await asyncio.to_thread(runner, files)

        self._mode = "import"
        self.import_button.setEnabled(False)
        self.status_label.setText(f"正在导入 {len(files)} 份课件…")
        self.status_message.emit(f"正在导入 {len(files)} 份课件…")
        self._pool.start(_AsyncCallTask(factory, self._signals))

    def _report_import(self, result: object) -> None:
        materials = list(getattr(result, "materials", []) or [])
        parsed = [item for item in materials if str(getattr(item, "status", "")) == "parsed"]
        failed = [item for item in materials if str(getattr(item, "status", "")) != "parsed"]
        figures = sum(int(getattr(item, "vision_called", 0) or 0) for item in materials)
        message = f"已导入 {len(parsed)} 份课件"
        if figures:
            message += f"（识别资料图 {figures} 张）"
        if failed:
            names = "、".join(str(getattr(item, "filename", "")) for item in failed)
            message += f"；{len(failed)} 份没解析成功：{names}"
        self.status_label.setText(message + "。可以点「本节参考」把它们指定给某一节了。")
        self.status_message.emit(message)

    # --- 追问放大（M23） -------------------------------------------------
    @Slot()
    def _open_chat_window(self) -> None:
        """把当前追问记录放大到独立窗口阅读（Markdown 渲染 + 更宽的阅读区）。"""
        from ..widgets.document_viewer import DocumentViewerDialog

        markdown_text = self.chat_drawer.conversation_markdown().strip()
        if not markdown_text:
            self.status_message.emit("还没有追问记录，先问一个问题吧")
            return
        DocumentViewerDialog(text=markdown_text, title="追问记录", parent=self).exec()

    # --- 追问会话（每个小节可以有多个会话） -------------------------------
    def _refresh_chat_sessions(self, *, keep_current: bool = False) -> None:
        """刷新会话下拉；keep_current=True 时保持当前选中（只看标题变化）。"""
        if self._lesson_dir is None:
            return
        if not lesson_service.chat_sessions(self._lesson_dir):
            lesson_service.ensure_chat_session(self._lesson_dir)
        sessions = lesson_service.chat_sessions(self._lesson_dir)
        if not sessions:
            return
        wanted = self._chat_session_id if keep_current else ""
        if not wanted or not any(item["id"] == wanted for item in sessions):
            wanted = lesson_service.latest_chat_session_id(self._lesson_dir)
        self._chat_session_id = wanted
        self.chat_drawer.set_sessions(sessions, current=wanted)
        self.chat_drawer.load_records(
            lesson_service.load_chat(self._lesson_dir, session_id=wanted)
        )

    @Slot(str)
    def _on_chat_session_changed(self, session_id: str) -> None:
        if self._lesson_dir is None or not session_id:
            return
        self._chat_session_id = session_id
        self.chat_drawer.load_records(
            lesson_service.load_chat(self._lesson_dir, session_id=session_id)
        )
        self.chat_drawer.focus_input()
        self.status_message.emit("已切换追问会话")

    @Slot()
    def _on_new_chat_session(self) -> None:
        """一键新建追问会话：独立上下文，互不串味。"""
        if self._lesson_dir is None:
            self.status_message.emit("请先在课程库里打开一门课程")
            return
        session = lesson_service.create_chat_session(self._lesson_dir)
        self._chat_session_id = session["id"]
        self._refresh_chat_sessions(keep_current=True)
        self.chat_drawer.focus_input()
        self.status_message.emit("已新建追问会话")

    @Slot(str)
    def _on_rename_chat_session(self, session_id: str) -> None:
        if self._lesson_dir is None or not session_id:
            return
        current = next(
            (item["title"] for item in lesson_service.chat_sessions(self._lesson_dir) if item["id"] == session_id),
            "",
        )
        title, ok = QInputDialog.getText(self, "重命名会话", "会话名称：", text=current)
        if not ok or not title.strip():
            return
        lesson_service.rename_chat_session(self._lesson_dir, session_id, title.strip())
        self._refresh_chat_sessions(keep_current=True)

    @Slot(str)
    def _on_delete_chat_session(self, session_id: str) -> None:
        if self._lesson_dir is None or not session_id:
            return
        answer = QMessageBox.question(self, "删除会话", "确定删除这个追问会话吗？（记录会保留在文件里，只是不再显示）")
        if answer != QMessageBox.StandardButton.Yes:
            return
        lesson_service.delete_chat_session(self._lesson_dir, session_id)
        self._chat_session_id = ""
        self._refresh_chat_sessions()
        self.status_message.emit("已删除追问会话")

    # --- 图示放大（双击图） ----------------------------------------------
    @Slot(dict)
    def _open_diagram(self, payload: dict) -> None:
        """双击追问气泡里的图 → 弹窗完整呈现整张图。"""
        from ..widgets.diagram_dialog import DiagramDialog

        dialog = DiagramDialog(
            svg=str(payload.get("svg") or "") or None,
            pixmap=payload.get("pixmap"),
            title=str(payload.get("title") or "图示"),
            parent=self,
        )
        if not dialog.loaded():
            self.status_message.emit("这张图没能渲染出来，正文里保留了源码")
            dialog.deleteLater()
            return
        dialog.exec()

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
        self._refresh_annotations()  # 「查最新」会整页重渲染，标注高亮跟着回来
        suffix = "（命中缓存）" if getattr(result, "cached", False) else ""
        self.status_label.setText(f"已补充 {len(getattr(result, 'hits', []))} 条网络来源{suffix}，请自行判断时效性")

    # --- 交互 -----------------------------------------------------------
    def _new_token(self) -> CancellationToken:
        self._token = CancellationToken()
        return self._token

    # 离开学习台就停掉事件轮询（没必要在后台每 250ms 问一次讲义页）
    def hideEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self._event_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        super().showEvent(event)
        if self._lesson_dir is not None and callable(getattr(self._renderer, "drain_events", None)):
            self._event_timer.start()

    def _on_stop(self) -> None:
        self._token.cancel()
        self.status_message.emit("已请求停止（已生成内容保留）")

    @Slot(str)
    def _on_partial(self, text: str) -> None:
        """逐字预览：正在生成的这一行里已经出现的文字，立即上屏（M21）。"""
        updater = getattr(self._renderer, "stream_update", None)
        if callable(updater):
            updater(text)
    @Slot(object)
    def _on_block_ready(self, block: object) -> None:
        """工作线程解析出一个内容块 → 主线程立刻追加渲染（M20 流式体验）。"""
        if not isinstance(block, dict):
            return
        ender = getattr(self._renderer, "stream_end", None)
        if callable(ender):
            ender()
        appender = getattr(self._renderer, "append_block", None)
        if callable(appender):
            appender(block)
        kind = str(block.get("type") or "")
        if kind and kind != "done":
            self.status_label.setText(f"正在生成… 已输出 {kind} 块")
    def _on_chunk(self, piece: str) -> None:
        if self._mode == "ask":
            self.chat_drawer.append_stream(piece)
        else:
            self.status_label.setText(("…" + piece[-120:]).replace("\n", " "))

    def _on_done(self, payload: object) -> None:
        mode, self._mode = self._mode, ""
        self.generate_button.setEnabled(True)
        self.latest_button.setEnabled(True)
        self.import_button.setEnabled(True)
        if mode == "import":
            self._report_import(payload)
            return
        if mode == "latest":
            self._render_latest(payload)
            self.status_message.emit("时效性校验完成")
            return
        if mode == "ask":
            self.chat_drawer.append_assistant_end()
            self._refresh_chat_sessions(keep_current=True)  # 首问会自动命名会话
            self.status_message.emit("追问完成（已写入 chat.jsonl）")
            return
        if self.last_content is not None and self._lesson_dir is not None:
            content = lesson_service.load_lesson(self._lesson_dir)
            self.last_content = content
            self._renderer.render_blocks(list(content.blocks)) if content.has_blocks else self._renderer.render_markdown(content.markdown)
            self._refresh_annotations()  # 重渲染会清空 DOM：标注高亮要画回去
        if self._course_dir is not None:
            self.lesson_tree.refresh_marks(self._course_dir)
        outcome = payload
        missing = list(getattr(outcome, "missing_knowledge_points", []) or [])
        total_points = len(self._context.get("knowledge_points") or [])
        ratio = 100 if total_points == 0 else int((total_points - len(missing)) / total_points * 100)
        basis = str(self._context.get("reference_summary") or "")
        if basis:
            message = f"讲义已生成 · 依据 {basis}；知识点覆盖 {ratio}%"
        else:
            message = f"讲义已生成 · 本节未指定参考（用自动检索）；知识点覆盖 {ratio}%"
        if missing:
            message += "（仍缺：" + "、".join(missing) + "）"
        ref_missing = list(getattr(outcome, "reference_missing", []) or [])
        if ref_missing:
            head = "、".join(ref_missing[:3])
            tail = f" 等 {len(ref_missing)} 处" if len(ref_missing) > 3 else ""
            message += f"；课件还有没讲到的：{head}{tail}"
        self.status_label.setText(message)
        self.status_message.emit(message)
        self.lesson_completed.emit(str((self._context.get("lesson") or {}).get("id") or ""))

    def _on_failed(self, message: str) -> None:
        self._mode = ""
        self.generate_button.setEnabled(True)
        self.import_button.setEnabled(True)
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
        # 学完就往下走：自动选中并打开下一节（没有下一节则提示整门课完成）
        nxt = self.lesson_tree.next_lesson()
        if nxt is not None:
            self.status_message.emit("已标记完成 → 自动进入下一节")
            self.load_lesson(nxt)
        else:
            self.status_message.emit("已标记完成 · 这门课的小节都学完了，可以去笔记中心生成笔记或导出啦")

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
        self._refresh_default_labels()

    def _global_persona_id(self) -> str:
        config = getattr(self._services, "config", None)
        return str(getattr(getattr(config, "persona", None), "default", "") or "default-teacher")

    def _global_depth(self) -> str:
        config = getattr(self._services, "config", None)
        value = str(getattr(getattr(config, "generation", None), "default_depth", "") or "standard")
        return value if value in ("minimal", "standard", "deep") else "standard"

    def _effective_depth(self) -> str:
        raw = str(self.depth_combo.currentData() or "inherit")
        return self._global_depth() if raw in ("", "inherit") else raw

    def _effective_assessment(self) -> dict[str, bool]:
        lesson = {}
        if self._lesson_dir is not None:
            lesson = lesson_service.read_json(self._lesson_dir / "lesson.json", default={}) or {}
        assessment = lesson.get("assessment") or {}
        course = {}
        if self._course_dir is not None:
            course = lesson_service.read_json(self._course_dir / "course.json", default={}) or {}
        learning = course.get("learning") or {}
        generation = getattr(getattr(self._services, "config", None), "generation", None)
        return resolve_assessment(
            lesson_quiz=str(assessment.get("quiz") or "inherit"),
            lesson_self=str(assessment.get("self_assessment") or "inherit"),
            course_quiz=str(learning.get("quiz") or "inherit"),
            course_self=str(learning.get("self_assessment") or "inherit"),
            global_quiz=bool(getattr(generation, "quiz_enabled", True)),
            global_self=bool(getattr(generation, "self_assessment_enabled", True)),
        )

    def _persona_display(self, persona_id: str) -> str:
        if self._services is None:
            return persona_id
        service = PersonaService(
            user_dir=self._services.layout.personas, index=getattr(self._services, "index", None)
        )
        persona = service.get(persona_id)
        if persona is None:
            return persona_id
        return f"{persona.avatar} {persona.name}".strip()

    def _refresh_default_labels(self) -> None:
        """让「跟随设置」直接显示当前实际生效的老师 / 深度 / 开关。"""
        if not hasattr(self, "persona_combo"):
            return
        persona_index = self.persona_combo.findData("inherit")
        if persona_index >= 0:
            self.persona_combo.setItemText(
                persona_index, f"跟随设置（{self._persona_display(self._global_persona_id())}）"
            )
        depth_index = self.depth_combo.findData("inherit")
        if depth_index >= 0:
            labels = dict(DEPTH_CHOICES)
            self.depth_combo.setItemText(
                depth_index, f"深度：跟随（{labels.get(self._global_depth(), self._global_depth())}）"
            )
        toggles = self._effective_assessment()
        for combo, key, prefix in (
            (self.quiz_toggle, "quiz", "测验"),
            (self.self_toggle, "self_assessment", "自评"),
        ):
            index = combo.findData("inherit")
            if index >= 0:
                combo.setItemText(index, f"{prefix}：跟随（{'开' if toggles[key] else '关'}）")

    def _sync_persona(self, content: lesson_service.LessonContent) -> None:
        """把下拉同步到小节记录的人设；未指定时选「跟随设置」。"""
        recorded = str((content.lesson.get("persona") or {}).get("id") or "inherit")
        index = self.persona_combo.findData(recorded if recorded not in ("", "inherit") else "inherit")
        if index < 0:
            index = self.persona_combo.findData("inherit")
        self.persona_combo.blockSignals(True)
        self.persona_combo.setCurrentIndex(index if index >= 0 else 0)
        self.persona_combo.blockSignals(False)

    def _sync_depth(self, content: lesson_service.LessonContent) -> None:
        recorded = str(content.lesson.get("depth") or "inherit")
        index = self.depth_combo.findData(recorded if recorded not in ("", "inherit") else "inherit")
        self.depth_combo.blockSignals(True)
        self.depth_combo.setCurrentIndex(index if index >= 0 else 0)
        self.depth_combo.blockSignals(False)
        self._refresh_default_labels()

    def _on_depth_changed(self) -> None:
        if self._lesson_dir is None:
            return
        depth = str(self.depth_combo.currentData() or "inherit")
        lesson = lesson_service.read_json(self._lesson_dir / "lesson.json", default={}) or {}
        lesson["depth"] = depth
        from ...infra import file_repository

        file_repository.write_json(self._lesson_dir / "lesson.json", lesson)
        self._context["depth"] = self._effective_depth()
        self._refresh_default_labels()
        effective = self._effective_depth()
        label = "跟随设置" if depth == "inherit" else depth
        self.status_message.emit(f"讲解深度已切换为「{label}」（当前生效：{effective}，只影响之后重新生成的讲义）")

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
        self._refresh_default_labels()
        toggles = self._effective_assessment()
        self.status_message.emit(
            "本小节的测验 / 自评开关已更新（当前生效："
            f"测验{'开' if toggles['quiz'] else '关'}、自评{'开' if toggles['self_assessment'] else '关'}）"
        )

    def _reload_personas(self, current: str | None = None) -> None:
        if self._services is None:
            return
        persona_service = PersonaService(
            user_dir=self._services.layout.personas, index=getattr(self._services, "index", None)
        )
        wanted = current or str(self.persona_combo.currentData() or "") or self._global_persona_id()
        self.persona_combo.blockSignals(True)
        self.persona_combo.clear()
        self.persona_combo.addItem("跟随设置", "inherit")
        for persona in persona_service.list_personas():
            self.persona_combo.addItem(f"{persona.avatar} {persona.name}", persona.id)
        index = self.persona_combo.findData(wanted)
        self.persona_combo.setCurrentIndex(index if index >= 0 else 0)
        self.persona_combo.blockSignals(False)
        self._refresh_default_labels()

    def _current_persona(self):
        if self._services is None:
            return None
        persona_service = PersonaService(
            user_dir=self._services.layout.personas, index=getattr(self._services, "index", None)
        )
        selected = str(self.persona_combo.currentData() or "inherit")
        persona_id = self._global_persona_id() if selected in ("", "inherit") else selected
        return persona_service.get(persona_id)

    def _on_persona_changed(self) -> None:
        persona = self._current_persona()
        selected = str(self.persona_combo.currentData() or "inherit")
        if self._lesson_dir is not None:
            lesson = lesson_service.read_json(self._lesson_dir / "lesson.json", default={}) or {}
            if selected in ("", "inherit"):
                lesson["persona"] = {"id": "inherit", "version": 1}
            elif persona is not None:
                lesson["persona"] = {"id": persona.id, "version": persona.version}
            from ...infra import file_repository

            file_repository.write_json(self._lesson_dir / "lesson.json", lesson)
        self._apply_persona_ui()
        self._refresh_default_labels()
        if persona is None:
            self.status_message.emit("当前人设不可用，已保留「跟随设置」")
            return
        self.status_message.emit(
            f"已切换人设：{persona.avatar} {persona.name}（讲义徽章与追问表头已同步；"
            f"已生成的小节可点「换种讲法」用新风格重讲）"
        )

    def _current_settings_payload(self) -> dict[str, Any]:
        persona_id = str(self.persona_combo.currentData() or "inherit")
        if persona_id in ("", "inherit"):
            persona = {"id": "inherit", "version": 1}
        else:
            current = self._current_persona()
            persona = {"id": persona_id, "version": int(getattr(current, "version", 1) or 1)}
        return {
            "persona": persona,
            "depth": str(self.depth_combo.currentData() or "inherit"),
            "assessment": {
                "quiz": str(self.quiz_toggle.currentData() or "inherit"),
                "self_assessment": str(self.self_toggle.currentData() or "inherit"),
            },
        }

    @Slot()
    def _apply_settings_to_course(self) -> None:
        """把当前老师 / 深度 / 测验 / 自评一次写到本课程所有小节。"""
        if self._course_dir is None:
            self.status_message.emit("请先打开一门课程")
            return
        answer = QMessageBox.question(
            self,
            "应用到全课程",
            "把当前老师、深度、测验与自评设置应用到本课程所有小节？\n"
            "已生成过的讲义内容不会被改写，只影响之后重新生成的内容。",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        from ...infra import file_repository

        payload = self._current_settings_payload()
        count = 0
        for chapter in course_service.lesson_tree(self._course_dir):
            for item in chapter.get("lessons") or []:
                lesson_dir = item.get("path")
                if not lesson_dir:
                    continue
                path = Path(lesson_dir) / "lesson.json"
                lesson = lesson_service.read_json(path, default={}) or {}
                if not lesson:
                    continue
                lesson["persona"] = dict(payload["persona"])
                lesson["depth"] = payload["depth"]
                lesson["assessment"] = dict(payload["assessment"])
                lesson["settings_version"] = lesson_service.SETTINGS_VERSION
                file_repository.write_json(path, lesson)
                count += 1
        self._load_context()
        self._refresh_default_labels()
        self.status_message.emit(f"已把当前设置应用到本课程 {count} 个小节")

    def refresh_generation_defaults(self) -> None:
        """设置页保存后调用：让「跟随设置」的显示与生成参数立刻更新。"""
        self._reload_personas()
        if self._lesson_dir is not None:
            self._sync_persona(lesson_service.load_lesson(self._lesson_dir))
            self._sync_depth(lesson_service.load_lesson(self._lesson_dir))
            self._load_context()
            self._apply_persona_ui()
            self._refresh_reference_button()
        self._refresh_default_labels()
