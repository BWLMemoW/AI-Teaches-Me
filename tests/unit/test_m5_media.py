"""M5 测试：资料解析与检索、SVG 清洗/渲染自检、动画沙箱。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.agents.illustrator import AnimatorAgent, IllustratorAgent, SvgSpec
from app.bootstrap import bootstrap_workspace
from app.infra.material_parser import parse_material
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sandbox import prepare_animation_html, sanitize_animation_html
from app.infra.sqlite_index import SqliteIndex
from app.infra.svg_sanitizer import clean, is_acceptable
from app.services.material_service import MaterialService

GOOD_SVG = (
    '<svg viewBox="0 0 200 100" xmlns="http://www.w3.org/2000/svg">'
    '<title>矩阵乘法</title><desc>两行两列相乘</desc>'
    '<rect x="0" y="0" width="200" height="100" fill="none" stroke="currentColor" />'
    '<text x="20" y="50">矩阵乘法</text></svg>'
)


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream_chat(self, messages, **kwargs):
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 47):
            yield text[index : index + 47]


# --- 资料解析 -------------------------------------------------------------
def test_parse_markdown_and_text(tmp_path: Path) -> None:
    md = tmp_path / "教材.md"
    md.write_text("# 第一章 矩阵\n\n矩阵乘法不是逐项相乘。" * 20, encoding="utf-8")
    txt = tmp_path / "笔记.txt"
    txt.write_text("行数等于列数才能相乘。" * 30, encoding="utf-8")

    parsed_md = parse_material(md)
    parsed_txt = parse_material(txt)

    assert parsed_md.status == "parsed" and parsed_md.chunks
    assert parsed_txt.kind == "txt" and parsed_txt.pages == 1
    assert parsed_md.material_id.startswith("m-")


def test_parse_pptx_and_docx(tmp_path: Path) -> None:
    from docx import Document
    from pptx import Presentation

    deck_path = tmp_path / "讲义.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "矩阵乘法"
    deck.save(str(deck_path))

    doc_path = tmp_path / "提纲.docx"
    document = Document()
    document.add_paragraph("特征值就是变换中方向不变的向量")
    document.save(str(doc_path))

    parsed_deck = parse_material(deck_path)
    parsed_doc = parse_material(doc_path)

    assert parsed_deck.status == "parsed" and parsed_deck.pages == 1
    assert "矩阵乘法" in parsed_deck.markdown
    assert "特征值" in parsed_doc.markdown


def test_parse_unsupported_and_missing(tmp_path: Path) -> None:
    odd = tmp_path / "a.xyz"
    odd.write_text("x", encoding="utf-8")

    assert parse_material(odd).status == "unsupported"
    assert parse_material(tmp_path / "nope.pdf").status == "missing"


def test_material_service_imports_and_indexes(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course = tmp_path / "Courses" / "ATM 使用指南"
    source = tmp_path / "教材.md"
    source.write_text("矩阵乘法不是逐项相乘，而是两个变换的复合。" * 10, encoding="utf-8")
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        service = MaterialService(course_dir=course, index=index)
        result = service.import_files([source], course_id="course-seed-atm-guide")

        assert result.parsed and result.files
        assert (course / "materials" / "raw" / "教材.md").exists()
        assert (course / "materials" / "parsed" / "教材.md").exists()
        manifest = json.loads((course / "materials" / "materials.json").read_text(encoding="utf-8"))
        assert manifest["materials"][0]["status"] == "parsed"
        assert service.search("逐项相乘")
        assert "m-" in result.summary_for_director()
    finally:
        index.close()


# --- SVG 清洗与自检 -------------------------------------------------------
def test_svg_clean_removes_dangerous_parts() -> None:
    dirty = (
        '<svg viewBox="0 0 10 10"><title>t</title><desc>d</desc>'
        '<script>alert(1)</script><foreignObject></foreignObject>'
        '<a href="http://evil.example/x" onclick="alert(1)">link</a>'
        '<text x="1" y="1">ok</text></svg>'
    )

    result = clean(dirty)

    assert is_acceptable(result) is True
    assert "script" not in result.svg and "foreignObject" not in result.svg
    assert "http://evil" not in result.svg
    assert any("禁止元素" in problem for problem in result.problems)
    assert any("事件属性" in problem for problem in result.problems)
    assert any("外链" in problem for problem in result.problems)


def test_svg_clean_requires_viewbox() -> None:
    result = clean('<svg xmlns="http://www.w3.org/2000/svg"><title>t</title><desc>d</desc></svg>')

    assert is_acceptable(result) is False
    assert any("viewBox" in problem for problem in result.problems)


def test_svg_clean_enforces_size_limit() -> None:
    huge = '<svg viewBox="0 0 1 1"><title>t</title><desc>d</desc>' + ("<circle r='1'/>" * 5000) + "</svg>"

    result = clean(huge)
    assert any("体积超过" in problem or "元素数超过" in problem for problem in result.problems)


@pytest.mark.ui
def test_svg_renderer_produces_png(qtbot) -> None:
    from app.infra.svg_renderer import render_svg_png

    payload = render_svg_png(GOOD_SVG, width=200)

    assert payload.startswith(b"\x89PNG")


def test_svg_renderer_returns_empty_for_broken_svg() -> None:
    from app.infra.svg_renderer import render_svg_png

    assert render_svg_png("这不是 SVG") == b""


# --- 动画沙箱 -------------------------------------------------------------
def test_animation_sanitizer_strips_network_and_wraps_csp() -> None:
    raw = (
        "<html><head></head><body>"
        '<script src="https://cdn.example/lib.js"></script>'
        '<link rel="stylesheet" href="https://cdn.example/a.css" />'
        "<script>fetch('https://api.example/x');</script>"
        "<button>播放</button></body></html>"
    )

    cleaned, problems = sanitize_animation_html(raw)
    prepared = prepare_animation_html(raw, {"--atm-text": "#111", "--atm-surface": "#fff"})

    assert problems
    assert "cdn.example" not in cleaned
    assert "Content-Security-Policy" in prepared
    assert "--atm-fg:#111" in prepared
    assert "prefers-reduced-motion" in prepared


def test_sandbox_host_page_is_offline_ready() -> None:
    html = Path("app/web/sandbox.html").read_text(encoding="utf-8")

    assert "ATMSandbox" in html
    assert "TIMEOUT_MS = 5000" in html
    assert "http://" not in html and "https://" not in html


# --- 图示 Agent（Render-and-Review） --------------------------------------
def test_illustrator_retries_until_checks_pass(tmp_path: Path) -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><title>t</title><desc>d</desc></svg>'
    client = _FakeClient([bad, GOOD_SVG])
    agent = IllustratorAgent(client, renderer=lambda svg, width=820: b"PNG")

    result = asyncio.run(agent.make_svg(SvgSpec(concept="矩阵乘法", title="图示", caption_md="说明")))

    assert result.ok is True
    assert result.rounds == 2
    assert "viewBox" in result.svg


def test_illustrator_falls_back_after_max_rounds(tmp_path: Path) -> None:
    bad = '<svg xmlns="http://www.w3.org/2000/svg"><title>t</title><desc>d</desc></svg>'
    agent = IllustratorAgent(_FakeClient([bad, bad, bad]), renderer=lambda svg, width=820: b"PNG")

    result = asyncio.run(agent.make_svg(SvgSpec(concept="x", caption_md="降级说明")))

    assert result.ok is False
    assert result.rounds == 3
    assert result.fallback_md == "降级说明"


def test_illustrator_vision_review_path() -> None:
    client = _FakeClient([GOOD_SVG])
    calls: list[int] = []

    def vision(png: bytes, prompt: str) -> str:
        calls.append(len(png))
        return "PASS"

    agent = IllustratorAgent(client, renderer=lambda svg, width=820: b"PNG-BYTES", vision=vision)
    result = asyncio.run(agent.make_svg(SvgSpec(concept="x")))

    assert result.ok is True
    assert calls == [9]


def test_illustrator_saves_asset_with_history(tmp_path: Path) -> None:
    lesson = tmp_path / "lesson"
    lesson.mkdir()
    agent = IllustratorAgent(_FakeClient([GOOD_SVG, GOOD_SVG]), renderer=lambda svg, width=820: b"PNG")

    first = asyncio.run(agent.make_svg(SvgSpec(concept="a")))
    IllustratorAgent.save_asset(lesson, SvgSpec(concept="a"), first)
    second = asyncio.run(agent.make_svg(SvgSpec(concept="a")))
    saved = IllustratorAgent.save_asset(lesson, SvgSpec(concept="a"), second)

    widgets = sorted(item.name for item in (lesson / "widgets").iterdir())
    assert "diagram-1.svg" in widgets
    assert any(name.startswith("diagram-1.v") for name in widgets)
    assert saved["svg"].endswith(".svg")


def test_animator_rejects_oversized_animation() -> None:
    client = _FakeClient(["<html><body>" + "x" * 200 + "</body></html>"])
    agent = AnimatorAgent(client, max_bytes=50)

    result = asyncio.run(agent.make_animation("矩阵乘法"))

    assert "error" in result and "fallback_md" in result
