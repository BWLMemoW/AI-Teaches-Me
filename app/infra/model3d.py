"""三维简单建模（M32）：几何构造 → 透视投影 → 静态 SVG + 可旋转的单文件 HTML。

设计要点（《项目文档》§8.4.3、《技术文档》§4.7）：

* **形状由代码算**：立体与曲面都是确定性几何，不存在「模型画歪」的问题；
* 静态 SVG 用深度排序 + 着色分档，把成百上千个面并成几条大 ``<path>``，
  因此元素数稳定在个位数、体积可控，且必过 ``svg_sanitizer``；
* 交互产物 = 仓库内固定模板 ``app/web/embed/model3d_viewer.html``（Canvas 2D 软件渲染，
  无 WebGL / 无 CDN / 无外链）+ 注入的几何 JSON，再过 ``prepare_animation_html`` 加 CSP 与主题变量；
* SVG **不写 ``xmlns``**：清洗器会重新序列化，带命名空间会变成 ``ns0:`` 前缀，浏览器认不出来；
* numpy 只在曲面求值时懒导入（复用 ``expr_eval``），静态几何完全不用 numpy。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

KINDS: tuple[str, ...] = (
    "box", "cylinder", "cone", "sphere", "prism", "pyramid", "torus", "surface", "scene",
)

# 「场景」类型的额度（模型给的结构化数据完全不可信，必须自己卡上限）
SCENE_MAX_SEGMENTS = 60
SCENE_MAX_FACES = 40
SCENE_MAX_LABELS = 40
SCENE_MAX_VERTICES = 500
SCENE_COORD_LIMIT = 1e4
SCENE_LABEL_CHARS = 24
SCENE_STYLES: tuple[str, ...] = ("solid", "dashed", "arrow")

MIN_SEGMENTS, MAX_SEGMENTS = 6, 48
MIN_GRID, MAX_GRID = 5, 31
MIN_PRISM_SIDES, MAX_PRISM_SIDES = 3, 12

MAX_FILLED_FACES = 500      # 超过这个数就退成线框（控制 SVG 体积）
MAX_WIRE_SEGMENTS = 900     # 线框最多画多少段
FILL_BANDS = 3              # 着色分档数（同时是 SVG 里填充 path 的条数）
MAX_HTML_BYTES = 80 * 1024  # 动画块红线

VIEWER_TEMPLATE = Path(__file__).resolve().parents[1] / "web" / "embed" / "model3d_viewer.html"
GEOMETRY_TOKEN = "__ATM_GEOMETRY_JSON__"

ACCENT = "#6C8AE4"          # 面填充色（明暗主题下都可读）
AXIS_LENGTH_SCALE = 0.85    # 坐标轴相对模型半径的长度


class GeometryError(ValueError):
    """几何参数不合法（尺寸非正、曲面表达式非法、范围内没有有限值等）。"""


@dataclass
class Geometry:
    """一个可渲染的网格：顶点 + 面（逆时针、外法向朝外）+ 线框。"""

    vertices: list[tuple[float, float, float]]
    faces: list[list[int]]
    edges: list[tuple[int, int]] = field(default_factory=list)
    lines: list[list[int]] = field(default_factory=list)  # 曲面的网格线（比逐面边更干净）
    closed: bool = True
    title: str = ""
    # 下面三项只有「场景」类型会用到，顺序与上面的列表一一对应
    labels: list[tuple[str, tuple[float, float, float]]] = field(default_factory=list)
    line_styles: list[str] = field(default_factory=list)
    face_opacities: list[float] = field(default_factory=list)


@dataclass
class Model3DSpec:
    """一次三维建模请求（参数语义见《项目文档》§8.4.3）。"""

    kind: str
    title: str = ""
    caption: str = ""
    size_a: float | None = None   # box→宽；cylinder/cone/sphere→半径；prism/pyramid→外接圆半径；torus→主半径
    size_b: float | None = None   # box→高；cylinder/cone/prism/pyramid→高；torus→管半径
    size_c: float | None = None   # box→深
    expression: str = ""
    x_min: float = -1.0
    x_max: float = 1.0
    y_min: float = -1.0
    y_max: float = 1.0
    grid: int = 21
    segments: int = 24
    az: float = -35.0
    el: float = 25.0
    width: int = 720
    height: int = 480
    scene: dict[str, Any] | None = None  # 仅 kind="scene"：线段 / 面 / 标注


@dataclass
class Model3DResult:
    ok: bool
    svg: str = ""
    html: str = ""
    faces: int = 0
    issues: list[str] = field(default_factory=list)
    fallback_md: str = ""


# --- 小工具 ---------------------------------------------------------------
def _clamp_int(value: Any, low: int, high: int, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(low, min(high, number))


def _positive(value: Any, name: str, default: float) -> float:
    number = default if value is None else float(value)
    if not math.isfinite(number) or number <= 0:
        raise GeometryError(f"{name} 必须是正数（收到 {value!r}）")
    return number


def _dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: Sequence[float], b: Sequence[float]) -> tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _normal(value: Sequence[float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot(value, value))
    if length < 1e-12:
        return (0.0, 0.0, 1.0)
    return (value[0] / length, value[1] / length, value[2] / length)


def _ring(count: int, radius: float, z: float) -> list[tuple[float, float, float]]:
    return [
        (radius * math.cos(2 * math.pi * index / count), radius * math.sin(2 * math.pi * index / count), z)
        for index in range(count)
    ]


def _edge_pairs(faces: Sequence[Sequence[int]]) -> list[tuple[int, int]]:
    seen: set[tuple[int, int]] = set()
    pairs: list[tuple[int, int]] = []
    for face in faces:
        for index, start in enumerate(face):
            end = face[(index + 1) % len(face)]
            key = (start, end) if start < end else (end, start)
            if key not in seen:
                seen.add(key)
                pairs.append((start, end))
    return pairs


def _signed_volume(vertices: Sequence[Sequence[float]], faces: Sequence[Sequence[int]]) -> float:
    """闭合网格的有向体积：为正说明面是「外法向朝外」的一致朝向。"""
    total = 0.0
    for face in faces:
        for index in range(1, len(face) - 1):
            a, b, c = vertices[face[0]], vertices[face[index]], vertices[face[index + 1]]
            total += _dot(a, _cross(b, c))
    return total / 6.0


def _orient_outward(geometry: Geometry) -> Geometry:
    """闭合网格若整体朝里，就把所有面翻过来（防止某个 kind 的顶点顺序写反）。"""
    if not geometry.closed or not geometry.faces:
        return geometry
    if _signed_volume(geometry.vertices, geometry.faces) < 0:
        geometry.faces = [list(reversed(face)) for face in geometry.faces]
    return geometry


# --- 几何构造 -------------------------------------------------------------
def build_geometry(spec: Model3DSpec) -> Geometry:
    """按 ``kind`` 造出网格；参数不合法抛 ``GeometryError``。"""
    kind = str(spec.kind or "").strip().lower()
    if kind not in KINDS:
        raise GeometryError(f"不认识的三维类型：{spec.kind!r}（可选 {'/'.join(KINDS)}）")
    segments = _clamp_int(spec.segments, MIN_SEGMENTS, MAX_SEGMENTS, 24)
    grid = _clamp_int(spec.grid, MIN_GRID, MAX_GRID, 21)

    if kind == "box":
        width = _positive(spec.size_a, "长（size_a）", 1.6)
        depth = _positive(spec.size_c, "宽（size_c）", 1.2)
        height = _positive(spec.size_b, "高（size_b）", 1.0)
        geometry = _box(width, depth, height)
    elif kind == "cylinder":
        geometry = _sweep(_positive(spec.size_a, "半径（size_a）", 0.7), _positive(spec.size_b, "高（size_b）", 1.6), segments)
    elif kind == "cone":
        geometry = _cone(_positive(spec.size_a, "半径（size_a）", 0.8), _positive(spec.size_b, "高（size_b）", 1.6), segments)
    elif kind == "sphere":
        geometry = _sphere(_positive(spec.size_a, "半径（size_a）", 1.0), segments)
    elif kind == "prism":
        sides = _clamp_int(spec.segments, MIN_PRISM_SIDES, MAX_PRISM_SIDES, 6)
        geometry = _sweep(_positive(spec.size_a, "外接圆半径（size_a）", 0.9), _positive(spec.size_b, "高（size_b）", 1.4), sides)
    elif kind == "pyramid":
        sides = _clamp_int(spec.segments, MIN_PRISM_SIDES, MAX_PRISM_SIDES, 4)
        geometry = _cone(_positive(spec.size_a, "外接圆半径（size_a）", 0.9), _positive(spec.size_b, "高（size_b）", 1.4), sides)
    elif kind == "torus":
        geometry = _torus(
            _positive(spec.size_a, "主半径（size_a）", 1.0),
            _positive(spec.size_b, "管半径（size_b）", 0.35),
            segments,
            max(6, segments // 2),
        )
    elif kind == "scene":
        geometry = _scene(spec)
    else:
        geometry = _surface(spec, grid)

    geometry.title = str(spec.title or "")
    return _orient_outward(geometry)


def _box(width: float, depth: float, height: float) -> Geometry:
    xs, ys, zs = width / 2, depth / 2, height / 2
    vertices = [
        (-xs, -ys, -zs), (xs, -ys, -zs), (xs, ys, -zs), (-xs, ys, -zs),
        (-xs, -ys, zs), (xs, -ys, zs), (xs, ys, zs), (-xs, ys, zs),
    ]
    faces = [
        [0, 3, 2, 1],  # 底
        [4, 5, 6, 7],  # 顶
        [0, 1, 5, 4],  # 前
        [1, 2, 6, 5],  # 右
        [2, 3, 7, 6],  # 后
        [3, 0, 4, 7],  # 左
    ]
    return Geometry(vertices=vertices, faces=faces, edges=_edge_pairs(faces), closed=True)


def _sweep(radius: float, height: float, sides: int) -> Geometry:
    """圆柱 / 棱柱：两圈顶点 + 侧面四边形 + 上下底。"""
    bottom = _ring(sides, radius, -height / 2)
    top = _ring(sides, radius, height / 2)
    vertices = bottom + top
    faces: list[list[int]] = []
    for index in range(sides):
        nxt = (index + 1) % sides
        faces.append([index, nxt, sides + nxt, sides + index])
    faces.append(list(reversed(range(sides))))                      # 底（法向朝下）
    faces.append([sides + index for index in range(sides)])         # 顶（法向朝上）
    return Geometry(vertices=vertices, faces=faces, edges=_edge_pairs(faces), closed=True)


def _cone(radius: float, height: float, sides: int) -> Geometry:
    """圆锥 / 棱锥：底面多边 + 顶点。"""
    vertices = _ring(sides, radius, -height / 2) + [(0.0, 0.0, height / 2)]
    apex = sides
    faces: list[list[int]] = []
    for index in range(sides):
        faces.append([index, (index + 1) % sides, apex])
    faces.append(list(reversed(range(sides))))
    return Geometry(vertices=vertices, faces=faces, edges=_edge_pairs(faces), closed=True)


def _sphere(radius: float, segments: int) -> Geometry:
    """经线 / 纬线细分球：上下极点三角形 + 中间四边形环。"""
    rings = max(3, segments // 2)
    vertices: list[tuple[float, float, float]] = [(0.0, 0.0, radius)]
    for ring in range(1, rings):
        phi = math.pi * ring / rings
        vertices.extend(_ring(segments, radius * math.sin(phi), radius * math.cos(phi)))
    bottom = len(vertices)
    vertices.append((0.0, 0.0, -radius))
    faces: list[list[int]] = []
    first = 1
    for column in range(segments):
        faces.append([0, first + column, first + (column + 1) % segments])
    for ring in range(rings - 2):
        upper = 1 + ring * segments
        lower = upper + segments
        for column in range(segments):
            nxt = (column + 1) % segments
            faces.append([upper + column, lower + column, lower + nxt, upper + nxt])
    last = 1 + (rings - 2) * segments
    for column in range(segments):
        faces.append([last + column, bottom, last + (column + 1) % segments])
    return Geometry(vertices=vertices, faces=faces, edges=_edge_pairs(faces), closed=True)


def _torus(major: float, minor: float, around: int, tube: int) -> Geometry:
    vertices: list[tuple[float, float, float]] = []
    for index in range(around):
        theta = 2 * math.pi * index / around
        for jndex in range(tube):
            phi = 2 * math.pi * jndex / tube
            radial = major + minor * math.cos(phi)
            vertices.append((radial * math.cos(theta), radial * math.sin(theta), minor * math.sin(phi)))
    faces: list[list[int]] = []
    for index in range(around):
        next_index = (index + 1) % around
        for jndex in range(tube):
            next_jndex = (jndex + 1) % tube
            faces.append(
                [
                    index * tube + jndex,
                    next_index * tube + jndex,
                    next_index * tube + next_jndex,
                    index * tube + next_jndex,
                ]
            )
    return Geometry(vertices=vertices, faces=faces, edges=_edge_pairs(faces), closed=True)


def _scene_point(value: Any, name: str) -> tuple[float, float, float]:
    """场景里的一个三维点：必须是 ``[x, y, z]`` 三个有限数。"""
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        raise GeometryError(f"{name} 必须是 [x, y, z] 三个数（收到 {value!r}）")
    numbers: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError) as exc:
            raise GeometryError(f"{name} 里含非数字：{item!r}") from exc
        if not math.isfinite(number) or abs(number) > SCENE_COORD_LIMIT:
            raise GeometryError(f"{name} 的坐标必须是有限数，且绝对值不超过 {SCENE_COORD_LIMIT:g}")
        numbers.append(number)
    return (numbers[0], numbers[1], numbers[2])


def _scene(spec: Model3DSpec) -> Geometry:
    """简单三维场景：线段（含光路箭头）/ 面 / 文字标注。

    小孔成像、光线传播这类**光路或空间关系图**用实体几何表达不了，所以单独开一种类型：
    模型只给「点 + 线 + 面 + 标注」这套最小语言，投影与渲染仍由本模块负责。
    """
    scene = spec.scene if isinstance(spec.scene, dict) else {}
    raw_segments = scene.get("segments") or []
    raw_faces = scene.get("faces") or []
    raw_labels = scene.get("labels") or []
    for value, name, limit in (
        (raw_segments, "segments", SCENE_MAX_SEGMENTS),
        (raw_faces, "faces", SCENE_MAX_FACES),
        (raw_labels, "labels", SCENE_MAX_LABELS),
    ):
        if not isinstance(value, list):
            raise GeometryError(f"scene.{name} 必须是数组")
        if len(value) > limit:
            raise GeometryError(f"scene.{name} 最多 {limit} 项（收到 {len(value)} 项）")
    if not raw_segments and not raw_faces:
        raise GeometryError("场景至少要有 segments（线段）或 faces（面）之一")

    geometry = Geometry(vertices=[], faces=[], closed=False)

    def add(vertex: tuple[float, float, float]) -> int:
        geometry.vertices.append(vertex)
        if len(geometry.vertices) > SCENE_MAX_VERTICES:
            raise GeometryError(f"场景顶点数超过 {SCENE_MAX_VERTICES}（把线段/面减少一些）")
        return len(geometry.vertices) - 1

    for index, item in enumerate(raw_segments):
        if not isinstance(item, dict):
            raise GeometryError(f"scene.segments[{index}] 必须是对象")
        start = add(_scene_point(item.get("from"), f"scene.segments[{index}].from"))
        end = add(_scene_point(item.get("to"), f"scene.segments[{index}].to"))
        style = str(item.get("style") or "solid").strip().lower()
        geometry.lines.append([start, end])
        geometry.line_styles.append(style if style in SCENE_STYLES else "solid")
        label = str(item.get("label") or "").strip()
        if label:
            middle = geometry.vertices[start]
            other = geometry.vertices[end]
            geometry.labels.append(
                (label[:SCENE_LABEL_CHARS], ((middle[0] + other[0]) / 2, (middle[1] + other[1]) / 2, (middle[2] + other[2]) / 2))
            )

    for index, item in enumerate(raw_faces):
        if not isinstance(item, dict):
            raise GeometryError(f"scene.faces[{index}] 必须是对象")
        points = item.get("points")
        if not isinstance(points, list) or not 3 <= len(points) <= 8:
            raise GeometryError(f"scene.faces[{index}].points 需要 3–8 个点")
        face = [add(_scene_point(point, f"scene.faces[{index}].points")) for point in points]
        geometry.faces.append(face)
        opacity = item.get("opacity")
        if opacity is None:
            geometry.face_opacities.append(0.14)
        else:
            try:
                geometry.face_opacities.append(max(0.02, min(0.6, float(opacity))))
            except (TypeError, ValueError):
                geometry.face_opacities.append(0.14)
        label = str(item.get("label") or "").strip()
        if label:
            centre = (
                sum(geometry.vertices[vertex][0] for vertex in face) / len(face),
                sum(geometry.vertices[vertex][1] for vertex in face) / len(face),
                sum(geometry.vertices[vertex][2] for vertex in face) / len(face),
            )
            geometry.labels.append((label[:SCENE_LABEL_CHARS], centre))

    for index, item in enumerate(raw_labels):
        if not isinstance(item, dict):
            raise GeometryError(f"scene.labels[{index}] 必须是对象")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        geometry.labels.append(
            (text[:SCENE_LABEL_CHARS], _scene_point(item.get("at"), f"scene.labels[{index}].at"))
        )
    if not geometry.faces:
        geometry.edges = []  # 只有线段时不需要按面推边
    return geometry


def _surface(spec: Model3DSpec, grid: int) -> Geometry:
    """曲面 z = f(x, y)：复用函数绘图那套安全求值器。"""
    from .expr_eval import ExprError, compile_expr

    x_min, x_max = float(spec.x_min), float(spec.x_max)
    y_min, y_max = float(spec.y_min), float(spec.y_max)
    if not (math.isfinite(x_min) and math.isfinite(x_max) and math.isfinite(y_min) and math.isfinite(y_max)):
        raise GeometryError("曲面的取值范围必须是有限数")
    if x_min >= x_max or y_min >= y_max:
        raise GeometryError("曲面的取值范围要满足 x_min < x_max 且 y_min < y_max")

    import numpy as np  # 懒导入：不进应用启动路径

    xs = np.linspace(x_min, x_max, grid)
    ys = np.linspace(y_min, y_max, grid)
    mesh_x, mesh_y = np.meshgrid(xs, ys)
    try:
        function = compile_expr(str(spec.expression or ""), variables=("x", "y"))
        mesh_z = function(x=mesh_x, y=mesh_y)
    except ExprError as exc:
        raise GeometryError(f"曲面表达式不合法：{exc}") from exc
    except Exception as exc:  # noqa: BLE001 - 求值期异常（除零、溢出等）同样退回结构化错误
        raise GeometryError(f"曲面表达式求值失败：{exc}") from exc
    values = np.asarray(mesh_z, dtype=float)
    if values.shape != mesh_x.shape:
        values = np.broadcast_to(values, mesh_x.shape).astype(float)
    if not np.any(np.isfinite(values)):
        raise GeometryError("这个范围内曲面没有有限值（换表达式或换区间）")

    finite = np.isfinite(values)
    span = float(np.max(values[finite]) - np.min(values[finite])) or 1.0
    half_span = max(float(np.max(np.abs([x_min, x_max, y_min, y_max]))), 1e-6)
    scale = 1.6 * half_span / span  # 压到与平面尺度同量级，视角才好看

    vertices: list[tuple[float, float, float]] = []
    for row in range(grid):
        for column in range(grid):
            z_value = float(values[row][column])
            if not math.isfinite(z_value):
                z_value = 0.0
            else:
                z_value *= scale
            vertices.append((float(mesh_x[row][column]), float(mesh_y[row][column]), z_value))

    faces: list[list[int]] = []
    for row in range(grid - 1):
        for column in range(grid - 1):
            corners = (row * grid + column, row * grid + column + 1, (row + 1) * grid + column + 1, (row + 1) * grid + column)
            if all(finite[index // grid][index % grid] for index in corners):
                faces.append(list(corners))

    lines: list[list[int]] = []
    for row in range(grid):
        lines.append([row * grid + column for column in range(grid)])
    for column in range(grid):
        lines.append([row * grid + column for row in range(grid)])
    return Geometry(vertices=vertices, faces=faces, edges=[], lines=lines, closed=False)


# --- 投影 -----------------------------------------------------------------
def _basis(az: float, el: float) -> tuple[tuple[float, float, float], ...]:
    """相机基：``(视线方向, 屏幕右, 屏幕上)``，视线从原点指向相机。"""
    azimuth, elevation = math.radians(az), math.radians(el)
    direction = (
        math.cos(elevation) * math.cos(azimuth),
        math.cos(elevation) * math.sin(azimuth),
        math.sin(elevation),
    )
    right = _cross((0.0, 0.0, 1.0), direction)
    if math.sqrt(_dot(right, right)) < 1e-9:
        right = (1.0, 0.0, 0.0)
    right = _normal(right)
    up = _cross(direction, right)
    return direction, right, up


def _camera(vertices: Sequence[Sequence[float]], az: float, el: float) -> tuple[list[tuple[float, float, float]], tuple[float, float, float], float]:
    """转成相机坐标 ``(右, 上, 深度)``；深度越大越远。"""
    direction, right, up = _basis(az, el)
    radius = max((math.sqrt(_dot(vertex, vertex)) for vertex in vertices), default=1.0) or 1.0
    distance = 3.2 * radius
    points = []
    for vertex in vertices:
        depth = max(distance - _dot(vertex, direction), 0.08 * radius)
        scale = distance / depth
        points.append((_dot(vertex, right) * scale, _dot(vertex, up) * scale, depth))
    return points, direction, radius


def project(
    vertices: Sequence[Sequence[float]],
    *,
    az: float = -35.0,
    el: float = 25.0,
    width: int = 720,
    height: int = 480,
    margin: float = 0.12,
) -> tuple[list[tuple[float, float]], list[float]]:
    """透视投影 → 屏幕坐标（已按画布自适应缩放到 ``margin`` 边距）。"""
    points, _, _ = _camera(vertices, az, el)
    xs = [point[0] for point in points] or [0.0]
    ys = [point[1] for point in points] or [0.0]
    span_x = (max(xs) - min(xs)) or 1.0
    span_y = (max(ys) - min(ys)) or 1.0
    scale = min(width * (1 - 2 * margin) / span_x, height * (1 - 2 * margin) / span_y)
    center_x = width / 2 - (min(xs) + max(xs)) / 2 * scale
    center_y = height / 2 + (min(ys) + max(ys)) / 2 * scale
    screen = [(center_x + point[0] * scale, center_y - point[1] * scale) for point in points]
    return screen, [point[2] for point in points]


def _face_normal(vertices: Sequence[Sequence[float]], face: Sequence[int]) -> tuple[float, float, float]:
    if len(face) < 3:
        return (0.0, 0.0, 1.0)
    a, b, c = vertices[face[0]], vertices[face[1]], vertices[face[2]]
    return _normal(_cross((b[0] - a[0], b[1] - a[1], b[2] - a[2]), (c[0] - b[0], c[1] - b[1], c[2] - b[2])))


# --- 静态 SVG -------------------------------------------------------------
def build_static_svg(geometry: Geometry, spec: Model3DSpec) -> str:
    """把网格画成一张静态投影 SVG（无 ``xmlns``，必过清洗器）。"""
    width = max(200, int(spec.width or 720))
    height = max(150, int(spec.height or 480))
    # 标注锚点与顶点一起投影：共用同一次自适应取景，标注才不会跑到画布外
    anchors = [position for _, position in geometry.labels]
    projected, depths = project(
        list(geometry.vertices) + anchors, az=spec.az, el=spec.el, width=width, height=height
    )
    screen = projected[: len(geometry.vertices)]
    label_screen = projected[len(geometry.vertices) :]
    title = str(spec.title or geometry.title or "三维示意图")
    description = f"{title}：{len(geometry.vertices)} 个顶点、{len(geometry.faces)} 个面，透视投影视图。"

    parts: list[str] = [
        f'<svg viewBox="0 0 {width} {height}">',
        f"<title>{_escape(title)}</title>",
        f"<desc>{_escape(description)}</desc>",
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="none" stroke="none" stroke-width="0"/>',
    ]
    filled = _svg_faces(geometry, screen, depths, spec)
    parts.extend(filled)
    parts.extend(_svg_wire(geometry, screen, coarse=bool(filled)))
    parts.extend(_svg_labels(geometry, label_screen))
    if str(spec.kind or "") != "scene":  # 光路这类场景图不需要坐标轴，留白更干净
        parts.extend(_svg_axes(geometry, spec, screen, width, height))
    parts.append("</svg>")
    return "".join(parts)


def _svg_faces(
    geometry: Geometry,
    screen: Sequence[tuple[float, float]],
    depths: Sequence[float],
    spec: Model3DSpec,
) -> list[str]:
    """面：按深度从远到近排序，再切成几档着色，各档并成一条大 path。"""
    if not geometry.faces or len(geometry.faces) > MAX_FILLED_FACES:
        return []
    direction, _, _ = _basis(spec.az, spec.el)
    order: list[tuple[float, int, float]] = []
    for index, face in enumerate(geometry.faces):
        normal = _face_normal(geometry.vertices, face)
        facing = _dot(normal, direction)
        if geometry.closed and facing <= 0:
            continue  # 背面剔除（闭合实体看不到的面不画）
        mean_depth = sum(depths[vertex] for vertex in face) / len(face)
        order.append((mean_depth, index, abs(facing)))
    if not order:
        return []
    order.sort(reverse=True)  # 远的先画

    if geometry.face_opacities:
        # 场景图：每个面自己的透明度，逐面画（面数有额度上限，元素数不会爆）
        parts: list[str] = []
        for _, face_index, _ in order:
            opacity = (
                geometry.face_opacities[face_index]
                if face_index < len(geometry.face_opacities)
                else 0.14
            )
            parts.append(
                f'<path d="{_polygon_path(geometry.faces[face_index], screen)}" fill="{ACCENT}" '
                f'fill-opacity="{opacity:.2f}" stroke="none" fill-rule="evenodd"/>'
            )
        return parts

    chunk = max(1, math.ceil(len(order) / FILL_BANDS))
    parts: list[str] = []
    for band in range(0, len(order), chunk):
        block = order[band : band + chunk]
        brightness = sum(item[2] for item in block) / len(block)
        opacity = 0.10 + 0.22 * min(1.0, brightness)
        path = "".join(_polygon_path(geometry.faces[item[1]], screen) for item in block)
        parts.append(
            f'<path d="{path}" fill="{ACCENT}" fill-opacity="{opacity:.2f}" stroke="none" fill-rule="evenodd"/>'
        )
    return parts


def _polygon_path(face: Sequence[int], screen: Sequence[tuple[float, float]]) -> str:
    points = " L ".join(f"{_fmt(screen[vertex][0])} {_fmt(screen[vertex][1])}" for vertex in face)
    return f"M {points} Z "


def _svg_wire(geometry: Geometry, screen: Sequence[tuple[float, float]], *, coarse: bool = False) -> list[str]:
    """线框：优先用网格线（曲面），否则用面的去重边；段数超限就抽稀。

    场景图会用到 ``line_styles``：``dashed`` 画虚线（辅助线 / 虚像），``arrow`` 在末端加箭头（光路）。
    """
    chains = list(geometry.lines)
    styles = list(geometry.line_styles)
    if chains and coarse:
        keep = list(range(0, len(chains), 2))  # 面已经着色时线框画粗一点，更干净也更小
        chains = [chains[index] for index in keep]
        styles = [styles[index] if index < len(styles) else "solid" for index in keep]

    solid: list[str] = []
    dashed: list[str] = []
    arrows: list[str] = []
    if chains:
        stride = 1
        while stride < 16 and sum(max(0, len(chain) - 1) for chain in chains[::stride]) > MAX_WIRE_SEGMENTS:
            stride += 1
        for index, chain in enumerate(chains):
            if index % stride:
                continue
            style = styles[index] if index < len(styles) else "solid"
            for start, end in zip(chain, chain[1:]):
                if style == "dashed":
                    dashed.append(_segment(screen[start], screen[end]))
                else:
                    solid.append(_segment(screen[start], screen[end]))
                if style == "arrow":
                    arrows.append(_arrowhead(screen[start], screen[end]))
    else:
        edges = geometry.edges or _edge_pairs(geometry.faces)
        step = max(1, math.ceil(len(edges) / MAX_WIRE_SEGMENTS))
        for start, end in edges[::step]:
            solid.append(_segment(screen[start], screen[end]))

    parts: list[str] = []
    if solid:
        parts.append(
            '<path d="' + "".join(solid) + '" fill="none" stroke="currentColor" '
            'stroke-opacity="0.55" stroke-width="1" stroke-linecap="round"/>'
        )
    if dashed:
        parts.append(
            '<path d="' + "".join(dashed) + '" fill="none" stroke="currentColor" '
            'stroke-opacity="0.5" stroke-width="1" stroke-dasharray="5 4" stroke-linecap="round"/>'
        )
    if arrows:
        parts.append(
            '<path d="' + "".join(arrows) + '" fill="currentColor" fill-opacity="0.75" stroke="none"/>'
        )
    return parts


def _arrowhead(start: tuple[float, float], end: tuple[float, float], *, size: float = 7.0) -> str:
    """在投影平面上画一个小箭头（方向 = 起点 → 终点）。"""
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return ""
    ux, uy = dx / length, dy / length
    base = (end[0] - ux * size, end[1] - uy * size)
    left = (base[0] - uy * size * 0.45, base[1] + ux * size * 0.45)
    right = (base[0] + uy * size * 0.45, base[1] - ux * size * 0.45)
    return (
        f"M {_fmt(end[0])} {_fmt(end[1])} L {_fmt(left[0])} {_fmt(left[1])} "
        f"L {_fmt(right[0])} {_fmt(right[1])} Z "
    )


def _svg_labels(geometry: Geometry, screen: Sequence[tuple[float, float]]) -> list[str]:
    """场景图的文字标注（物 / 小孔 / 屏 / 像……）。"""
    parts: list[str] = []
    for (text, _), point in zip(geometry.labels, screen):
        parts.append(
            f'<text x="{_fmt(point[0] + 6)}" y="{_fmt(point[1] - 6)}" font-size="13" '
            f'fill="currentColor" font-family="Microsoft YaHei, sans-serif">{_escape(text)}</text>'
        )
    return parts


def _segment(start: tuple[float, float], end: tuple[float, float]) -> str:
    return f"M {_fmt(start[0])} {_fmt(start[1])} L {_fmt(end[0])} {_fmt(end[1])} "


def _svg_axes(
    geometry: Geometry,
    spec: Model3DSpec,
    screen: Sequence[tuple[float, float]],
    width: int,
    height: int,
) -> list[str]:
    """坐标轴：模型中心向外三条轴线 + 字母标注。"""
    radius = max((math.sqrt(_dot(vertex, vertex)) for vertex in geometry.vertices), default=1.0) or 1.0
    length = radius * AXIS_LENGTH_SCALE
    origin = (0.0, 0.0, 0.0)
    axes = ((length, 0.0, 0.0, "x"), (0.0, length, 0.0, "y"), (0.0, 0.0, length, "z"))
    all_vertices = list(geometry.vertices) + [origin] + [(item[0], item[1], item[2]) for item in axes]
    projected, _ = project(all_vertices, az=spec.az, el=spec.el, width=width, height=height)
    base = len(geometry.vertices)
    origin_point = projected[base]
    parts: list[str] = []
    lines: list[str] = []
    for index, (_, _, _, label) in enumerate(axes):
        tip = projected[base + 1 + index]
        lines.append(_segment(origin_point, tip))
        parts.append(
            f'<text x="{_fmt(tip[0] + 4)}" y="{_fmt(tip[1] - 4)}" font-size="13" '
            f'fill="currentColor" font-family="Microsoft YaHei, sans-serif">{label}</text>'
        )
    parts.insert(
        0,
        '<path d="' + "".join(lines) + '" fill="none" stroke="currentColor" stroke-opacity="0.45" '
        'stroke-width="1" stroke-dasharray="4 3"/>',
    )
    return parts


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _fmt(value: float) -> str:
    return f"{float(value):.1f}"


# --- 交互 HTML -------------------------------------------------------------
def build_viewer_html(
    geometry: Geometry,
    spec: Model3DSpec,
    *,
    theme: Mapping[str, str] | None = None,
) -> str:
    """固定模板 + 注入几何 JSON → 加 CSP 与主题变量（《技术文档》§4.7）。"""
    from .sandbox import prepare_animation_html

    template = VIEWER_TEMPLATE.read_text(encoding="utf-8")
    payload = {
        "title": str(spec.title or geometry.title or "三维视图"),
        "az": float(spec.az),
        "el": float(spec.el),
        "vertices": [[round(float(value), 3) for value in vertex] for vertex in geometry.vertices],
        "faces": [list(face) for face in geometry.faces],
        "lines": [list(chain) for chain in geometry.lines],
        "line_styles": [str(style) for style in geometry.line_styles],
        "face_opacities": [round(float(value), 3) for value in geometry.face_opacities],
        "labels": [
            {"text": str(text), "at": [round(float(value), 3) for value in position]}
            for text, position in geometry.labels
        ],
        "closed": bool(geometry.closed),
        "axes": str(spec.kind or "") != "scene",  # 光路场景不画坐标轴
    }
    # ``<`` 必须转义，否则 JSON 里的 ``</script>`` 会提前闭合脚本块
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    html = prepare_animation_html(template.replace(GEOMETRY_TOKEN, data), dict(theme) if theme else None)
    if len(html.encode("utf-8")) > MAX_HTML_BYTES:
        raise GeometryError(
            f"三维交互页超过 {MAX_HTML_BYTES // 1024}KB（当前 {len(html.encode('utf-8')) // 1024}KB），请降低 grid 或 segments"
        )
    return html


def build_model3d(spec: Model3DSpec, *, theme: Mapping[str, str] | None = None) -> Model3DResult:
    """一次完整的出图：几何 → 静态 SVG（+ 交互 HTML）；失败只返回结构化错误。"""
    try:
        geometry = build_geometry(spec)
    except (GeometryError, ValueError) as exc:
        return Model3DResult(
            ok=False,
            issues=[str(exc)],
            fallback_md=f"（三维示意图「{spec.title or spec.kind}」生成失败：{exc}）",
        )

    svg = build_static_svg(geometry, spec)
    issues: list[str] = []
    try:
        html = build_viewer_html(geometry, spec, theme=theme)
    except Exception as exc:  # noqa: BLE001 - 模板缺失 / 体积超限都退回静态图
        html = ""
        issues.append(f"交互视图不可用：{exc}")
    return Model3DResult(ok=True, svg=svg, html=html, faces=len(geometry.faces), issues=issues)
