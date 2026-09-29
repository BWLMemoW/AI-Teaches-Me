"""创建课程向导（《项目文档》FR-03 / §11.3 第 4 步；《技术文档》§7.3 关键流程）。

四步：① 基本信息 → ② AI 澄清（可跳过）→ ③ 生成大纲（流式）→ ④ 预览、编辑、确认落盘。
网络请求全在工作线程（§1.5 红线 3），每一步都能取消或返回。
"""

from __future__ import annotations

import asyncio

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from pathlib import Path

from PySide6.QtWidgets import (
    QScrollArea,
    QToolButton,
    QButtonGroup,
    QListWidget,
    QListWidgetItem,
    QHBoxLayout,
    QListView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ...core.cancel import CancellationToken
from ...core.models import GOAL_LABELS, CourseIcon, CourseOutline
from ...services.outline_service import CreateResult, OutlineDraft, OutlineService
from ..widgets.icon_grid import IconGrid
from ..widgets.outline_tree import OutlineTreeWidget


class _Signals(QObject):
    chunk = Signal(str)
    done = Signal(object)
    failed = Signal(str)


class _ClarifyTask(QRunnable):
    def __init__(self, service: OutlineService, draft: OutlineDraft, token: CancellationToken, signals: _Signals) -> None:
        super().__init__()
        self._service = service
        self._draft = draft
        self._token = token
        self._signals = signals

    @Slot()
    def run(self) -> None:
        async def go() -> None:
            async for piece in self._service.ask_clarify(self._draft, cancel=self._token):
                self._signals.chunk.emit(piece)

        try:
            asyncio.run(go())
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(self._draft)


class _GenerateTask(QRunnable):
    def __init__(self, service: OutlineService, draft: OutlineDraft, token: CancellationToken, signals: _Signals) -> None:
        super().__init__()
        self._service = service
        self._draft = draft
        self._token = token
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            outline = asyncio.run(self._service.generate(self._draft, cancel=self._token))
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(outline)



class _IconSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _IconTask(QRunnable):
    """在工作线程里跑 AI 图标生成。"""

    def __init__(self, factory, signals: _IconSignals) -> None:
        super().__init__()
        self._factory = factory
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            result = self._factory()
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(result)


class WizardPage(QWidget):
    """课程创建向导。"""

    course_created = Signal(object)
    status_message = Signal(str)

    STEP_BASIC = 0
    STEP_CLARIFY = 1
    STEP_GENERATE = 2
    STEP_PREVIEW = 3

    def __init__(self, service: OutlineService, *, thread_pool: QThreadPool | None = None, parent=None) -> None:
        super().__init__(parent)
        self._service = service
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._draft: OutlineDraft | None = None
        self._material_paths: list[Path] = []
        self._icon_color = "#E86A92"
        self._icon_bg = "#FFF5F8"
        self._icon_svg: str | None = None
        self._outline: CourseOutline | None = None
        self._token = CancellationToken()
        self._signals = _Signals()
        self._signals.chunk.connect(self._on_chunk)
        self._signals.done.connect(self._on_done)
        self._signals.failed.connect(self._on_failed)
        self._icon_signals = _IconSignals()
        self._icon_signals.done.connect(self._on_icon_ready)
        self._icon_signals.failed.connect(lambda message: (self.icon_hint.setText(f"图标生成失败：{message}"), self.ai_icon_button.setEnabled(True)))

        layout = QVBoxLayout(self)
        title = QLabel("新建课程")
        title.setProperty("role", "title")
        layout.addWidget(title)

        self.stack = QStackedWidget()
        self.stack.addWidget(self._build_basic_step())
        self.stack.addWidget(self._build_clarify_step())
        self.stack.addWidget(self._build_generate_step())
        self.stack.addWidget(self._build_preview_step())
        self._rebuild_icon_list()
        layout.addWidget(self.stack, 1)

    # --- 四个步骤 -------------------------------------------------------
    def _build_basic_step(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        layout.addWidget(QLabel("第一步：课程名（必填），你自己的大纲可选，写得越具体，AI 细化得越准。"))

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("例如：线性代数速通")
        layout.addWidget(QLabel("课程名"))
        layout.addWidget(self.name_edit)

        self.user_outline_edit = QPlainTextEdit()
        self.user_outline_edit.setMinimumHeight(150)  # 别被布局压成一行
        self.user_outline_edit.setPlaceholderText("（选填）例如：\nC1 矩阵与线性方程组\n  L1-1 矩阵到底是什么\n  L1-2 矩阵乘法")
        layout.addWidget(QLabel("我的大纲（选填）"))
        layout.addWidget(self.user_outline_edit, 1)

        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("用逗号分隔，例如：数学, 考研")
        layout.addWidget(QLabel("标签（选填）"))
        layout.addWidget(self.tags_edit)

        self.material_button = QPushButton("选择资料并解析（PDF / PPTX / DOCX / TXT / MD / 图片）")
        self.material_button.clicked.connect(self._on_pick_materials)
        layout.addWidget(self.material_button)
        self.material_label = QLabel("尚未选择资料（解析结果会写进课程 materials/，并作为大纲依据）")
        self.material_label.setProperty("role", "muted")
        self.material_label.setWordWrap(True)
        layout.addWidget(self.material_label)

        # 课程目标：同一批知识点，应试 / 速成 / 兴趣 / 精讲的拆法与讲法完全不同
        goal_label = QLabel("课程目标（决定大纲结构与讲解侧重）")
        layout.addWidget(goal_label)
        goal_row = QHBoxLayout()
        goal_row.setSpacing(6)
        self.goal_group = QButtonGroup(self)
        self.goal_group.setExclusive(False)  # 允许多选：应试 + 求职 + 项目实战 这类组合很常见
        self.goal_buttons: dict[str, QToolButton] = {}
        for value, label in GOAL_LABELS.items():
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setToolTip(f"{label}（{value}）")
            button.setMinimumHeight(30)
            button.setMinimumWidth(64)
            if value == "general":
                button.setChecked(True)
            self.goal_group.addButton(button)
            goal_row.addWidget(button)
            self.goal_buttons[value] = button
        goal_row.addStretch(1)
        layout.addLayout(goal_row)
        self._goal_type = "general"

        layout.addWidget(QLabel("课程图标（选预设 / 换颜色 / 或让 AI 生成）"))
        self.icon_grid = IconGrid(columns=6)
        self.icon_grid.setMinimumHeight(self.icon_grid.height_hint())
        self.icon_grid.selected_changed.connect(lambda *_: self._refresh_icon_preview())
        layout.addWidget(self.icon_grid)

        icon_row = QHBoxLayout()
        self.icon_preview = QLabel()
        icon_row.addWidget(self.icon_preview)
        self.icon_color_button = QPushButton("主色…")
        self.icon_color_button.clicked.connect(lambda: self._pick_icon_color(color=True))
        icon_row.addWidget(self.icon_color_button)
        self.icon_bg_button = QPushButton("底色…")
        self.icon_bg_button.clicked.connect(lambda: self._pick_icon_color(color=False))
        icon_row.addWidget(self.icon_bg_button)
        self.ai_icon_button = QPushButton("让 AI 生成图标")
        self.ai_icon_button.clicked.connect(self._on_generate_icon)
        icon_row.addWidget(self.ai_icon_button)
        self.icon_hint = QLabel("")
        self.icon_hint.setProperty("role", "muted")
        icon_row.addWidget(self.icon_hint, 1)
        layout.addLayout(icon_row)

        note = QLabel("支持 PDF / PPTX / DOCX / TXT / MD 与图片；解析在后台进行，可随时跳过。")
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        layout.addWidget(note)

        row = QHBoxLayout()
        row.addStretch(1)
        self.basic_next_button = QPushButton("下一步：让 AI 澄清")
        self.basic_next_button.setProperty("role", "primary")
        self.basic_next_button.clicked.connect(self._on_basic_next)
        row.addWidget(self.basic_next_button)
        layout.addLayout(row)
        return page

    def _build_clarify_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("第二步：AI 先问几个关键问题（最多 5 个）。不想回答就直接生成。"))

        self.clarify_output = QPlainTextEdit()
        self.clarify_output.setReadOnly(True)
        self.clarify_output.setPlaceholderText("澄清问题会逐字出现…")
        layout.addWidget(self.clarify_output, 1)

        self.clarify_answers_edit = QPlainTextEdit()
        self.clarify_answers_edit.setPlaceholderText("你的回答（选填），可以只回答关心的那几条")
        layout.addWidget(QLabel("我的回答"))
        layout.addWidget(self.clarify_answers_edit, 1)

        row = QHBoxLayout()
        self.clarify_back_button = QPushButton("上一步")
        self.clarify_back_button.clicked.connect(self._on_back_to_basic)
        row.addWidget(self.clarify_back_button)
        self.restart_clarify_button = QPushButton("重新提问")
        self.restart_clarify_button.clicked.connect(lambda: self._start_clarify())
        row.addWidget(self.restart_clarify_button)
        row.addStretch(1)
        self.clarify_skip_button = QPushButton("跳过，直接生成大纲")
        self.clarify_skip_button.setProperty("role", "primary")
        self.clarify_skip_button.clicked.connect(self._on_start_generate)
        row.addWidget(self.clarify_skip_button)
        layout.addLayout(row)
        return page

    def _build_generate_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("第三步：正在生成大纲（流式显示模型原始输出）。"))

        self.generate_output = QPlainTextEdit()
        self.generate_output.setReadOnly(True)
        layout.addWidget(self.generate_output, 1)

        self.generate_bar = QProgressBar()
        self.generate_bar.setRange(0, 0)
        layout.addWidget(self.generate_bar)

        row = QHBoxLayout()
        self.generate_stop_button = QPushButton("停止")
        self.generate_stop_button.clicked.connect(self._on_cancel)
        row.addWidget(self.generate_stop_button)
        row.addStretch(1)
        layout.addLayout(row)
        return page

    def _build_preview_step(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.addWidget(QLabel("第四步：大纲预览（可直接双击改名，支持增删与拖动）。"))

        self.issues_label = QLabel("")
        self.issues_label.setWordWrap(True)
        self.issues_label.setProperty("role", "muted")
        layout.addWidget(self.issues_label)

        self.outline_tree = OutlineTreeWidget()
        layout.addWidget(self.outline_tree, 1)

        tools = QHBoxLayout()
        self.add_chapter_button = QPushButton("+ 章")
        self.add_chapter_button.clicked.connect(lambda: self.outline_tree.add_chapter())
        tools.addWidget(self.add_chapter_button)
        self.add_lesson_button = QPushButton("+ 小节")
        self.add_lesson_button.clicked.connect(lambda: self.outline_tree.add_lesson())
        tools.addWidget(self.add_lesson_button)
        self.remove_button = QPushButton("删除选中")
        self.remove_button.clicked.connect(lambda: self.outline_tree.remove_selected())
        tools.addWidget(self.remove_button)
        tools.addStretch(1)
        self.regenerate_button = QPushButton("重新生成")
        self.regenerate_button.clicked.connect(self._on_start_generate)
        tools.addWidget(self.regenerate_button)
        self.commit_button = QPushButton("确认并创建课程")
        self.commit_button.setProperty("role", "primary")
        self.commit_button.clicked.connect(self._on_commit)
        tools.addWidget(self.commit_button)
        layout.addLayout(tools)
        return page

    # --- 流程 -----------------------------------------------------------
    @Slot()
    def _on_pick_materials(self) -> None:
        """选择资料文件并解析（解析放到后台线程，避免卡界面）。"""
        from PySide6.QtWidgets import QFileDialog

        files, _ = QFileDialog.getOpenFileNames(
            self,
            "选择资料文件",
            "",
            "资料 (*.pdf *.pptx *.ppt *.docx *.txt *.md *.png *.jpg *.jpeg *.webp)",
        )
        if not files:
            return
        self._material_paths = [Path(item) for item in files]
        from ...infra.sqlite_index import SqliteIndex
        from ...services.material_service import MaterialService

        workspace = self._service.courses_dir.parent
        cache_dir = workspace / "data" / "cache" / "draft_materials"
        index = None
        try:
            index = SqliteIndex(workspace / "data" / "library.db")
            service = MaterialService(course_dir=cache_dir, index=index)
            result = service.import_files(self._material_paths, course_id="draft")
        except Exception as exc:  # noqa: BLE001 - 解析失败不阻塞建课
            self.material_label.setText(f"资料解析失败：{exc}")
            return
        finally:
            if index is not None:
                index.close()
        parsed = len(result.parsed)
        failed = len(result.failed)
        summary = result.summary_for_director()
        self._material_summary = summary
        self.material_label.setText(
            f"已解析 {parsed} 份资料" + (f"，{failed} 份失败" if failed else "") + "；生成大纲时会被引用。"
        )

    def _current_goals(self) -> list[str]:
        """当前选中的课程目标（可多选；一个都没选时回落到通用）。"""
        chosen = [value for value, button in getattr(self, "goal_buttons", {}).items() if button.isChecked()]
        return chosen or ["general"]

    def _current_goal(self) -> str:
        """主目标（= 第一个选中的项），兼容单值字段。"""
        return self._current_goals()[0]
    # --- 图标 -----------------------------------------------------------
    def _current_icon(self) -> CourseIcon:
        preset_id = self.icon_grid.selected_id() or "general"
        return CourseIcon(kind="preset", value=preset_id, color=self._icon_color, bg=self._icon_bg)

    def _rebuild_icon_list(self) -> None:
        """重建图标网格（固定列数，永不裁切）。"""
        from ...services import icon_service

        current = self.icon_grid.selected_id() or "general"
        self.icon_grid.set_icons(
            icon_service.preset_choices(),
            color=self._icon_color,
            bg=self._icon_bg,
            current=current,
        )
        self.icon_grid.setMinimumHeight(self.icon_grid.height_hint())
        self._refresh_icon_preview()

    def _refresh_icon_preview(self) -> None:
        from ..icon_render import course_icon_pixmap

        icon = self._current_icon()
        if self._icon_svg:
            from ...services.icon_service import tint_svg

            pixmap = course_icon_pixmap(Path("."), icon, size=72)
            from ..icon_render import svg_to_pixmap

            inner = svg_to_pixmap(tint_svg(self._icon_svg, self._icon_color), size=44)
            pixmap = course_icon_pixmap(Path("."), icon, size=72)
            from PySide6.QtGui import QPainter

            painter = QPainter(pixmap)
            try:
                painter.drawPixmap(14, 14, inner)
            finally:
                painter.end()
            self.icon_preview.setPixmap(pixmap)
        else:
            self.icon_preview.setPixmap(course_icon_pixmap(Path("."), icon, size=72))

    def _pick_icon_color(self, *, color: bool) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import QColorDialog

        current = QColor(self._icon_color if color else self._icon_bg)
        chosen = QColorDialog.getColor(current, self, "选择颜色")
        if not chosen.isValid():
            return
        if color:
            self._icon_color = chosen.name().upper()
        else:
            self._icon_bg = chosen.name().upper()
        self._rebuild_icon_list()

    def _on_generate_icon(self) -> None:
        """让 AI 生成图标（工作线程；失败自动降级到预设）。"""
        client = getattr(self._service, "client", None)
        if client is None:
            self.icon_hint.setText("没有可用的模型配置，已保持预设图标")
            return
        name = self.name_edit.text().strip() or "我的课程"
        self.ai_icon_button.setEnabled(False)
        self.icon_hint.setText("正在生成图标…")

        # 草稿阶段还没有课程目录：图标写到工作区缓存目录，绝不能用 "."（会污染当前工作目录）
        workspace = Path(self._service.courses_dir).parent
        draft_dir = workspace / "data" / "cache" / "draft_icon"

        def factory():
            from ...services.icon_service import IconService

            service = IconService(client=client)
            return asyncio.run(
                service.generate_with_ai(
                    draft_dir, concept=name, color=self._icon_color, bg=self._icon_bg
                )
            )

        self._pool.start(_IconTask(factory, self._icon_signals))

    @Slot(object)
    def _on_icon_ready(self, result) -> None:
        self.ai_icon_button.setEnabled(True)
        if getattr(result, "generated_by_ai", False):
            self._icon_svg = result.svg
            self.icon_hint.setText("AI 图标已生成并通过自检")
        else:
            self._icon_svg = None
            self.icon_hint.setText("；".join(getattr(result, "issues", [])) or "已改用预设图标")
        self._refresh_icon_preview()

    @Slot()
    def _on_basic_next(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            self.status_message.emit("请先填写课程名")
            return
        tags = [tag.strip() for tag in self.tags_edit.text().split(",") if tag.strip()]
        self._draft = self._service.new_draft(
            name,
            user_outline=self.user_outline_edit.toPlainText().strip(),
            material_summary=getattr(self, "_material_summary", ""),
            icon=self._current_icon(),
            icon_svg=self._icon_svg,
            goal_type=self._current_goal(),
            goal_types=self._current_goals(),
            tags=tags,
        )
        if self._draft.name != name:
            self.status_message.emit(f"已有同名课程，本次将创建《{self._draft.name}》")
        self.stack.setCurrentIndex(self.STEP_CLARIFY)
        self._start_clarify()

    @Slot()
    def _on_back_to_basic(self) -> None:
        self._on_cancel()
        self.stack.setCurrentIndex(self.STEP_BASIC)

    def _start_clarify(self) -> None:
        if self._draft is None:
            return
        self.clarify_output.clear()
        self._token = CancellationToken()
        self._pool.start(_ClarifyTask(self._service, self._draft, self._token, self._signals))

    @Slot()
    def _on_start_generate(self) -> None:
        if self._draft is None:
            return
        self._draft.clarify_answers = self.clarify_answers_edit.toPlainText().strip()
        self.generate_output.clear()
        self._token = CancellationToken()
        self.stack.setCurrentIndex(self.STEP_GENERATE)
        self.generate_bar.setRange(0, 0)
        self._pool.start(_GenerateTask(self._service, self._draft, self._token, self._signals))

    @Slot()
    def _on_cancel(self) -> None:
        self._token.cancel()
        self.status_message.emit("已请求停止（已生成的内容会保留）")

    @Slot(str)
    def _on_chunk(self, piece: str) -> None:
        target = self.clarify_output if self.stack.currentIndex() == self.STEP_CLARIFY else self.generate_output
        target.insertPlainText(piece)
        target.verticalScrollBar().setValue(target.verticalScrollBar().maximum())

    @Slot(object)
    def _on_done(self, payload: object) -> None:
        if isinstance(payload, CourseOutline):
            self._outline = payload
            self.outline_tree.set_outline(payload)
            issues = self._service.validate(payload)
            self.issues_label.setText(
                "结构自检：没问题，可以直接创建。" if not issues else "结构自检提示：" + "；".join(issues)
            )
            self.stack.setCurrentIndex(self.STEP_PREVIEW)
            self.status_message.emit(
                f"大纲已生成：{len(payload.chapters)} 章 / {payload.total_lessons()} 小节"
            )

    @Slot(str)
    def _on_failed(self, message: str) -> None:
        self.generate_bar.setRange(0, 1)
        self.generate_bar.setValue(0)
        self.stack.setCurrentIndex(self.STEP_CLARIFY if self._outline is None else self.STEP_PREVIEW)
        self.status_message.emit(f"生成失败：{message}")

    @Slot()
    def _on_commit(self) -> None:
        if self._draft is None or self._outline is None:
            return
        edited = self.outline_tree.to_outline(self._outline)
        try:
            result: CreateResult = self._service.commit(self._draft, edited)
        except Exception as exc:  # noqa: BLE001
            self.status_message.emit(f"创建失败：{exc}")
            return
        self.course_created.emit(result)
        self.status_message.emit(f"《{result.name}》已创建（{len(result.files)} 个文件）")
        self.reset()

    # --- 复位 -----------------------------------------------------------
    def reset(self) -> None:
        self._draft = None
        self._outline = None
        self._token = CancellationToken()
        self.name_edit.clear()
        self.user_outline_edit.clear()
        self.tags_edit.clear()
        self.clarify_output.clear()
        self.clarify_answers_edit.clear()
        self.generate_output.clear()
        self.outline_tree.clear()
        self.issues_label.clear()
        self.stack.setCurrentIndex(self.STEP_BASIC)
