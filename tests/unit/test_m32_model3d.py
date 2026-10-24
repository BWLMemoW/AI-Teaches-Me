"""M32 测试：三维简单建模（几何构造 / 透视投影 / 静态 SVG / 交互 HTML）。

守住三件事：

1. 几何是对的：顶点数、面数、朝向（闭合网格的有向体积为正）都可验证；
2. 静态 SVG 能真的画到讲义上：过 ``svg_sanitizer``、有 ``<title>``/``<desc>``、元素数与体积受控；
3. 交互 HTML 守红线：≤80KB、无外链、无 CDN、支持 ``prefers-reduced-motion``，几何 JSON 可被解析。
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ElementTree

import pytest

from app.infra.model3d import (
    KINDS,
    Geometry,
    GeometryError,
    Model3DSpec,
    build_geometry,
    build_model3d,
    build_static_svg,
    build_viewer_html,
    project,
)
from app.infra.svg_sanitizer import clean, is_acceptable

pytestmark = pytest.mark.no_network

MAX_SVG_ELEMENTS = 400
MAX_SVG_BYTES = 40 * 1024
MAX_HTML_BYTES = 80 * 1024


def _spec(kind: str, **kwargs) -> Model3DSpec:
    return Model3DSpec(kind=kind, **kwargs)


def _elements(svg: str) -> int:
    return sum(1 for _ in ElementTree.fromstring(svg).iter())


# --- 几何构造 -------------------------------------------------------------
def test_box_geometry_is_correct() -> None:
    geometry = build_geometry(_spec("box", size_a=2.0, size_b=1.0, size_c=3.0))

    assert len(geometry.vertices) == 8
    assert len(geometry.faces) == 6
    assert geometry.closed
    xs = [abs(vertex[0]) for vertex in geometry.vertices]
    zs = [abs(vertex[2]) for vertex in geometry.vertices]
    assert max(xs) == pytest.approx(1.0)
    assert max(zs) == pytest.approx(0.5)


@pytest.mark.parametrize("kind", ["box", "cylinder", "cone", "sphere", "prism", "pyramid", "torus"])
def test_solid_meshes_face_outward(kind: str) -> None:
    """闭合网格的有向体积为正 = 面是逆时针、外法向朝外（否则渲染会「里外翻转」）。"""
    geometry = build_geometry(_spec(kind, segments=12))

    total = 0.0
    for face in geometry.faces:
        for index in range(1, len(face) - 1):
            a = geometry.vertices[face[0]]
            b = geometry.vertices[face[index]]
            c = geometry.vertices[face[index + 1]]
            cross = (
                b[1] * c[2] - b[2] * c[1],
                b[2] * c[0] - b[0] * c[2],
                b[0] * c[1] - b[1] * c[0],
            )
            total += a[0] * cross[0] + a[1] * cross[1] + a[2] * cross[2]
    assert total / 6.0 > 0


@pytest.mark.parametrize(
    "kind, segments, vertices, faces",
    [
        ("cylinder", 12, 24, 14),
        ("cone", 12, 13, 13),
        ("prism", 6, 12, 8),
        ("pyramid", 4, 5, 5),
    ],
)
def test_solids_vertex_and_face_counts(kind: str, segments: int, vertices: int, faces: int) -> None:
    geometry = build_geometry(_spec(kind, segments=segments))

    assert len(geometry.vertices) == vertices
    assert len(geometry.faces) == faces


def test_segments_and_grid_are_clamped() -> None:
    dense = build_geometry(_spec("cylinder", segments=1000))
    assert len(dense.vertices) == 2 * 48

    coarse = build_geometry(_spec("cylinder", segments=1))
    assert len(coarse.vertices) == 2 * 6

    grid = build_geometry(_spec("surface", expression="x+y", grid=1000))
    assert len(grid.vertices) == 31 * 31

    tiny = build_geometry(_spec("surface", expression="x+y", grid=2))
    assert len(tiny.vertices) == 5 * 5


def test_surface_reuses_safe_evaluator() -> None:
    geometry = build_geometry(
        _spec("surface", expression="sin(x)*cos(y)", x_min=-2.0, x_max=2.0, y_min=-2.0, y_max=2.0, grid=21)
    )

    assert len(geometry.vertices) == 441
    assert len(geometry.faces) == 400
    assert geometry.lines and len(geometry.lines) == 42
    assert not geometry.closed


def test_bad_parameters_raise_geometry_error() -> None:
    cases = (
        _spec("tesseract"),
        _spec("box", size_a=-1.0),
        _spec("cylinder", size_b=0.0),
        _spec("surface", expression="__import__('os')"),
        _spec("surface", expression="x.__class__"),
        _spec("surface", expression="1/0"),
        _spec("surface", expression="x+y", x_min=1.0, x_max=1.0),
    )
    for spec in cases:
        with pytest.raises(GeometryError):
            build_geometry(spec)

    result = build_model3d(_spec("surface", expression="__import__('os')"))
    assert result.ok is False and result.issues and result.fallback_md


@pytest.mark.parametrize("kind", KINDS)
def test_every_kind_builds(kind: str) -> None:
    spec = _spec(kind)
    if kind == "surface":
        spec = _spec(kind, expression="sin(x)*cos(y)")
    elif kind == "scene":
        spec = _spec(kind, scene=PINHOLE_SCENE)
    result = build_model3d(spec)

    assert result.ok is True
    assert result.faces > 0


# --- 投影 -----------------------------------------------------------------
def test_projection_reacts_to_azimuth_and_depth_is_monotonic() -> None:
    geometry = build_geometry(_spec("box"))
    first, depths = project(geometry.vertices, az=-35.0, el=25.0)
    second, _ = project(geometry.vertices, az=45.0, el=25.0)

    assert any(
        abs(a[0] - b[0]) > 1e-6 or abs(a[1] - b[1]) > 1e-6
        for a, b in zip(first, second)
    )

    # 深度越大越远：把相机搬到 +x 方向，最靠前的顶点必然是 x 最大的那个
    _, depths_x = project(geometry.vertices, az=0.0, el=0.0)
    nearest = min(range(len(depths_x)), key=lambda index: depths_x[index])
    assert geometry.vertices[nearest][0] == pytest.approx(0.8)


def test_projection_fits_inside_canvas() -> None:
    geometry = build_geometry(_spec("sphere"))
    screen, _ = project(geometry.vertices, width=400, height=300)

    assert all(0 <= point[0] <= 400 and 0 <= point[1] <= 300 for point in screen)


# --- 静态 SVG -------------------------------------------------------------
@pytest.mark.parametrize("kind", KINDS)
def test_static_svg_passes_sanitizer_and_stays_small(kind: str) -> None:
    spec = _spec(kind)
    if kind == "surface":
        spec = _spec(kind, expression="sin(x)*cos(y)", grid=21)
    elif kind == "scene":
        spec = _spec(kind, scene=PINHOLE_SCENE)
    geometry = build_geometry(spec)
    svg = build_static_svg(geometry, spec)

    checked = clean(svg)
    assert is_acceptable(checked)
    assert "ns0:" not in checked.svg, "带 xmlns 的 SVG 会被清洗器序列化成 ns0: 前缀，浏览器认不出来"
    assert checked.svg.lstrip().startswith("<svg")
    assert "<title>" in svg and "<desc>" in svg
    assert _elements(svg) <= MAX_SVG_ELEMENTS, _elements(svg)
    assert len(svg.encode("utf-8")) <= MAX_SVG_BYTES


@pytest.mark.ui
def test_static_svg_renders_to_png(qtbot) -> None:
    """QtSvg 需要一个 QApplication（`qtbot` 提供）；没有它 QSvgRenderer 会判非法、返回空。"""
    from app.infra.svg_renderer import render_svg_png

    spec = _spec("box")
    payload = render_svg_png(build_static_svg(build_geometry(spec), spec), width=400)

    assert payload.startswith(b"\x89PNG")


def test_dense_surface_switches_to_wireframe() -> None:
    """面太多时只画线框，元素数不会爆。"""
    spec = _spec("surface", expression="sin(x)*cos(y)", grid=31)
    svg = build_static_svg(build_geometry(spec), spec)

    assert svg.count("<path") <= 3
    assert _elements(svg) <= MAX_SVG_ELEMENTS
    assert len(svg.encode("utf-8")) <= MAX_SVG_BYTES
    assert is_acceptable(clean(svg))


def test_svg_is_escaped_for_titles() -> None:
    spec = _spec("sphere", title="<script>alert(1)</script>")
    svg = build_static_svg(build_geometry(spec), spec)

    assert "<script>" not in svg


# --- 交互 HTML -------------------------------------------------------------
def test_viewer_html_respects_redlines() -> None:
    spec = _spec("surface", expression="sin(x)*cos(y)", grid=21, title="马鞍面")
    geometry = build_geometry(spec)
    html = build_viewer_html(geometry, spec, theme={"--atm-fg": "#1F2328", "--atm-bg": "#FFFFFF"})

    assert len(html.encode("utf-8")) <= MAX_HTML_BYTES
    assert "http://" not in html and "https://" not in html
    assert "<script src" not in html and "<link" not in html
    assert "prefers-reduced-motion" in html
    assert "Content-Security-Policy" in html  # prepare_animation_html 注入的禁网 CSP
    assert "--atm-primary" in html


def test_viewer_html_carries_full_geometry() -> None:
    spec = _spec("sphere", segments=12)
    geometry = build_geometry(spec)
    html = build_viewer_html(geometry, spec)

    match = re.search(
        r'<script id="atm-geo" type="application/json">(.*?)</script>', html, re.DOTALL
    )
    assert match is not None
    payload = json.loads(match.group(1))

    assert len(payload["vertices"]) == len(geometry.vertices)
    assert len(payload["faces"]) == len(geometry.faces)
    assert payload["closed"] is True
    assert payload["az"] == pytest.approx(spec.az)


def test_viewer_html_rejects_oversized_mesh() -> None:
    spec = _spec("torus", segments=48, size_a=1.0, size_b=0.4)
    geometry = build_geometry(spec)
    html = build_viewer_html(geometry, spec)
    assert len(html.encode("utf-8")) <= MAX_HTML_BYTES

    # 人为喂一个超长网格，验证 80KB 红线真的会拦下来
    from app.infra import model3d

    huge = Geometry(
        vertices=[(float(index), 0.0, 0.0) for index in range(20000)],
        faces=[[index, index + 1, index + 2] for index in range(19990)],
        closed=False,
    )
    with pytest.raises(model3d.GeometryError):
        build_viewer_html(huge, spec)


def test_html_has_no_network_call_names() -> None:
    """清洗器会把 fetch / XMLHttpRequest 这类调用替换掉，模板里不能出现。"""
    text = (__import__("pathlib").Path(__file__).resolve().parents[2] / "app" / "web" / "embed" / "model3d_viewer.html").read_text(encoding="utf-8")

    for token in ("fetch(", "XMLHttpRequest(", "WebSocket(", "EventSource(", "importScripts("):
        assert token not in text
    assert "http://" not in text and "https://" not in text


def test_projection_matches_between_python_and_template_contract() -> None:
    """模板与 Python 用同一套参数：默认 az/el 一致，缩放范围有限。"""
    geometry = build_geometry(_spec("cone"))
    screen, _ = project(geometry.vertices, az=-35.0, el=25.0, width=720, height=480)

    assert len(screen) == len(geometry.vertices)
    assert math.isfinite(screen[0][0]) and math.isfinite(screen[0][1])


# --- 场景（M36：小孔成像这类光路 / 空间关系图） ---------------------------
PINHOLE_SCENE = {
    "segments": [
        {"from": [-6, 0, 0], "to": [-6, 0, 1.6], "style": "arrow", "label": "物"},
        {"from": [-6, 0, 1.6], "to": [3, 0, -0.8], "style": "arrow"},
        {"from": [-6, 0, 0], "to": [3, 0, 0], "style": "dashed"},
    ],
    "faces": [
        {
            "points": [[-0.02, -0.9, -0.9], [-0.02, 0.9, -0.9], [-0.02, 0.9, 0.9], [-0.02, -0.9, 0.9]],
            "opacity": 0.1,
            "label": "小孔",
        }
    ],
    "labels": [{"text": "屏", "at": [3, 0, 1.4]}],
}


def test_scene_builds_segments_faces_and_labels() -> None:
    geometry = build_geometry(_spec("scene", scene=PINHOLE_SCENE))

    assert geometry.closed is False, "场景不做背面剔除（面朝向随便给）"
    assert len(geometry.faces) == 1 and len(geometry.lines) == 3
    assert geometry.line_styles == ["arrow", "arrow", "dashed"]
    assert geometry.face_opacities == [0.1]
    assert [text for text, _ in geometry.labels] == ["物", "小孔", "屏"]


def test_scene_svg_and_html_are_clean_and_labelled() -> None:
    spec = _spec("scene", scene=PINHOLE_SCENE, az=-58.0, el=16.0, title="小孔成像")
    geometry = build_geometry(spec)
    svg = build_static_svg(geometry, spec)
    html = build_viewer_html(geometry, spec)

    assert is_acceptable(clean(svg)) and "ns0:" not in svg
    assert ">小孔</text>" in svg and ">屏</text>" in svg, "标注要以 <text> 画出来（不是只在标题里）"
    assert "stroke-dasharray" in svg, "虚线（虚像 / 光轴）要能看出来"
    assert _elements(svg) <= MAX_SVG_ELEMENTS and len(svg.encode("utf-8")) <= MAX_SVG_BYTES
    assert '"labels"' in html and "小孔" in html
    assert len(html.encode("utf-8")) <= MAX_HTML_BYTES
    assert "axes" in html  # 场景图不画坐标轴（payload 里带开关）


def test_scene_rejects_bad_input() -> None:
    cases = (
        {},                                                              # 空场景
        {"segments": [{"from": [0, 0], "to": [1, 1, 1]}]},               # 点维度不对
        {"segments": [{"from": [0, 0, 0], "to": [1e9, 0, 0]}]},          # 坐标越界
        {"segments": "不是数组"},
        {"faces": [{"points": [[0, 0, 0], [1, 1, 1]]}]},                 # 面只有 2 个点
        {"segments": [{"from": [0, 0, 0], "to": [1, 1, 1]}] * 61},       # 线段超额度
    )
    for scene in cases:
        with pytest.raises(GeometryError):
            build_geometry(_spec("scene", scene=scene))
        assert build_model3d(_spec("scene", scene=scene)).ok is False


def test_scene_unknown_style_falls_back_to_solid() -> None:
    geometry = build_geometry(
        _spec("scene", scene={"segments": [{"from": [0, 0, 0], "to": [1, 0, 0], "style": "螺旋"}]})
    )

    assert geometry.line_styles == ["solid"]
