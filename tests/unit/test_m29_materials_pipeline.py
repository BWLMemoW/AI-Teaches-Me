"""M29 测试：建课资料链路——读全（文本/表格/图片/备注）、只读知识图、资料进课程、可重建。

对应用户确认的四条：
1. 资料成为课程资产（原件 + 解析稿 + 清单进课程目录，索引可重建）；
2. PDF / PPTX / DOCX / 图片的文本、表格、图片、演讲者备注都要读得到；
3. 只读「知识图」：本地先过滤装饰图，剩下的交给模型判定与转写；
4. 大纲上下文用分级摘要，每份资料都有内容进上下文，且受预算约束。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from app.infra.material_parser import (
    VISION_ANSWER_NO,
    VISION_ANSWER_YES,
    VISION_PREFIX,
    MaterialParseOptions,
    apply_vision,
    parse_material,
)
from app.infra.sqlite_index import SqliteIndex
from app.services.material_service import MaterialService, build_material_digest

pytestmark = pytest.mark.ui  # 部分用例要造 PDF，需要 Qt


def _png(path: Path, size: tuple[int, int], color: tuple[int, int, int] = (200, 60, 90)) -> Path:
    Image.new("RGB", size, color).save(path)
    return path


def _make_pptx(path: Path, *, logo: Path | None = None, slides: int = 1) -> Path:
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for index in range(slides):
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = f"矩阵乘法 {index + 1}"
        box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), Inches(4), Inches(1))
        box.text_frame.text = "只有前者的列数等于后者的行数时才能相乘。"
        table = slide.shapes.add_table(2, 2, Inches(0.5), Inches(2.4), Inches(4), Inches(1)).table
        table.cell(0, 0).text = "步骤"
        table.cell(0, 1).text = "说明"
        table.cell(1, 0).text = "对齐"
        table.cell(1, 1).text = "先看维度"
        slide.notes_slide.notes_text_frame.text = "备注：这里要强调维度对齐。"
        if logo is not None:
            slide.shapes.add_picture(str(logo), Inches(0.2), Inches(0.1), width=Inches(0.3), height=Inches(0.15))
    deck.save(str(path))
    return path


def _make_docx(path: Path, *, image: Path | None = None) -> Path:
    import docx

    document = docx.Document()
    document.add_heading("第二章 矩阵", level=1)
    document.add_paragraph("矩阵乘法不是逐项相乘，而是两个变换的复合。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "条件"
    table.cell(0, 1).text = "结果"
    table.cell(1, 0).text = "列数 = 行数"
    table.cell(1, 1).text = "可以相乘"
    if image is not None:
        document.add_picture(str(image))
    document.save(str(path))
    return path


def _make_pdf_with_image(path: Path, image: Path) -> Path:
    """用 Qt 生成一页"只有图、没有文字层"的 PDF（模拟扫描件）。"""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPdfWriter

    assert QGuiApplication.instance() is not None
    writer = QPdfWriter(str(path))
    writer.setResolution(96)
    painter = QPainter(writer)
    painter.drawImage(QRectF(40, 40, 480, 360), QImage(str(image)))
    painter.end()
    return path


class _FakeVision:
    """假的读图回调：记录调用并返回固定文本（含「有知识内容」判定行）。"""

    def __init__(self, answer: str = VISION_ANSWER_YES, body: str = "图中写着：矩阵 A 为 m×n") -> None:
        self.answer = answer
        self.body = body
        self.calls: list[bytes] = []

    def __call__(self, png: bytes, prompt: str) -> str:
        self.calls.append(png)
        return f"{self.answer}\n{self.body}" if self.answer == VISION_ANSWER_YES else self.answer


class _FakeVisionBatch:
    """假的打包读图：按「【第N张】」格式返回，记录每批放进去了几张。"""

    def __init__(self, *, answer: str = VISION_ANSWER_YES, body: str = "图中写着：A 为 m×n") -> None:
        self.answer = answer
        self.body = body
        self.batches: list[list[bytes]] = []
        self.broken = False  # 置 True 模拟「模型没用编号格式」→ 调用方退回逐张

    def __call__(self, images: list[bytes], prompt: str) -> str:
        self.batches.append(list(images))
        if self.broken:
            return "这里没有编号，模型没按格式回答。"
        blocks = []
        for index in range(len(images)):
            blocks.append(f"【第{index + 1}张】\n{self.answer}\n{self.body} {index + 1}")
        return "\n".join(blocks)


# --- 1. 解析覆盖 ------------------------------------------------------------
def test_pptx_reads_text_table_notes_and_pictures(tmp_path: Path) -> None:
    deck = _make_pptx(tmp_path / "讲义.pptx")
    parsed = parse_material(deck)

    assert parsed.status == "parsed"
    sources = {str(chunk.get("source") or "text") for chunk in parsed.chunks}
    assert {"text", "table", "notes"} <= sources
    assert parsed.tables == 1
    assert any("维度对齐" in str(chunk["text"]) for chunk in parsed.chunks)
    assert parsed.headings and parsed.headings[0].startswith("矩阵乘法")
    # 表格也进了正文（不只是索引）
    assert "| 步骤 | 说明 |" in parsed.markdown
    assert "演讲者备注" in parsed.markdown


def test_pptx_skips_decorative_logo_and_reads_knowledge_figure(tmp_path: Path) -> None:
    logo = _png(tmp_path / "logo.png", (64, 24))
    deck = _make_pptx(tmp_path / "讲义.pptx", logo=logo)
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation(str(deck))
    slide = presentation.slides[0]
    figure = _png(tmp_path / "figure.png", (900, 600))
    slide.shapes.add_picture(str(figure), Inches(1), Inches(4), width=Inches(4), height=Inches(3))
    presentation.save(str(deck))

    parsed = parse_material(deck)

    assert parsed.dropped_images == 1  # 小 Logo 被本地过滤
    assert len(parsed.images) == 1  # 知识图留下等模型判定
    assert parsed.images[0].data  # 图数据带上了


def test_docx_reads_table_and_inline_image(tmp_path: Path) -> None:
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    parsed = parse_material(doc)

    assert parsed.tables == 1
    assert "| 条件 | 结果 |" in parsed.markdown
    assert any(str(chunk.get("source")) == "table" for chunk in parsed.chunks)
    assert len(parsed.images) == 1  # 内嵌图片成为识图候选
    assert parsed.headings and parsed.headings[0] == "第二章 矩阵"


def test_pdf_textless_page_collects_embedded_image(qtbot, tmp_path: Path) -> None:
    assert qtbot is not None  # 请求 qtbot 只是为了确保 QGuiApplication 已创建（Qt 画 PDF 需要）
    picture = _png(tmp_path / "scan.png", (900, 1200))
    pdf = _make_pdf_with_image(tmp_path / "扫描件.pdf", picture)
    parsed = parse_material(pdf)

    assert parsed.kind == "pdf" and parsed.pages == 1
    assert parsed.pages_without_text == 1
    assert len(parsed.images) == 1
    assert parsed.images[0].locator == "#p1"


def test_text_keeps_heading_path(tmp_path: Path) -> None:
    note = tmp_path / "笔记.md"
    note.write_text("# 第一章\n\n矩阵乘法不是逐项相乘。\n\n## 1.1 条件\n\n列数等于行数才能相乘。\n", encoding="utf-8")
    parsed = parse_material(note)

    assert parsed.headings[:2] == ["第一章", "1.1 条件"]
    assert any(chunk["heading"] == "1.1 条件" for chunk in parsed.chunks)


# --- 2. 只读知识图 ----------------------------------------------------------
def test_repeated_watermark_is_dropped_locally(tmp_path: Path) -> None:
    logo = _png(tmp_path / "wm.png", (400, 400))
    deck = _make_pptx(tmp_path / "讲义.pptx", logo=logo, slides=4)
    parsed = parse_material(deck)

    # 同一张图出现在 4 张幻灯片上 → 判为水印，不送识别
    assert parsed.images == []
    assert parsed.dropped_images == 4


def test_repeated_figures_are_dropped(tmp_path: Path) -> None:
    logo = _png(tmp_path / "logo.png", (600, 400))
    deck = _make_pptx(tmp_path / "讲义.pptx", logo=logo, slides=4)
    parsed = parse_material(deck)

    assert parsed.images == []
    assert parsed.dropped_images == 4


def test_model_answer_no_means_decorative(tmp_path: Path) -> None:
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    parsed = parse_material(doc)

    apply_vision(parsed, {parsed.images[0].key: VISION_ANSWER_NO})

    assert parsed.vision_dropped == 1
    assert parsed.has_vision_text is False
    assert VISION_PREFIX not in parsed.markdown
    assert not any(str(chunk.get("source")) == "vision" for chunk in parsed.chunks)


def test_model_answer_yes_inserts_vision_text(tmp_path: Path) -> None:
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    parsed = parse_material(doc)

    apply_vision(parsed, {parsed.images[0].key: f"{VISION_ANSWER_YES}\n图中写着：列数等于行数"})

    assert parsed.has_vision_text is True
    assert f"{VISION_PREFIX}图中写着：列数等于行数" in parsed.markdown
    vision_chunk = next(chunk for chunk in parsed.chunks if chunk.get("source") == "vision")
    assert "列数等于行数" in vision_chunk["text"]
    assert "可能有误" in parsed.markdown  # 统一提示


def test_include_all_images_keeps_decorative_ones(tmp_path: Path) -> None:
    logo = _png(tmp_path / "logo.png", (64, 24))
    deck = _make_pptx(tmp_path / "讲义.pptx", logo=logo)
    parsed = parse_material(deck, options=MaterialParseOptions(include_all_images=True))

    assert parsed.dropped_images == 0
    assert len(parsed.images) == 1


# --- 3. 服务层：确认 / 缓存 / 报告 -------------------------------------------
def test_service_calls_vision_and_reports(tmp_path: Path) -> None:
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    vision = _FakeVision()
    service = MaterialService(
        course_dir=tmp_path / "course", cache_dir=tmp_path / "cache", vision=vision
    )

    materials = service.parse_files([doc], course_id="c1")

    assert len(vision.calls) == 1
    report = materials[0].report()
    assert report["images_read"] == 1
    assert report["vision_skipped"] is False


def test_service_without_vision_marks_skipped(tmp_path: Path) -> None:
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    service = MaterialService(course_dir=tmp_path / "course", vision=None)

    materials = service.parse_files([doc], course_id="c1")

    assert materials[0].vision_skipped is True
    assert materials[0].vision_called == 0
    assert materials[0].status == "parsed"  # 没识图也照样能建课


def test_vision_cache_avoids_second_call(tmp_path: Path) -> None:
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    vision = _FakeVision()
    service = MaterialService(course_dir=tmp_path / "course", cache_dir=tmp_path / "cache", vision=vision)

    service.parse_files([doc], course_id="c1")
    service.parse_files([doc], course_id="c1")

    assert len(vision.calls) == 1  # 第二次命中缓存


def test_second_batch_does_not_ask_again_for_cached_images(tmp_path: Path) -> None:
    """分批上传：第二批里若全是已识别过的图，不该再弹一次确认。"""
    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    vision = _FakeVision()
    asked: list[int] = []
    service = MaterialService(
        course_dir=tmp_path / "course",
        cache_dir=tmp_path / "cache",
        vision=vision,
        # 阈值以上才回调；这里直接把阈值效果等价成「有未缓存图才问」
        confirm=lambda count, names: asked.append(count) or True,
    )

    service.parse_files([doc], course_id="c1")
    service.parse_files([doc], course_id="c1")

    assert asked == []  # 第一张就低于阈值、第二次全命中缓存 → 一次都没问
    assert len(vision.calls) == 1


def _scanned_pptx(tmp_path: Path, pages: int) -> Path:
    """造一份「每页一张整页大图」的 PPT，模拟扫描版 PDF 的页数规模。"""
    from pptx import Presentation
    from pptx.util import Inches

    deck = Presentation()
    for index in range(pages):
        slide = deck.slides.add_slide(deck.slide_layouts[6])  # 空白版式
        picture = _png(tmp_path / f"page-{index}.png", (1200, 1600), (index * 7 % 255, 100, 140))
        slide.shapes.add_picture(str(picture), Inches(0.5), Inches(0.5), width=Inches(9), height=Inches(6.5))
    deck.save(str(tmp_path / "扫描件.pptx"))
    return tmp_path / "扫描件.pptx"


def test_vision_calls_are_batched(tmp_path: Path) -> None:
    """核心：几百页扫描件不该一页一次调用，而要按批打包（4 张/次）。"""
    deck = _scanned_pptx(tmp_path, 9)
    batch = _FakeVisionBatch()
    single = _FakeVision()
    service = MaterialService(
        course_dir=tmp_path / "course",
        cache_dir=tmp_path / "cache",
        vision=single,
        vision_batch=batch,
    )

    materials = service.parse_files([deck], course_id="c1")

    # 9 张 → 3 次调用；批次可能并发完成，顺序不参与断言
    assert sorted(len(item) for item in batch.batches) == [1, 4, 4]
    assert single.calls == []  # 没退回逐张
    assert materials[0].vision_called == 9
    assert materials[0].has_vision_text is True


def test_batch_falls_back_to_single_images_when_format_is_broken(tmp_path: Path) -> None:
    """模型没按「【第N张】」回答时，必须退回逐张，不能把 9 页内容丢掉。"""
    deck = _scanned_pptx(tmp_path, 3)
    batch = _FakeVisionBatch()
    batch.broken = True
    single = _FakeVision()
    service = MaterialService(
        course_dir=tmp_path / "course",
        cache_dir=tmp_path / "cache",
        vision=single,
        vision_batch=batch,
    )

    materials = service.parse_files([deck], course_id="c1")

    assert len(single.calls) == 3  # 逐张补齐
    assert materials[0].vision_called == 3


def test_cached_pages_do_not_cost_again(tmp_path: Path) -> None:
    """第二次导入同一份扫描件：全部命中缓存，一次调用都不发。"""
    deck = _scanned_pptx(tmp_path, 5)
    batch = _FakeVisionBatch()
    service = MaterialService(
        course_dir=tmp_path / "course", cache_dir=tmp_path / "cache", vision_batch=batch
    )

    service.parse_files([deck], course_id="c1")
    first_round = len(batch.batches)
    service.parse_files([deck], course_id="c1")

    assert first_round == 2  # 5 张 → 4 + 1
    assert len(batch.batches) == first_round  # 第二轮没有再调


def test_split_batch_answer_handles_markers() -> None:
    from app.infra.material_parser import split_batch_answer

    text = "【第1张】\n有知识内容：是\n第一页文字\n【第2张】\n有知识内容：否"
    parts = split_batch_answer(text, 2)
    assert parts is not None and len(parts) == 2
    assert "第一页文字" in parts[0] and "否" in parts[1]
    assert split_batch_answer(text, 3) is None  # 少了编号 → 退回逐张

    variant = "第1张：\n有知识内容：是\n甲\n图2：\n有知识内容：是\n乙"
    parts = split_batch_answer(variant, 2)
    assert parts is not None and "甲" in parts[0] and "乙" in parts[1]


def test_cancelled_parse_skips_vision_calls(tmp_path: Path) -> None:
    """已经按了停止就不该再发识图请求，也不能让按钮背后的任务继续拖。"""
    from app.core.cancel import CancellationToken

    deck = _scanned_pptx(tmp_path, 3)
    token = CancellationToken()
    token.cancel()
    vision = _FakeVision()
    batch = _FakeVisionBatch()
    service = MaterialService(
        course_dir=tmp_path / "course",
        cache_dir=tmp_path / "cache",
        vision=vision,
        vision_batch=batch,
        cancel=token,
    )

    materials = service.parse_files([deck], course_id="c1")

    assert vision.calls == []
    assert batch.batches == []
    assert materials == []  # 还没开始就停了：不再白读文件


def test_confirm_required_when_many_images(tmp_path: Path) -> None:
    deck = _make_pptx(tmp_path / "讲义.pptx", slides=1)
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation(str(deck))
    for index in range(32):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = f"图 {index}"
        # 每张都不一样：避免被「重复图 = 水印」规则先过滤掉
        picture = _png(tmp_path / f"big-{index}.png", (1200, 900), (index * 3 % 255, 90, 160))
        slide.shapes.add_picture(str(picture), Inches(0.5), Inches(1.5), width=Inches(6), height=Inches(4.5))
    presentation.save(str(deck))

    asked: list[tuple[int, list[str]]] = []
    vision = _FakeVision()
    service = MaterialService(
        course_dir=tmp_path / "course",
        cache_dir=tmp_path / "cache",
        vision=vision,
        confirm=lambda count, names: asked.append((count, names)) or False,
    )

    materials = service.parse_files([deck], course_id="c1")

    assert asked and asked[0][0] > 30
    assert vision.calls == []  # 用户拒绝 → 一次都不调
    assert materials[0].vision_skipped is True


def test_confirm_can_include_decorative_images(tmp_path: Path) -> None:
    logo = _png(tmp_path / "logo.png", (64, 24))
    deck = _make_pptx(tmp_path / "讲义.pptx", logo=logo, slides=1)
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation(str(deck))
    for index in range(31):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = f"图 {index}"
        picture = _png(tmp_path / f"big-{index}.png", (1200, 900), (index * 5 % 255, 120, 60))
        slide.shapes.add_picture(str(picture), Inches(0.5), Inches(1.5), width=Inches(6), height=Inches(4.5))
    presentation.save(str(deck))

    vision = _FakeVision()
    service = MaterialService(
        course_dir=tmp_path / "course",
        cache_dir=tmp_path / "cache",
        vision=vision,
        confirm=lambda count, names: {"proceed": True, "include_all_images": True},
    )

    materials = service.parse_files([deck], course_id="c1")

    # 连装饰图一起识别：Logo 也进了候选
    assert len(vision.calls) >= 32
    assert materials[0].dropped_images == 0


# --- 4. 归属 / 隔离 / 重建 ---------------------------------------------------
def test_import_lands_in_course_and_index(tmp_path: Path) -> None:
    source = tmp_path / "教材.md"
    source.write_text("# 第一章\n\n矩阵乘法不是逐项相乘。" * 5, encoding="utf-8")
    course = tmp_path / "Courses" / "线性代数"
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        service = MaterialService(course_dir=course, index=index)
        result = service.import_files([source], course_id="c1")

        assert (course / "materials" / "raw" / "教材.md").exists()
        assert (course / "materials" / "parsed" / "教材.md").exists()
        assert (course / "materials" / "parsed" / "教材.chunks.json").exists()
        manifest = json.loads((course / "materials" / "materials.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["materials"] if item["id"] == result.materials[0].material_id)
        assert entry["source"] == "user"
        assert entry["report"]["chunks"] >= 1
        assert index.count_chunks(result.materials[0].material_id) >= 1
        assert service.search("逐项相乘", course_id="c1")
    finally:
        index.close()


def test_same_filename_in_two_courses_does_not_collide(tmp_path: Path) -> None:
    first = tmp_path / "a" / "教材.md"
    second = tmp_path / "b" / "教材.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("甲的课程：矩阵乘法的列数要等于行数。" * 5, encoding="utf-8")
    second.write_text("乙的课程：特征值方向不变。" * 5, encoding="utf-8")
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        service_a = MaterialService(course_dir=tmp_path / "course-a", index=index)
        service_b = MaterialService(course_dir=tmp_path / "course-b", index=index)
        result_a = service_a.import_files([first], course_id="course-a")
        result_b = service_b.import_files([second], course_id="course-b")

        assert result_a.materials[0].material_id != result_b.materials[0].material_id
        # 两份资料都在，没有互相删掉
        assert service_a.search("列数要等于行数", course_id="course-a")
        assert service_b.search("特征值方向不变", course_id="course-b")
        # 课程隔离：甲课搜不到乙课的内容
        assert service_a.search("特征值方向不变", course_id="course-a") == []
    finally:
        index.close()


def test_search_without_course_filter_keeps_old_behaviour(tmp_path: Path) -> None:
    source = tmp_path / "教材.md"
    source.write_text("矩阵乘法不是逐项相乘。" * 5, encoding="utf-8")
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        service = MaterialService(course_dir=tmp_path / "course", index=index)
        service.import_files([source], course_id="c1")
        assert service.search("逐项相乘")  # 不传课程时仍可全局检索（兼容旧调用）
    finally:
        index.close()


def test_reindex_restores_chunks_after_db_wipe(tmp_path: Path) -> None:
    source = tmp_path / "教材.md"
    source.write_text("# 第一章\n\n矩阵乘法不是逐项相乘。" * 5, encoding="utf-8")
    course = tmp_path / "course"
    db = tmp_path / "data" / "library.db"
    index = SqliteIndex(db)
    service = MaterialService(course_dir=course, index=index)
    result = service.import_files([source], course_id="c1")
    material_id = result.materials[0].material_id
    index.close()

    db.unlink()  # 索引库是可重建缓存：直接删掉
    rebuilt = SqliteIndex(db)
    try:
        restored = MaterialService.reindex_course(course, course_id="c1", index=rebuilt)
        assert restored >= 1
        assert rebuilt.count_chunks(material_id) >= 1
        assert rebuilt.search_chunks("逐项相乘", course_id="c1")
    finally:
        rebuilt.close()


# --- 5. 大纲上下文（分级摘要） -----------------------------------------------
def test_digest_covers_every_material_and_respects_budget(tmp_path: Path) -> None:
    materials = []
    for index in range(3):
        page = tmp_path / f"m{index}.md"
        page.write_text(f"# 第{index}份\n\n" + (f"第{index}份资料的内容要点，讲的是知识块 {index}。" * 60), encoding="utf-8")
        parsed = parse_material(page, material_id=f"m-{index}", course_id="c1")
        materials.append(parsed)

    digest = build_material_digest(materials, budget_chars=3000)

    assert len(digest) <= 3000
    for index in range(3):
        assert f"m-{index}" in digest  # 每份资料都有内容进上下文


def test_preview_materials_returns_summary_and_report(tmp_path: Path) -> None:
    from app.services.outline_service import OutlineService

    source = tmp_path / "教材.md"
    source.write_text("# 第一章\n\n矩阵乘法不是逐项相乘。" * 20, encoding="utf-8")
    service = OutlineService(courses_dir=tmp_path / "Courses", client=None)

    result = service.preview_materials([source], workspace=tmp_path)

    assert "教材.md" in result["summary"]
    assert result["stats"]["files"] == 1
    assert result["stats"]["chunks"] >= 1
    assert result["report"][0]["status"] == "parsed"


def test_preview_with_client_keeps_every_material_within_budget(tmp_path: Path) -> None:
    """有可用档案时走第 3 层摘要，但预算与「每份都有」两个约束不能破。"""
    from app.services.outline_service import OutlineService

    class _FakeClient:
        def __init__(self) -> None:
            self.calls = 0

        async def stream_chat(self, messages, **kwargs):
            self.calls += 1
            yield "目录级摘要：这份资料讲的是矩阵乘法的形状条件。\n关键词：矩阵、维度"

    sources = []
    for index in range(3):
        page = tmp_path / f"m{index}.md"
        page.write_text(f"# 第{index}份\n\n" + (f"第{index}份资料的内容要点。" * 80), encoding="utf-8")
        sources.append(page)
    client = _FakeClient()
    service = OutlineService(courses_dir=tmp_path / "Courses", client=client)

    result = service.preview_materials(sources, workspace=tmp_path, budget_chars=4000)

    assert client.calls == 3  # 每份资料一次目录级摘要
    assert len(result["summary"]) <= 4000
    for index in range(3):
        assert f"第{index}份" in result["summary"]
    assert result["stats"]["budget_chars"] == 4000


def test_cancelled_preview_skips_summary_model_calls(tmp_path: Path) -> None:
    """停止识别后，目录级摘要不能再偷偷跑一分钟；本地摘要要照样能交差。"""
    from app.core.cancel import CancellationToken
    from app.services.outline_service import OutlineService

    picture = _png(tmp_path / "fig.png", (800, 600))
    doc = _make_docx(tmp_path / "教材.docx", image=picture)
    token = CancellationToken()

    class _CancelDuringVisionClient:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def stream_chat(self, messages, **kwargs):
            self.calls.append(str(kwargs.get("task") or ""))
            # 第一批识图还没返回时用户就按了停止
            token.cancel()
            yield "【第1张】\n有知识内容：是\n图中写着：矩阵 A 为 m×n"

    client = _CancelDuringVisionClient()
    service = OutlineService(courses_dir=tmp_path / "Courses", client=client)

    result = service.preview_materials([doc], workspace=tmp_path, cancel=token)

    assert client.calls == ["vision_review"]  # 只有那次被打断的识图，没有摘要调用
    assert result["stats"]["cancelled"] is True
    assert "教材.docx" in result["summary"]


def test_commit_imports_materials_into_course(tmp_path: Path) -> None:
    """回归：以前「传了资料但课程里什么都没有」——现在原件、解析稿、清单、索引都要有。"""
    from app.core.models import CourseOutline
    from app.services.outline_service import OutlineService
    from tests.unit.test_m17_materials import OUTLINE

    course_dir = tmp_path / "Courses"
    source = tmp_path / "教材.md"
    source.write_text("# 第一章\n\n矩阵乘法不是逐项相乘。" * 5, encoding="utf-8")
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        service = OutlineService(courses_dir=course_dir, client=None, index=index)
        draft = service.new_draft("线性代数", material_paths=[str(source)], material_summary="（摘要）")
        draft.outline = CourseOutline.model_validate(OUTLINE)

        result = service.commit(draft)

        materials_dir = result.course_dir / "materials"
        assert (materials_dir / "raw" / "教材.md").exists()
        assert (materials_dir / "parsed" / "教材.md").exists()
        manifest = json.loads((materials_dir / "materials.json").read_text(encoding="utf-8"))
        ids = [item["id"] for item in manifest["materials"]]
        assert "m-outline" in ids
        user_entry = next(item for item in manifest["materials"] if item.get("source") == "user")
        assert user_entry["report"]["chunks"] >= 1
        assert index.count_chunks(user_entry["id"]) >= 1
        # 《课程资料》末尾带资料清单，用户在应用里就能看到读到了什么
        assert "资料清单" in (materials_dir / "课程资料.md").read_text(encoding="utf-8")
    finally:
        index.close()


def test_wizard_shows_material_health_report(qtbot, tmp_path: Path) -> None:
    """向导选完资料后要给出「体检报告」，而不是只报一句「已解析 N 份」。"""
    from app.services.outline_service import OutlineService
    from app.ui.pages.wizard_page import WizardPage

    page = WizardPage(OutlineService(courses_dir=tmp_path / "Courses", client=None))
    qtbot.addWidget(page)
    page._on_material_ready(
        {
            "summary": "（分级摘要）",
            "report": [
                {
                    "filename": "教材.pdf",
                    "kind": "pdf",
                    "status": "parsed",
                    "pages": 120,
                    "chunks": 300,
                    "tables": 4,
                    "images": 9,
                    "images_read": 7,
                    "images_skipped_decor": 2,
                    "pages_without_text": 12,
                    "vision_skipped": False,
                }
            ],
            "stats": {
                "files": 1,
                "pages": 120,
                "chunks": 300,
                "tables": 4,
                "figures_read": 7,
                "figures_skipped": 2,
                "vision_skipped": False,
            },
        }
    )

    text = page.material_label.text()
    assert "120 页" in text and "300 段" in text
    assert "表格 4" in text and "读 7" in text and "无文字页 12" in text
    assert page._material_summary == "（分级摘要）"
    assert page._material_report[0]["filename"] == "教材.pdf"
    assert page.material_button.isEnabled() is True


def test_wizard_stop_materials_restores_button_immediately(qtbot, tmp_path: Path) -> None:
    """回归：识别图片时按钮被禁用又没有停止入口，用户点完「停止」就卡住了。"""
    from app.core.cancel import CancellationToken
    from app.services.outline_service import OutlineService
    from app.ui.pages.wizard_page import WizardPage

    page = WizardPage(OutlineService(courses_dir=tmp_path / "Courses", client=None))
    qtbot.addWidget(page)
    page._material_paths = [tmp_path / "讲义.pptx"]
    page._material_token = CancellationToken()
    page._set_material_busy(True)

    assert page.material_button.isEnabled() is False
    assert page.material_stop_button.isHidden() is False
    page._on_stop_materials()

    assert page._material_token.cancelled is True
    assert page.material_button.isEnabled() is True
    assert page.material_stop_button.isHidden() is True
    assert "正在停止" in page.material_label.text()

    # 工作线程真的停下来后，再给出最终提示（部分结果为空时也不显示「读取 0 份」）
    page._on_material_done_signal(page._material_task_id, {"summary": "", "report": [], "stats": {}})
    assert "已停止" in page.material_label.text()


def test_wizard_cancel_also_stops_material_preview(qtbot, tmp_path: Path) -> None:
    """用户在大纲生成页按「停止」时，后台资料识别也要一起停，不能残留禁用按钮。"""
    from app.core.cancel import CancellationToken
    from app.services.outline_service import OutlineService
    from app.ui.pages.wizard_page import WizardPage

    page = WizardPage(OutlineService(courses_dir=tmp_path / "Courses", client=None))
    qtbot.addWidget(page)
    page._material_token = CancellationToken()
    page._set_material_busy(True)

    page._on_cancel()

    assert page._material_token.cancelled is True
    assert page.material_button.isEnabled() is True
    assert "正在停止识别" in page.material_label.text()


def test_wizard_accumulates_materials_across_batches(qtbot, tmp_path: Path) -> None:
    """回归：以前第二次选文件会**整体替换**第一批，用户按章节分批传 PPT 时前面的会被丢掉。"""
    from app.services.outline_service import OutlineService
    from app.ui.pages.wizard_page import WizardPage

    page = WizardPage(OutlineService(courses_dir=tmp_path / "Courses", client=None))
    qtbot.addWidget(page)
    first = tmp_path / "第1章.pptx"
    second = tmp_path / "第2章.pptx"
    third = tmp_path / "第3章.pptx"
    for item in (first, second, third):
        item.write_bytes(b"x")

    merged, added, duplicated = page._merge_material_paths([first, second])
    page._material_paths = merged
    assert (added, duplicated) == (2, 0)

    merged, added, duplicated = page._merge_material_paths([first, third])
    page._material_paths = merged

    assert [item.name for item in page._material_paths] == ["第1章.pptx", "第2章.pptx", "第3章.pptx"]
    assert (added, duplicated) == (1, 1)  # 第 1 章重复，被跳过而不是再存一份


def test_same_named_files_from_two_batches_do_not_overwrite(tmp_path: Path) -> None:
    """每章 PPT 都叫 PPT.pptx 时，后传的不该覆盖先传的。"""
    first = tmp_path / "ch1" / "PPT.md"
    second = tmp_path / "ch2" / "PPT.md"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("第一章：矩阵乘法的列数要等于行数。" * 5, encoding="utf-8")
    second.write_text("第二章：特征值方向不变。" * 5, encoding="utf-8")

    course = tmp_path / "course"
    index = SqliteIndex(tmp_path / "data" / "library.db")
    try:
        service = MaterialService(course_dir=course, index=index)
        service.import_files([first], course_id="c1")
        service.import_files([second], course_id="c1")

        raw_names = sorted(item.name for item in (course / "materials" / "raw").iterdir())
        assert raw_names == ["PPT-2.md", "PPT.md"]  # 同名不同内容 → 自动改名，两份都在
        assert service.search("列数要等于行数", course_id="c1")
        assert service.search("特征值方向不变", course_id="c1")
        manifest = json.loads((course / "materials" / "materials.json").read_text(encoding="utf-8"))
        assert len([item for item in manifest["materials"] if item.get("source") == "user"]) == 2
    finally:
        index.close()


def test_reimporting_same_file_is_idempotent(tmp_path: Path) -> None:
    """同一份文件重复导入：复用同一个 raw 文件，不再堆 -2 / -3。"""
    source = tmp_path / "教材.md"
    source.write_text("矩阵乘法不是逐项相乘。" * 5, encoding="utf-8")
    course = tmp_path / "course"
    service = MaterialService(course_dir=course)

    service.import_files([source], course_id="c1")
    service.import_files([source], course_id="c1")

    assert sorted(item.name for item in (course / "materials" / "raw").iterdir()) == ["教材.md"]
