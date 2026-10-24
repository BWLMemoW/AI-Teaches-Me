"""M32 测试：两个新工具在注册表里的接线、落盘与契约（不含 Qt，可无 GUI 跑）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.tools import RENDER_TIMEOUT, build_registry
from app.infra.svg_sanitizer import clean, is_acceptable
from app.infra.tool_executor import ToolExecutor
from app.services.persona_service import PersonaService

pytestmark = pytest.mark.no_network

NEW_TOOLS = ("plot_function", "model_3d")


def _run(registry, name: str, arguments: dict) -> dict:
    executor = ToolExecutor(registry)
    return asyncio.run(executor.run({"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}))


def _failed(outcome: dict) -> bool:
    """工具层失败，或工具内部 ok=False（两种都算「结构化失败」，都不许抛异常）。"""
    if outcome.get("ok") is False:
        return True
    result = outcome.get("result") or {}
    return bool(result.get("error")) or result.get("ok") is False


def test_registry_exposes_both_m32_tools() -> None:
    registry = build_registry()
    names = registry.names()
    for name in NEW_TOOLS:
        assert name in names

    plot = registry.get("plot_function")
    model = registry.get("model_3d")
    assert plot is not None and model is not None
    assert plot.parameters["required"] == ["expressions", "x_min", "x_max"]
    assert model.parameters["required"] == ["kind"]
    assert plot.timeout == RENDER_TIMEOUT
    assert model.timeout == RENDER_TIMEOUT
    assert set(model.parameters["properties"]["kind"]["enum"]) >= {"box", "sphere", "surface"}
    assert plot.parameters["properties"]["expressions"]["maxItems"] == 3


def test_plot_function_returns_svg_and_saves_widgets(tmp_path: Path) -> None:
    lesson = tmp_path / "L1-1-函数图像"
    lesson.mkdir(parents=True)

    outcome = _run(
        build_registry(lesson_dir=lesson),
        "plot_function",
        {"expressions": ["sin(x)/x"], "x_min": -20, "x_max": 20, "title": "y=sin(x)/x"},
    )

    assert outcome["ok"] is True, outcome
    result = outcome["result"]
    assert result["ok"] is True
    assert result["svg"].startswith("<svg")
    assert result["points"] == 401
    assert (lesson / "widgets" / "plot-1.svg").exists()
    assert result["assets"]["svg"] == "plot-1.svg"


def test_plot_function_rotates_previous_version(tmp_path: Path) -> None:
    lesson = tmp_path / "L1-1-函数图像"
    lesson.mkdir(parents=True)
    registry = build_registry(lesson_dir=lesson)

    first = _run(registry, "plot_function", {"expressions": ["sin(x)"], "x_min": -3, "x_max": 3})
    second = _run(registry, "plot_function", {"expressions": ["cos(x)"], "x_min": -3, "x_max": 3})

    assert first["ok"] is True and second["ok"] is True
    widget_names = {item.name for item in (lesson / "widgets").iterdir()}
    assert "plot-1.svg" in widget_names
    assert any(name.startswith("plot-1.v") and name.endswith(".svg") for name in widget_names)


def test_plot_function_without_lesson_dir_still_draws(tmp_path: Path) -> None:
    outcome = _run(
        build_registry(),
        "plot_function",
        {"expressions": ["x**2-4"], "x_min": -5, "x_max": 5},
    )

    assert outcome["ok"] is True, outcome
    result = outcome["result"]
    assert result["svg"].startswith("<svg")
    assert not result.get("assets")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "arguments",
    [
        {"expressions": [], "x_min": 0, "x_max": 1},
        {"expressions": ["x", "x", "x", "x"], "x_min": 0, "x_max": 1},
        {"expressions": ["x"], "x_min": 1, "x_max": 1},
        {"expressions": ['__import__("os")'], "x_min": 0, "x_max": 1},
    ],
)
def test_plot_function_reports_invalid_input_without_raising(arguments: dict) -> None:
    outcome = _run(build_registry(), "plot_function", arguments)

    assert _failed(outcome), outcome
    assert outcome["elapsed_ms"] >= 0


def test_model_3d_rejects_unknown_kind() -> None:
    outcome = _run(build_registry(), "model_3d", {"kind": "cube"})

    assert _failed(outcome), outcome


def test_builtin_personas_can_reach_the_new_tools() -> None:
    service = PersonaService(user_dir=Path("personas"))
    personas = service.list_personas()
    assert personas
    for persona in personas:
        if not persona.tools:
            continue
        assert "plot_function" in persona.tools, persona.id
        assert "model_3d" in persona.tools, persona.id


# --- 三维在两条链路上的产物（追问只出静态图 / 讲义出交互页） -----------------
def test_model_3d_static_only_for_tutor_path(tmp_path: Path) -> None:
    lesson = tmp_path / "L1-1-立体几何"
    lesson.mkdir(parents=True)

    outcome = _run(
        build_registry(lesson_dir=lesson),
        "model_3d",
        {"kind": "sphere", "size_a": 1.0, "interactive": False, "title": "球"},
    )

    assert outcome["ok"] is True, outcome
    result = outcome["result"]
    assert "html" not in result, "追问气泡跑不了 iframe，不该把整段 HTML 回传给模型"
    assert result["faces"] > 0
    assert is_acceptable(clean(result["svg"]))
    assert (lesson / "widgets" / "model3d-1.svg").exists()
    assert not (lesson / "widgets" / "model3d-1.html").exists()


def test_model_3d_interactive_surface_saves_html(tmp_path: Path) -> None:
    lesson = tmp_path / "L1-1-立体几何"
    lesson.mkdir(parents=True)

    outcome = _run(
        build_registry(lesson_dir=lesson),
        "model_3d",
        {
            "kind": "surface",
            "expression": "sin(x)*cos(y)",
            "x_min": -3,
            "x_max": 3,
            "y_min": -3,
            "y_max": 3,
            "grid": 15,
            "title": "马鞍面",
        },
    )

    assert outcome["ok"] is True, outcome
    result = outcome["result"]
    assert result["assets"]["html"] == "model3d-1.html"
    assert result["assets"]["svg"] == "model3d-1.svg"
    assert (lesson / "widgets" / "model3d-1.html").exists()
    html = result["html"]
    assert len(html.encode("utf-8")) <= 80 * 1024
    assert "prefers-reduced-motion" in html
    assert "http://" not in html and "https://" not in html


# --- 三维场景（M36：小孔成像这类光路 / 空间关系图） -----------------------
PINHOLE_SCENE = {
    "segments": [
        {"from": [-6, 0, 0], "to": [-6, 0, 1.6], "style": "arrow", "label": "物"},
        {"from": [-6, 0, 1.6], "to": [3, 0, -0.8], "style": "arrow"},
    ],
    "faces": [
        {
            "points": [[3, -1.2, -1.2], [3, 1.2, -1.2], [3, 1.2, 1.2], [3, -1.2, 1.2]],
            "opacity": 0.08,
            "label": "屏",
        }
    ],
    "labels": [{"text": "小孔", "at": [0, 0, 1.2]}],
}


def test_model_3d_scene_builds_pinhole_setup(tmp_path: Path) -> None:
    lesson = tmp_path / "L1-1-光学"
    lesson.mkdir(parents=True)

    outcome = _run(
        build_registry(lesson_dir=lesson),
        "model_3d",
        {"kind": "scene", "scene": PINHOLE_SCENE, "interactive": False, "title": "小孔成像"},
    )

    assert outcome["ok"] is True, outcome
    result = outcome["result"]
    assert "html" not in result, "追问里只取静态图"
    assert ">小孔</text>" in result["svg"] and ">屏</text>" in result["svg"]
    assert result["assets"]["svg"] == "model3d-1.svg"
    assert is_acceptable(clean(result["svg"]))


def test_model_3d_scene_rejects_malformed_scene() -> None:
    cases = (
        {},
        {"segments": [{"from": [0, 0], "to": [1, 1, 1]}]},
        {"segments": [{"from": [0, 0, 0], "to": ["甲", 0, 0]}]},
        {"segments": "不是数组"},
        {"faces": [{"points": [[0, 0, 0], [1, 1, 1]]}]},
    )
    for scene in cases:
        outcome = _run(build_registry(), "model_3d", {"kind": "scene", "scene": scene})
        assert _failed(outcome), scene
