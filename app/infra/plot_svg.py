"""函数图像 → SVG（M32 的「函数绘图」能力）。

与 `gen_svg` 的分工：形状不是模型「画」出来的，而是 **Python 采样算出来的** ——
安全求值（`expr_eval`）→ 均匀采样 → 自绘坐标轴 / 网格 / 刻度 / 多条曲线 / 图例，
模型只负责把返回的 SVG 源码放进 ``svg`` 块。因此曲线形状一定准确。

硬约束与 `svg_sanitizer` 对齐：单个 ``<svg>`` 带 ``viewBox``、必须有 ``<title>`` /
``<desc>``、元素数 ≤ 400、体积 ≤ 34KB（留出余量）；颜色用 ``currentColor`` 或中亮度固定色，
明暗主题下都可读。
"""

from __future__ import annotations

import html
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .expr_eval import MAX_EXPR_LENGTH, ExprError, check, compile_expr
from .svg_sanitizer import clean, is_acceptable

VIEW_WIDTH = 820
VIEW_HEIGHT = 460
DEFAULT_SAMPLES = 401
# 与 tools.py 的 JSON Schema（minimum=51 / maximum=2001）和文档口径保持一致
MIN_SAMPLES = 51
MAX_SAMPLES = 2001
MAX_CURVES = 3
MAX_X_SPAN = 1e6
MAX_SVG_BYTES = 34 * 1024

MARGIN_LEFT = 64
MARGIN_RIGHT = 24
MARGIN_TOP = 40
MARGIN_BOTTOM = 48

# 中亮度固定色：浅色与深色背景下都能看清（不依赖主题变量，导出时不丢色）
CURVE_COLORS: tuple[str, ...] = ("#D1495B", "#2E86AB", "#E1A100")

FALLBACK_MD = "（函数图像生成失败，已降级为文字说明。）"


def _np() -> Any:
    """懒导入 numpy（模块只在工具调用时才被导入，这里再兜一层缺依赖的错误）。"""
    try:
        import numpy  # noqa: PLC0415 - 故意放在函数内
    except Exception as exc:  # noqa: BLE001
        raise ExprError("缺少 numpy 依赖，无法绘制函数图像") from exc
    return numpy


@dataclass
class PlotSpec:
    """一次函数绘图请求（字段与 ``tools.plot_function`` 的参数一一对应）。"""

    expressions: list[str]
    x_min: float
    x_max: float
    y_min: float | None = None
    y_max: float | None = None
    samples: int = DEFAULT_SAMPLES
    title: str = ""
    caption: str = ""
    width: int = VIEW_WIDTH


@dataclass
class PlotResult:
    ok: bool
    svg: str = ""
    points: int = 0
    issues: list[str] = field(default_factory=list)
    fallback_md: str = ""


# --- 刻度与格式 -----------------------------------------------------------
def _nice_ticks(low: float, high: float, count: int = 8) -> list[float]:
    """取「好看数字」刻度：1 / 2 / 2.5 / 5 × 10ⁿ，目标 ``count`` 条左右。"""
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return []
    raw = (high - low) / max(2, count)
    magnitude = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1.0
    step = magnitude
    for multiplier in (1, 2, 2.5, 5, 10):
        step = multiplier * magnitude
        if (high - low) / step <= count * 1.5:
            break
    start = math.ceil(low / step) * step
    ticks: list[float] = []
    value = start
    while value <= high + step * 1e-6 and len(ticks) < 60:
        ticks.append(0.0 if abs(value) < step * 1e-9 else value)
        value += step
    return ticks


def _fmt_coord(value: float) -> str:
    return f"{value:.1f}"


def _fmt_label(value: float) -> str:
    if value == 0:
        return "0"
    magnitude = abs(value)
    if magnitude >= 1e5 or magnitude < 1e-4:
        return f"{value:.1e}"
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return str(int(rounded))
    return f"{value:.1f}"


def _escape(text: Any) -> str:
    return html.escape(str(text or ""), quote=True)


def _safe_width(width: Any) -> int:
    try:
        value = int(width)
    except (TypeError, ValueError):
        return VIEW_WIDTH
    return value if 320 <= value <= 4096 else VIEW_WIDTH


