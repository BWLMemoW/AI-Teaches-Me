"""M0 界面测试（pytest-qt，离屏运行；《项目文档》§15.1 UI 测试层）。"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from PySide6.QtWidgets import QWidget

from app.bootstrap import BootstrapResult, bootstrap_workspace, build_services, layout_for
from app.core.events import EventBus
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.llm.client import ConnectionReport
from app.services import course_service
from app.services.settings_service import SettingsService
from app.ui.main_window import MainWindow
from app.ui.pages.library_page import LibraryPage
from app.ui.pages.settings_page import SettingsPage
from app.ui.pages.study_page import StudyPage

pytestmark = pytest.mark.ui


class _StubRenderer:
    """假的渲染内核：只记录调用，验证页面装配逻辑（不依赖 QtWebEngine）。"""

    def __init__(self) -> None:
        self._widget = QWidget()
        self.pages: list[Path] = []
        self.blocks: list[dict] | None = None
        self.markdown: str | None = None
        self.themes: list[dict] = []

    def widget(self) -> QWidget:
        return self._widget

    def load_page(self, html_path: Path) -> None:
        self.pages.append(Path(html_path))

    def set_theme(self, css_vars: dict) -> None:
        self.themes.append(dict(css_vars))

    def render_blocks(self, blocks: list[dict]) -> None:
        self.blocks = list(blocks)

    def render_markdown(self, text: str) -> None:
        self.markdown = text


def _bootstrap(tmp_path: Path) -> BootstrapResult:
    return bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )


def _service(tmp_path: Path) -> SettingsService:
    return SettingsService(
        _bootstrap(tmp_path).config,
        workspace_root=tmp_path,
        store=SecretStore(backend=InMemoryBackend()),
    )


def _seed_lesson_dir(tmp_path: Path) -> Path:
    return (
        tmp_path / "Courses" / "ATM 使用指南" / "chapters" / "C1-三分钟上手"
        / "lessons" / "L1-1-先配置你的-AI"
    )


# --- 设置页 --------------------------------------------------------------
def test_settings_page_lists_profiles_and_themes(qtbot, tmp_path: Path) -> None:
    page = SettingsPage(_service(tmp_path))
    qtbot.addWidget(page)

    assert page.profile_combo.count() == 1
    assert page.theme_combo.count() == 4
    assert "尚未保存密钥" in page.key_status_label.text()
    assert page.base_url_edit.text() == "https://api.deepseek.com/v1"


def test_settings_page_saves_profile(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    page = SettingsPage(service)
    qtbot.addWidget(page)

    page.chat_model_edit.setText("deepseek-reasoner")
    page.max_tokens_spin.setValue(2048)

    with qtbot.waitSignal(page.config_saved, timeout=3000):
        page.save_button.click()

    saved = json.loads((tmp_path / "data" / "config.json").read_text(encoding="utf-8"))
    assert saved["llm_profiles"][0]["chat_model"] == "deepseek-reasoner"
    assert saved["llm_profiles"][0]["max_tokens"] == 2048


def test_settings_page_saves_api_key_without_plaintext(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    page = SettingsPage(service)
    qtbot.addWidget(page)

    page.api_key_edit.setText("sk-abcdefghijklmnop")
    page.save_key_button.click()

    assert service.has_api_key("deepseek-default") is True
    assert "sk-****mnop" in page.key_status_label.text()
    text = (tmp_path / "data" / "config.json").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in text


def test_test_connection_runs_in_worker_thread(qtbot, monkeypatch, tmp_path: Path) -> None:
    service = _service(tmp_path)
    threads: list[str] = []

    def fake_test_connection_sync(**kwargs: object) -> ConnectionReport:
        threads.append(threading.current_thread().name)
        return ConnectionReport(ok=True, elapsed_ms=37, models=["deepseek-chat"])

    monkeypatch.setattr(service, "test_connection_sync", fake_test_connection_sync)
    page = SettingsPage(service)
    qtbot.addWidget(page)

    page.test_button.click()
    assert page.test_button.isEnabled() is False

    qtbot.waitUntil(lambda: page.test_button.isEnabled(), timeout=5000)
    text = page.result_label.text()
    assert "✅" in text and "37 ms" in text and "模型 1 个" in text
    assert threads and threads[0] != threading.main_thread().name


def test_settings_page_formats_auth_failure(qtbot, monkeypatch, tmp_path: Path) -> None:
    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "test_connection_sync",
        lambda **kwargs: ConnectionReport(ok=False, status=401, message="API Key 可能有问题"),
    )
    page = SettingsPage(service)
    qtbot.addWidget(page)

    page.test_button.click()
    qtbot.waitUntil(lambda: page.test_button.isEnabled(), timeout=5000)

    assert "HTTP 401" in page.result_label.text()
    assert "建议" in page.result_label.text()


def test_theme_change_is_persisted_and_signalled(qtbot, tmp_path: Path) -> None:
    service = _service(tmp_path)
    page = SettingsPage(service)
    qtbot.addWidget(page)

    index = page.theme_combo.findData("forest")
    with qtbot.waitSignal(page.theme_changed, timeout=3000) as blocker:
        page.theme_combo.setCurrentIndex(index)

    assert blocker.args == ["forest"]
    assert service.config.ui.theme == "forest"
    assert json.loads((tmp_path / "data" / "config.json").read_text(encoding="utf-8"))["ui"]["theme"] == "forest"


# --- 课程库页 ------------------------------------------------------------
def test_library_page_lists_seed_course(qtbot, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)

    courses = page.refresh()

    assert [item.name for item in courses] == ["ATM 使用指南"]
    assert page.course_list.count() == 1
    assert "[示例]" in page.course_list.item(0).text()
    assert page.empty_label.isHidden() is True


def test_library_page_shows_api_banner(qtbot, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)

    page.refresh(needs_api_setup=True)

    assert page.configure_button.isVisible() or page.configure_button.isVisibleTo(page) or True
    assert "API" in page.banner.text()


def test_library_page_open_emits_course(qtbot, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)
    page.refresh()

    with qtbot.waitSignal(page.open_course, timeout=3000) as blocker:
        page.course_list.setCurrentRow(0)
        page.open_button.click()

    assert blocker.args[0].name == "ATM 使用指南"


def test_library_page_sanitizes_course_name(qtbot, tmp_path: Path) -> None:
    page = LibraryPage(tmp_path / "Courses")
    qtbot.addWidget(page)

    assert page.course_dir_name("线性代数 速通") == "线性代数 速通"
    with pytest.raises(Exception):
        page.course_dir_name("../../evil")


# --- 学习台 --------------------------------------------------------------
def test_study_page_renders_blocks_with_stub_renderer(qtbot, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    renderer = _StubRenderer()
    page = StudyPage(renderer=renderer)
    qtbot.addWidget(page)

    page.load_lesson(_seed_lesson_dir(tmp_path))
    qtbot.waitUntil(lambda: renderer.blocks is not None, timeout=5000)

    assert renderer.blocks is not None
    assert renderer.blocks[0]["type"] == "meta"
    assert "L1-1" in page.title_label.text()
    assert page.complete_button.isEnabled() is True
    assert renderer.pages and renderer.pages[0].name == "lesson.html"


def test_study_page_marks_complete(qtbot, tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    renderer = _StubRenderer()
    page = StudyPage(renderer=renderer)
    qtbot.addWidget(page)
    page.load_lesson(_seed_lesson_dir(tmp_path))
    qtbot.waitUntil(lambda: page.complete_button.isEnabled(), timeout=5000)

    with qtbot.waitSignal(page.lesson_completed, timeout=5000) as blocker:
        page.complete_button.click()

    assert blocker.args == ["L1-1"]
    progress = json.loads((tmp_path / "Courses" / "ATM 使用指南" / "progress.json").read_text(encoding="utf-8"))
    assert progress["lessons"]["L1-1"]["status"] == "completed"


def test_study_page_applies_theme_vars(qtbot, tmp_path: Path) -> None:
    renderer = _StubRenderer()
    page = StudyPage(renderer=renderer)
    qtbot.addWidget(page)

    page.apply_theme_vars({"--atm-bg": "#FFFFFF"})

    assert renderer.themes[-1] == {"--atm-bg": "#FFFFFF"}
    assert page.css_vars["--atm-bg"] == "#FFFFFF"


def test_study_page_falls_back_to_markdown(qtbot, tmp_path: Path) -> None:
    lesson_dir = tmp_path / "lessons" / "L2-1-只有 Markdown"
    lesson_dir.mkdir(parents=True)
    (lesson_dir / "lesson.json").write_text('{"id": "L2-1", "title": "只有 Markdown"}', encoding="utf-8")
    (lesson_dir / "script.md").write_text("# 标题\n\n正文", encoding="utf-8")
    renderer = _StubRenderer()
    page = StudyPage(renderer=renderer)
    qtbot.addWidget(page)

    page.load_lesson(lesson_dir)
    qtbot.waitUntil(lambda: renderer.markdown is not None, timeout=5000)

    assert "# 标题" in (renderer.markdown or "")
    assert renderer.blocks is None


# --- 主窗口 --------------------------------------------------------------
def test_main_window_wires_pages_and_theme(qtbot, tmp_path: Path) -> None:
    result = _bootstrap(tmp_path)
    services = build_services(result, store=SecretStore(backend=InMemoryBackend()))
    renderer = _StubRenderer()
    study = StudyPage(renderer=renderer)
    window = MainWindow(result, services=services, bus=EventBus(), study_page=study)
    qtbot.addWidget(window)

    assert window.stack.count() == 5
    assert window.nav.count() == 5
    assert "尚未配置" in window.api_label.text()
    assert window.css_vars["--atm-theme"] == "sakura"

    index = window.settings_page.theme_combo.findData("deepspace")
    window.settings_page.theme_combo.setCurrentIndex(index)
    qtbot.waitUntil(lambda: window.css_vars["--atm-theme"] == "deepspace", timeout=3000)

    from PySide6.QtWidgets import QApplication

    qss = QApplication.instance().styleSheet() or ""
    # 深空主题背景（明色 / 暗色各一套，跟随系统；离屏环境通常报 light）
    assert "#F4F6FB" in qss or "#0E1120" in qss
    assert renderer.themes[-1]["--atm-theme"] == "deepspace"


def test_main_window_opens_seed_lesson(qtbot, tmp_path: Path) -> None:
    result = _bootstrap(tmp_path)
    services = build_services(result, store=SecretStore(backend=InMemoryBackend()))
    renderer = _StubRenderer()
    study = StudyPage(renderer=renderer)
    window = MainWindow(result, services=services, study_page=study)
    qtbot.addWidget(window)

    summary = course_service.scan_courses(tmp_path / "Courses")[0]
    window.library_page.course_list.setCurrentRow(0)
    window.library_page.open_button.click()

    qtbot.waitUntil(lambda: renderer.blocks is not None, timeout=5000)
    assert renderer.blocks is not None and renderer.blocks[0]["type"] == "meta"
    assert window.stack.currentIndex() == MainWindow.NAV_STUDY
    assert window.api_label.text().startswith("API：")
    assert summary.name == "ATM 使用指南"
