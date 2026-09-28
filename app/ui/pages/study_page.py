"""学习台（M0 最小版：离线阅读预置讲义）。

渲染交给 ``app/web/lesson.html`` + ``renderer.js``（教学区 / 追问抽屉 / 小灶共用一套内核）。
为了让无显示环境也能测试，本页依赖一个 ``LessonRenderer`` 接口，默认实现才是 QWebEngineView。
文件读写与落盘都在工作线程（《技术文档》§1.5 红线 3）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Protocol

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QUrl, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from ...services import lesson_service
from ...services.lesson_service import LessonContent


def default_html_path() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "lesson.html"


class LessonRenderer(Protocol):
    """渲染内核的最小接口（便于注入假实现做无界面测试）。"""

    def widget(self) -> QWidget: ...

    def load_page(self, html_path: Path) -> None: ...

    def set_theme(self, css_vars: dict) -> None: ...

    def render_blocks(self, blocks: list[dict]) -> None: ...

    def render_markdown(self, text: str) -> None: ...


class WebEngineRenderer:
    """默认渲染器：QWebEngineView 加载本地 ``lesson.html``（禁网，见 §4.8）。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        from PySide6.QtWebEngineWidgets import QWebEngineView

        self._view = QWebEngineView(parent)
        self._ready = False
        self._queue: list[str] = []
        self._view.loadFinished.connect(self._on_load_finished)
        self.last_load_ok: bool | None = None

    def widget(self) -> QWidget:
        return self._view

    @property
    def ready(self) -> bool:
        """页面是否已加载完成（自检与调试用）。"""
        return self._ready

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
        payload = json.dumps(list(blocks), ensure_ascii=False)
        self._run(f"window.ATM.renderBlocks({payload});")

    def render_markdown(self, text: str) -> None:
        payload = json.dumps(text, ensure_ascii=False)
        self._run(f"window.ATM.renderFallbackMarkdown({payload}, '缺少结构化块，已按整段 Markdown 渲染。');")


class _LoadSignals(QObject):
    loaded = Signal(object)


class _LessonLoadTask(QRunnable):
    """在工作线程里读取小节内容（讲义文件可能不小，不能卡住界面）。"""

    def __init__(self, lesson_dir: Path, signals: _LoadSignals) -> None:
        super().__init__()
        self._lesson_dir = lesson_dir
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            content = lesson_service.load_lesson(self._lesson_dir)
        except Exception as exc:  # noqa: BLE001
            content = LessonContent(
                lesson_dir=self._lesson_dir,
                lesson={},
                blocks=(),
                markdown="",
                issues=(f"读取失败：{exc}",),
            )
        self._signals.loaded.emit(content)


class _CompleteSignals(QObject):
    done = Signal(str)
    failed = Signal(str)


class _CompleteTask(QRunnable):
    def __init__(self, lesson_dir: Path, minutes: int, signals: _CompleteSignals) -> None:
        super().__init__()
        self._lesson_dir = lesson_dir
        self._minutes = minutes
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            lesson = lesson_service.mark_complete(self._lesson_dir, minutes=self._minutes)
            self._signals.done.emit(str(lesson.get("id") or self._lesson_dir.name))
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))


class StudyPage(QWidget):
    """阅读 + 标记完成（M2 会加上章节树、流式生成与追问抽屉）。"""

    back_requested = Signal()
    lesson_completed = Signal(str)
    status_message = Signal(str)

    def __init__(
        self,
        *,
        renderer: LessonRenderer | None = None,
        html_path: Path | None = None,
        thread_pool: QThreadPool | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._renderer: LessonRenderer = renderer if renderer is not None else WebEngineRenderer()
        self._load_signals = _LoadSignals()
        self._load_signals.loaded.connect(self._on_lesson_loaded)
        self._complete_signals = _CompleteSignals()
        self._complete_signals.done.connect(self._on_completed)
        self._complete_signals.failed.connect(lambda message: self.status_message.emit(f"标记完成失败：{message}"))
        self._lesson_dir: Path | None = None
        self._css_vars: dict = {}
        self.last_content: LessonContent | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        bar = QHBoxLayout()
        self.back_button = QPushButton("← 课程库")
        self.back_button.clicked.connect(self.back_requested.emit)
        bar.addWidget(self.back_button)
        self.title_label = QLabel("尚未打开小节")
        self.title_label.setProperty("role", "subtitle")
        bar.addWidget(self.title_label, 1)
        self.complete_button = QPushButton("标记完成")
        self.complete_button.setProperty("role", "primary")
        self.complete_button.setEnabled(False)
        self.complete_button.clicked.connect(self._on_mark_complete)
        bar.addWidget(self.complete_button)
        layout.addLayout(bar)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        layout.addWidget(self._renderer.widget(), 1)
        self._renderer.load_page(html_path or default_html_path())

    # --- 对外 -----------------------------------------------------------
    @property
    def renderer(self) -> LessonRenderer:
        return self._renderer

    def load_lesson(self, lesson_dir: str | Path) -> None:
        self._lesson_dir = Path(lesson_dir)
        self.title_label.setText(f"正在读取：{self._lesson_dir.name}")
        self._pool.start(_LessonLoadTask(self._lesson_dir, self._load_signals))

    def apply_theme_vars(self, css_vars: dict) -> None:
        self._css_vars = dict(css_vars or {})
        self._renderer.set_theme(self._css_vars)

    @property
    def css_vars(self) -> dict:
        return dict(self._css_vars)

    # --- 内部 -----------------------------------------------------------
    @Slot(object)
    def _on_lesson_loaded(self, content: LessonContent) -> None:
        self.last_content = content
        self.title_label.setText(f"{content.lesson_id} · {content.title}")
        if content.issues:
            self.status_label.setText("提示：" + "；".join(content.issues))
        else:
            self.status_label.setText("")
        if content.has_blocks:
            self._renderer.render_blocks(list(content.blocks))
        elif content.markdown:
            self._renderer.render_markdown(content.markdown)
        self._renderer.set_theme(self._css_vars)
        self.complete_button.setEnabled(True)
        self.status_message.emit(f"已打开 {content.lesson_id}")

    @Slot()
    def _on_mark_complete(self) -> None:
        if self._lesson_dir is None:
            return
        self.complete_button.setEnabled(False)
        self.status_message.emit("正在保存学习状态…")
        self._pool.start(_CompleteTask(self._lesson_dir, 1, self._complete_signals))

    @Slot(str)
    def _on_completed(self, lesson_id: str) -> None:
        self.complete_button.setEnabled(True)
        self.lesson_completed.emit(lesson_id)
        self.status_message.emit(f"{lesson_id} 已标记完成")
