"""主窗口（《项目文档》§11.2 信息架构的 M0 骨架：课程库 / 学习台 / 设置）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, Slot
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from ..bootstrap import BootstrapResult
from ..core.events import EventBus
from ..services import course_service
from ..services.settings_service import SettingsService
from .pages.library_page import LibraryPage
from .pages.settings_page import SettingsPage
from .pages.study_page import StudyPage
from .theme.presets import get_theme
from .theme.qss import apply_theme, resolve_mode


def system_is_dark() -> bool | None:
    """系统是否处于暗色（读不到就返回 None，交给主题默认值兜底）。"""
    app = QApplication.instance()
    if app is None:
        return None
    try:
        scheme = app.styleHints().colorScheme()
        dark = getattr(Qt, "ColorScheme", None)
        if dark is None:
            return None
        return scheme == dark.Dark
    except Exception:  # noqa: BLE001 - 老版本 Qt 没有该接口
        return None


class MainWindow(QMainWindow):
    NAV_COURSES = 0
    NAV_STUDY = 1
    NAV_SETTINGS = 2

    def __init__(
        self,
        result: BootstrapResult,
        *,
        settings_service: SettingsService | None = None,
        bus: EventBus | None = None,
        thread_pool: QThreadPool | None = None,
        study_page: StudyPage | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._result = result
        self._config = result.config
        self._bus = bus if bus is not None else EventBus()
        self._service = settings_service or SettingsService(
            self._config, workspace_root=result.layout.root
        )
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._css_vars: dict = {}

        self.setWindowTitle("AI Teaches Me")
        self.resize(1180, 760)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 8)

        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        for label in ("课程库", "学习台", "设置"):
            self.nav.addItem(QListWidgetItem(label))
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.library_page = LibraryPage(result.layout.courses)
        self.study_page = study_page if study_page is not None else StudyPage(thread_pool=self._pool)
        self.settings_page = SettingsPage(self._service, thread_pool=self._pool)
        self.stack.addWidget(self.library_page)
        self.stack.addWidget(self.study_page)
        self.stack.addWidget(self.settings_page)
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        status = QStatusBar()
        self.setStatusBar(status)
        self.workspace_label = QLabel(f"工作区：{result.layout.root}")
        self.workspace_label.setProperty("role", "muted")
        self.api_label = QLabel("")
        self.api_label.setProperty("role", "muted")
        status.addWidget(self.workspace_label, 1)
        status.addPermanentWidget(self.api_label)

        self.library_page.open_course.connect(self._open_course)
        self.library_page.go_settings.connect(lambda: self.nav.setCurrentRow(self.NAV_SETTINGS))
        self.library_page.refresh_requested.connect(self.refresh_library)
        self.library_page.new_course_requested.connect(self._on_new_course)
        self.settings_page.theme_changed.connect(self._on_theme_changed)
        self.settings_page.config_saved.connect(self.refresh_library)
        self.settings_page.status_message.connect(status.showMessage)
        self.study_page.back_requested.connect(lambda: self.nav.setCurrentRow(self.NAV_COURSES))
        self.study_page.lesson_completed.connect(self._on_lesson_completed)
        self.study_page.status_message.connect(status.showMessage)

        self._bus.subscribe("theme:changed", lambda event: self.study_page.apply_theme_vars(event.payload))

        for message in result.messages:
            status.showMessage(message, 15000)

        self.refresh_library()
        self.apply_current_theme()
        self.nav.setCurrentRow(self.NAV_COURSES)

    # --- 主题 -----------------------------------------------------------
    def apply_current_theme(self) -> dict:
        ui = self._config.ui
        theme = get_theme(ui.theme)
        mode = resolve_mode(theme, follow_system=ui.follow_system, system_dark=system_is_dark())
        app = QApplication.instance()
        if app is not None:
            self._css_vars = apply_theme(
                app,
                theme,
                ui.background,
                mode=mode,
                bus=self._bus,
                font_size=ui.font_size,
                reading_width=ui.reading_width,
            )
        else:
            from .theme.qss import to_css_vars

            self._css_vars = to_css_vars(theme, mode, ui.background)
        self.study_page.apply_theme_vars(self._css_vars)
        return self._css_vars

    @property
    def css_vars(self) -> dict:
        return dict(self._css_vars)

    @Slot(str)
    def _on_theme_changed(self, _key: str) -> None:
        self.apply_current_theme()
        self.statusBar().showMessage(f"主题已切换：{get_theme(self._config.ui.theme).name}", 6000)

    # --- 课程库 ---------------------------------------------------------
    def refresh_library(self) -> list[course_service.CourseSummary]:
        needs_api = not self._service.has_api_key(self._config.active_profile)
        courses = self.library_page.refresh(needs_api_setup=needs_api)
        self._update_api_label()
        return courses

    def _update_api_label(self) -> None:
        if self._service.has_api_key(self._config.active_profile):
            masked = self._service.api_key_masked(self._config.active_profile)
            self.api_label.setText(f"API：已配置 {masked}")
        else:
            self.api_label.setText("API：尚未配置（去设置页填写）")

    @Slot()
    def _on_new_course(self) -> None:
        self.statusBar().showMessage("创建向导将在 M1 提供；现在可以先读内置示例课程。", 8000)

    @Slot(object)
    def _open_course(self, summary: course_service.CourseSummary) -> None:
        tree = course_service.lesson_tree(summary.path)
        lesson = next((item for chapter in tree for item in chapter["lessons"]), None)
        if lesson is None or lesson.get("path") is None:
            self.statusBar().showMessage(f"《{summary.name}》还没有可读的小节。", 8000)
            return
        self.study_page.load_lesson(Path(lesson["path"]))
        self.nav.setCurrentRow(self.NAV_STUDY)

    @Slot(str)
    def _on_lesson_completed(self, lesson_id: str) -> None:
        self.refresh_library()
        self.statusBar().showMessage(f"{lesson_id} 已完成，进度已落盘。", 8000)

    @Slot(int)
    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
