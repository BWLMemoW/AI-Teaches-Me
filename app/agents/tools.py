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
    course_id: str | None = None,
    progress: dict[str, Any] | None = None,
    search_service: Any = None,
    client: Any = None,
    illustrator: Any = None,
    animator: Any = None,
    vision: Any = None,
    notes_agent: Any = None,
    theme: dict[str, str] | None = None,
) -> ToolRegistry:
    """按当前上下文装配可用工具（缺依赖的工具仍注册，但会返回结构化错误）。"""
    from ..infra import file_repository

    registry = ToolRegistry()
    root = Path(course_dir) if course_dir else None
    lesson = Path(lesson_dir) if lesson_dir else None

    def save_visual(stem: str, payloads: dict[str, str | bytes]) -> dict[str, str]:
        """把视觉产物写进 ``<lesson>/widgets/``（历史版本 ``.vK`` 轮转）；没有小节就只返回空字典。"""
        if lesson is None or not payloads:
            return {}
        from ..infra.asset_store import save_versioned

        try:
            return save_versioned(lesson / "widgets", stem, payloads)
        except OSError:
            return {}

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
        """检索资料片段：**限定在本课程**；本节指定了参考就再收窄到那几份资料。"""
        if index is None:
            return []
        return index.search_chunks(
            query,
            k=int(k),
            material_ids=list(material_ids) if material_ids else None,
            course_id=str(course_id) if course_id else None,
        )

    def read_material_slice(material_id: str, page: int) -> dict[str, Any]:
        """按页取原文：PPT 的 ``page`` 就是幻灯片号。"""
        if index is None:
            return {"error": "资料索引不可用"}
        identifier = str(material_id or "").strip()
        if not identifier:
            return {"error": "缺少 material_id"}
        if material_ids and identifier not in set(material_ids):
            return {
                "error": "这份资料不在本节指定的参考范围内",
                "allowed": sorted(str(item) for item in material_ids),
            }
        fetch = getattr(index, "chunks_for_page", None)
        if not callable(fetch):
            return {"error": "索引不支持按页取原文"}
        rows = fetch(identifier, int(page or 0))
        if not rows:
            return {
                "material_id": identifier,
                "page": int(page or 0),
                "text": "",
                "hint": "这一页没有可读文字（可能是纯图片页），可以换一页或改用 search_materials",
            }
        text = "\n".join(str(row.get("text") or "") for row in rows if str(row.get("text") or "").strip())
        heading = next((str(row.get("heading") or "") for row in rows if str(row.get("heading") or "")), "")
        return {
            "material_id": identifier,
            "page": int(page or 0),
            "heading": heading,
            "text": text[:2400],
        }

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
            "检索本课程的资料片段（本课程内；本节指定了参考就只搜那几份）",
            _fn("search_materials", "检索本课程资料片段（FTS5 / trigram）", {"query": {"type": "string"}, "k": {"type": "integer", "minimum": 1, "maximum": 10}}, ["query"]),
            search_materials,
            cacheable=True,
        )
    )
    registry.register(
        ToolSpec(
            "read_material_slice",
            "按页读取资料原文（PPT 用幻灯片号）；要引用课件原话、例题或表格时用它",
            _fn("read_material_slice", "读取资料指定页 / 幻灯片的原文", {"material_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1}}, ["material_id", "page"]),
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

    def plot_function(
        expressions: list[str],
        x_min: float,
        x_max: float,
        y_min: float | None = None,
        y_max: float | None = None,
        samples: int = 401,
        title: str = "",
        caption: str = "",
    ) -> dict[str, Any]:
        """精确函数图像：安全求值表达式 → 采样 → 自绘坐标轴/网格/刻度/曲线 → SVG（M32）。

        曲线由 Python 算出来，模型只负责把返回的 SVG 放进 ``svg`` 块，
        因此形状一定准确，不存在「手画曲线画歪」的问题。
        """
        from ..infra.plot_svg import PlotSpec, build_plot
        from ..infra.svg_renderer import render_svg_png
        from ..infra.svg_sanitizer import clean, is_acceptable

        if isinstance(expressions, str):
            expressions = [expressions]
        if not isinstance(expressions, (list, tuple)):
            return {"ok": False, "error": "expressions 必须是字符串数组（1–3 条）"}
        try:
            spec = PlotSpec(
                expressions=[str(item) for item in expressions],
                x_min=float(x_min),
                x_max=float(x_max),
                y_min=None if y_min is None else float(y_min),
                y_max=None if y_max is None else float(y_max),
                samples=int(samples or 401),
                title=str(title or ""),
                caption=str(caption or ""),
            )
        except (TypeError, ValueError) as exc:  # noqa: BLE001 - 参数不可信，一律结构化返回
            return {"ok": False, "error": f"参数不合法：{exc}"}

        try:
            result = build_plot(spec)
        except Exception as exc:  # noqa: BLE001 - 工具永不抛异常（ToolExecutor 只校验必填字段）
            return {"ok": False, "error": f"函数图生成失败：{exc}"}
        if not result.ok:
            return {
                "ok": False,
                "issues": list(result.issues),
                "fallback_md": result.fallback_md or "（函数图生成失败，改用文字说明。）",
            }

        checked = clean(result.svg)
        if not is_acceptable(checked):
            return {
                "ok": False,
                "issues": list(checked.problems),
                "fallback_md": "（函数图未通过清洗，改用文字说明。）",
            }
        png = render_svg_png(checked.svg, width=spec.width)
        warnings = [] if png else ["PNG 占位图渲染失败（讲义内联 SVG 不受影响）"]
        saved = save_visual("plot-1", {"svg": checked.svg, "png": png} if png else {"svg": checked.svg})
        payload: dict[str, Any] = {
            "ok": True,
            "svg": checked.svg,
            "points": result.points,
            "warnings": warnings,
        }
        if saved:
            payload["assets"] = saved
        return payload

    def model_3d(
        kind: str,
        size_a: float | None = None,
        size_b: float | None = None,
        size_c: float | None = None,
        segments: int = 24,
        expression: str = "",
        x_min: float = -1.0,
        x_max: float = 1.0,
        y_min: float = -1.0,
        y_max: float = 1.0,
        grid: int = 21,
        az: float = -35.0,
        el: float = 25.0,
        interactive: bool = True,
        scene: dict[str, Any] | None = None,
        title: str = "",
        caption: str = "",
    ) -> dict[str, Any]:
        """简单的三维建模与展示（M32）：预置立体 / 曲面 z=f(x,y) → 可旋转 HTML + 静态投影 SVG。

        ``kind="scene"`` 时改为「简单场景」：用 ``scene`` 里的线段（可带箭头/虚线，画光路）、
        面（画屏/挡板）与文字标注拼出一个立体场景（小孔成像、光线传播、空间关系），仍然可旋转。

        ``interactive=false`` 时只出静态 SVG（追问气泡里跑不了 iframe，别白搬一段 HTML）。"""
        from ..infra.model3d import Model3DSpec, build_model3d
        from ..infra.svg_renderer import render_svg_png
        from ..infra.svg_sanitizer import clean, is_acceptable

        try:
            spec = Model3DSpec(
                kind=str(kind or ""),
                title=str(title or ""),
                caption=str(caption or ""),
                size_a=None if size_a is None else float(size_a),
                size_b=None if size_b is None else float(size_b),
                size_c=None if size_c is None else float(size_c),
                expression=str(expression or ""),
                x_min=float(x_min),
                x_max=float(x_max),
                y_min=float(y_min),
                y_max=float(y_max),
                grid=int(grid or 21),
                segments=int(segments or 24),
                az=float(az),
                el=float(el),
                scene=scene if isinstance(scene, dict) else None,
            )
        except (TypeError, ValueError) as exc:  # noqa: BLE001 - 参数不可信
            return {"ok": False, "error": f"参数不合法：{exc}"}

        try:
            result = build_model3d(spec, theme=theme)
        except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
            return {"ok": False, "error": f"三维建模失败：{exc}"}
        if not result.ok:
            return {
                "ok": False,
                "issues": list(result.issues),
                "fallback_md": result.fallback_md or "（三维示意图生成失败，改用文字说明。）",
            }

        checked = clean(result.svg)
        if not is_acceptable(checked):
            return {
                "ok": False,
                "issues": list(checked.problems),
                "fallback_md": "（三维示意图未通过清洗，改用文字说明。）",
            }
        png = render_svg_png(checked.svg, width=spec.width)
        # interactive=False（追问场景）只要静态 SVG：不保存也不回传 HTML，省掉整段 iframe 源码的搬运开销
        keep_html = bool(interactive) and bool(result.html)
        payloads: dict[str, str | bytes] = {"svg": checked.svg}
        if keep_html:
            payloads["html"] = result.html
        if png:
            payloads["png"] = png
        saved = save_visual("model3d-1", payloads)

        payload: dict[str, Any] = {
            "ok": True,
            "svg": checked.svg,
            "faces": result.faces,
            "warnings": [] if png else ["PNG 占位图渲染失败（讲义内联 SVG 不受影响）"],
        }
        if keep_html:
            payload["html"] = result.html
        if saved:
            payload["assets"] = saved
        return payload

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
            "plot_function",
            "精确绘制函数图像 y=f(x)：1–3 条曲线、真实坐标轴与刻度，返回 SVG 源码（放进 svg 块，不要手画曲线）",
            _fn(
                "plot_function",
                "精确函数图像（采样求值 → SVG）",
                {
                    "expressions": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3},
                    "x_min": {"type": "number"},
                    "x_max": {"type": "number"},
                    "y_min": {"type": "number"},
                    "y_max": {"type": "number"},
                    "samples": {"type": "integer", "minimum": 51, "maximum": 2001},
                    "title": {"type": "string"},
                    "caption": {"type": "string"},
                },
                ["expressions", "x_min", "x_max"],
            ),
            plot_function,
            timeout=RENDER_TIMEOUT,
        )
    )
    registry.register(
        ToolSpec(
            "model_3d",
            "简单的三维建模：预置立体（box/cylinder/cone/sphere/prism/pyramid/torus）或曲面 z=f(x,y)，"
            "或简单场景（kind=scene：线段/光路 + 面 + 文字标注，适合小孔成像、光线传播这类空间关系图），"
            "返回可拖拽旋转的 HTML（讲义里放 animation 块）与静态投影 SVG（追问里用 interactive=false 只取 SVG）",
            _fn(
                "model_3d",
                "生成三维立体 / 曲面的交互视图与静态投影图",
                {
                    "kind": {"type": "string", "enum": ["box", "cylinder", "cone", "sphere", "prism", "pyramid", "torus", "surface", "scene"]},
                    "size_a": {"type": "number"},
                    "size_b": {"type": "number"},
                    "size_c": {"type": "number"},
                    "segments": {"type": "integer", "minimum": 6, "maximum": 48},
                    "expression": {"type": "string"},
                    "x_min": {"type": "number"},
                    "x_max": {"type": "number"},
                    "y_min": {"type": "number"},
                    "y_max": {"type": "number"},
                    "grid": {"type": "integer", "minimum": 5, "maximum": 31},
                    "az": {"type": "number"},
                    "el": {"type": "number"},
                    "interactive": {"type": "boolean"},
                    "scene": {
                        "type": "object",
                        "description": "kind=scene 时的场景：segments 线段（style=solid/dashed/arrow，光路用 arrow）、"
                        "faces 面（points 是 3–8 个 [x,y,z]，opacity 可选）、labels 文字标注（text + at）",
                        "properties": {
                            "segments": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "from": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                                        "to": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                                        "style": {"type": "string", "enum": ["solid", "dashed", "arrow"]},
                                        "label": {"type": "string"},
                                    },
                                    "required": ["from", "to"],
                                },
                            },
                            "faces": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "points": {
                                            "type": "array",
                                            "items": {
                                                "type": "array",
                                                "items": {"type": "number"},
                                                "minItems": 3,
                                                "maxItems": 3,
                                            },
                                        },
                                        "opacity": {"type": "number"},
                                        "label": {"type": "string"},
                                    },
                                    "required": ["points"],
                                },
                            },
                            "labels": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "text": {"type": "string"},
                                        "at": {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3},
                                    },
                                    "required": ["text", "at"],
                                },
                            },
                        },
                    },
                    "title": {"type": "string"},
                    "caption": {"type": "string"},
                },
                ["kind"],
            ),
            model_3d,
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
