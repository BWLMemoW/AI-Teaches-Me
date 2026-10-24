"""M32 测试：函数绘图链（安全求值 + 采样 + 自绘坐标轴 SVG）。"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ElementTree

import numpy as np
import pytest

from app.infra.expr_eval import ExprError, allowed_functions, check, evaluate
from app.infra.plot_svg import (
    CURVE_COLORS,
    DEFAULT_SAMPLES,
    MAX_SVG_BYTES,
    PlotSpec,
    build_plot,
)
from app.infra.svg_sanitizer import MAX_ELEMENTS, clean, is_acceptable

pytestmark = pytest.mark.no_network


def _plot(expressions, x_min: float = -6.0, x_max: float = 6.0, **kwargs) -> object:
    return build_plot(PlotSpec(expressions=list(expressions), x_min=x_min, x_max=x_max, **kwargs))


def _element_count(svg: str) -> int:
    return sum(1 for _ in ElementTree.fromstring(svg).iter())


def _path_data(svg: str) -> list[str]:
    root = ElementTree.fromstring(svg)
    return [
        element.attrib["d"]
        for element in root.iter()
        if element.tag.split("}")[-1] == "path" and element.attrib.get("d")
    ]


def _y_range_from_desc(svg: str) -> tuple[float, float]:
    match = re.search(r"y ∈ \[([^,]+), ([^\]]+)\]", svg)
    assert match is not None, svg[:200]
    return float(match.group(1)), float(match.group(2))


# --- 表达式白名单 ---------------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        '__import__("os")',
        "x.__class__",
        "x[0]",
        "lambda x: x",
        "[i for i in x]",
        "{'a': x}",
        "x**200",
        "os.system('x')",
        '"a" * 3',
        "f'{x}'",
    ],
)
def test_expression_whitelist_rejects_dangerous_syntax(bad: str) -> None:
    assert check(bad) != []
    with pytest.raises(ExprError):
        evaluate(bad, x=1.0)


def test_expression_rejects_overlong_and_too_complex() -> None:
    too_long = "x+" * 120 + "x"
    assert len(too_long) > 200
    assert check(too_long) != []
    assert check("+".join(["x"] * 200)) != []  # AST 节点数超限


@pytest.mark.parametrize(
    "good",
    [
        "sin(x)",
        "x**2-4",
        "clip(sin(x), -0.5, 0.5)",
        "x if x > 0 else -x",
        "1/x",
        "tan(x)",
        "log(x, 2)",
        "min(x, 1)",
        "hypot(x, 1)",
        "abs(x) + sign(x)",
        "atan2(x, 2)",
    ],
)
def test_expression_whitelist_accepts_math_functions(good: str) -> None:
    assert check(good) == []


def test_allowed_functions_covers_the_documented_table() -> None:
    names = set(allowed_functions())
    for name in ("sin", "cos", "tan", "exp", "log", "sqrt", "clip", "hypot", "min", "max", "pow"):
        assert name in names


# --- 求值语义 -------------------------------------------------------------
def test_evaluate_returns_python_float_for_scalars() -> None:
    value = evaluate("sin(0)")
    assert isinstance(value, float)
    assert value == 0.0


def test_evaluate_returns_inf_instead_of_raising() -> None:
    assert math.isinf(evaluate("1/x", x=0.0))
    assert math.isnan(evaluate("sqrt(x)", x=-1.0))
    assert math.isinf(evaluate("log(x)", x=0.0))


def test_evaluate_is_elementwise_for_arrays() -> None:
    values = evaluate("x**2", x=np.array([1.0, 2.0, 3.0]))
    assert list(values) == [1.0, 4.0, 9.0]


# --- 断线与渐近线 ---------------------------------------------------------
def test_tangent_gets_broken_at_asymptotes() -> None:
    result = _plot(["tan(x)"], -6.0, 6.0)
    assert result.ok, result.issues
    data = _path_data(result.svg)[0]
    assert data.count("M") >= 3  # π/2、3π/2 等渐近线处都要断开


def test_reciprocal_does_not_connect_across_zero() -> None:
    result = _plot(["1/x"], -3.0, 3.0)
    assert result.ok, result.issues
    data = _path_data(result.svg)[0]
    assert data.count("M") >= 2
    # 左支与右支是两条独立子路径，中间留出断口（不画跨过 x=0 的竖线）
    points = [
        (float(pair.split(",")[0]), float(pair.split(",")[1]))
        for pair in data.replace("M", " ").replace("L", " ").split()
    ]
    subpaths = [piece for piece in re.findall(r"M([^M]*)", data) if piece.strip()]
    assert len(subpaths) >= 2
    boundaries = sorted({point[0] for point in points})
    spans = []
    for piece in subpaths:
        xs = [float(pair.split(",")[0]) for pair in piece.replace("L", " ").split()]
        spans.append((min(xs), max(xs)))
    spans.sort()
    for (_start, left_end), (right_start, _end) in zip(spans, spans[1:]):
        assert left_end < right_start
    assert boundaries[0] >= 0 and boundaries[-1] <= 820


def test_smooth_function_stays_in_one_stroke() -> None:
    result = _plot(["sin(x)"], -6.0, 6.0)
    assert result.ok, result.issues
    assert _path_data(result.svg)[0].count("M") == 1


# --- y 轴范围 -------------------------------------------------------------
def test_y_range_clips_extreme_values() -> None:
    result = _plot(["1/x"], -3.0, 3.0)
    assert result.ok, result.issues
    low, high = _y_range_from_desc(result.svg)
    assert high < 100 and low > -100


def test_y_range_can_be_overridden() -> None:
    result = _plot(["x**3"], -6.0, 6.0, y_min=-5.0, y_max=5.0)
    assert result.ok, result.issues
    assert _y_range_from_desc(result.svg) == (-5.0, 5.0)


def test_y_range_rejects_inverted_bounds() -> None:
    result = _plot(["sin(x)"], -3.0, 3.0, y_min=5.0, y_max=-5.0)
    assert result.ok is False


# --- 多曲线与图例 ---------------------------------------------------------
def test_three_curves_share_one_plot_with_legend() -> None:
    result = _plot(["sin(x)", "cos(x)", "x/3"])
    assert result.ok, result.issues
    assert len(_path_data(result.svg)) == 3
    for color in CURVE_COLORS:
        assert color in result.svg
    assert "cos(x)" in result.svg


def test_single_curve_has_no_legend() -> None:
    result = _plot(["sin(x)"])
    assert result.ok, result.issues
    assert CURVE_COLORS[1] not in result.svg


# --- 产物质量闸门 ---------------------------------------------------------
@pytest.mark.parametrize(
    "expressions,x_min,x_max,kwargs",
    [
        (["sin(x)/x"], -20.0, 20.0, {}),
        (["tan(x)"], -6.0, 6.0, {}),
        (["1/x"], -3.0, 3.0, {}),
        (["sin(x)", "cos(x)", "tan(x)"], -6.0, 6.0, {}),
        (["x**2-4"], -10.0, 10.0, {"samples": 2001}),
        (["x if x > 0 else -x"], -5.0, 5.0, {"y_min": -1.0, "y_max": 1.0}),
    ],
)
def test_generated_svg_passes_the_sanitizer_within_limits(
    expressions: list[str], x_min: float, x_max: float, kwargs: dict
) -> None:
    result = _plot(expressions, x_min, x_max, **kwargs)
    assert result.ok, result.issues

    checked = clean(result.svg)
    assert checked.problems == []
    assert is_acceptable(checked) is True
    tags = {element.tag.split("}")[-1] for element in ElementTree.fromstring(result.svg).iter()}
    assert "viewBox" in result.svg
    assert {"svg", "title", "desc"} <= tags
    # 回归：清洗器会把带 xmlns 的文档重写成 <ns0:svg>，讲义页用 querySelector("svg") 就找不到图了
    assert "<ns0:" not in result.svg and result.svg.startswith("<svg")
    assert len(result.svg.encode("utf-8")) <= MAX_SVG_BYTES
    assert _element_count(result.svg) <= MAX_ELEMENTS
    assert result.points == kwargs.get("samples", DEFAULT_SAMPLES)


# --- 非法输入一律结构化返回 -----------------------------------------------
@pytest.mark.parametrize(
    "expressions,x_min,x_max,kwargs",
    [
        ([], -1.0, 1.0, {}),
        (["x", "x", "x", "x"], -1.0, 1.0, {}),
        (["sin(x)"], 1.0, 1.0, {}),
        (["sin(x)"], -1.0, 1.0, {"samples": 0}),
        (["sin(x)"], -1.0, 1.0, {"samples": 99999}),
        (["sin(x"], -1.0, 1.0, {}),
        (['__import__("os")'], -1.0, 1.0, {}),
        (["x"], -1.0, 1e9, {}),
        (["x"], -1.0, 1.0, {"y_min": 1.0, "y_max": 1.0}),
    ],
)
def test_invalid_parameters_return_structured_failure(
    expressions: list[str], x_min: float, x_max: float, kwargs: dict
) -> None:
    result = _plot(expressions, x_min, x_max, **kwargs)
    assert result.ok is False
    assert result.issues
    assert result.fallback_md
    assert result.svg == ""
