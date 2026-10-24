"""M28 测试：章节收起 / 展开、讲义划词标注（引用到追问）、人设可见性。

三条需求的落点：
1. 学习台左侧章节树可以整章收起 / 展开（点章标题或「展开 / 收起全部」）；
2. 讲义里选中一段文字可以「标注」，标注能从右侧一键引用到追问（原文 + 批注随提问一起发）；
3. 人设在讲义（顶部徽章）与追问（气泡表头 + 提示）里都看得见，且真的进了提示词。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from app.bootstrap import Services, bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryService
from app.services.outline_service import OutlineService
from app.services.settings_service import SettingsService
from app.ui.pages.study_page import StudyPage, decode_renderer_events
from app.ui.widgets.chat_drawer import ChatDrawer
from app.ui.widgets.lesson_tree import ARROW_COLLAPSED, ARROW_EXPANDED, LessonTreeWidget
from app.ui.widgets import annotation_dialog as annotation_dialog_module

pytestmark = pytest.mark.ui

SEED_COURSE = Path("Courses") / "ATM 使用指南"


class _SilentAnnotationDialog:
    """替身：真的 ``exec()`` 会阻塞测试；这里只记录参数并立刻返回。"""

    instances: list["_SilentAnnotationDialog"] = []
    action = "close"

    def __init__(self, *, quote="", note="", index=0, total=0, parent=None) -> None:
        self.quote = quote
        self.note = note
        self.index = index
        self.total = total
        self.action = type(self).action
        _SilentAnnotationDialog.instances.append(self)

    def exec(self) -> int:
        return 0

    def current_note(self) -> str:
        return self.note


@pytest.fixture()
def silent_annotation_dialog(monkeypatch):
    """让标注流程不弹窗（默认「关闭」），需要断言动作时改 ``_SilentAnnotationDialog.action``。"""
    _SilentAnnotationDialog.instances = []
    monkeypatch.setattr(annotation_dialog_module, "AnnotationDialog", _SilentAnnotationDialog)
    monkeypatch.setattr(
        __import__("app.ui.pages.study_page", fromlist=["x"]), "AnnotationDialog", _SilentAnnotationDialog
    )
    yield _SilentAnnotationDialog
    _SilentAnnotationDialog.action = "close"
    _SilentAnnotationDialog.instances = []


class _SpyRenderer:
    """记录渲染内核收到的指令，并提供事件取件口（模拟讲义页 → 主线程）。"""

    def __init__(self) -> None:
        self._widget = QWidget()
        self.blocks: list[dict] | None = None
        self.theme: dict = {}
        self.personas: list[dict] = []
        self.annotations: list[list[dict]] = []
        self.queued: list[dict] = []
        self.selection: dict = {"quote": "", "block_type": "", "block_index": None}

    def widget(self) -> QWidget:
        return self._widget

    def load_page(self, html_path: Path) -> None:
        self.html_path = html_path

    def set_theme(self, css_vars: dict) -> None:
        self.theme = dict(css_vars)

    def render_blocks(self, blocks: list[dict]) -> None:
        self.blocks = list(blocks)

    def render_markdown(self, text: str) -> None:
        self.markdown = text

    def set_persona(self, persona: dict) -> None:
        self.personas.append(dict(persona))

    def set_annotations(self, items: list[dict]) -> None:
        self.annotations.append([dict(item) for item in items])

    def drain_events(self, callback) -> None:
        pending, self.queued = self.queued, []
        callback(pending)

    def selected_text(self, callback) -> None:
        callback(dict(self.selection))


def _services(tmp_path: Path) -> Services:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = object()
    return Services(
        result=result,
        index=index,
        client=client,
        settings=SettingsService(
            result.config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())
        ),
        library=LibraryService(courses_dir=result.layout.courses, index=index),
        outline=OutlineService(courses_dir=result.layout.courses, client=client, index=index),
        course=course_service,
        lesson=lesson_service,
    )


# --- 1. 章节收起 / 展开 -----------------------------------------------------
def test_chapter_tree_collapses_and_expands(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path)
    tree = LessonTreeWidget()
    qtbot.addWidget(tree)
    try:
        tree.load_course(tmp_path / SEED_COURSE)
        chapters = tree.chapter_items()
        assert len(chapters) == 2
        assert all(item.isExpanded() for item in chapters)  # 默认全展开（观感不变）
        assert chapters[0].text(0).startswith(ARROW_EXPANDED)

        tree.collapse_all()
        assert all(not item.isExpanded() for item in chapters)
        assert all(item.text(0).startswith(ARROW_COLLAPSED) for item in chapters)
        assert tree.chapter_states() == {f"C{index}": False for index in (1, 2)}

        tree.expand_all()
        assert all(item.isExpanded() for item in tree.chapter_items())

        # 双击章标题 = 收起 / 展开（单击留给「看这一章的参考资料」）
        first = tree.chapter_items()[0]
        tree._on_item_double_clicked(first, 0)
        assert first.isExpanded() is False
        tree._on_item_double_clicked(first, 0)
        assert first.isExpanded() is True

        # 单击章 = 通知学习台打开「章节资料」页，并顺手展开
        tree.collapse_all()
        seen: list[str] = []
        tree.chapter_selected.connect(seen.append)
        tree._on_item_clicked(first, 0)
        assert seen == ["C1"]
        assert first.isExpanded() is True

        assert "2 章" in tree.summary_text()
    finally:
        services.index.close()


def test_chapter_collapsed_state_survives_mark_refresh(qtbot, tmp_path: Path) -> None:
    """回归：以前每次刷新小节状态都重建树 → 用户刚收起的章又被全部展开。"""
    services = _services(tmp_path)
    tree = LessonTreeWidget()
    qtbot.addWidget(tree)
    try:
        course_dir = tmp_path / SEED_COURSE
        tree.load_course(course_dir)
        tree.collapse_all()

        tree.refresh_marks(course_dir)

        assert tree.chapter_states() == {f"C{index}": False for index in (1, 2)}
        assert tree.chapter_items()[0].text(0).startswith(ARROW_COLLAPSED)
    finally:
        services.index.close()


def test_lesson_tree_load_course_accepts_expanded_state(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path)
    tree = LessonTreeWidget()
    qtbot.addWidget(tree)
    try:
        tree.load_course(tmp_path / SEED_COURSE, expanded_state={"C2": False})
        states = tree.chapter_states()
        assert states["C1"] is True
        assert states["C2"] is False
    finally:
        services.index.close()


def test_left_chapter_panel_can_be_collapsed_away(qtbot, tmp_path: Path) -> None:
    """「收起」= 收起整个左侧章节面板，靠卡片边上的凸起标签操作（不是折叠某一章）。"""
    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        assert page.left_panel.isVisibleTo(page) is True
        assert page.collapsed_bar.isVisibleTo(page) is False
        # 凸起标签：展开态写「‹」= 收起，收起态写「›」= 展开
        assert page.panel_collapse_tab.text().startswith("‹")
        assert page.panel_expand_button.text().startswith("›")
        assert page.panel_collapse_tab.objectName() == page.panel_expand_button.objectName() == "PanelTabButton"

        page.panel_collapse_tab.click()  # 点卡片边上的凸起 → 收起整块面板
        assert page._panel_collapsed is True
        assert page.left_panel.isVisibleTo(page) is False
        assert page.collapsed_bar.isVisibleTo(page) is True  # 左边留一个小凸起，能点回来

        page.panel_expand_button.click()  # 凸起上的「›」
        assert page._panel_collapsed is False
        assert page.left_panel.isVisibleTo(page) is True
        assert page.collapsed_bar.isVisibleTo(page) is False
    finally:
        services.index.close()


def test_selection_buttons_work_without_js_event_bridge(qtbot, tmp_path: Path, silent_annotation_dialog) -> None:
    """第二条通路：在讲义里选好文字后点顶部「标注选中 / 追问选中」。"""
    services = _services(tmp_path)
    renderer = _SpyRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)

        renderer.selection = {"quote": "矩阵 A 为 m×n", "block_type": "text", "block_index": 2}
        page.annotate_selection()
        stored = lesson_service.load_annotations(page._lesson_dir)
        assert [item["quote"] for item in stored] == ["矩阵 A 为 m×n"]
        assert stored[0]["block_index"] == 2

        renderer.selection = {"quote": "只有前者的列数等于后者的行数时才能相乘", "block_type": "text", "block_index": 3}
        page.ask_selection()
        assert "只有前者的列数等于后者的行数时才能相乘" in page.chat_drawer.selection

        # 没选中任何东西时给出明确提示，而不是静默失败
        messages: list[str] = []
        page.status_message.connect(messages.append)
        renderer.selection = {"quote": "", "block_type": "", "block_index": None}
        page.annotate_selection()
        assert any("先在中间讲义里选中" in message for message in messages)
    finally:
        services.index.close()


def test_decode_renderer_events_tolerates_every_shape() -> None:
    """QtWebEngine 只回传基本类型 → 事件是 JSON 字符串；坏数据一律忽略。"""
    assert decode_renderer_events('[{"action":"annotate","quote":"x"}]') == [
        {"action": "annotate", "quote": "x"}
    ]
    assert decode_renderer_events([]) == []
    assert decode_renderer_events([{"action": "x"}, "坏数据"]) == [{"action": "x"}]
    for bad in ("", "   ", "不是 JSON", None, 3):
        assert decode_renderer_events(bad) == []


def test_annotation_dialog_actions(qtbot) -> None:
    """标注窗口：写评论 / 引用到追问 / 删除，三个动作都要能取到评论内容。"""
    dialog = annotation_dialog_module.AnnotationDialog(quote="矩阵 A 为 m×n", note="旧评论", index=1, total=3)
    qtbot.addWidget(dialog)
    assert dialog.note_edit.toPlainText() == "旧评论"

    dialog.note_edit.setPlainText("新评论")
    dialog.save_button.click()
    assert dialog.action == annotation_dialog_module.ACTION_SAVE
    assert dialog.current_note() == "新评论"

    dialog2 = annotation_dialog_module.AnnotationDialog(quote="x")
    qtbot.addWidget(dialog2)
    dialog2.note_edit.setPlainText("要引用")
    dialog2.cite_button.click()
    assert dialog2.action == annotation_dialog_module.ACTION_CITE
    assert dialog2.current_note() == "要引用"

    dialog3 = annotation_dialog_module.AnnotationDialog(quote="x")
    qtbot.addWidget(dialog3)
    dialog3.delete_button.click()
    assert dialog3.action == annotation_dialog_module.ACTION_DELETE


def test_annotate_alone_does_not_popup_dialog(qtbot, tmp_path: Path, silent_annotation_dialog) -> None:
    """回归：标注只落划线和状态提示，**不**弹窗（评论由鼠标悬停在划线上时按需打开）。"""
    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        messages: list[str] = []
        page.status_message.connect(messages.append)

        page._add_annotation("矩阵 A 为 m×n", block_type="text")

        assert silent_annotation_dialog.instances == []  # 没有弹窗
        assert lesson_service.load_annotations(page._lesson_dir)[0]["quote"] == "矩阵 A 为 m×n"
        assert any("黄色划线" in message for message in messages)
    finally:
        services.index.close()


def test_hover_comment_then_cite_keeps_comment(
    qtbot, tmp_path: Path, silent_annotation_dialog, monkeypatch
) -> None:
    """划线上「评论」写的内容，会在「引用到追问」时一起带给老师。"""
    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        page._add_annotation("矩阵 A 为 m×n", block_type="text")
        annotation_id = lesson_service.load_annotations(page._lesson_dir)[0]["id"]

        # 悬停气泡里点「评论」→ 打开评论窗口 → 写评论并保存
        monkeypatch.setattr(_SilentAnnotationDialog, "current_note", lambda self: "先看维度")
        silent_annotation_dialog.action = annotation_dialog_module.ACTION_SAVE
        page._open_annotation_dialog(annotation_id)
        assert lesson_service.load_annotations(page._lesson_dir)[0]["note"] == "先看维度"

        # 悬停气泡里点「引用到追问」→ 原文 + 评论一起进引用区
        page.chat_drawer.clear_citations()
        page._on_renderer_events([{"action": "cite_annotation", "annotation_id": annotation_id}])

        assert "矩阵 A 为 m×n" in page.chat_drawer.selection
        assert "先看维度" in page.chat_drawer.selection
        cited = page.chat_drawer.citations()
        assert cited[0]["note"] == "先看维度"
    finally:
        services.index.close()


def test_cite_block_event_puts_figure_into_question(qtbot, tmp_path: Path) -> None:
    """讲义里点图片上的「引用到追问」→ 引用文本带图片路径。"""
    services = _services(tmp_path)
    renderer = _SpyRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        asset = tmp_path / "figure.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)

        renderer.queued.append(
            {
                "action": "cite_block",
                "quote": f"图片：示意图（文件：{asset}）",
                "block_type": "image",
                "block_index": 5,
                "asset": str(asset),
            }
        )
        page._poll_renderer_events()

        assert "示意图" in page.chat_drawer.selection
        assert str(asset) in page.chat_drawer.selection
    finally:
        services.index.close()


def test_question_attachments_follow_vision_capability(qtbot, tmp_path: Path) -> None:
    """引用了图片：档案支持读图就带上图片本体，不支持就只发文字。"""
    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        asset = tmp_path / "figure.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"1" * 64)
        page.chat_drawer.cite_quote(f"图片：示意图（文件：{asset}）", kind="image", asset=str(asset))

        services.config.require_profile().capabilities.vision = True
        attachments = page._question_attachments()
        assert len(attachments) == 1
        assert attachments[0]["data_url"].startswith("data:image/png;base64,")
        assert attachments[0]["path"] == str(asset)

        services.config.require_profile().capabilities.vision = False
        assert page._question_attachments() == []

        # 引用区里没有图片时永远不带附件
        services.config.require_profile().capabilities.vision = True
        page.chat_drawer.clear_citations()
        page.chat_drawer.cite_quote("只是引用了一段文字")
        assert page._question_attachments() == []
    finally:
        services.index.close()


def test_ask_passes_image_attachment_to_service(qtbot, tmp_path: Path, monkeypatch) -> None:
    """端到端：同时引用「一句话 + 一张图」+ 提问 → 服务层拿到文字与图片附件。"""
    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    captured: dict = {}
    try:
        page.open_course(tmp_path / SEED_COURSE)
        asset = tmp_path / "figure.png"
        asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"2" * 64)
        services.config.require_profile().capabilities.vision = True

        async def fake_ask(lesson_dir, **kwargs):
            captured.update(kwargs)
            if False:  # pragma: no cover - 让它成为异步生成器
                yield ""

        monkeypatch.setattr(lesson_service, "ask_question", fake_ask)
        page.chat_drawer.cite_quote(f"图片：示意图（文件：{asset}）")
        page.chat_drawer.cite_quote("矩阵 A 为 m×n", note="先看维度")
        selection = page.chat_drawer.selection
        page.ask("这张图在说什么？", selection)

        qtbot.waitUntil(lambda: bool(captured), timeout=10000)
        attachments = captured["attachments"]
        assert len(attachments) == 1
        assert attachments[0]["data_url"].startswith("data:image/png;base64,")
        sent = captured["selection"]
        assert "矩阵 A 为 m×n" in sent and "先看维度" in sent  # 文字引用 + 评论
        assert "示意图" in sent and str(asset) in sent  # 图片引用
    finally:
        services.index.close()


def test_qa_agent_sends_multimodal_message_when_image_attached() -> None:
    """答疑 Agent：带图时用 text + image_url 的多模态消息（与读图评审同一套形状）。"""
    import asyncio

    from app.agents.qa import QaAgent

    class _CaptureClient:
        def __init__(self) -> None:
            self.messages = None

        async def stream_chat(self, messages, **kwargs):
            self.messages = list(messages)
            yield '{"type":"text","md":"看到了"}\n'

    client = _CaptureClient()
    agent = QaAgent(client)

    async def run() -> None:
        async for _piece in agent.answer(
            question="这张图在说什么？",
            attachments=[{"data_url": "data:image/png;base64,AAAA", "caption": "示意图"}],
        ):
            pass

    asyncio.run(run())
    content = client.messages[-1]["content"]
    assert isinstance(content, list)
    assert content[0]["type"] == "text"
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "示意图" in content[0]["text"]


# --- 2. 讲义标注（划词 → 引用追问） -----------------------------------------
def test_annotation_service_roundtrip(tmp_path: Path) -> None:
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()

    item = lesson_service.add_annotation(
        lesson_dir, quote="矩阵   A 为 m×n", note="先看维度", block_index=3, block_type="text"
    )
    assert item["quote"] == "矩阵 A 为 m×n"  # 空白折叠（选中文字常带换行）
    assert item["color"] == "yellow"
    assert item["block_type"] == "text"

    again = lesson_service.add_annotation(lesson_dir, quote="矩阵 A 为 m×n", note="改过的批注")
    assert again["id"] == item["id"]  # 同一片段重复标注 = 合并，不刷屏
    stored = lesson_service.load_annotations(lesson_dir)
    assert len(stored) == 1
    assert stored[0]["note"] == "改过的批注"  # 合并后的批注要真的落盘

    assert lesson_service.annotation_citation(stored[0]) == "「矩阵 A 为 m×n」（我的批注：改过的批注）"

    updated = lesson_service.update_annotation(lesson_dir, item["id"], note="再改一次", color="blue")
    assert updated is not None and updated["color"] == "blue"
    assert lesson_service.load_annotations(lesson_dir)[0]["color"] == "blue"

    assert lesson_service.annotations_markdown(lesson_dir).startswith("- “矩阵 A 为 m×n”")

    assert lesson_service.delete_annotation(lesson_dir, item["id"]) is True
    assert lesson_service.load_annotations(lesson_dir) == []

    with pytest.raises(ValueError):
        lesson_service.add_annotation(lesson_dir, quote="   ")


def test_annotation_file_is_human_readable_and_survives_bad_json(tmp_path: Path) -> None:
    lesson_dir = tmp_path / "lesson"
    lesson_dir.mkdir()
    lesson_service.add_annotation(lesson_dir, quote="一句话")

    payload = json.loads((lesson_dir / "annotations.json").read_text(encoding="utf-8"))
    assert payload["version"] == 1 and payload["items"][0]["quote"] == "一句话"

    (lesson_dir / "annotations.json").write_text("{ 坏掉的 JSON", encoding="utf-8")
    assert lesson_service.load_annotations(lesson_dir) == []  # 坏文件不影响讲义阅读


def test_chat_drawer_cites_annotation_into_question(qtbot) -> None:
    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.set_annotations(
        [{"id": "an-1", "quote": "矩阵 A 为 m×n", "note": "先看维度", "color": "yellow"}]
    )

    assert drawer.annotation_list.count() == 1
    assert drawer.cite_annotation(drawer.current_annotation()) is True
    assert "矩阵 A 为 m×n" in drawer.selection
    assert "先看维度" in drawer.selection
    assert drawer.citation_list.count() == 1

    with qtbot.waitSignal(drawer.send_requested, timeout=3000) as blocker:
        drawer.input_edit.setPlainText("为什么行数等于列数才能相乘？")
        drawer.send_button.click()

    question, selection = blocker.args
    assert question == "为什么行数等于列数才能相乘？"
    assert "矩阵 A 为 m×n" in selection  # 引用随提问一起发给答疑 Agent
    assert "先看维度" in selection  # 评论也要带给老师
    assert "引用讲义" in drawer.history.toPlainText()  # 气泡里也看得见引用
    assert drawer.citation_list.count() == 0  # 发完自动清空，下一问重新选


def test_citations_accept_text_and_image_together(qtbot, tmp_path: Path) -> None:
    """引用区可以同时装「一句话 + 一张图」——这正是主人要的多对象引用。"""
    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    asset = tmp_path / "figure.png"
    asset.write_bytes(b"\x89PNG\r\n\x1a\n" + b"3" * 32)

    drawer.cite_quote("矩阵 A 为 m×n", note="先看维度", kind="text")
    drawer.cite_quote(f"图片：形状示意（文件：{asset}）", kind="image", asset=str(asset))
    # 重复引用同一句：合并，不重复堆积
    drawer.cite_quote("矩阵 A 为 m×n", note="先看维度", kind="text")

    citations = drawer.citations()
    assert [item["kind"] for item in citations] == ["text", "image"]
    assert drawer.citation_list.count() == 2
    assert drawer.citation_assets() == [str(asset)]

    text = drawer.selection_text()
    assert "矩阵 A 为 m×n" in text and "先看维度" in text
    assert "图片：形状示意" in text and str(asset) in text

    drawer.remove_current_citation()
    assert drawer.citation_list.count() == 1
    drawer.clear_citations()
    assert drawer.citations() == [] and drawer.selection == ""


def test_study_page_routes_lecture_events(qtbot, tmp_path: Path, silent_annotation_dialog) -> None:
    """讲义页点「标注 / 追问」→ 主线程收事件 → 落盘 + 高亮 + 引用到追问。"""
    services = _services(tmp_path)
    renderer = _SpyRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        lesson_dir = page._lesson_dir
        assert lesson_dir is not None
        assert page._event_timer.isActive()  # 有 drain_events 的渲染器 → 事件轮询已开

        renderer.queued.append(
            {"action": "annotate", "quote": "工作区与课程数据分离", "block_index": 1, "block_type": "text"}
        )
        page._poll_renderer_events()

        stored = lesson_service.load_annotations(lesson_dir)
        assert [item["quote"] for item in stored] == ["工作区与课程数据分离"]
        assert page.chat_drawer.annotation_list.count() == 1
        assert renderer.annotations[-1] == stored  # 高亮画回了讲义

        renderer.queued.append({"action": "ask_quote", "quote": "工作区与课程数据分离"})
        page._poll_renderer_events()
        assert "工作区与课程数据分离" in page.chat_drawer.selection
    finally:
        services.index.close()


def test_study_page_drops_empty_quote_annotation(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path)
    renderer = _SpyRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        messages: list[str] = []
        page.status_message.connect(messages.append)

        renderer.queued.append({"action": "annotate", "quote": "   "})
        page._poll_renderer_events()

        assert lesson_service.load_annotations(page._lesson_dir) == []
        assert any("空的" in message for message in messages)
    finally:
        services.index.close()


def test_study_page_passes_persona_label_into_generation(qtbot, tmp_path: Path, monkeypatch) -> None:
    """人设要真的进提示词：只给正文时模型不知道自己是谁，语气会退化。"""
    services = _services(tmp_path)
    page = StudyPage(services=services, renderer=_SpyRenderer())
    qtbot.addWidget(page)
    captured: dict = {}
    try:
        page.open_course(tmp_path / SEED_COURSE)
        page.persona_combo.setCurrentIndex(page.persona_combo.findData("catgirl-assistant"))

        async def fake_generate(lesson_dir, **kwargs):
            captured.update(kwargs)
            return None

        monkeypatch.setattr(lesson_service, "generate_script", fake_generate)
        # 生成前会弹「准备页」；这个用例只验证人设进了提示词，跳过弹窗
        monkeypatch.setattr(page, "_open_generate_dialog", lambda: True)
        page.generate_button.click()

        qtbot.waitUntil(lambda: bool(captured), timeout=10000)  # 生成跑在工作线程里
        assert captured["persona_label"] == "🐱 猫娘助教"
        assert "本喵" in (captured["persona_body"] or "")
    finally:
        services.index.close()


# --- 3. 人设可见性 ----------------------------------------------------------
def test_chat_drawer_shows_answering_persona(qtbot) -> None:
    drawer = ChatDrawer()
    qtbot.addWidget(drawer)
    drawer.set_persona({"name": "猫娘助教", "avatar": "🐱"})

    assert "猫娘助教" in drawer.persona_hint.text()
    bubble = drawer.history.add_bubble("assistant", "本喵来回答这个问题。")
    assert "猫娘助教" in bubble._header_label.text()
    assert drawer.history.role_prefix == "猫娘助教："
    assert "猫娘助教：本喵来回答这个问题。" in drawer.history.toPlainText()


def test_study_page_pushes_persona_to_lecture_and_drawer(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path)
    renderer = _SpyRenderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / SEED_COURSE)
        assert renderer.personas[-1]["name"]  # 讲义顶部徽章拿到了人设
        assert "在回答" in page.chat_drawer.persona_hint.text()

        page.persona_combo.setCurrentIndex(page.persona_combo.findData("catgirl-assistant"))

        assert renderer.personas[-1] == {
            "id": "catgirl-assistant",
            "name": "猫娘助教",
            "avatar": "🐱",
            "tone": ["活泼", "亲切", "鼓励式"],
        }
        assert "猫娘助教" in page.chat_drawer.persona_hint.text()
    finally:
        services.index.close()


def test_persona_identity_reaches_system_prompt() -> None:
    from app.agents.qa import QaAgent
    from app.agents.teacher import TeacherAgent

    teacher_prompt = TeacherAgent(client=None).system_prompt(
        persona_body="你自称「本喵」。", persona_label="🐱 猫娘助教", depth="minimal"
    )
    assert "你的身份是「🐱 猫娘助教」" in teacher_prompt
    assert "本喵" in teacher_prompt

    qa_prompt = QaAgent(client=None).system_prompt(
        persona_body="你自称「本喵」。", persona_label="🐱 猫娘助教", depth="minimal"
    )
    assert "你的身份是「🐱 猫娘助教」" in qa_prompt


def test_renderer_assets_expose_annotation_and_persona_api() -> None:
    """渲染内核要真的提供标注 / 人设 / 事件队列的入口（离线、无外链）。"""
    web_dir = Path(__file__).resolve().parents[2] / "app" / "web"
    renderer = (web_dir / "renderer.js").read_text(encoding="utf-8")
    html = (web_dir / "lesson.html").read_text(encoding="utf-8")

    for method in ("setPersona", "setAnnotations", "drainEvents", "pushEvent"):
        assert f"{method}: {method}" in renderer
    assert "atm-annotbar" in renderer and "atm-annotbar" in html
    assert "atm-annotation" in renderer and "atm-annotation" in html
    assert "atm-persona" in renderer and "atm-persona" in html
    assert "选中任意文字可标注或追问" in renderer
    # 黄色划线（3px 实线）+ 评论标记 + 悬停气泡 + 图片「引用到追问」按钮
    assert "border-bottom: 3px solid #F2B705" in html
    assert 'content: " 💬"' in html
    assert "atm-annotbubble" in renderer and "atm-annotbubble" in html
    assert 'action: "comment_annotation"' in renderer
    assert 'action: "cite_annotation"' in renderer
    assert "atm-cite-button" in renderer and "atm-cite-button" in html
    assert 'action: "cite_block"' in renderer
    assert 'action: "open_annotation"' in renderer
    assert "data-has-note" in renderer and "data-has-note" in html
    assert "http://" not in renderer and "https://" not in renderer
    assert "http://" not in html and "https://" not in html
