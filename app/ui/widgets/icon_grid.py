"""图标选择网格（M13）：用固定列数的 ``QToolButton`` 网格替代 ``QListView(IconMode)``。

为什么换：``QListView`` 的 IconMode 在宽窗口下会把所有图标挤成一行并按 gridSize 裁切
（实测出现「图标上下被切、标签看不见、下方留一大片空白」）。网格按钮是确定性的：
列数固定、格子尺寸固定、选中态清晰，不需要滚动也永不裁切。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import QButtonGroup, QGridLayout, QToolButton, QWidget

from ...core.models import CourseIcon
from ..icon_render import course_icon_pixmap


class IconGrid(QWidget):
    """固定列数的课程图标选择网格。"""

    selected_changed = Signal(str)

    def __init__(self, *, columns: int = 6, icon_size: int = 44, parent=None) -> None:
        super().__init__(parent)
        self.columns = columns
        self.icon_size = icon_size
        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setSpacing(8)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QToolButton] = {}
        self._current = ""

    # --- 装载 -----------------------------------------------------------
    def set_icons(self, choices: list[tuple[str, str]], *, color: str = "#E86A92", bg: str = "#FFF5F8", current: str | None = None) -> None:
        """``choices`` 为 (id, 中文名) 列表；按 ``columns`` 排列成网格。"""
        self._clear()
        for index, (preset_id, label) in enumerate(choices):
            icon = CourseIcon(kind="preset", value=preset_id, color=color, bg=bg)
            button = QToolButton(self)
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            button.setIconSize(QSize(self.icon_size, self.icon_size))
            button.setIcon(course_icon_pixmap(Path("."), icon, size=self.icon_size + 20, padding=8))
            button.setText(label)
            button.setToolTip(f"{label}（{preset_id}）")
            button.setProperty("iconId", preset_id)
            button.setMinimumSize(QSize(76, 78))
            button.clicked.connect(lambda _checked=False, value=preset_id: self.select(value))
            self._group.addButton(button)
            self._grid.addWidget(button, index // self.columns, index % self.columns)
            self._buttons[preset_id] = button

        for column in range(self.columns):
            self._grid.setColumnStretch(column, 1)
        target = current if current in self._buttons else (choices[0][0] if choices else "")
        if target:
            self.select(target)

    def _clear(self) -> None:
        for button in self._buttons.values():
            self._group.removeButton(button)
            button.setParent(None)
            button.deleteLater()
        self._buttons.clear()
        self._current = ""

    # --- 选择 -----------------------------------------------------------
    def select(self, icon_id: str) -> None:
        if icon_id not in self._buttons:
            return
        self._buttons[icon_id].setChecked(True)
        self._current = icon_id
        self.selected_changed.emit(icon_id)

    def selected_id(self) -> str:
        return self._current

    def ids(self) -> list[str]:
        return list(self._buttons)

    def label_of(self, icon_id: str) -> str:
        button = self._buttons.get(icon_id)
        return button.text() if button is not None else icon_id

    def rows(self) -> int:
        if not self._buttons:
            return 0
        return (len(self._buttons) + self.columns - 1) // self.columns

    def height_hint(self) -> int:
        return max(1, self.rows()) * 86 + 4
