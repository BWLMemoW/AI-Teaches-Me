"""资料解析（《项目文档》§4.8 FR-08 / §13.11；《技术文档》§3.3 ``materials/``）。

产出「带页码 / 幻灯片定位的 Markdown 片段」，供大纲生成与讲解检索引用：

* **文本、表格、图片、演讲者备注、图表数据**都会读（不是只读文字）；
* 图片不会全部送给模型：本地先按尺寸与重复度过滤掉明显装饰（Logo / 水印 / 小图标），
  剩下的作为「候选知识图」交给上层用模型原生读图能力判定与转写（``MaterialImage``）；
* 重依赖（pypdf / python-pptx / python-docx / Pillow）都是可选导入：缺库时返回结构化错误，
  不让课程创建流程崩掉。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_SUFFIXES = (".pdf", ".ppt", ".pptx", ".docx", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp")

CHUNK_SIZE = 800
CHUNK_OVERLAP = 120

# 识图提示词：先判断「有没有知识内容」，再决定是否转写（装饰图不读）。
VISION_PROMPT = (
    "这是一份学习资料里的图片 / 扫描页。请严格按下面两步输出纯文本：\n"
    "第一步（只占一行）：如果它包含可教学的知识内容（图示、图表、公式、表格、流程图、电路图、"
    "代码截图、带文字的讲义页或课本页），输出「有知识内容：是」；如果它只是图标、Logo、"
    "装饰花纹、纯照片而没有教学信息，输出「有知识内容：否」。\n"
    "第二步（仅当第一步为「是」时）：逐行转写图中可见的关键文字与结构——标题、标签、"
    "公式（用 LaTeX）、表格（用 Markdown 表格）；不要解释、不要补充图外知识、不要客套。\n"
)

VISION_ANSWER_YES = "有知识内容：是"
VISION_ANSWER_NO = "有知识内容：否"
VISION_PREFIX = "〔模型识图〕"

#: 打包识别：一次调用里放几张图（扫描版 PDF 几百页时，调用量按这个比例下降）
VISION_BATCH_PROMPT = (
    "下面**依次**给出同一份学习资料里的 {count} 张图片 / 扫描页。请按顺序逐张处理，"
    "每张都先单独输出一行「【第N张】」（N 从 1 开始），然后：\n"
    "· 若该图包含可教学的知识内容（图示、图表、公式、表格、流程图、电路图、代码截图、"
    "带文字的讲义页或课本页）→ 再输出一行「有知识内容：是」，随后转写图中可见的关键文字与结构"
    "（标题、标签、公式用 LaTeX、表格用 Markdown）；\n"
    "· 若它只是图标、Logo、装饰花纹、没有教学信息的照片 → 只输出「有知识内容：否」。\n"
    "不要解释、不要补充图外知识、不要客套；务必保持「【第N张】」的顺序与编号。"
)


def split_batch_answer(text: str, count: int) -> list[str] | None:
    """把打包识别的回答按「【第N张】」等标记切成逐张文本；切不出来返回 ``None``。

    模型并不总会严格照抄「【第N张】」：有时写成「第 2 张：」「图3：」。
    只认一种写法会让整批退回逐张、白白多等很多轮，所以这里接受几种常见变体。
    """
    import re

    raw = str(text or "")
    if count <= 1:
        return [raw.strip()] if raw.strip() else None
    pattern = re.compile(
        r"^\s*(?:【\s*(?:第\s*)?(\d+)\s*张\s*】"
        r"|【\s*图\s*(\d+)\s*】"
        r"|第\s*(\d+)\s*张\s*[：:]?"
        r"|图\s*(\d+)\s*[：:])",
        re.MULTILINE,
    )
    matches = list(pattern.finditer(raw))
    positions: dict[int, tuple[int, int]] = {}
    for match in matches:
        number = next((int(value) for value in match.groups() if value), 0)
        if 1 <= number <= count and number not in positions:
            positions[number] = (match.start(), match.end())
    if len(positions) < count:
        return None
    parts: list[str] = []
    ordered = sorted(positions.items())
    for index, (_number, (_start, body_start)) in enumerate(ordered):
        end = ordered[index + 1][1][0] if index + 1 < count else len(raw)
        parts.append(raw[body_start:end].strip())
    return parts

# 装饰图本地过滤阈值（单位：像素；PPTX 的 EMU 会先换算成像素）
EMU_PER_PIXEL = 9525
PPT_MIN_EDGE = 80
PPT_MIN_AREA_RATIO = 0.06
PDF_MIN_EDGE = 120
PDF_MIN_AREA_RATIO = 0.05
DOCX_MIN_EDGE = 100
DOCX_MIN_AREA = 30_000
REPEAT_MIN_HITS = 3
REPEAT_RATIO = 0.30


@dataclass
class MaterialImage:
    """待识别的图片（已过本地装饰图过滤，最终是否读由模型判定）。"""

    key: str
    data: bytes
    page: int
    width: int = 0
    height: int = 0
    heading: str = ""
    order: int = 0
    anchor_index: int = 0

    @property
    def locator(self) -> str:
        return f"#p{self.page}" if self.page else ""


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
    checksum: str = ""
    images: list[MaterialImage] = field(default_factory=list)
    dropped_images: int = 0  # 本地判定为装饰、根本没有送出去的图
    tables: int = 0
    pages_without_text: int = 0
    vision_called: int = 0
    vision_dropped: int = 0  # 送出去了，但模型判定「无知识内容」
    vision_failed: int = 0
    vision_skipped: bool = False
    has_vision_text: bool = False
    headings: list[str] = field(default_factory=list)

    def report(self) -> dict:
        """资料体检报告（写入 materials.json，并在界面上展示）。"""
        return {
            "filename": self.filename,
            "kind": self.kind,
            "pages": self.pages,
            "chunks": len(self.chunks),
            "tables": self.tables,
            "images": len(self.images) + self.dropped_images,
            "images_read": max(0, self.vision_called - self.vision_dropped),
            "images_skipped_decor": self.dropped_images,
            "images_dropped_by_model": self.vision_dropped,
            "images_failed": self.vision_failed,
            "pages_without_text": self.pages_without_text,
            "vision_skipped": self.vision_skipped,
            "headings": self.headings[:20],
            "status": self.status,
            "error": self.error,
        }


@dataclass
class MaterialParseOptions:
    """解析选项（阈值与分块参数；测试里会用到）。"""

    include_all_images: bool = False  # 连装饰图一起送去识别（用户在确认框里勾选）
    chunk_size: int = CHUNK_SIZE
    chunk_overlap: int = CHUNK_OVERLAP


def checksum_of(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()[:32]


def material_id_for(path: str | Path, *, course_id: str = "", checksum: str = "") -> str:
    """资料 id。

    给了 ``course_id`` 就按「课程 + 文件名 + 内容校验和」派生，避免不同课程里的同名文件
    互相覆盖（真实 bug：以前只用文件名哈希，后导入的同名文件会删掉先前的分块）；
    没给课程时保持旧行为，兼容既有调用。
    """
    source = Path(path)
    if not course_id:
        return "m-" + hashlib.sha256(source.stem.encode("utf-8")).hexdigest()[:6]
    digest = checksum or (checksum_of(source) if source.exists() else "")
    raw = f"{course_id}|{source.name}|{digest}".encode("utf-8")
    return "m-" + hashlib.sha256(raw).hexdigest()[:8]


def parse_material(
    path: str | Path,
    *,
    material_id: str | None = None,
    course_id: str = "",
    options: MaterialParseOptions | None = None,
) -> ParsedMaterial:
    """解析单个资料文件；不支持或失败时返回 ``status != parsed`` 的结果。

    **不会**自己调用模型：候选知识图放在 ``ParsedMaterial.images`` 里，
    由上层（``MaterialService``）统一做确认、缓存、进度与调用，再接 ``apply_vision``。
    """
    source = Path(path)
    config = options or MaterialParseOptions()
    identifier = material_id or material_id_for(source, course_id=course_id)
    suffix = source.suffix.lower()
    if not source.exists():
        return ParsedMaterial(identifier, source.name, suffix.lstrip("."), status="missing", error="文件不存在")
    if suffix not in SUPPORTED_SUFFIXES:
        return ParsedMaterial(identifier, source.name, suffix.lstrip("."), status="unsupported", error="暂不支持该格式")

    try:
        if suffix == ".pdf":
            parsed = _parse_pdf(source, identifier, config)
        elif suffix in (".ppt", ".pptx"):
            parsed = _parse_pptx(source, identifier, config)
        elif suffix == ".docx":
            parsed = _parse_docx(source, identifier, config)
        elif suffix in (".txt", ".md"):
            parsed = _parse_text(source, identifier, config)
        else:
            parsed = _parse_image(source, identifier)
    except Exception as exc:  # noqa: BLE001 - 解析失败要能定位到文件
        return ParsedMaterial(identifier, source.name, suffix.lstrip("."), status="failed", error=str(exc))
    _filter_repeated_images(parsed)
    return parsed


def apply_vision(parsed: ParsedMaterial, results: dict[str, str]) -> ParsedMaterial:
    """把识图结果并进正文与分块（``results: image.key -> 模型输出``；缺 key 视为失败）。"""

    def _split_answer(raw: str) -> tuple[bool, str]:
        text = str(raw or "").strip()
        if not text:
            return False, ""
        first, _, rest = text.partition("\n")
        head = first.strip().replace(" ", "")
        if VISION_ANSWER_YES.replace(" ", "") in head or head.endswith("是"):
            body = rest.strip() or first.replace(VISION_ANSWER_YES, "").strip()
            return True, body
        if VISION_ANSWER_NO.replace(" ", "") in head or head.endswith("否"):
            return False, ""
        # 模型没按格式回答：当作「有内容」整段保留（宁可多一点，也不要丢信息）
        return True, text

    offset = 0
    for image in sorted(parsed.images, key=lambda item: item.order):
        raw = results.get(image.key)
        if raw is None:
            parsed.vision_failed += 1
            _remove_placeholder(parsed, image.key)
            continue
        useful, body = _split_answer(raw)
        if not useful or not body.strip():
            parsed.vision_dropped += 1
            _remove_placeholder(parsed, image.key)
            continue
        block = f"{VISION_PREFIX}{body.strip()}"
        parsed.markdown = parsed.markdown.replace(_placeholder(image.key), block)
        parsed.chunks.insert(
            min(image.anchor_index + offset, len(parsed.chunks)),
            {
                "page": image.page,
                "heading": image.heading,
                "text": block,
                "source": "vision",
                "locator": image.locator,
            },
        )
        offset += 1
        parsed.has_vision_text = True
    if parsed.has_vision_text:
        parsed.markdown = parsed.markdown.rstrip() + (
            "\n\n---\n\n> 本资料部分内容由模型识图得到，可能有误；关键结论请回原文核对。\n"
        )
    return parsed


def _placeholder(key: str) -> str:
    return f"<!-- atm:image {key} -->"


def _remove_placeholder(parsed: ParsedMaterial, key: str) -> None:
    parsed.markdown = parsed.markdown.replace(_placeholder(key) + "\n", "").replace(_placeholder(key), "")


def _chunk(
    text: str,
    *,
    page: int,
    heading: str = "",
    source: str = "text",
    locator: str = "",
    size: int = CHUNK_SIZE,
    overlap: int = CHUNK_OVERLAP,
) -> list[dict]:
    """带定位分块：600–900 字符、重叠 15%（§4.4.5 长资料处理链）。"""
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        return []
    chunks: list[dict] = []
    start = 0
    while start < len(cleaned):
        piece = cleaned[start : start + size]
        chunks.append(
            {"page": page, "heading": heading, "text": piece, "source": source, "locator": locator}
        )
        if start + size >= len(cleaned):
            break
        start += size - overlap
    return chunks


def _append_heading(parsed: ParsedMaterial, heading: str) -> None:
    value = " ".join(str(heading or "").split())
    if value and value not in parsed.headings:
        parsed.headings.append(value)


def _looks_like_heading(line: str) -> bool:
    text = str(line or "").strip()
    if not text or len(text) > 40:
        return False
    return not text.endswith(("。", "，", "；", "：", ",", ".", ";", ":", "、"))


def _image_size(data: bytes) -> tuple[int, int]:
    try:
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            return int(image.width), int(image.height)
    except Exception:  # noqa: BLE001 - 尺寸读不出来时按 0 处理
        return 0, 0


def _make_image(
    parsed: ParsedMaterial,
    *,
    key: str,
    data: bytes,
    page: int,
    heading: str,
    width: int,
    height: int,
) -> MaterialImage:
    """登记一张候选知识图。

    注意：这里**只登记**，不动 markdown——调用方要把 ``_placeholder(key)`` 插到自己
    正在拼的正文里（早期实现直接往 markdown 追加，随后被解析器整体赋值覆盖，图片就丢了）。
    """
    image = MaterialImage(
        key=key,
        data=data,
        page=page,
        heading=heading,
        width=width,
        height=height,
        order=len(parsed.images),
        anchor_index=len(parsed.chunks),
    )
    parsed.images.append(image)
    return image


def _filter_repeated_images(parsed: ParsedMaterial) -> None:
    """同一张图在文档里反复出现 = 水印 / Logo：本地直接丢掉，不浪费识图调用。"""
    if not parsed.images:
        return
    counts: dict[str, int] = {}
    for image in parsed.images:
        digest = hashlib.sha1(image.data).hexdigest()
        counts[digest] = counts.get(digest, 0) + 1
    scope = max(parsed.pages, 1)
    kept: list[MaterialImage] = []
    for image in parsed.images:
        digest = hashlib.sha1(image.data).hexdigest()
        repeated = counts[digest] >= REPEAT_MIN_HITS and counts[digest] / scope >= REPEAT_RATIO
        if repeated:
            parsed.dropped_images += 1
            _remove_placeholder(parsed, image.key)
            continue
        kept.append(image)
    parsed.images = kept


def _parse_pdf(path: Path, identifier: str, config: MaterialParseOptions) -> ParsedMaterial:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    parsed = ParsedMaterial(identifier, path.name, "pdf", pages=len(reader.pages))
    lines: list[str] = [f"# {path.name}（PDF 解析）", ""]
    heading = ""
    for index, page in enumerate(reader.pages, start=1):
        text = page.extract_text() or ""
        stripped = text.strip()
        first_line = next((line for line in stripped.splitlines() if line.strip()), "")
        if _looks_like_heading(first_line):
            heading = first_line.strip()
        _append_heading(parsed, heading)
        lines += [f"<!-- page {index} -->", text, ""]
        parsed.chunks.extend(
            _chunk(
                text,
                page=index,
                heading=heading,
                locator=f"#p{index}",
                size=config.chunk_size,
                overlap=config.chunk_overlap,
            )
        )
        if len(stripped) < 30:  # 无文字页（扫描件）→ 取内嵌图交给识图
            parsed.pages_without_text += 1
            page_area = _pdf_page_area_px(page)
            for image_index, raw_image in enumerate(getattr(page, "images", []) or [], start=1):
                data = raw_image.data
                width, height = _image_size(data)
                if not config.include_all_images and not _keep_pdf_image(width, height, page_area):
                    parsed.dropped_images += 1
                    continue
                image = _make_image(
                    parsed,
                    key=f"p{index}-i{image_index}",
                    data=data,
                    page=index,
                    heading=heading,
                    width=width,
                    height=height,
                )
                lines.append(_placeholder(image.key))
                lines.append("")
    parsed.markdown = "\n".join(lines).strip() + "\n"
    return parsed


def _pdf_page_area_px(page: object) -> float:
    try:
        box = page.mediabox  # type: ignore[attr-defined]
        return float(box.width) * (96 / 72) * float(box.height) * (96 / 72)
    except Exception:  # noqa: BLE001
        return float(595 * 842)  # A4 @96dpi 兜底


def _keep_pdf_image(width: int, height: int, page_area: float) -> bool:
    if width <= 0 or height <= 0:
        return True  # 尺寸读不出来时宁可送去识别，也不要漏掉知识图
    if min(width, height) < PDF_MIN_EDGE:
        return False
    return (width * height) >= page_area * PDF_MIN_AREA_RATIO


def _parse_pptx(path: Path, identifier: str, config: MaterialParseOptions) -> ParsedMaterial:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    deck = Presentation(str(path))
    parsed = ParsedMaterial(identifier, path.name, "pptx", pages=len(deck.slides))
    slide_area = max(int(deck.slide_width or 0) * int(deck.slide_height or 0), 1)
    lines: list[str] = [f"# {path.name}（PPTX 解析）", ""]
    for index, slide in enumerate(deck.slides, start=1):
        heading = _slide_title(slide)
        _append_heading(parsed, heading)
        body_parts: list[str] = []
        for shape in slide.shapes:
            if getattr(shape, "has_text_frame", False) and shape.text.strip():
                body_parts.append(shape.text.strip())
            if getattr(shape, "has_table", False):
                table_text = _table_to_markdown(shape.table)
                if table_text:
                    parsed.tables += 1
                    parsed.chunks.extend(
                        _chunk(
                            table_text,
                            page=index,
                            heading=heading,
                            source="table",
                            locator=f"#s{index}",
                            size=config.chunk_size,
                            overlap=config.chunk_overlap,
                        )
                    )
                    body_parts.append(table_text)
            if getattr(shape, "has_chart", False):
                chart_text = _chart_to_text(shape.chart)
                if chart_text:
                    parsed.chunks.extend(
                        _chunk(
                            chart_text,
                            page=index,
                            heading=heading,
                            source="chart",
                            locator=f"#s{index}",
                            size=config.chunk_size,
                            overlap=config.chunk_overlap,
                        )
                    )
                    body_parts.append(chart_text)
        body = "\n".join(body_parts)
        lines += [f"## 幻灯片 {index}" + (f" · {heading}" if heading else ""), body, ""]
        parsed.chunks.extend(
            _chunk(
                body,
                page=index,
                heading=heading,
                locator=f"#s{index}",
                size=config.chunk_size,
                overlap=config.chunk_overlap,
            )
        )
        notes = _slide_notes(slide)
        if notes:
            lines += ["**演讲者备注：**", notes, ""]
            parsed.chunks.extend(
                _chunk(
                    notes,
                    page=index,
                    heading=heading,
                    source="notes",
                    locator=f"#s{index}",
                    size=config.chunk_size,
                    overlap=config.chunk_overlap,
                )
            )
        picture_index = 0
        for shape in slide.shapes:
            if getattr(shape, "shape_type", None) != MSO_SHAPE_TYPE.PICTURE:
                continue
            picture_index += 1
            try:
                data = shape.image.blob
            except Exception:  # noqa: BLE001 - 个别图片对象取不到就跳过
                continue
            width, height = _image_size(data)
            keep = config.include_all_images or _keep_pptx_image(
                int(getattr(shape, "width", 0) or 0),
                int(getattr(shape, "height", 0) or 0),
                width,
                height,
                slide_area,
            )
            if not keep:
                parsed.dropped_images += 1
                continue
            image = _make_image(
                parsed,
                key=f"s{index}-i{picture_index}",
                data=data,
                page=index,
                heading=heading,
                width=width,
                height=height,
            )
            lines.append(_placeholder(image.key))
            lines.append("")
    parsed.markdown = "\n".join(lines).strip() + "\n"
    return parsed


def _slide_title(slide: object) -> str:
    try:
        title = slide.shapes.title  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""
    text = getattr(title, "text", "") if title is not None else ""
    return " ".join(str(text or "").split())


def _slide_notes(slide: object) -> str:
    try:
        if not slide.has_notes_slide:  # type: ignore[attr-defined]
            return ""
        frame = slide.notes_slide.notes_text_frame  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return ""
    return str(getattr(frame, "text", "") or "").strip()


def _table_to_markdown(table: object) -> str:
    rows = list(getattr(table, "rows", []) or [])
    if not rows:
        return ""

    def cells(row: object) -> list[str]:
        return [" ".join(str(getattr(cell, "text", "") or "").split()) for cell in getattr(row, "cells", [])]

    header = cells(rows[0])
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for row in rows[1:]:
        lines.append("| " + " | ".join(cells(row)) + " |")
    return "\n".join(lines)


def _chart_to_text(chart: object) -> str:
    try:
        categories = [
            " ".join(str(getattr(point, "label", "") or "").split())
            for point in getattr(chart.plots[0], "categories", [])
        ]
    except Exception:  # noqa: BLE001
        categories = []
    lines: list[str] = []
    if categories:
        lines.append("图表类别：" + "、".join(item for item in categories if item))
    try:
        for series in chart.series:
            name = " ".join(str(getattr(series, "name", "") or "").split())
            values = [str(value) for value in getattr(series, "values", [])]
            lines.append(f"系列 {name or '（未命名）'}：" + "、".join(values))
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(line for line in lines if line.strip())


def _keep_pptx_image(shape_width: int, shape_height: int, width: int, height: int, slide_area: int) -> bool:
    if shape_width and shape_height and slide_area:
        area_ratio = (shape_width * shape_height) / slide_area
        edge = min(shape_width, shape_height) / EMU_PER_PIXEL
        return area_ratio >= PPT_MIN_AREA_RATIO and edge >= PPT_MIN_EDGE
    if width and height:
        return min(width, height) >= PPT_MIN_EDGE
    return True


def _parse_docx(path: Path, identifier: str, config: MaterialParseOptions) -> ParsedMaterial:
    import docx
    from docx.oxml.ns import qn

    document = docx.Document(str(path))
    parsed = ParsedMaterial(identifier, path.name, "docx", pages=1)
    lines: list[str] = [f"# {path.name}（DOCX 解析）", ""]
    heading = ""
    paragraph_index = 0
    image_index = 0
    for child in document.element.body.iterchildren():
        if child.tag == qn("w:p"):
            paragraph = docx.text.paragraph.Paragraph(child, document)
            text = paragraph.text.strip()
            if text:
                paragraph_index += 1
                style = str(getattr(getattr(paragraph, "style", None), "name", "") or "")
                if style.lower().startswith("heading") or style.startswith("标题"):
                    heading = text
                    _append_heading(parsed, heading)
                lines += [text, ""]
                parsed.chunks.extend(
                    _chunk(
                        text,
                        page=1,
                        heading=heading,
                        locator=f"#para{paragraph_index}",
                        size=config.chunk_size,
                        overlap=config.chunk_overlap,
                    )
                )
            for rel_id in _paragraph_image_ids(child, qn):
                image_index += 1
                blob = _docx_image_blob(document, rel_id)
                if not blob:
                    continue
                width, height = _image_size(blob)
                if not config.include_all_images and not _keep_docx_image(width, height):
                    parsed.dropped_images += 1
                    continue
                image = _make_image(
                    parsed,
                    key=f"d{image_index}",
                    data=blob,
                    page=1,
                    heading=heading,
                    width=width,
                    height=height,
                )
                lines.append(_placeholder(image.key))
                lines.append("")
        elif child.tag == qn("w:tbl"):
            table = docx.table.Table(child, document)
            table_text = _table_to_markdown(table)
            if table_text:
                parsed.tables += 1
                lines += [table_text, ""]
                parsed.chunks.extend(
                    _chunk(
                        table_text,
                        page=1,
                        heading=heading,
                        source="table",
                        locator=f"#table{parsed.tables}",
                        size=config.chunk_size,
                        overlap=config.chunk_overlap,
                    )
                )
    parsed.markdown = "\n".join(lines).strip() + "\n"
    return parsed


def _paragraph_image_ids(paragraph_element: object, qn) -> list[str]:
    ids: list[str] = []
    try:
        for blip in paragraph_element.findall(".//" + qn("a:blip")):  # type: ignore[attr-defined]
            rel_id = blip.get(qn("r:embed"))
            if rel_id:
                ids.append(str(rel_id))
        for image_data in paragraph_element.findall(".//" + qn("v:imagedata")):  # type: ignore[attr-defined]
            rel_id = image_data.get(qn("r:id"))
            if rel_id:
                ids.append(str(rel_id))
    except Exception:  # noqa: BLE001
        return ids
    return ids


def _docx_image_blob(document: object, rel_id: str) -> bytes:
    try:
        part = document.part.related_parts[rel_id]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return b""
    return bytes(getattr(part, "blob", b"") or b"")


def _keep_docx_image(width: int, height: int) -> bool:
    if width <= 0 or height <= 0:
        return True
    if min(width, height) < DOCX_MIN_EDGE:
        return False
    return (width * height) >= DOCX_MIN_AREA


def _parse_text(path: Path, identifier: str, config: MaterialParseOptions) -> ParsedMaterial:
    text = path.read_text(encoding="utf-8", errors="replace")
    kind = "md" if path.suffix.lower() == ".md" else "txt"
    parsed = ParsedMaterial(identifier, path.name, kind, pages=1)
    heading = ""
    buffer: list[str] = []
    line_number = 0
    start_line = 1

    def flush() -> None:
        if not buffer:
            return
        body = "\n".join(buffer)
        buffer.clear()
        parsed.chunks.extend(
            _chunk(
                body,
                page=1,
                heading=heading,
                locator=f"#line{start_line}",
                size=config.chunk_size,
                overlap=config.chunk_overlap,
            )
        )

    for index, line in enumerate(text.splitlines(), start=1):
        line_number = index
        stripped = line.strip()
        if stripped.startswith("#"):
            flush()
            heading = stripped.lstrip("#").strip()
            _append_heading(parsed, heading)
            start_line = index + 1
            continue
        if not stripped:
            flush()
            start_line = index + 1
            continue
        if not buffer:
            start_line = line_number
        buffer.append(stripped)
    flush()
    parsed.markdown = text
    return parsed


def _parse_image(path: Path, identifier: str) -> ParsedMaterial:
    data = path.read_bytes()
    width, height = _image_size(data)
    parsed = ParsedMaterial(identifier, path.name, "image", pages=1)
    _make_image(parsed, key="img1", data=data, page=1, heading="", width=width, height=height)
    parsed.markdown = f"# {path.name}\n\n{_placeholder('img1')}\n"
    return parsed
