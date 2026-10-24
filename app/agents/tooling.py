"""工具装配的共用部分（《技术文档》§4.5 Tool Loop、《项目文档》§8.6 工具清单）。

学习台（讲义生成 / 课内追问）与小灶都用**同一套工具**：检索资料、画图、三维、出题、联网……
这里把「视觉与笔记支持 + 注册表 + 执行器」的装配集中起来，避免两处各写一份后慢慢漂移。

本模块只做装配，不执行；超时 / 缓存 / 预算在 ``infra/tool_executor.py``。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class VisualSupport:
    """图 / 动画 / 笔记 / 读图这四件「要模型之外能力」的支持对象。"""

    illustrator: Any = None
    animator: Any = None
    notes_agent: Any = None
    vision: Any = None


def _supports_vision(config: Any) -> bool:
    """档案声明支持读图才带 vision；取不到配置时按支持处理（与学习台一致）。"""
    if config is None:
        return True
    try:
        return bool(config.require_profile().capabilities.vision)
    except Exception:  # noqa: BLE001 - 配置异常不该阻断答疑
        return True


def build_visual_support(client: Any, config: Any = None) -> VisualSupport:
    """按客户端能力装配图示 / 动画 / 笔记 Agent 与读图回调（没有 client 就全为空）。"""
    if client is None:
        return VisualSupport()

    def vision(png: bytes, prompt: str) -> str:
        return asyncio.run(client.review_image(png, prompt=prompt))

    enabled = _supports_vision(config)
    from .illustrator import AnimatorAgent, IllustratorAgent
    from .notes import NoteKeeperAgent

    return VisualSupport(
        illustrator=IllustratorAgent(client, vision=vision if enabled else None),
        animator=AnimatorAgent(client),
        notes_agent=NoteKeeperAgent(client),
        vision=vision if enabled else None,
    )


def tools_allowed(config: Any) -> bool:
    """工具循环总开关 + 档案能力（与学习台 ``_build_tooling`` 同一判据）。"""
    if config is None:
        return True
    try:
        if not bool(getattr(config.generation, "allow_tools", True)):
            return False
    except Exception:  # noqa: BLE001 - 配置缺字段时按允许处理
        return True
    try:
        return bool(config.require_profile().capabilities.tools)
    except Exception:  # noqa: BLE001
        return True


def build_course_tooling(
    *,
    course_dir: str | Path,
    index: Any = None,
    client: Any = None,
    config: Any = None,
    search_service: Any = None,
    theme: dict[str, str] | None = None,
    material_ids: Sequence[str] | None = None,
    allowed: Sequence[str] | None = None,
) -> tuple[Any, list[dict[str, Any]] | None]:
    """课程级工具装配（小灶这类「没有具体小节」的场景）。

    返回 ``(executor, tools)``；工具被开关或档案能力禁用时返回 ``(None, None)``。
    没有 ``lesson_dir``，所以落盘类动作（生成图的 ``widgets/`` 资产）会自动跳过，
    但出图本身照常（``plot_function`` / ``model_3d`` 只返回 SVG / HTML）。
    """
    if client is None or not tools_allowed(config):
        return None, None
    from ..infra import file_repository
    from ..infra.tool_executor import CallBudget, ToolExecutor
    from .tools import build_registry

    root = Path(course_dir)
    meta = file_repository.read_json_or_default(root / "course.json", {}) or {}
    support = build_visual_support(client, config)
    registry = build_registry(
        course_dir=root,
        index=index,
        material_ids=material_ids,
        course_id=str(meta.get("id") or root.name),
        client=client,
        search_service=search_service,
        illustrator=support.illustrator,
        animator=support.animator,
        vision=support.vision,
        notes_agent=support.notes_agent,
        theme=theme,
    )
    schemas = registry.schemas(allowed) if allowed else registry.schemas()
    return ToolExecutor(registry, budget=CallBudget()), (schemas or None)
