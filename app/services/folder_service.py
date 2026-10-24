"""课程库虚拟文件夹：多级树、课程归属与持久化。

课程目录始终平铺在 ``Courses/<课程名>/``。文件夹只改变课程库的组织视图，
元数据写到 ``Courses/.library/folders.json``，删索引库不会丢。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..core.paths import MAX_COMPONENT_LENGTH, atomic_write_json, read_json, safe_component

FOLDERS_FILE = "folders.json"
SCHEMA_VERSION = 1


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class FolderNode:
    id: str
    name: str
    parent_id: str | None = None
    sort_index: int = 0
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "parent_id": self.parent_id,
            "sort_index": int(self.sort_index),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FolderNode":
        return cls(
            id=str(data.get("id") or ""),
            name=str(data.get("name") or ""),
            parent_id=str(data["parent_id"]) if data.get("parent_id") else None,
            sort_index=int(data.get("sort_index") or 0),
            created_at=str(data.get("created_at") or ""),
        )


class FolderService:
    """文件系统支持的文件夹树。所有写操作立即原子落盘。"""

    def __init__(self, courses_dir: str | Path) -> None:
        self.courses_dir = Path(courses_dir)
        self.path = self.courses_dir / ".library" / FOLDERS_FILE
        self._folders: dict[str, FolderNode] = {}
        self._assignments: dict[str, str] = {}
        self._last_folder_id: str | None = None
        self.load()

    # --- 持久化 ---------------------------------------------------------
    def load(self) -> None:
        self._folders = {}
        self._assignments = {}
        self._last_folder_id = None
        try:
            payload = read_json(self.path, default={}) or {}
        except (OSError, ValueError):
            return
        if not isinstance(payload, dict):
            return
        for raw in payload.get("folders") or []:
            if not isinstance(raw, dict):
                continue
            node = FolderNode.from_dict(raw)
            if node.id and node.name:
                self._folders[node.id] = node
        assignments = payload.get("assignments")
        if isinstance(assignments, dict):
            self._assignments = {
                str(course_id): str(folder_id)
                for course_id, folder_id in assignments.items()
                if str(course_id) and str(folder_id) in self._folders
            }
        last = payload.get("last_folder_id")
        self._last_folder_id = str(last) if last and str(last) in self._folders else None

    def save(self) -> None:
        atomic_write_json(
            self.path,
            {
                "schema_version": SCHEMA_VERSION,
                "folders": [node.to_dict() for node in self._ordered_nodes()],
                "assignments": dict(sorted(self._assignments.items())),
                "last_folder_id": self._last_folder_id,
            },
        )

    # --- 查询 -----------------------------------------------------------
    def folders(self) -> list[FolderNode]:
        return list(self._ordered_nodes())

    def get(self, folder_id: str | None) -> FolderNode | None:
        return self._folders.get(str(folder_id)) if folder_id else None

    def children(self, parent_id: str | None) -> list[FolderNode]:
        return [
            node
            for node in self.folders()
            if node.parent_id == (str(parent_id) if parent_id else None)
        ]

    def path_of(self, folder_id: str | None) -> list[FolderNode]:
        node = self.get(folder_id)
        if node is None:
            return []
        chain: list[FolderNode] = [node]
        seen = {node.id}
        while node.parent_id:
            parent = self.get(node.parent_id)
            if parent is None or parent.id in seen:
                break
            chain.append(parent)
            seen.add(parent.id)
            node = parent
        return list(reversed(chain))

    def descendant_ids(self, folder_id: str) -> set[str]:
        found: set[str] = set()
        stack = [str(folder_id)]
        while stack:
            current = stack.pop()
            if current in found:
                continue
            found.add(current)
            stack.extend(node.id for node in self.children(current))
        return found

    # --- 变更 -----------------------------------------------------------
    def create(self, name: str, parent_id: str | None = None) -> FolderNode:
        parent = self._require_parent(parent_id)
        clean = self._unique_name(safe_component(name), parent.id if parent else None)
        node = FolderNode(
            id=f"f-{uuid.uuid4().hex[:12]}",
            name=clean,
            parent_id=parent.id if parent else None,
            sort_index=self._next_sort_index(parent.id if parent else None),
            created_at=_now_iso(),
        )
        self._folders[node.id] = node
        self.save()
        return node

    def rename(self, folder_id: str, name: str) -> FolderNode:
        node = self._require_folder(folder_id)
        node.name = self._unique_name(
            safe_component(name),
            node.parent_id,
            exclude_id=node.id,
        )
        self.save()
        return node

    def move(self, folder_id: str, new_parent_id: str | None) -> FolderNode:
        node = self._require_folder(folder_id)
        parent = self._require_parent(new_parent_id)
        target_parent = parent.id if parent else None
        if target_parent and target_parent in self.descendant_ids(node.id):
            raise ValueError("不能把文件夹移动到它自己或它的子文件夹里")
        node.parent_id = target_parent
        node.sort_index = self._next_sort_index(target_parent)
        self.save()
        return node

    def delete(self, folder_id: str) -> None:
        node = self._require_folder(folder_id)
        parent_id = node.parent_id
        next_index = self._next_sort_index(parent_id)
        for child in self.children(node.id):
            child.parent_id = parent_id
            child.sort_index = next_index
            next_index += 1
        for course_id, assigned in list(self._assignments.items()):
            if assigned == node.id:
                if parent_id:
                    self._assignments[course_id] = parent_id
                else:
                    self._assignments.pop(course_id, None)
        if self._last_folder_id == node.id:
            self._last_folder_id = parent_id
        self._folders.pop(node.id, None)
        self.save()

    def assign_course(self, course_id: str, folder_id: str | None) -> None:
        course_key = str(course_id or "").strip()
        if not course_key:
            raise ValueError("课程 id 不能为空")
        if folder_id:
            self._require_folder(folder_id)
            self._assignments[course_key] = str(folder_id)
        else:
            self._assignments.pop(course_key, None)
        self.save()

    def folder_of(self, course_id: str) -> str | None:
        folder_id = self._assignments.get(str(course_id or ""))
        return folder_id if folder_id in self._folders else None

    def remove_missing_courses(self, course_ids: set[str]) -> int:
        allowed = {str(item) for item in course_ids}
        stale = [course_id for course_id in self._assignments if course_id not in allowed]
        for course_id in stale:
            self._assignments.pop(course_id, None)
        if stale:
            self.save()
        return len(stale)

    def set_last_folder(self, folder_id: str | None) -> None:
        target = str(folder_id) if folder_id else None
        self._last_folder_id = target if target in self._folders else None
        self.save()

    def last_folder(self) -> str | None:
        return self._last_folder_id

    # --- 内部 -----------------------------------------------------------
    def _ordered_nodes(self) -> list[FolderNode]:
        return sorted(
            self._folders.values(),
            key=lambda node: (node.parent_id or "", int(node.sort_index), node.name.casefold()),
        )

    def _require_folder(self, folder_id: str) -> FolderNode:
        node = self._folders.get(str(folder_id or ""))
        if node is None:
            raise KeyError(f"文件夹不存在：{folder_id}")
        return node

    def _require_parent(self, parent_id: str | None) -> FolderNode | None:
        if not parent_id:
            return None
        return self._require_folder(parent_id)

    def _next_sort_index(self, parent_id: str | None) -> int:
        siblings = self.children(parent_id)
        return max((int(node.sort_index) for node in siblings), default=-1) + 1

    def _unique_name(
        self,
        name: str,
        parent_id: str | None,
        *,
        exclude_id: str | None = None,
    ) -> str:
        base = name
        used = {
            node.name.casefold()
            for node in self.children(parent_id)
            if node.id != exclude_id
        }
        if base.casefold() not in used:
            return base
        index = 2
        while self._suffix_name(base, index).casefold() in used:
            index += 1
        return self._suffix_name(base, index)

    @staticmethod
    def _suffix_name(base: str, index: int) -> str:
        suffix = f"-{index}"
        room = max(1, MAX_COMPONENT_LENGTH - len(suffix))
        return safe_component(f"{base[:room]}{suffix}")