# --- 校验 -----------------------------------------------------------------
def _validate(spec: PlotSpec) -> tuple[list[str], int]:
    """返回（问题清单, 采样点数）。问题清单非空即视为参数非法。"""
    problems: list[str] = []
    expressions = [str(item).strip() for item in (spec.expressions or [])]
    if not expressions:
        problems.append("expressions 至少要有 1 条表达式")
    if len(expressions) > MAX_CURVES:
        problems.append(f"expressions 最多 {MAX_CURVES} 条曲线")
    if any(not item for item in expressions):
        problems.append("expressions 里不能有空表达式")
    for expression in expressions:
        if len(expression) > MAX_EXPR_LENGTH:
            problems.append(f"表达式过长（上限 {MAX_EXPR_LENGTH} 字符）：{expression[:24]}…")

    samples = DEFAULT_SAMPLES
    try:
        samples = int(spec.samples)
    except (TypeError, ValueError):
        problems.append("samples 必须是整数")
    else:
        if not MIN_SAMPLES <= samples <= MAX_SAMPLES:
            problems.append(f"samples 必须在 {MIN_SAMPLES}–{MAX_SAMPLES} 之间")

    try:
        x_low = float(spec.x_min)
        x_high = float(spec.x_max)
    except (TypeError, ValueError):
        problems.append("x_min / x_max 必须是数字")
    else:
        if not (math.isfinite(x_low) and math.isfinite(x_high)):
            problems.append("x_min / x_max 必须是有限数")
        elif x_low >= x_high:
            problems.append("x_min 必须小于 x_max")
        elif x_high - x_low > MAX_X_SPAN:
            problems.append(f"x 跨度不能超过 {MAX_X_SPAN:g}")

    for name, value in (("y_min", spec.y_min), ("y_max", spec.y_max)):
        if value is None:
            continue
        try:
            number = float(value)
        except (TypeError, ValueError):
            problems.append(f"{name} 必须是数字")
            continue
        if not math.isfinite(number):
            problems.append(f"{name} 必须是有限数")
    if spec.y_min is not None and spec.y_max is not None:
        try:
            if float(spec.y_min) >= float(spec.y_max):
                problems.append("y_min 必须小于 y_max")
        except (TypeError, ValueError):
            pass

    for expression in expressions:
        for issue in check(expression, variables=("x",)):
            problems.append(f"表达式「{expression}」不合法：{issue}")
    return problems, samples


# --- 采样 -----------------------------------------------------------------
def _sample(expressions: Sequence[str], x_low: float, x_high: float, samples: int) -> tuple[list[Any], str]:
    """逐条曲线采样；返回（[(表达式, xs, ys)], 错误信息）。"""
    np = _np()
    xs = np.linspace(x_low, x_high, samples)
    curves: list[Any] = []
    for expression in expressions:
        try:
            function = compile_expr(expression, variables=("x",))
            with np.errstate(all="ignore"):
                values = np.asarray(function(x=xs), dtype=float)
        except ExprError as exc:
            return [], f"表达式「{expression}」求值失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
            return [], f"表达式「{expression}」求值失败：{exc}"
        if values.ndim == 0:
            values = np.full(xs.shape, float(values))
        if values.shape != xs.shape:
            return [], f"表达式「{expression}」的取值不是一维序列"
        curves.append((expression, xs, values))
    return curves, ""


def _y_range(curves: Sequence[Any], spec: PlotSpec) -> tuple[float, float, list[str]]:
    """确定 y 轴范围：默认取 2%–98% 分位数（防单个极值把主图压成一条线），可用参数覆盖。"""
    np = _np()
    finite = [values[np.isfinite(values)] for _expr, _xs, values in curves]
    finite = [item for item in finite if item.size]
    if not finite:
        return math.nan, math.nan, ["所有取值都不是有限数，画不出图像（检查定义域）"]
    pool = np.concatenate(finite)

    low_override = None if spec.y_min is None else float(spec.y_min)
    high_override = None if spec.y_max is None else float(spec.y_max)
    if low_override is not None and high_override is not None:
        return low_override, high_override, []

    p2, p98 = (float(item) for item in np.percentile(pool, [2, 98]))
    if not (math.isfinite(p2) and math.isfinite(p98)) or p98 <= p2:
        p2, p98 = float(np.min(pool)), float(np.max(pool))
    if p98 <= p2:
        p2, p98 = p2 - 1.0, p2 + 1.0
    padding = 0.08 * (p98 - p2)
    low, high = p2 - padding, p98 + padding
    if low_override is not None:
        low = low_override
    if high_override is not None:
        high = high_override
    if not (math.isfinite(low) and math.isfinite(high)) or high <= low:
        return math.nan, math.nan, ["y 轴范围无法确定（数据退化或参数冲突）"]
    return low, high, []


