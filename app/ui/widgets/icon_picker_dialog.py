"""图标选择对话框（卡片右键「换图标 / 换颜色」与向导共用）。

内容：预设图标网格 + 主色/底色取色 + 「让 AI 生成」+ 实时预览。
确认后直接写入课程资产（``assets/icon.svg``）与 ``course.json``。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QSize, Qt, QThreadPool, Signal, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QHBoxLayout,
    QLabel,
    QListView,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ...core.models import CourseIcon
from ...services.icon_service import IconService, preset_choices, tint_svg
from ..icon_render import course_icon_pixmap, svg_to_pixmap


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _IconTask(QRunnable):
    def __init__(self, factory, signals: _Signals) -> None:
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


class IconPickerDialog(QDialog):
    """课程图标选择器。"""

    def __init__(
        self,
        *,
        course_dir: str | Path,
        course_name: str,
        client: object | None = None,
        current: CourseIcon | None = None,
        pool: QThreadPool | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.course_dir = Path(course_dir)
        self.client = client
        self._pool = pool if pool is not None else QThreadPool.globalInstance()
        self._signals = _Signals()
        self._signals.done.connect(self._on_ai_done)
        self._signals.failed.connect(self._on_ai_failed)
        self._svg_override: str | None = None

        base = current or IconService.load(self.course_dir)
        self._color = base.color
        self._bg = base.bg if base.bg != "#FFF5F8" or base.kind != "preset" else base.bg

        self.setWindowTitle(f"课程图标 · {course_name}")
        self.resize(560, 420)
        layout = QVBoxLayout(self)

        self.icon_list = QListWidget()
        self.icon_list.setViewMode(QListView.ViewMode.IconMode)
        self.icon_list.setIconSize(QSize(48, 48))
        self.icon_list.setGridSize(QSize(72, 80))
        self.icon_list.setMovement(QListView.Movement.Static)
        self.icon_list.currentRowChanged.connect(lambda *_: self._refresh_preview())
        layout.addWidget(self.icon_list, 1)

        row = QHBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(72, 72)
        row.addWidget(self.preview)
        self.color_button = QPushButton("主色…")
        self.color_button.clicked.connect(lambda: self._pick_color(True))
        row.addWidget(self.color_button)
        self.bg_button = QPushButton("底色…")
        self.bg_button.clicked.connect(lambda: self._pick_color(False))
        row.addWidget(self.bg_button)
        self.ai_button = QPushButton("让 AI 生成")
        self.ai_button.clicked.connect(self._generate_ai)
        row.addWidget(self.ai_button)
        self.hint = QLabel("")
        self.hint.setProperty("role", "muted")
        row.addWidget(self.hint, 1)
        layout.addLayout(row)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.ok_button = QPushButton("应用")
        self.ok_button.setProperty("role", "primary")
        self.ok_button.clicked.connect(self._apply_and_accept)
        buttons.addWidget(self.ok_button)
        layout.addLayout(buttons)

        self._rebuild_list(base_value=base.value if base.is_preset else None)

    # --- 列表与预览 -----------------------------------------------------
    def _rebuild_list(self, *, base_value: str | None = None) -> None:
        self.icon_list.blockSignals(True)
        self.icon_list.clear()
        for preset_id, label in preset_choices():
            icon = CourseIcon(kind="preset", value=preset_id, color=self._color, bg=self._bg)
            item = QListWidgetItem(course_icon_pixmap(Path("."), icon, size=64), label)
            item.setData(Qt.ItemDataRole.UserRole, preset_id)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.icon_list.addItem(item)
        self.icon_list.blockSignals(False)
        target = base_value or "general"
        for row in range(self.icon_list.count()):
            if self.icon_list.item(row).data(Qt.ItemDataRole.UserRole) == target:
                self.icon_list.setCurrentRow(row)
                break
        else:
            self.icon_list.setCurrentRow(0)
        self._refresh_preview()

    def _current_icon(self) -> CourseIcon:
        item = self.icon_list.currentItem()
        value = str(item.data(Qt.ItemDataRole.UserRole)) if item is not None else "general"
        return CourseIcon(kind="preset", value=value, color=self._color, bg=self._bg)

    def _refresh_preview(self) -> None:
        icon = self._current_icon()
        pixmap = course_icon_pixmap(Path("."), icon, size=72, padding=8)
        if self._svg_override:
            from PySide6.QtGui import QPainter

            inner = svg_to_pixmap(tint_svg(self._svg_override, self._color), size=44)
            painter = QPainter(pixmap)
            try:
                painter.drawPixmap(14, 14, inner)
            finally:
                painter.end()
        self.preview.setPixmap(pixmap)

    def _pick_color(self, color: bool) -> None:
        current = QColor(self._color if color else self._bg)
        chosen = QColorDialog.getColor(current, self, "选择颜色")
        if not chosen.isValid():
            return
        if color:
            self._color = chosen.name().upper()
        else:
            self._bg = chosen.name().upper()
        self._rebuild_list(base_value=self._current_icon().value)

    # --- AI 生成 --------------------------------------------------------
    def _generate_ai(self) -> None:
        if self.client is None:
            self.hint.setText("没有可用的模型配置")
            return
        self.ai_button.setEnabled(False)
        self.hint.setText("正在生成…")
        concept = self.parent().windowTitle() if self.parent() is not None else "课程"
        color, bg, course_dir = self._color, self._bg, self.course_dir
        client = self.client

        def factory():
            return asyncio.run(
                IconService(client=client).generate_with_ai(course_dir, concept=concept, color=color, bg=bg)
            )

        self._pool.start(_IconTask(factory, self._signals))

    @Slot(object)
    def _on_ai_done(self, result) -> None:
        self.ai_button.setEnabled(True)
        if getattr(result, "generated_by_ai", False):
            self._svg_override = result.svg
            self.hint.setText("AI 图标已生成并通过自检")
        else:
            self._svg_override = None
            self.hint.setText("；".join(getattr(result, "issues", [])) or "已改用预设图标")
        self._refresh_preview()

    @Slot(str)
    def _on_ai_failed(self, message: str) -> None:
        self.ai_button.setEnabled(True)
        self.hint.setText(f"生成失败：{message}")

    # --- 应用 -----------------------------------------------------------
    def _apply_and_accept(self) -> None:
        service = IconService()
        if self._svg_override:
            outcome = service.apply_svg(self.course_dir, self._svg_override, color=self._color, bg=self._bg)
            if not outcome.svg:
                outcome = service.apply_preset(self.course_dir, "general", color=self._color, bg=self._bg)
        else:
            outcome = service.apply_preset(
                self.course_dir, self._current_icon().value, color=self._color, bg=self._bg
            )
        IconService.store(self.course_dir, outcome.icon)
        self.applied_icon = outcome.icon
        self.accept()
