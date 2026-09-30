"""渲染内核资源约束的回归测试（《技术文档》§1.5 红线 4、§4.8、§6 安全边界）。

这些用例的作用是：**以后任何人往渲染页里加外链或 CDN 都会直接失败。**
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WEB_DIR = REPO_ROOT / "app" / "web"
VENDOR_DIR = WEB_DIR / "vendor"

REQUIRED_VENDOR_FILES = (
    "markdown-it/markdown-it.min.js",
    "katex/katex.min.js",
    "katex/katex.min.css",
    "highlight.js/highlight.min.js",
    "highlight.js/atom-one-light.min.css",
    "highlight.js/atom-one-dark.min.css",
)

OUR_FILES = ("lesson.html", "renderer.js", "theme.css")


@pytest.mark.parametrize("name", OUR_FILES)
def test_our_web_assets_have_no_remote_url(name: str) -> None:
    text = (WEB_DIR / name).read_text(encoding="utf-8")

    assert "http://" not in text
    assert "https://" not in text
    assert "//cdn" not in text


def test_lesson_html_has_lockdown_csp() -> None:
    text = (WEB_DIR / "lesson.html").read_text(encoding="utf-8")
    match = re.search(r'http-equiv="Content-Security-Policy"\s+content="([^"]+)"', text)

    assert match is not None
    csp = match.group(1)
    assert "default-src 'none'" in csp
    assert "connect-src 'none'" in csp
    assert "object-src 'none'" in csp
    assert "script-src" in csp and "file:" in csp
    assert "img-src" in csp and "data:" in csp


def test_lesson_html_only_references_local_vendor() -> None:
    text = (WEB_DIR / "lesson.html").read_text(encoding="utf-8")
    sources = re.findall(r'(?:src|href)="([^"]+)"', text)

    assert sources
    for source in sources:
        assert not source.startswith(("http:", "https:", "//")), source
        assert source.startswith(("vendor/", "theme.css", "renderer.js")), source


def test_vendor_files_are_installed() -> None:
    missing = [name for name in REQUIRED_VENDOR_FILES if not (VENDOR_DIR / name).exists()]
    assert missing == []

    fonts = list((VENDOR_DIR / "katex" / "fonts").glob("*.woff2"))
    assert len(fonts) >= 20


def test_vendor_manifest_records_upstream_integrity() -> None:
    manifest = json.loads((VENDOR_DIR / "VENDOR.json").read_text(encoding="utf-8"))

    packages = {item["name"]: item for item in manifest["packages"]}
    assert set(packages) == {"markdown-it", "katex", "@highlightjs/cdn-assets"}
    for name, item in packages.items():
        assert item["integrity"].startswith("sha512-"), name
        assert "registry.npmjs.org" in item["source"], name


def test_vendor_css_has_no_remote_font_url() -> None:
    for name in ("katex/katex.min.css", "highlight.js/atom-one-light.min.css"):
        text = (VENDOR_DIR / name).read_text(encoding="utf-8", errors="replace")
        assert "url(http" not in text


def test_renderer_exposes_atm_api() -> None:
    text = (WEB_DIR / "renderer.js").read_text(encoding="utf-8")

    assert "window.ATM" in text
    for method in ("init", "appendBlock", "appendBlocks", "reset", "setTheme", "getState", "renderFallbackMarkdown"):
        assert method + ":" in text


def test_renderer_handles_every_documented_block_type() -> None:
    text = (WEB_DIR / "renderer.js").read_text(encoding="utf-8")

    for block_type in (
        "meta", "text", "callout", "analogy", "diagram", "image", "svg", "animation",
        "code", "quiz", "table", "summary", "next_preview", "ref", "error", "done",
    ):
        assert f'case "{block_type}"' in text


def test_renderer_refuses_remote_images() -> None:
    text = (WEB_DIR / "renderer.js").read_text(encoding="utf-8")

    assert "safeLocalUrl" in text
    assert "图片来源被拒绝" in text

def test_renderer_supports_diagram_lightbox() -> None:
    """双击讲义里的 SVG → 全屏浮层看整张图（离线实现）。"""
    text = (WEB_DIR / "renderer.js").read_text(encoding="utf-8")

    assert "openDiagramLightbox" in text
    assert 'addEventListener("dblclick"' in text
    assert "双击看整张图" in text


def test_lesson_html_styles_diagram_lightbox() -> None:
    text = (WEB_DIR / "lesson.html").read_text(encoding="utf-8")

    assert ".atm-lightbox-host" in text
    assert ".atm-lightbox-caption" in text