def _is_break(previous: float, current: float, span: float) -> bool:
    """相邻两采样点之间要不要断开（渐近线 / 跳变不允许连成一条竖线）。"""
    if not (math.isfinite(previous) and math.isfinite(current)):
        return True
    delta = abs(current - previous)
    if delta > 1e6 * span:
        return True
    # 变号且跨得比半个可见高度还猛 → 视为穿过渐近线
    return previous * current < 0 and delta > 0.5 * span


def _curve_path(xs: Any, ys: Any, sx: Any, sy: Any, low: float, high: float) -> str:
    span = high - low
    parts: list[str] = []
    previous: float | None = None
    start = True
    for x_value, y_value in zip(xs, ys):
        y = float(y_value)
        if not math.isfinite(y):
            start = True
            previous = None
            continue
        if previous is not None and _is_break(previous, y, span):
            start = True
        clamped = min(max(y, low), high)
        command = "M" if start else "L"
        parts.append(f"{command}{_fmt_coord(sx(float(x_value)))},{_fmt_coord(sy(clamped))}")
        start = False
        previous = y
    return "".join(parts)


# --- 出图 -----------------------------------------------------------------
def _render(spec: PlotSpec, curves: Sequence[Any], low: float, high: float) -> str:
    width = _safe_width(spec.width)
    height = VIEW_HEIGHT
    left, top = MARGIN_LEFT, MARGIN_TOP
    right, bottom = width - MARGIN_RIGHT, height - MARGIN_BOTTOM
    plot_width = max(1.0, right - left)
    plot_height = max(1.0, bottom - top)
    x_low, x_high = float(spec.x_min), float(spec.x_max)

    def sx(value: float) -> float:
        return left + (value - x_low) / (x_high - x_low) * plot_width

    def sy(value: float) -> float:
        return top + (high - value) / (high - low) * plot_height

    x_ticks = _nice_ticks(x_low, x_high, 8)
    y_ticks = _nice_ticks(low, high, 7)

    grid: list[str] = []
    for tick in y_ticks:
        y = sy(tick)
        grid.append(f'<line x1="{_fmt_coord(left)}" y1="{_fmt_coord(y)}" x2="{_fmt_coord(right)}" y2="{_fmt_coord(y)}"/>')
    for tick in x_ticks:
        x = sx(tick)
        grid.append(f'<line x1="{_fmt_coord(x)}" y1="{_fmt_coord(top)}" x2="{_fmt_coord(x)}" y2="{_fmt_coord(bottom)}"/>')

    axis_y = sy(0.0) if low <= 0.0 <= high else bottom
    axis_y = min(max(axis_y, top), bottom)
    axis_x = sx(0.0) if x_low <= 0.0 <= x_high else left
    axis_x = min(max(axis_x, left), right)

    axes = [
        f'<line x1="{_fmt_coord(left)}" y1="{_fmt_coord(axis_y)}" x2="{_fmt_coord(right)}" y2="{_fmt_coord(axis_y)}"/>',
        f'<line x1="{_fmt_coord(axis_x)}" y1="{_fmt_coord(top)}" x2="{_fmt_coord(axis_x)}" y2="{_fmt_coord(bottom)}"/>',
        f'<polygon points="{_fmt_coord(right)},{_fmt_coord(axis_y)} {_fmt_coord(right - 9)},{_fmt_coord(axis_y - 4)} '
        f'{_fmt_coord(right - 9)},{_fmt_coord(axis_y + 4)}"/>',
        f'<polygon points="{_fmt_coord(axis_x)},{_fmt_coord(top)} {_fmt_coord(axis_x - 4)},{_fmt_coord(top + 9)} '
        f'{_fmt_coord(axis_x + 4)},{_fmt_coord(top + 9)}"/>',
    ]

    labels: list[str] = []
    for tick in x_ticks:
        if abs(tick) < 1e-12 and x_low <= 0.0 <= x_high:
            continue
        labels.append(
            f'<text x="{_fmt_coord(sx(tick))}" y="{_fmt_coord(axis_y + 16)}" text-anchor="middle" '
            f'font-size="12">{_fmt_label(tick)}</text>'
        )
    for tick in y_ticks:
        if abs(tick) < 1e-12:
            continue
        labels.append(
            f'<text x="{_fmt_coord(axis_x - 8)}" y="{_fmt_coord(sy(tick) + 4)}" text-anchor="end" '
            f'font-size="12">{_fmt_label(tick)}</text>'
        )
    labels.append(f'<text x="{_fmt_coord(axis_x + 14)}" y="{_fmt_coord(bottom)}" font-size="13">x</text>')
    labels.append(f'<text x="{_fmt_coord(left - 4)}" y="{_fmt_coord(top - 12)}" font-size="13">y</text>')
    if abs(axis_x - left) > 12 and abs(axis_y - bottom) > 12:
        labels.append(
            f'<text x="{_fmt_coord(axis_x - 8)}" y="{_fmt_coord(axis_y + 16)}" text-anchor="end" '
            f'font-size="12">0</text>'
        )

    body: list[str] = []
    if grid:
        body.append('<g stroke="currentColor" stroke-opacity="0.15" stroke-width="1">' + "".join(grid) + "</g>")
    body.append(
        '<g stroke="currentColor" stroke-opacity="0.55" stroke-width="1.5" fill="currentColor" '
        'fill-opacity="0.55">' + "".join(axes) + "</g>"
    )
    body.append('<g fill="currentColor">' + "".join(labels) + "</g>")

    for index, (expression, xs, ys) in enumerate(curves):
        path = _curve_path(xs, ys, sx, sy, low, high)
        if not path:
            continue
        color = CURVE_COLORS[index % len(CURVE_COLORS)]
        body.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" '
            f'stroke-linecap="round"/>'
        )

    if len(curves) > 1:
        legend: list[str] = []
        for index, (expression, *_rest) in enumerate(curves):
            y = top + 4 + index * 20
            color = CURVE_COLORS[index % len(CURVE_COLORS)]
            legend.append(
                f'<line x1="{_fmt_coord(right - 150)}" y1="{_fmt_coord(y)}" x2="{_fmt_coord(right - 124)}" '
                f'y2="{_fmt_coord(y)}" stroke="{color}" stroke-width="3"/>'
            )
            legend.append(
                f'<text x="{_fmt_coord(right - 116)}" y="{_fmt_coord(y + 4)}" font-size="12" '
                f'fill="currentColor">{_escape(str(expression)[:40])}</text>'
            )
        body.append("<g>" + "".join(legend) + "</g>")

    heading = str(spec.title or "函数图像")
    summary = "、".join(str(item) for item in spec.expressions)
    description = f"{summary}；x ∈ [{x_low:g}, {x_high:g}]，y ∈ [{low:g}, {high:g}]"
    if spec.caption:
        description = f"{description}。{spec.caption}"
    # 刻意**不写 xmlns**：`svg_sanitizer.clean()` 会把带命名空间的文档重新序列化成
    # `<ns0:svg>`，而讲义页用 `querySelector("svg")` 取元素——带前缀的标签找不到，
    # 图就白画了。无命名空间的 `<svg>` 清洗前后都是它自己，QtSvg 与浏览器也都认。
    return (
        f'<svg viewBox="0 0 {width} {height}" role="img">'
        f"<title>{_escape(heading)}</title><desc>{_escape(description)}</desc>"
        + "".join(body)
        + "</svg>"
    )


