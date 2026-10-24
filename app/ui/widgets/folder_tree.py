"""课程库文件夹树：资源管理器式选择、拖拽与右键菜单。"""

from __future__ import annotations

from PySide6.QtCore import QMimeData, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (
    QAbstractItemView,
    QMenu,
    QStyle,
    QTreeWidget,
    QTreeWidgetItem,
)

from ...services.folder_service import FolderNode, FolderService
from ...services.library_service import UNFILED_FOLDER_ID

ALL_FOLDERS_ID = "__all_folders__"
COURSE_MIME = "application/x-atm-course-id"
FOLDER_MIME = "application/x-atm-folder-id"


class FolderTree(QTreeWidget):
    """左侧文件夹树。特殊节点：全部课程 / 未分类。"""

    folder_selected = Signal(str)
    new_folder_requested = Signal(object)
    rename_requested = Signal(str)
    delete_requested = Signal(str)
    course_dropped = Signal(str, object)
    folder_dropped = Signal(str, object)

    FOLDER_ROLE = int(Qt.ItemDataRole.UserRole)
    KIND_ROLE = FOLDER_ROLE + 1

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setColumnCount(2)
        self.setColumnWidth(0, 168)
        self.setColumnWidth(1, 32)
        self.setIndentation(14)
        self.setRootIsDecorated(True)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._open_menu)
        self.currentItemChanged.connect(lambda *_: self.folder_selected.emit(self.current_folder_id()))
        self._items: dict[str, QTreeWidgetItem] = {}

    # --- 构建 -----------------------------------------------------------
    def build(
        self,
        service: FolderService,
        *,
        all_count: int,
        total_counts: dict[str, int],
        unfiled_count: int,
        current_id: str,
    ) -> None:
        self.blockSignals(True)
        self.clear()
        self._items = {}
        all_item = QTreeWidgetItem(["全部课程", str(int(all_count))])
        all_item.setData(0, self.FOLDER_ROLE, ALL_FOLDERS_ID)
        all_item.setData(0, self.KIND_ROLE, "all")
        all_item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))
        all_item.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled)
        self.addTopLevelItem(all_item)
        self._items[ALL_FOLDERS_ID] = all_item

        unfiled_item = QTreeWidgetItem(["未分类", str(unfiled_count)])
        unfiled_item.setData(0, self.FOLDER_ROLE, UNFILED_FOLDER_ID)
        unfiled_item.setData(0, self.KIND_ROLE, "unfiled")
        unfiled_item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogContentsView))
        unfiled_item.setFlags(
            Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsDropEnabled
        )
        self.addTopLevelItem(unfiled_item)
        self._items[UNFILED_FOLDER_ID] = unfiled_item

        for node in service.children(None):
            self._add_node(self, node, service, total_counts)
        self.expandAll()
        self.setCurrentItem(self._items.get(current_id) or all_item)
        self.blockSignals(False)

    def _add_node(
        self,
        parent,
        node: FolderNode,
        service: FolderService,
        total_counts: dict[str, int],
    ) -> None:
        count = int(total_counts.get(node.id, 0))
        item = QTreeWidgetItem(parent, [node.name, str(count)])
        item.setData(0, self.FOLDER_ROLE, node.id)
        item.setData(0, self.KIND_ROLE, "folder")
        item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon))
        item.setToolTip(0, f"{node.name}\n共 {count} 门课程（含子文件夹）")
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsSelectable
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        self._items[node.id] = item
        for child in service.children(node.id):
            self._add_node(item, child, service, total_counts)

    # --- 查询 -----------------------------------------------------------
    def current_folder_id(self) -> str:
        item = self.currentItem()
        if item is None:
            return ALL_FOLDERS_ID
        return str(item.data(0, self.FOLDER_ROLE) or ALL_FOLDERS_ID)

    def current_folder_node_id(self) -> str | None:
        item = self.currentItem()
        if item is None or item.data(0, self.KIND_ROLE) != "folder":
            return None
        return str(item.data(0, self.FOLDER_ROLE) or "")

    def select_folder(self, folder_id: str) -> None:
        item = self._items.get(folder_id)
        if item is not None:
            self.setCurrentItem(item)

    # --- 右键菜单 -------------------------------------------------------
    def _open_menu(self, pos) -> None:  # type: ignore[no-untyped-def]
        item = self.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            menu.addAction("新建文件夹").triggered.connect(
                lambda: self.new_folder_requested.emit(None)
            )
        elif item.data(0, self.KIND_ROLE) == "folder":
            folder_id = str(item.data(0, self.FOLDER_ROLE))
            menu.addAction("新建子文件夹").triggered.connect(
                lambda: self.new_folder_requested.emit(folder_id)
            )
            menu.addAction("重命名…").triggered.connect(
                lambda: self.rename_requested.emit(folder_id)
            )
            menu.addSeparator()
            menu.addAction("删除文件夹…").triggered.connect(
                lambda: self.delete_requested.emit(folder_id)
            )
        else:
            menu.addAction("新建文件夹").triggered.connect(
                lambda: self.new_folder_requested.emit(None)
            )
        menu.exec(self.viewport().mapToGlobal(pos))

    # --- 拖拽 -----------------------------------------------------------
    def startDrag(self, supported_actions) -> None:  # type: ignore[no-untyped-def]
        item = self.currentItem()
        if item is None or item.data(0, self.KIND_ROLE) != "folder":
            return
        folder_id = str(item.data(0, self.FOLDER_ROLE))
        mime = QMimeData()
        mime.setData(FOLDER_MIME, folder_id.encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME) or event.mimeData().hasFormat(FOLDER_MIME):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        if event.mimeData().hasFormat(COURSE_MIME) or event.mimeData().hasFormat(FOLDER_MIME):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dropEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        mime = event.mimeData()
        item = self.itemAt(event.position().toPoint())
        target: str | None = None
        if item is not None and item.data(0, self.KIND_ROLE) == "folder":
            target = str(item.data(0, self.FOLDER_ROLE))
        if mime.hasFormat(COURSE_MIME):
            for course_id in bytes(mime.data(COURSE_MIME)).decode("utf-8").splitlines():
                if course_id.strip():
                    self.course_dropped.emit(course_id.strip(), target)
            event.acceptProposedAction()
            return
        if mime.hasFormat(FOLDER_MIME):
            folder_id = bytes(mime.data(FOLDER_MIME)).decode("utf-8").strip()
            if folder_id:
                self.folder_dropped.emit(folder_id, target)
            event.acceptProposedAction()
            return
        super().dropEvent(event)

    # --- 键盘 -----------------------------------------------------------
    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 命名
        key = event.key()
        modifiers = event.modifiers()
        folder_id = self.current_folder_node_id()
        if key == Qt.Key.Key_F2 and folder_id:
            self.rename_requested.emit(folder_id)
            event.accept()
            return
        if key == Qt.Key.Key_Delete and folder_id:
            self.delete_requested.emit(folder_id)
            event.accept()
            return
        if (
            key == Qt.Key.Key_N
            and modifiers & Qt.KeyboardModifier.ControlModifier
            and modifiers & Qt.KeyboardModifier.ShiftModifier
        ):
            self.new_folder_requested.emit(folder_id)
            event.accept()
            return
        super().keyPressEvent(event)
