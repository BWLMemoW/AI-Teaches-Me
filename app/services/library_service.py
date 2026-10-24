"""课程库服务：检索 / 筛选 / 排序 / 标签 / 智能视图（《项目文档》§4.17、AC-42~AC-58）。

文件扫描结果与 SQLite 索引在这里合流；标签与智能视图除写库外还落盘
``Courses/.library/``（§3.7 可重建性规则 2：删库不丢）。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.paths import atomic_write_json, read_json, read_text
from ..infra.sqlite_index import SqliteIndex
from . import course_service
from .folder_service import FolderService

LIBRARY_DIR_NAME = ".library"
TAGS_FILE = "tags.json"
VIEWS_FILE = "saved_views.json"
UNFILED_FOLDER_ID = "__unfiled__"

SORT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("recent", "最近学习"),
    ("created", "创建时间"),
    ("name", "名称"),
    ("progress", "进度"),
    ("manual", "手动排序"),
)
VIEW_MODES: tuple[tuple[str, str], ...] = (
    ("all", "全部"),
    ("in_progress", "进行中"),
    ("review", "待复习"),
    ("favorite", "已收藏"),
    ("completed", "已完成"),
)


@dataclass(frozen=True)
class LibraryFilter:
    """一次筛选（可另存为智能视图）。"""

    query: str = ""
    view: str = "all"
    tags: tuple[str, ...] = ()
    tag_mode: str = "any"  # any=并集（默认）/ all=交集
    sort: str = "recent"
    persona: str | None = None
    has_material: bool | None = None
    #: None=全部课程；UNFILED_FOLDER_ID=未分类；其它值=某个虚拟文件夹
    folder_id: str | None = None
    #: 搜索时通常为 True，浏览文件夹时只看直属课程
    include_descendants: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "query": self.query,
            "view": self.view,
            "tags": list(self.tags),
            "tag_mode": self.tag_mode,
            "sort": self.sort,
            "persona": self.persona,
            "has_material": self.has_material,
            "folder_id": self.folder_id,
            "include_descendants": self.include_descendants,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "LibraryFilter":
        return cls(
            query=str(data.get("query") or ""),
            view=str(data.get("view") or "all"),
            tags=tuple(str(tag) for tag in (data.get("tags") or [])),  # type: ignore[arg-type]
            tag_mode=str(data.get("tag_mode") or "any"),
            sort=str(data.get("sort") or "recent"),
            persona=data.get("persona") or None,  # type: ignore[arg-type]
            has_material=data.get("has_material") if isinstance(data.get("has_material"), bool) else None,
            folder_id=data.get("folder_id") or None,  # type: ignore[arg-type]
            include_descendants=bool(data.get("include_descendants") or False),
        )


class LibraryService:
    def __init__(
        self,
        *,
        courses_dir: str | Path,
        index: SqliteIndex | None = None,
        folders: FolderService | None = None,
    ) -> None:
        self.courses_dir = Path(courses_dir)
        self.index = index
        self.folders = folders or FolderService(self.courses_dir)

    # --- 落盘 / 装载（删库不丢） ----------------------------------------
    @property
    def library_dir(self) -> Path:
        return self.courses_dir / LIBRARY_DIR_NAME

    def persist_state(self) -> None:
        if self.index is None:
            return
        tags = self.index.list_tags()
        views = self.index.list_views()
        atomic_write_json(self.library_dir / TAGS_FILE, tags)
        atomic_write_json(
            self.library_dir / VIEWS_FILE,
            [{"name": view["name"], "query": view["query"]} for view in views],
        )

    def load_state(self) -> int:
        """索引库重建后，把落盘的标签与智能视图回灌（§3.7）。"""
        if self.index is None:
            return 0
        restored = 0
        tags = read_json(self.library_dir / TAGS_FILE, default=[]) or []
        for tag in tags:
            if isinstance(tag, dict) and tag.get("name"):
                self.index.add_tag(str(tag["name"]), tag.get("color"))
                restored += 1
        views = read_json(self.library_dir / VIEWS_FILE, default=[]) or []
        for view in views:
            if isinstance(view, dict) and view.get("name"):
                self.index.save_view(str(view["name"]), dict(view.get("query") or {}))
                restored += 1
        return restored

    # --- 扫描 / 检索 ----------------------------------------------------
    def refresh(self) -> list[course_service.CourseSummary]:
        summaries = course_service.scan_courses(self.courses_dir)
        self.folders.remove_missing_courses({summary.id for summary in summaries})
        if self.index is not None:
            self.index.rebuild_from_courses(summaries)
            if not self.index.list_tags():
                self.load_state()
            self.reindex_materials(summaries)
        merged: list[course_service.CourseSummary] = []
        for summary in summaries:
            folder_id = self.folders.folder_of(summary.id) or ""
            tag_names = (
                tuple(tag["name"] for tag in self.index.tags_for_course(summary.id))
                if self.index is not None
                else ()
            )
            changes: dict[str, object] = {}
            if folder_id:
                changes["folder_id"] = folder_id
            if tag_names:
                changes["tags"] = tag_names
            merged.append(replace(summary, **changes) if changes else summary)
        return merged

    def reindex_materials(self, summaries: Sequence[course_service.CourseSummary]) -> int:
        """按课程目录补建资料分块（``library.db`` 删掉后的自愈路径，§3.7）。"""
        from .material_service import MaterialService

        restored = 0
        for summary in summaries:
            try:
                restored += MaterialService.reindex_course(
                    summary.path, course_id=summary.id, index=self.index
                )
            except Exception:  # noqa: BLE001 - 资料重建失败不该影响课程库刷新
                continue
        return restored

    def search(
        self,
        filter_: LibraryFilter | None = None,
        *,
        summaries: Sequence[course_service.CourseSummary] | None = None,
    ) -> list[course_service.CourseSummary]:
        """搜索覆盖：课程名 / 描述 / 标签 / 章节标题 / 资料名（AC-44）。"""
        active = filter_ or LibraryFilter()
        items = list(summaries) if summaries is not None else self.refresh()

        if active.folder_id == UNFILED_FOLDER_ID:
            items = [item for item in items if not self.folders.folder_of(item.id)]
        elif active.folder_id:
            allowed = {active.folder_id}
            if active.include_descendants:
                allowed |= self.folders.descendant_ids(active.folder_id)
            items = [item for item in items if self.folders.folder_of(item.id) in allowed]

        if active.tags and self.index is not None:
            tag_ids = [
                int(tag["id"])
                for tag in self.index.list_tags()
                if str(tag["name"]) in set(active.tags)
            ]
            matched = (
                self.index.courses_with_all_tags(tag_ids)
                if active.tag_mode == "all"
                else self.index.courses_with_any_tags(tag_ids)
            )
            allowed = set(matched)
            items = [item for item in items if item.id in allowed]

        if active.persona:
            items = [item for item in items if (item.persona or "") == active.persona]

        if active.has_material is not None:
            items = [
                item
                for item in items
                if (item.path / "materials" / "materials.json").exists() is active.has_material
            ]

        if active.view == "in_progress":
            items = [item for item in items if 0 < item.completed_lessons < max(1, item.lessons)]
        elif active.view == "completed":
            items = [item for item in items if item.lessons > 0 and item.completed_lessons >= item.lessons]
        elif active.view == "review":
            items = [item for item in items if self._has_review_items(item.path)]
        elif active.view == "favorite":
            items = [item for item in items if self._has_favorite(item.path)]

        query = active.query.strip().lower()
        if query:
            items = [item for item in items if self._matches_query(item, query)]

        return self._sort(items, active.sort)

    # --- 标签 -----------------------------------------------------------
    def add_tag(self, course_id: str, name: str, color: str | None = None) -> int:
        if self.index is None:
            raise RuntimeError("需要索引库才能管理标签")
        tag_id = self.index.add_tag(name, color)
        existing = [int(tag["id"]) for tag in self.index.tags_for_course(course_id)]
        if tag_id not in existing:
            self.index.set_course_tags(course_id, [*existing, tag_id])
        self.persist_state()
        return tag_id

    def remove_tag(self, course_id: str, tag_id: int) -> None:
        if self.index is None:
            return
        remaining = [
            int(tag["id"]) for tag in self.index.tags_for_course(course_id) if int(tag["id"]) != int(tag_id)
        ]
        self.index.set_course_tags(course_id, remaining)
        self.persist_state()

    def rename_tag(self, tag_id: int, name: str) -> None:
        if self.index is not None:
            self.index.rename_tag(tag_id, name)
            self.persist_state()

    def merge_tags(self, source_id: int, target_id: int) -> None:
        if self.index is not None:
            self.index.merge_tags(source_id, target_id)
            self.persist_state()

    def delete_tag(self, tag_id: int) -> None:
        """删除标签只解除关联，不删课程（AC-54）。"""
        if self.index is not None:
            self.index.delete_tag(tag_id)
            self.persist_state()

    def prune_unused_tags(self) -> int:
        if self.index is None:
            return 0
        removed = self.index.prune_unused_tags()
        self.persist_state()
        return removed

    def tags(self) -> list[dict[str, object]]:
        return self.index.list_tags() if self.index is not None else []

    # --- 智能视图 -------------------------------------------------------
    def save_view(self, name: str, filter_: LibraryFilter) -> int:
        if self.index is None:
            raise RuntimeError("需要索引库才能保存智能视图")
        view_id = self.index.save_view(name, filter_.to_dict())
        self.persist_state()
        return view_id

    def views(self) -> list[dict[str, object]]:
        return self.index.list_views() if self.index is not None else []

    def delete_view(self, view_id: int) -> None:
        if self.index is not None:
            self.index.delete_view(view_id)
            self.persist_state()

    # --- 内部 -----------------------------------------------------------
    @staticmethod
    def _matches_query(item: course_service.CourseSummary, query: str) -> bool:
        haystack = [item.name.lower(), item.description.lower(), " ".join(item.tags).lower()]
        outline_path = item.path / "outline.json"
        if outline_path.exists():
            try:
                outline = json.loads(read_text(outline_path))
            except ValueError:
                outline = {}
            for chapter in outline.get("chapters", []) or []:
                haystack.append(str(chapter.get("title", "")).lower())
                for lesson in chapter.get("lessons", []) or []:
                    haystack.append(str(lesson.get("title", "")).lower())
        materials = item.path / "materials" / "materials.json"
        if materials.exists():
            try:
                payload = json.loads(read_text(materials))
            except ValueError:
                payload = {}
            for entry in (payload.get("materials") or []) if isinstance(payload, dict) else []:
                haystack.append(str(entry.get("filename", "")).lower())
        return any(query in field for field in haystack)

    @staticmethod
    def _has_review_items(course_dir: Path) -> bool:
        progress = read_json(course_dir / "progress.json", default={}) or {}
        return any(not item.get("excluded") for item in progress.get("review_queue", []) or [])

    @staticmethod
    def _has_favorite(course_dir: Path) -> bool:
        chapters = course_dir / "chapters"
        if not chapters.is_dir():
            return False
        for lesson in chapters.glob("*/lessons/*/lesson.json"):
            try:
                data = json.loads(read_text(lesson))
            except ValueError:
                continue
            if data.get("favorite"):
                return True
        return False

    @staticmethod
    def _sort(items: Sequence[course_service.CourseSummary], sort: str) -> list[course_service.CourseSummary]:
        if sort == "name":
            return sorted(items, key=lambda item: item.name)
        if sort == "progress":
            return sorted(items, key=lambda item: (-item.progress_ratio, item.name))
        if sort == "created":
            return sorted(items, key=lambda item: LibraryService._created_at(item.path), reverse=True)
        if sort == "manual":
            return sorted(items, key=lambda item: (item.sort_index, item.name))
        # 默认：最近学习（progress.json 的 updated_at，其次目录 mtime）
        def recent_key(item: course_service.CourseSummary) -> str:
            progress = read_json(item.path / "progress.json", default={}) or {}
            return str(progress.get("updated_at") or "")

        return sorted(items, key=lambda item: recent_key(item) or "0", reverse=True)

    @staticmethod
    def _created_at(course_dir: Path) -> str:
        meta = read_json(course_dir / "course.json", default={}) or {}
        return str(meta.get("created_at") or "")