def build_plot(spec: PlotSpec) -> PlotResult:
    """采样求值 → 出图 → 清洗自检。参数非法或出图失败都返回结构化结果，**永不抛异常**。"""
    try:
        problems, samples = _validate(spec)
        if problems:
            return PlotResult(ok=False, issues=problems, fallback_md=FALLBACK_MD)

        expressions = [str(item).strip() for item in spec.expressions]
        curves, error = _sample(expressions, float(spec.x_min), float(spec.x_max), samples)
        if error:
            return PlotResult(ok=False, issues=[error], fallback_md=FALLBACK_MD)

        low, high, issues = _y_range(curves, spec)
        if issues:
            return PlotResult(ok=False, issues=issues, fallback_md=FALLBACK_MD)

        svg = _render(spec, curves, low, high)
    except ExprError as exc:
        return PlotResult(ok=False, issues=[str(exc)], fallback_md=FALLBACK_MD)
    except Exception as exc:  # noqa: BLE001 - 工具永不抛异常
        return PlotResult(ok=False, issues=[f"函数图像生成失败：{exc}"], fallback_md=FALLBACK_MD)

    checked = clean(svg)
    if not is_acceptable(checked):
        return PlotResult(ok=False, issues=list(checked.problems), fallback_md=FALLBACK_MD)
    size = len(checked.svg.encode("utf-8"))
    if size > MAX_SVG_BYTES:
        return PlotResult(
            ok=False,
            issues=[f"SVG 体积 {size // 1024}KB 超过上限 {MAX_SVG_BYTES // 1024}KB"],
            fallback_md=FALLBACK_MD,
        )
    return PlotResult(ok=True, svg=checked.svg, points=samples, issues=list(checked.problems))
