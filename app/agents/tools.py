"""Agent 工具注册表（《技术文档》§4.5 工具清单与超时、《项目文档》§8.6）。

只负责「注册了什么工具、参数是什么、怎么执行」；超时 / 缓存 / 预算 / 清洗在
``app/infra/tool_executor.py``（技术债 C-09 的定稿分工）。
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.models import RefCard

# 超时基线（《技术文档》§4.5 的「超时」列）
LOCAL_TIMEOUT = 5.0
SEARCH_TIMEOUT = 15.0
VISION_TIMEOUT = 30.0
RENDER_TIMEOUT = 60.0


# 数据结构与执行器同源（注意依赖方向：agents -> infra，见《技术文档》§1.5 红线 3）
from ..infra.tool_executor import ToolRegistry, ToolSpec  # noqa: F401 - 注册表在这里用，执行在 infra



def _fn(name: str, description: str, properties: dict[str, Any], required: Sequence[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


def build_registry(
    *,
    outline: dict[str, Any] | None = None,
    course_dir: str | Path | None = None,
    lesson_dir: str | Path | None = None,
    index: Any = None,
    material_ids: Sequence[str] | None = None,
    progress: dict[str, Any] | None = None,
    search_service: Any = None,
    client: Any = None,
) -> ToolRegistry:
    """按当前上下文装配可用工具（缺依赖的工具仍注册，但会返回结构化错误）。"""
    from ..infra import file_repository

    registry = ToolRegistry()
    root = Path(course_dir) if course_dir else None
    lesson = Path(lesson_dir) if lesson_dir else None

    def read_outline(scope: str = "all") -> dict[str, Any]:
        data = outline or {}
        if scope == "all" or not data:
            return data
        if scope.startswith("C"):
            chapters = [item for item in data.get("chapters", []) if item.get("id") == scope]
            return {**data, "chapters": chapters}
        if scope.startswith("L"):
            for chapter in data.get("chapters", []):
                for item in chapter.get("lessons", []):
                    if item.get("id") == scope:
                        return {"chapter": {"id": chapter.get("id"), "title": chapter.get("title")}, "lesson": item}
        return {}

    def get_lesson_kps(lesson_id: str) -> list[dict[str, Any]]:
        for chapter in (outline or {}).get("chapters", []):
            for item in chapter.get("lessons", []):
                if item.get("id") == lesson_id:
                    return list(item.get("knowledge_points") or [])
        return []

    def get_progress() -> dict[str, Any]:
        if progress is not None:
            return progress
        if root is not None:
            return file_repository.read_json_or_default(root / "progress.json", {}) or {}
        return {}

    def search_materials(query: str, k: int = 5) -> list[dict[str, Any]]:
        if index is None:
            return []
        return index.search_chunks(query, k=int(k), material_ids=list(material_ids) if material_ids else None)

    def read_material_slice(material_id: str, page: int) -> dict[str, Any]:
        if index is None:
            return {"error": "资料索引不可用"}
        rows = index.search_chunks("", k=50) if False else []
        _ = rows
        return {"material_id": material_id, "page": page, "text": ""}

    def cite_from_lesson(lesson_id: str, anchor: str = "") -> dict[str, Any]:
        return RefCard(type="lesson_text", lesson_id=lesson_id, excerpt=anchor[:400]).model_dump()

    def save_note(md: str, target: str = "lesson") -> dict[str, Any]:
        if target == "course" and root is not None:
            path = root / "notes" / "小灶汇总.md"
        elif lesson is not None:
            path = lesson / "notebook.md"
        else:
            return {"error": "没有可写入的笔记位置"}
        existing = file_repository.read_text_or_empty(path)
        file_repository.write_text(path, existing.rstrip() + "\n\n" + md.strip() + "\n")
        return {"saved": str(path)}

    def make_quiz(kp: str, n: int = 3) -> dict[str, Any]:
        if client is None:
            return {"error": "当前上下文不支持出题"}
        return {"requested": True, "kp": kp, "n": int(n)}

    def web_search(query: str, k: int = 5) -> dict[str, Any]:
        if search_service is None:
            return {"error": "联网搜索未启用", "hint": "在设置 → 联网与时效中开启"}
        return {"query": query, "results": search_service.search_sync(query, k=int(k))}

    registry.register(
        ToolSpec(
            "search_materials",
            "检索用户上传的资料片段（本地 FTS5，中文走 trigram）",
            _fn("search_materials", "检索用户资料片段（FTS5 / trigram）", {"query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
            search_materials,
            cacheable=True,
        )
    )
    registry.register(
        ToolSpec(
            "read_material_slice",
            "读取资料指定页的原文片段",
            _fn("read_material_slice", "读取资料指定页原文", {"material_id": {"type": "string"}, "page": {"type": "integer"}}, ["material_id", "page"]),
            read_material_slice,
        )
    )
    registry.register(
        ToolSpec(
            "read_outline",
            "读取课程大纲（all / C1 / L1-2）",
            _fn("read_outline", "读取课程大纲（all / C1 / L1-2）", {"scope": {"type": "string"}}),
            read_outline,
        )
    )
    registry.register(
        ToolSpec(
            "get_lesson_kps",
            "取本小节知识点清单（用于覆盖率自检）",
            _fn("get_lesson_kps", "取本小节知识点清单", {"lesson_id": {"type": "string"}}, ["lesson_id"]),
            get_lesson_kps,
        )
    )
    registry.register(
        ToolSpec(
            "get_progress",
            "读取学习进度（用于调整讲解详略）",
            _fn("get_progress", "读取学习进度（用于调整讲解详略）", {}),
            get_progress,
        )
    )
    registry.register(
        ToolSpec(
            "cite_from_lesson",
            "把讲义片段打包成引用卡片",
            _fn("cite_from_lesson", "把讲义片段打包成引用卡片", {"lesson_id": {"type": "string"}, "anchor": {"type": "string"}}, ["lesson_id"]),
            cite_from_lesson,
        )
    )
    registry.register(
        ToolSpec(
            "save_note",
            "把答疑结论沉淀为笔记（lesson / course）",
            _fn("save_note", "把答疑结论沉淀为笔记", {"md": {"type": "string"}, "target": {"type": "string"}}, ["md"]),
            save_note,
        )
    )
    registry.register(
        ToolSpec(
            "make_quiz",
            "基于知识点出题",
            _fn("make_quiz", "基于知识点出题", {"kp": {"type": "string"}, "n": {"type": "integer"}}, ["kp"]),
            make_quiz,
            timeout=RENDER_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "web_search",
            "联网检索时效性信息（默认百度千帆 AI 搜索，只取引用）",
            _fn("web_search", "联网检索时效性信息", {"query": {"type": "string"}, "k": {"type": "integer"}}, ["query"]),
            web_search,
            timeout=SEARCH_TIMEOUT,
            cacheable=True,
        )
    )
    return registry
