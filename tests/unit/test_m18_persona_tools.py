"""M18 测试：人设过滤器不再误删风格句；推理模型自动关闭工具循环。"""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from app.agents.base import PromptAssembler
from app.bootstrap import Services, bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.infra.sqlite_index import SqliteIndex
from app.services import course_service, lesson_service
from app.services.library_service import LibraryService
from app.services.outline_service import OutlineService
from app.services.persona_service import PersonaService
from app.services.settings_service import SettingsService
from app.ui.pages.study_page import StudyPage

pytestmark = pytest.mark.ui


class _StubRenderer:
    def __init__(self) -> None:
        self._widget = QWidget()

    def widget(self) -> QWidget:
        return self._widget

    def load_page(self, path: Path) -> None: ...

    def set_theme(self, css: dict) -> None: ...

    def render_blocks(self, blocks: list[dict]) -> None: ...

    def render_markdown(self, text: str) -> None: ...


def _services(tmp_path: Path, *, chat_model: str = "deepseek-flash", allow_tools: bool = True) -> Services:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    config = result.config
    config.generation.allow_tools = allow_tools
    config.require_profile().chat_model = chat_model
    index = SqliteIndex(tmp_path / "data" / "library.db")
    client = object()
    return Services(
        result=result,
        index=index,
        client=client,
        settings=SettingsService(config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())),
        library=LibraryService(courses_dir=result.layout.courses, index=index),
        outline=OutlineService(courses_dir=result.layout.courses, client=client, index=index),
        course=course_service,
        lesson=lesson_service,
        search=None,
        personas=PersonaService(user_dir=result.layout.personas, index=index),
    )


def test_persona_filter_keeps_style_lines() -> None:
    """回归：旧过滤器只要出现「省略 / 输出」就删整行，把猫娘的「本喵」风格句也删了。"""
    service = PersonaService(user_dir=Path("personas"))
    assembler = PromptAssembler()

    catgirl = service.get("catgirl-assistant")
    body, dropped = assembler.filter_persona(catgirl.body)

    assert "本喵" in body  # 风格句必须保留
    assert "不要省略术语的准确解释" in body
    assert len(dropped) <= 1

    for persona in service.list_personas():
        kept, removed = assembler.filter_persona(persona.body)
        assert kept.strip(), persona.id
        assert len(removed) <= 1, (persona.id, removed)


def test_persona_body_reaches_system_prompt() -> None:
    from app.agents.teacher import TeacherAgent

    agent = TeacherAgent(client=None)
    prompt = agent.system_prompt(persona_body="你自称本喵，句尾偶尔带喵。", depth="minimal")

    assert "本喵" in prompt
    assert "五段式" in prompt or "教学骨架" in prompt  # 骨架仍在（人设不能覆盖结构）


def test_reasoner_model_auto_disables_tools(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, chat_model="deepseek-reasoner", allow_tools=True)
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")
        executor, tools = page._build_tooling()
        assert executor is None and tools is None  # 推理模型：自动纯流式
    finally:
        services.index.close()


def test_deepseek_chat_keeps_tools_when_enabled(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, chat_model="deepseek-chat", allow_tools=True)
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")
        executor, tools = page._build_tooling()
        assert executor is not None and tools  # deepseek-chat 支持 function calling
    finally:
        services.index.close()


def test_tools_off_by_default(qtbot, tmp_path: Path) -> None:
    services = _services(tmp_path, chat_model="deepseek-chat", allow_tools=False)
    page = StudyPage(services=services, renderer=_StubRenderer())
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")
        executor, tools = page._build_tooling()
        assert executor is None and tools is None
    finally:
        services.index.close()
