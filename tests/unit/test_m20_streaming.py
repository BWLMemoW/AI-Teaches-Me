"""M20 测试：讲义生成边生成边输出。"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.ui


def test_blocks_are_appended_while_generating(qtbot, tmp_path) -> None:
    """M20 回归：每个内容块到达都要立刻追加到阅读区（不再等全部生成完）。"""
    from PySide6.QtWidgets import QWidget

    from app.bootstrap import Services, bootstrap_workspace
    from app.infra.secret_store import InMemoryBackend, SecretStore
    from app.infra.sqlite_index import SqliteIndex
    from app.services import course_service, lesson_service
    from app.services.library_service import LibraryService
    from app.services.outline_service import OutlineService
    from app.services.settings_service import SettingsService
    from app.ui.pages.study_page import StudyPage

    class _Renderer:
        def __init__(self) -> None:
            self._widget = QWidget()
            self.appended: list[dict] = []
            self.batches: list[list[dict]] = []

        def widget(self): return self._widget
        def load_page(self, path): ...
        def set_theme(self, css): ...
        def render_blocks(self, blocks): self.batches.append(list(blocks))
        def append_block(self, block): self.appended.append(dict(block))
        def render_markdown(self, text): ...

    result = bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    index = SqliteIndex(tmp_path / "data" / "library.db")
    services = Services(
        result=result, index=index, client=object(),
        settings=SettingsService(result.config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())),
        library=LibraryService(courses_dir=result.layout.courses, index=index),
        outline=OutlineService(courses_dir=result.layout.courses, client=object(), index=index),
        course=course_service, lesson=lesson_service,
    )
    renderer = _Renderer()
    page = StudyPage(services=services, renderer=renderer)
    qtbot.addWidget(page)
    try:
        page.open_course(tmp_path / "Courses" / "ATM 使用指南")

        page.generate_button.click()          # 应先出现占位提示批次
        assert renderer.batches and "正在生成讲义" in str(renderer.batches[-1])

        page._on_block_ready({"type": "text", "md": "第一块"})
        page._on_block_ready({"type": "summary", "points": ["要点"]})

        assert [item["type"] for item in renderer.appended] == ["text", "summary"]
        assert "已输出" in page.status_label.text()
    finally:
        services.index.close()
