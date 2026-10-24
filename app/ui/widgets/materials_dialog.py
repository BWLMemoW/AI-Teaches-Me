"""参考资料设置窗口：一张页面同时管「本节」和「本章默认」。

以前的毛病（主人原话：很反人类）：

* 有两个窗口做同一件事——「本节参考」按钮一个、点「生成讲义」又弹一个，能力还不一样；
* 「作用范围」是个隐藏模式，切成「本章」之后表格长得一模一样，看不出在改哪一层。

现在改成一屏两块、各自独立：谁是谁一眼分明，也不用记"我现在在编辑哪一层"。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Qt, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

#: 与建课时一致的可导入格式
MATERIAL_FILTER = "资料 (*.pdf *.pptx *.ppt *.docx *.txt *.md *.png *.jpg *.jpeg *.webp)"


def _locator_kind(kind: str) -> str:
    """PDF 用 ``p``，PPT 用 ``s``，其余类型不支持范围（只能整份引用）。"""
    value = str(kind or "").lower()
    if value == "pdf":
        return "p"
    if value in ("pptx", "ppt"):
        return "s"
    return ""


def _parse_range(text: str) -> tuple[int | None, int | None]:
    """``3-8`` / ``3`` / ``3-`` / ``空`` → ``(start, end)``；读不懂返回 ``(None, None)``。"""
    raw = str(text or "").strip().replace("－", "-").replace("—", "-")
    if not raw:
        return None, None
    if "-" in raw:
        head, _, tail = raw.partition("-")
        head, tail = head.strip(), tail.strip()
        if not head.isdigit():
            return None, None
        return int(head), (int(tail) if tail.isdigit() else None)
    if raw.isdigit():
        return int(raw), None
    return None, None


def _split_refs(refs: Sequence[str]) -> dict[str, str]:
    """``["m-a#p3-8"]`` → ``{"m-a": "3-8"}``（整份引用记为空串）。"""
    found: dict[str, str] = {}
    for raw in refs or []:
        text = str(raw or "").strip()
        if not text:
            continue
        identifier, _, locator = text.partition("#")
        if not locator:
            found.setdefault(identifier.strip(), "")
            continue
        found[identifier.strip()] = "".join(ch for ch in locator if ch.isdigit() or ch == "-")
    return found


class _ImportSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class _ImportTask(QRunnable):
    """在工作线程里跑资料导入（解析 + 识图很慢，不能卡住界面）。"""

    def __init__(self, runner: Callable[[Sequence[str]], Any], files: Sequence[str], signals: _ImportSignals) -> None:
        super().__init__()
        self._runner = runner
        self._files = list(files)
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            result = self._runner(self._files)
        except Exception as exc:  # noqa: BLE001 - 导入失败要报给用户，不能静默
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(result)


class _RefTable(QTableWidget):
    """一小块「资料 → 勾选 + 页码范围」。每份资料一行，勾了才算引用。"""

    def __init__(self, parent: Any = None) -> None:
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["参考", "资料", "页码范围（留空 = 整份）"])
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.verticalHeader().setVisible(False)
        self._materials: list[dict] = []

    def set_entries(self, materials: Sequence[dict], refs: Sequence[str]) -> None:
        """重排表格；已勾选的排前面，避免资料多了要滚动找。"""
        self._materials = [dict(item) for item in materials]
        chosen = _split_refs(refs)
        self.setRowCount(0)
        ordered = sorted(
            self._materials,
            key=lambda item: (0 if str(item.get("id") or "") in chosen else 1, str(item.get("filename") or "")),
        )
        for entry in ordered:
            row = self.rowCount()
            self.insertRow(row)
            identifier = str(entry.get("id") or "")
            kind = str(entry.get("kind") or "")
            check = QTableWidgetItem(f"{entry.get('filename', '')}（{kind}）")
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if identifier in chosen else Qt.CheckState.Unchecked)
            check.setData(Qt.ItemDataRole.UserRole, identifier)
            self.setItem(row, 0, check)

            info = QTableWidgetItem(f"{entry.get('pages') or 0} 页")
            info.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.setItem(row, 1, info)

            editor = QLineEdit(chosen.get(identifier, ""))
            editor.setPlaceholderText("如 3-8" if _locator_kind(kind) else "整份")
            editor.setEnabled(bool(_locator_kind(kind)))
            self.setCellWidget(row, 2, editor)
        self.resizeRowsToContents()

    def set_all(self, value: bool) -> None:
        state = Qt.CheckState.Checked if value else Qt.CheckState.Unchecked
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is not None:
                item.setCheckState(state)

    def check_only(self, identifiers: Sequence[str]) -> None:
        wanted = {str(value) for value in identifiers}
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is None:
                continue
            identifier = str(item.data(Qt.ItemDataRole.UserRole) or "")
            item.setCheckState(Qt.CheckState.Checked if identifier in wanted else Qt.CheckState.Unchecked)

    def refs(self) -> list[str]:
        """把这张表读回 ``source_refs``。"""
        result: list[str] = []
        kinds = {str(item.get("id") or ""): _locator_kind(str(item.get("kind") or "")) for item in self._materials}
        for row in range(self.rowCount()):
            item = self.item(row, 0)
            if item is None or item.checkState() != Qt.CheckState.Checked:
                continue
            identifier = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not identifier:
                continue
            editor = self.cellWidget(row, 2)
            start, end = _parse_range(editor.text() if isinstance(editor, QLineEdit) else "")
            prefix = kinds.get(identifier, "")
            if start is None or not prefix:
                result.append(identifier)  # 不支持范围 / 没填范围 = 整份
            elif end is None or end == start:
                result.append(f"{identifier}#{prefix}{start}")
            else:
                result.append(f"{identifier}#{prefix}{min(start, end)}-{max(start, end)}")
        return result


class MaterialsDialog(QDialog):
    """本节参考 + 本章默认，一屏两块。"""

    def __init__(
        self,
        *,
        materials: Sequence[dict],
        lesson_refs: Sequence[str] = (),
        chapter_refs: Sequence[str] = (),
        lesson_label: str = "",
        chapter_label: str = "",
        importer: Callable[[Sequence[str]], Any] | None = None,
        material_loader: Callable[[], list[dict]] | None = None,
        ok_text: str = "保存",
        show_skip: bool = False,
        skip_prompt: bool = False,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("参考资料")
        self.resize(760, 660)
        self._materials = [dict(item) for item in materials]
        self._lesson_refs = [str(item) for item in lesson_refs]
        self._chapter_refs = [str(item) for item in chapter_refs]
        self._importer = importer
        self._material_loader = material_loader
        self._pool = QThreadPool.globalInstance()
        self._signals = _ImportSignals()
        self._signals.done.connect(self._on_import_done)
        self._signals.failed.connect(self._on_import_failed)
        self._pending_ids: set[str] = set()
        self._importing = False

        layout = QVBoxLayout(self)

        caption = QLabel("这一节依据什么来讲？勾选的资料会作为讲义的主要依据，术语与例子尽量保持一致。")
        caption.setWordWrap(True)
        layout.addWidget(caption)

        # --- 本节 ---
        lesson_head = QLabel(f"本节参考　{lesson_label}".strip())
        lesson_head.setProperty("role", "subtitle")
        layout.addWidget(lesson_head)
        lesson_hint = QLabel("只对这一节生效，优先级最高。不勾 = 本节不单独指定，往下继承「本章默认」。")
        lesson_hint.setProperty("role", "muted")
        lesson_hint.setWordWrap(True)
        layout.addWidget(lesson_hint)
        self.lesson_table = _RefTable()
        layout.addWidget(self.lesson_table, 3)
        layout.addLayout(self._table_actions(self.lesson_table))

        # --- 本章 ---
        chapter_head = QLabel(f"本章默认　{chapter_label}".strip())
        chapter_head.setProperty("role", "subtitle")
        layout.addWidget(chapter_head)
        chapter_hint = QLabel("没单独指定参考的小节都会用这些；小节自己指定了就听小节的。")
        chapter_hint.setProperty("role", "muted")
        chapter_hint.setWordWrap(True)
        layout.addWidget(chapter_hint)
        self.chapter_table = _RefTable()
        layout.addWidget(self.chapter_table, 2)
        layout.addLayout(self._table_actions(self.chapter_table))

        # --- 上传 / 状态 ---
        upload_row = QHBoxLayout()
        self.upload_button = QPushButton("上传资料…")
        self.upload_button.setToolTip("上传 PPT / PDF / 教材；导入后自动勾进「本节参考」")
        self.upload_button.clicked.connect(self._on_upload)
        self.upload_button.setEnabled(self._importer is not None)
        upload_row.addWidget(self.upload_button)
        upload_row.addStretch(1)
        layout.addLayout(upload_row)

        self.status_label = QLabel("" if self._materials else "这门课还没有资料：点「上传资料…」导入课件。")
        self.status_label.setProperty("role", "muted")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        if show_skip:
            self.skip_check = QCheckBox("以后不再显示这个窗口，点「生成讲义」直接开始")
            self.skip_check.setChecked(bool(skip_prompt))
            layout.addWidget(self.skip_check)
        else:
            self.skip_check = None

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_button.setText(ok_text)
        self.ok_button.setProperty("role", "primary")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_tables()

    @staticmethod
    def _table_actions(table: _RefTable) -> QHBoxLayout:
        row = QHBoxLayout()
        all_button = QPushButton("全选")
        all_button.setProperty("role", "ghost")
        all_button.clicked.connect(lambda: table.set_all(True))
        row.addWidget(all_button)
        clear_button = QPushButton("清空")
        clear_button.setProperty("role", "ghost")
        clear_button.clicked.connect(lambda: table.set_all(False))
        row.addWidget(clear_button)
        row.addStretch(1)
        return row

    def _refresh_tables(self) -> None:
        self.lesson_table.set_entries(self._materials, self._lesson_refs)
        self.chapter_table.set_entries(self._materials, self._chapter_refs)

    # --- 上传资料 -------------------------------------------------------
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
        self.ok_button.setEnabled(False)
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

        if self._material_loader is not None:
            try:
                self._materials = [dict(item) for item in self._material_loader()]
            except Exception:  # noqa: BLE001 - 清单读不出来就用旧列表
                pass
        else:
            existing = {str(item.get("id") or "") for item in self._materials}
            for item in parsed:
                identifier = str(getattr(item, "material_id", "") or "")
                if identifier and identifier not in existing:
                    self._materials.append(
                        {
                            "id": identifier,
                            "filename": str(getattr(item, "filename", "") or ""),
                            "kind": str(getattr(item, "kind", "") or ""),
                            "pages": int(getattr(item, "pages", 0) or 0),
                        }
                    )
        self._pending_ids = {str(getattr(item, "material_id", "") or "") for item in parsed}
        self._pending_ids.discard("")
        # 刚上传的资料自动勾进「本节参考」——多半就是为这一节找的
        self._lesson_refs = sorted(set(self._lesson_refs) | self._pending_ids)
        self._pending_ids = set()
        self._refresh_tables()
        self.status_label.setText(note + "，已自动勾进「本节参考」。")
        self._importing = False
        self.upload_button.setEnabled(True)
        self.ok_button.setEnabled(True)

    @Slot(str)
    def _on_import_failed(self, message: str) -> None:
        self.status_label.setText(f"导入失败：{message}")
        self._importing = False
        self.upload_button.setEnabled(True)
        self.ok_button.setEnabled(True)

    # 导入中不许关窗：否则线程回调会打到已经销毁的窗口上
    def reject(self) -> None:
        if self._importing:
            return
        super().reject()

    def accept(self) -> None:
        if self._importing:
            return
        super().accept()

    # --- 结果 -----------------------------------------------------------
    def lesson_refs(self) -> list[str]:
        return self.lesson_table.refs()

    def chapter_refs(self) -> list[str]:
        return self.chapter_table.refs()

    def skip_next_time(self) -> bool:
        return bool(self.skip_check is not None and self.skip_check.isChecked())
