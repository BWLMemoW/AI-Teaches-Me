"""中间区的「章节资料」页：点左侧的章，就在这里给整章挑参考。

以前章级参考藏在工具栏的一个按钮里，点章只会收起/展开——主人要的是「点章就看见这一章用哪些资料」。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .materials_dialog import MATERIAL_FILTER, _ImportSignals, _ImportTask, _RefTable


class ChapterMaterialsPanel(QWidget):
    """一屏给整章选参考：勾选 + 页码范围 + 上传 + 保存。"""

    save_requested = Signal()

    def __init__(
        self,
        *,
        importer: Callable[[Sequence[str]], Any] | None = None,
        material_loader: Callable[[], list[dict]] | None = None,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self._importer = importer
        self._material_loader = material_loader
        self._pool = QThreadPool.globalInstance()
        self._signals = _ImportSignals()
        self._signals.done.connect(self._on_import_done)
        self._signals.failed.connect(self._on_import_failed)
        self._importing = False
        self._chapter_id = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        self.title_label = QLabel("章节资料")
        self.title_label.setProperty("role", "subtitle")
        layout.addWidget(self.title_label)

        hint = QLabel(
            "本章默认参考：没单独指定参考的小节都会用这些；某个小节自己指定了就听小节的。"
        )
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.table = _RefTable()
        layout.addWidget(self.table, 1)

        row = QHBoxLayout()
        self.select_all_button = QPushButton("全选")
        self.select_all_button.setProperty("role", "ghost")
        self.select_all_button.clicked.connect(lambda: self.table.set_all(True))
        row.addWidget(self.select_all_button)
        self.clear_button = QPushButton("清空")
        self.clear_button.setProperty("role", "ghost")
        self.clear_button.clicked.connect(lambda: self.table.set_all(False))
        row.addWidget(self.clear_button)
        row.addStretch(1)
        self.upload_button = QPushButton("上传资料…")
        self.upload_button.setToolTip("上传 PPT / PDF / 教材；导入后自动勾上")
        self.upload_button.clicked.connect(self._on_upload)
        self.upload_button.setEnabled(self._importer is not None)
        row.addWidget(self.upload_button)
        self.save_button = QPushButton("保存本章资料")
        self.save_button.setProperty("role", "primary")
        self.save_button.clicked.connect(self.save_requested.emit)
        row.addWidget(self.save_button)
        layout.addLayout(row)

        self.status_label = QLabel("")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

    # --- 装载 -----------------------------------------------------------
    def set_sources(
        self,
        *,
        importer: Callable[[Sequence[str]], Any] | None,
        material_loader: Callable[[], list[dict]] | None,
    ) -> None:
        """课程打开之后才知道课程目录，所以导入入口是晚绑定的。"""
        self._importer = importer
        self._material_loader = material_loader
        self.upload_button.setEnabled(importer is not None)

    def set_chapter(
        self,
        *,
        chapter_id: str,
        chapter_title: str,
        materials: Sequence[dict],
        refs: Sequence[str],
    ) -> None:
        self._chapter_id = str(chapter_id or "")
        self.title_label.setText(f"{chapter_id} {chapter_title} · 章节资料".strip())
        self.table.set_entries(materials, refs)
        if not materials:
            self.status_label.setText("这门课还没有资料：点「上传资料…」导入课件。")
        elif not refs:
            self.status_label.setText("还没有设置本章默认：勾选后点「保存本章资料」。")
        else:
            self.status_label.setText(f"本章默认已设 {len(list(refs))} 份。")

    @property
    def chapter_id(self) -> str:
        return self._chapter_id

    def chosen_refs(self) -> list[str]:
        return self.table.refs()

    def set_status(self, text: str) -> None:
        self.status_label.setText(str(text or ""))

    # --- 上传 -----------------------------------------------------------
    def _on_upload(self) -> None:
        if self._importer is None:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "选择课件 / 教材", "", MATERIAL_FILTER)
        if not files:
            return
        if (
            QMessageBox.question(
                self,
                "导入资料",
                "导入时会读取资料里的文字、表格与演讲者备注；资料中的知识图会一并识别"
                "（装饰图本地过滤，识别图片会产生模型调用费用）。要开始吗？",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self._importing = True
        self.upload_button.setEnabled(False)
        self.save_button.setEnabled(False)
        self.status_label.setText(f"正在导入 {len(files)} 份资料…（解析 + 识图，稍等一下）")
        self._pool.start(_ImportTask(self._importer, files, self._signals))

    @Slot(object)
    def _on_import_done(self, result: object) -> None:
        materials = list(getattr(result, "materials", []) or [])
        parsed = [item for item in materials if str(getattr(item, "status", "")) == "parsed"]
        figures = sum(int(getattr(item, "vision_called", 0) or 0) for item in materials)
        note = f"已导入 {len(parsed)} 份资料"
        if figures:
            note += f"（识别资料图 {figures} 张）"
        if len(materials) - len(parsed):
            note += f"；{len(materials) - len(parsed)} 份没解析成功"
        fresh = None
        if self._material_loader is not None:
            try:
                fresh = list(self._material_loader())
            except Exception:  # noqa: BLE001 - 清单读不出来就维持原样
                fresh = None
        refs = sorted(set(self.chosen_refs()) | {str(getattr(item, "material_id", "") or "") for item in parsed})
        refs = [item for item in refs if item]
        if fresh is not None:
            self.table.set_entries(fresh, refs)
        self.status_label.setText(note + "，记得点「保存本章资料」。")
        self._importing = False
        self.upload_button.setEnabled(True)
        self.save_button.setEnabled(True)

    @Slot(str)
    def _on_import_failed(self, message: str) -> None:
        self.status_label.setText(f"导入失败：{message}")
        self._importing = False
        self.upload_button.setEnabled(True)
        self.save_button.setEnabled(True)
