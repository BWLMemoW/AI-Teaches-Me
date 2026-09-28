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
    illustrator: Any = None,
    animator: Any = None,
    vision: Any = None,
    notes_agent: Any = None,
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
    def make_notes(lesson_id: str, scope: str = "lesson") -> dict[str, Any]:
        if notes_agent is None or lesson is None or root is None:
            return {"error": "当前上下文没有笔记能力"}
        from ..services import notes_service

        result = notes_service.generate_notes(root, lesson, agent=notes_agent)
        return {"saved": str(result.path), "generated_by_model": result.generated_by_model}

    registry.register(
        ToolSpec(
            "make_notes",
            "整理小节 / 课程结构化 Markdown 笔记",
            _fn("make_notes", "整理笔记", {"lesson_id": {"type": "string"}, "scope": {"type": "string"}}, ["lesson_id"]),
            make_notes,
            timeout=RENDER_TIMEOUT,
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
    def gen_svg(spec: str, title: str = "示意图", caption: str = "") -> dict[str, Any]:
        """生成并**自检**SVG（内部走 Illustrator 的 Render-and-Review，§8.4.1）。"""
        if illustrator is None:
            return {"error": "当前上下文没有图示能力"}
        import asyncio

        from ..agents.illustrator import SvgSpec

        result = asyncio.run(
            illustrator.make_svg(SvgSpec(concept=spec, title=title, caption_md=caption, lesson_id=str(lesson or "")))
        )
        if not result.ok:
            return {"ok": False, "fallback_md": result.fallback_md, "issues": result.issues}
        saved = illustrator.save_asset(lesson, SvgSpec(concept=spec, title=title), result) if lesson else {}
        return {"ok": True, "svg": result.svg, "rounds": result.rounds, "assets": saved}

    def render_svg(svg: str) -> dict[str, Any]:
        from ..infra.svg_renderer import render_svg_png

        payload = render_svg_png(svg, width=820)
        return {"ok": bool(payload), "bytes": len(payload)}

    def review_svg_image(png_base64: str, spec: str = "") -> dict[str, Any]:
        if vision is None:
            return {"error": "当前模型不支持读图，已退回启发式检查（T-26b）"}
        import base64

        try:
            payload = base64.b64decode(png_base64)
        except Exception as exc:  # noqa: BLE001
            return {"error": f"PNG 解码失败：{exc}"}
        return {"verdict": vision(payload, spec or "请检查这张示意图是否文字溢出、标注齐全、明暗可读。")}

    def refine_svg(svg: str, feedback: str) -> dict[str, Any]:
        if illustrator is None:
            return {"error": "当前上下文没有图示能力"}
        import asyncio

        from ..agents.illustrator import SvgSpec

        spec = SvgSpec(concept=f"按反馈修改这张图：{feedback}", title="修改稿")
        result = asyncio.run(illustrator.make_svg(spec))
        return {"ok": result.ok, "svg": result.svg, "rounds": result.rounds, "issues": result.issues}

    def gen_animation(spec: str, title: str = "微动画") -> dict[str, Any]:
        if animator is None:
            return {"error": "当前上下文没有动画能力"}
        import asyncio

        payload = asyncio.run(animator.make_animation(spec))
        if payload.get("error"):
            return {"error": payload["error"], "fallback_md": payload.get("fallback_md", "")}
        return {"ok": True, "title": title, "html": payload["html"], "problems": payload.get("problems", [])}

    def read_image(path: str, question: str = "") -> dict[str, Any]:
        if vision is None:
            return {"error": "当前模型不支持读图"}
        from ..infra import file_repository
        from pathlib import Path as _Path

        file_path = _Path(str(path))
        if not file_path.exists():
            return {"error": "图片不存在"}
        return {"answer": vision(file_path.read_bytes(), question or "请描述这张图片里与学习相关的内容。")}

    registry.register(
        ToolSpec(
            "gen_svg",
            "生成并自检 SVG 矢量图示（Render-and-Review，最多 3 轮）",
            _fn("gen_svg", "生成 SVG 图示", {"spec": {"type": "string"}, "title": {"type": "string"}, "caption": {"type": "string"}}, ["spec"]),
            gen_svg,
            timeout=RENDER_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "render_svg",
            "把 SVG 渲染成 PNG（自检图 / 首屏占位图）",
            _fn("render_svg", "SVG 渲染成 PNG", {"svg": {"type": "string"}}, ["svg"]),
            render_svg,
            timeout=RENDER_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "review_svg_image",
            "读图评审渲染结果，产出问题清单",
            _fn("review_svg_image", "读图评审", {"png_base64": {"type": "string"}, "spec": {"type": "string"}}, ["png_base64"]),
            review_svg_image,
            timeout=VISION_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "refine_svg",
            "按反馈重画 / 局部修改 SVG",
            _fn("refine_svg", "按反馈重画", {"svg": {"type": "string"}, "feedback": {"type": "string"}}, ["svg", "feedback"]),
            refine_svg,
            timeout=RENDER_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "gen_animation",
            "生成单文件 HTML 微动画（沙箱禁网 + 5s 熔断）",
            _fn("gen_animation", "生成微动画", {"spec": {"type": "string"}, "title": {"type": "string"}}, ["spec"]),
            gen_animation,
            timeout=RENDER_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "read_image",
            "多模态读图（资料插图 / 手写笔记）",
            _fn("read_image", "读图", {"path": {"type": "string"}, "question": {"type": "string"}}, ["path"]),
            read_image,
            timeout=VISION_TIMEOUT,
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
