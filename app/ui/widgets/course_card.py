"""课程卡片（《项目文档》§11.4 ``CourseCard``、§11.3 课程库线框图）。

排版要求（M9）：**标题与元信息分行、分级配色**——
标题最大最粗用正文色；「N 章 · M 小节」用次级色；「最近学习」用弱化色；
进度单独一行（细进度条 + 百分比强调色）；标签用彩色胶囊（见 tag_chip.py）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from ...core.models import CourseIcon
from .tag_chip import build_tag_row
from ..theme.presets import PRESETS


@dataclass(frozen=True)
class CardStyle:
    """卡片用到的主题色（从 ColorTokens 里挑，保证跟主题一致）。"""

    bg: str = "#FFFFFF"
    border: str = "#EADCE3"
    text: str = "#2B2430"
    text_dim: str = "#5C5566"
    text_muted: str = "#8A8394"
    primary: str = "#E86A92"
    surface_alt: str = "#F7F2F4"

    @classmethod
    def from_theme(cls, key: str = "sakura", mode: str = "light") -> "CardStyle":
        theme = PRESETS.get(key, PRESETS["sakura"])
        colors = theme.colors(mode)
        return cls(
            bg=colors.surface,
            border=colors.border,
            text=colors.text,
            text_dim=colors.text_dim,
            text_muted=colors.text_muted,
            primary=colors.primary,
            surface_alt=colors.surface_alt,
        )


def human_time(progress: dict) -> str:
    """把 progress.json 的 updated_at 变成「2 小时前 / 昨天 / 未开始」。"""
    stamp = str((progress or {}).get("updated_at") or "")
    if not stamp:
        return "尚未开始"
    try:
        when = datetime.fromisoformat(stamp)
    except ValueError:
        return "尚未开始"
    delta = datetime.now().astimezone() - when
    seconds = delta.total_seconds()
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    days = int(seconds // 86400)
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days} 天前"
    return when.strftime("%Y-%m-%d")


class CourseCard(QFrame):
    """一张课程卡片。"""

    activated = Signal(object)
    context_requested = Signal(object, object)  # (summary, 全局坐标)

    CARD_WIDTH = 244

    def __init__(
        self,
        summary,
        *,
        icon_pixmap: QPixmap | None = None,
        style: CardStyle | None = None,
        progress: dict | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.summary = summary
        self.style_tokens = style or CardStyle()
        self._progress = progress or {}
        self.setFixedWidth(self.CARD_WIDTH)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setObjectName("CourseCard")
        self._apply_style(selected=False)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(10)
        self.icon_label = QLabel()
        self.icon_label.setFixedSize(56, 56)
        if icon_pixmap is not None and not icon_pixmap.isNull():
            self.icon_label.setPixmap(icon_pixmap.scaled(56, 56, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        top.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        title_box = QVBoxLayout()
        title_box.setSpacing(2)
        self.title_label = QLabel(summary.name)
        self.title_label.setWordWrap(True)
        self.title_label.setStyleSheet(f"color: {self.style_tokens.text}; font-size: 15px; font-weight: 600;")
        title_box.addWidget(self.title_label)

        badges = []
        if getattr(summary, "seed", False):
            badges.append("示例")
        if getattr(summary, "pinned", False):
            badges.append("置顶")
        self.badge_label = QLabel("  ·  ".join(badges))
        self.badge_label.setStyleSheet(f"color: {self.style_tokens.primary}; font-size: 11px;")
        self.badge_label.setVisible(bool(badges))
        title_box.addWidget(self.badge_label)
        title_box.addStretch(1)
        top.addLayout(title_box, 1)
        layout.addLayout(top)

        self.meta_label = QLabel(f"{summary.chapters} 章 · {summary.lessons} 小节")
        self.meta_label.setStyleSheet(f"color: {self.style_tokens.text_dim}; font-size: 12px;")
        layout.addWidget(self.meta_label)

        self.time_label = QLabel(f"最近学习：{human_time(self._progress)}")
        self.time_label.setStyleSheet(f"color: {self.style_tokens.text_muted}; font-size: 11px;")
        layout.addWidget(self.time_label)

        progress_row = QHBoxLayout()
        progress_row.setSpacing(8)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(int(summary.progress_ratio * 100))
        self.progress_bar.setTextVisible(False)
        self.progress_bar.setFixedHeight(6)
        progress_row.addWidget(self.progress_bar, 1)
        self.percent_label = QLabel(f"{int(summary.progress_ratio * 100)}%")
        self.percent_label.setStyleSheet(f"color: {self.style_tokens.primary}; font-size: 12px; font-weight: 600;")
        progress_row.addWidget(self.percent_label)
        layout.addLayout(progress_row)

        if summary.tags:
            chip_row = QHBoxLayout()
            chip_row.setSpacing(4)
            for chip in build_tag_row(list(summary.tags)):
                chip_row.addWidget(chip)
            chip_row.addStretch(1)
            layout.addLayout(chip_row)

        if summary.broken:
            broken = QLabel("需要修复：" + "；".join(summary.issues))
            broken.setWordWrap(True)
            broken.setStyleSheet("color: #9B3A2E; font-size: 11px;")
            layout.addWidget(broken)

        self.setToolTip(f"{summary.name}\n{summary.description or '（无描述）'}")

    # --- 样式 -----------------------------------------------------------
    def _apply_style(self, *, selected: bool) -> None:
        border = self.style_tokens.primary if selected else self.style_tokens.border
        width = 2 if selected else 1
        self.setStyleSheet(
            f"#CourseCard {{ background-color: {self.style_tokens.bg};"
            f" border: {width}px solid {border}; border-radius: 16px; }}"
            f"#CourseCard:hover {{ border-color: {self.style_tokens.primary};"
            f" background-color: {self.style_tokens.surface_alt}; }}"
            "QProgressBar { background-color: #00000000; border: none; }"
            f"QProgressBar::chunk {{ background-color: {self.style_tokens.primary}; border-radius: 3px; }}"
        )

    def set_selected(self, selected: bool) -> None:
        self._apply_style(selected=selected)

    # --- 交互 -----------------------------------------------------------
    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.activated.emit(self.summary)
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        self.context_requested.emit(self.summary, event.globalPos())
        event.accept()


def load_progress(course_dir: str | Path) -> dict:
    from ...core.paths import read_json

    return read_json(Path(course_dir) / "progress.json", default={}) or {}


def icon_for(course_dir: str | Path) -> CourseIcon:
    from ...services.icon_service import IconService

    return IconService.load(course_dir)
