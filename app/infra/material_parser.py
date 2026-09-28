"""资料解析（《项目文档》§4.8 FR-08 / §13.11；《技术文档》§3.3 ``materials/``）。

产出「带页码 / 幻灯片定位的 Markdown 片段」，供大纲生成与讲解检索引用。
重依赖（pypdf / python-pptx / python-docx / Pillow）都是可选导入：
缺库时返回结构化错误，不让课程创建流程崩掉。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_SUFFIXES = (".pdf", ".ppt", ".pptx", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp")


@dataclass
class ParsedMaterial:
    material_id: str
    filename: str
    kind: str
    pages: int = 0
    chunks: list[dict] = field(default_factory=list)
    markdown: str = ""
    status: str = "parsed"
    error: str = ""


def checksum_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()[:32]


def material_id_for(path: str | Path) -> str:
    name = Path(path).stem
    return "m-" + hashlib.sha256(name.encode("utf-8")).hexdigest()[:6]


def parse_material(path: str | Path, *, material_id: str | None = None) -> ParsedMaterial:
    """解析单个资料文件；不支持或失败时返回 ``status != parsed`` 的结果。"""
    source = Path(path)
    identifier = material_id or material_id_for(source)
    suffix = source.suffix.lower()
    if not source.exists():
        return ParsedMaterial(identifier, source.name, suffix.lstrip("."), status="missing", error="文件不存在")
    if suffix not in SUPPORTED_SUFFIXES:
        return ParsedMaterial(identifier, source.name, suffix.lstrip("."), status="unsupported", error="暂不支持该格式")

    try:
        if suffix == ".pdf":
            return _parse_pdf(source, identifier)
        if suffix in (".ppt", ".pptx"):
            return _parse_pptx(source, identifier)
        if suffix == ".docx":
            return _parse_docx(source, identifier)
        if suffix in (".txt", ".md"):
            return _parse_text(source, identifier)
        return _parse_image(source, identifier)
    except Exception as exc:  # noqa: BLE001 - 解析失败要能定位到文件
        return ParsedMaterial(identifier, source.name, suffix.lstrip("."), status="failed", error=str(exc))


def _chunk(text: str, *, page: int, heading: str = "", size: int = 800, overlap: int = 120) -> list[dict]:
    """带定位分块：600–900 字符、重叠 15%（§4.4.5 长资料处理链）。"""
    cleaned = " ".join(text.split())
    if not cleaned:
        return []
    chunks: list[dict] = []
    start = 0
    while start < len(cleaned):
        piece = cleaned[start : start + size]
        chunks.append({"page": page, "heading": heading, "text": piece})
        if start + size >= len(cleaned):
            break
        start += size - overlap
    return chunks


def _parse_pdf(path: Path, identifier: str) -> ParsedMaterial:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    lines: list[str] = [f"# {path.name}（PDF 解析）", ""]
    chunks: list[dict] = []
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        lines += [f"<!-- page {index} -->", text, ""]
        chunks.extend(_chunk(text, page=index))
    return ParsedMaterial(identifier, path.name, "pdf", pages=len(reader.pages), chunks=chunks, markdown="\n".join(lines).strip() + "\n")


def _parse_pptx(path: Path, identifier: str) -> ParsedMaterial:
    from pptx import Presentation

    deck = Presentation(str(path))
    lines = [f"# {path.name}（PPTX 解析）", ""]
    chunks: list[dict] = []
    for index, slide in enumerate(deck.slides, start=1):
        texts = [shape.text for shape in slide.shapes if getattr(shape, "has_text_frame", False) and shape.text]
        body = "\n".join(texts)
        lines += [f"## 幻灯片 {index}", body, ""]
        chunks.extend(_chunk(body, page=index))
    return ParsedMaterial(identifier, path.name, "pptx", pages=len(deck.slides), chunks=chunks, markdown="\n".join(lines).strip() + "\n")


def _parse_docx(path: Path, identifier: str) -> ParsedMaterial:
    import docx

    document = docx.Document(str(path))
    body = "\n".join(paragraph.text for paragraph in document.paragraphs if paragraph.text.strip())
    return ParsedMaterial(
        identifier,
        path.name,
        "docx",
        pages=1,
        chunks=_chunk(body, page=1),
        markdown=f"# {path.name}（DOCX 解析）\n\n{body}\n",
    )


def _parse_text(path: Path, identifier: str) -> ParsedMaterial:
    text = path.read_text(encoding="utf-8", errors="replace")
    kind = "md" if path.suffix.lower() == ".md" else "txt"
    return ParsedMaterial(identifier, path.name, kind, pages=1, chunks=_chunk(text, page=1), markdown=text)


def _parse_image(path: Path, identifier: str) -> ParsedMaterial:
    from PIL import Image

    with Image.open(path) as image:
        width, height = image.size
        description = f"（图片资料：{path.name}，{width}×{height}）"
    return ParsedMaterial(
        identifier,
        path.name,
        "image",
        pages=1,
        chunks=[{"page": 1, "heading": "", "text": description}],
        markdown=f"# {path.name}\n\n{description}\n",
    )
