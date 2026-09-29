"""主窗口（《项目文档》§11.2 信息架构：课程库 / 新建课程 / 学习台 / 设置）。"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, Qt, QTimer, QUrl, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QStackedWidget,
    QStatusBar,
    QWidget,
)

from ..bootstrap import BootstrapResult, Services, build_services
from ..core.events import EventBus
from ..services import course_service
from .pages.library_page import LibraryPage
from .pages.notes_page import NotesPage
from .pages.qa_page import QaPage
from .widgets.course_card import CardStyle
from .widgets.first_run_dialog import FirstRunDialog
from .widgets.icon_picker_dialog import IconPickerDialog
from .pages.settings_page import SettingsPage
from .pages.study_page import StudyPage
from .pages.wizard_page import WizardPage
from .theme.presets import get_theme
from .theme.qss import apply_theme, resolve_mode


def system_is_dark() -> bool | None:
    """系统是否处于暗色（读不到就返回 None，交给主题默认值兜底）。"""
    app = QApplication.instance()
    if app is None:
        return None
    try:
        scheme = app.styleHints().colorScheme()
        color_scheme = getattr(Qt, "ColorScheme", None)
        if color_scheme is None:
            return None
        return scheme == color_scheme.Dark
    except Exception:  # noqa: BLE001 - 老版本 Qt 没有该接口
        return None


class MainWindow(QMainWindow):
    NAV_COURSES = 0
    NAV_WIZARD = 1
    NAV_STUDY = 2
    NAV_QA = 3
    NAV_NOTES = 4
    NAV_SETTINGS = 5

    def __init__(
        self,
        result: BootstrapResult,
        *,
        services: Services | None = None,
        bus: EventBus | None = None,
        thread_pool: QThreadPool | None = None,
        study_page: StudyPage | None = None,
        show_first_run: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._result = result
        self._services = services if services is not None else build_services(result)
        self._config = result.config
        self._bus = bus if bus is not None else EventBus()
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._css_vars: dict = {}
        self._first_run_shown = not show_first_run
        # 首启引导延迟到事件循环启动后（主窗口已显示再弹，避免点在未显示的窗口上）
        QTimer.singleShot(0, self._maybe_show_first_run)

        self.setWindowTitle("AI Teaches Me")
        self.resize(1220, 780)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 8)

        self.nav = QListWidget()
        self.nav.setFixedWidth(170)
        for label in ("课程库", "新建课程", "学习台", "小灶", "笔记中心", "设置"):
            self.nav.addItem(QListWidgetItem(label))
        self.nav.currentRowChanged.connect(self._on_nav_changed)
        layout.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.library_page = LibraryPage(result.layout.courses, service=self._services.library)
        self.wizard_page = WizardPage(self._services.outline, thread_pool=self._pool)
        self.study_page = study_page if study_page is not None else StudyPage(services=self._services, thread_pool=self._pool)
        from ..agents.qa import QaAgent

        self.qa_page = QaPage(
            courses_dir=result.layout.courses,
            agent_factory=lambda: QaAgent(self._services.client),
            thread_pool=self._pool,
        )
        self.qa_page.status_message.connect(lambda message: self.statusBar().showMessage(message, 10000))
        self.notes_page = NotesPage(courses_dir=result.layout.courses, pdf_printer=None)
        self.notes_page.status_message.connect(lambda message: self.statusBar().showMessage(message, 10000))
        self.settings_page = SettingsPage(
            self._services.settings,
            thread_pool=self._pool,
            client=getattr(self._services, "client", None),
            courses_dir=result.layout.courses,
        )
        for page in (self.library_page, self.wizard_page, self.study_page, self.qa_page, self.notes_page, self.settings_page):
            self.stack.addWidget(page)
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

        self._wire_signals()
        for message in result.messages:
            status.showMessage(message, 15000)

        self.refresh_library()
        self.apply_current_theme()
        self.nav.setCurrentRow(self.NAV_COURSES)

    def _wire_signals(self) -> None:
        library = self.library_page
        library.open_course.connect(self._open_course)
        library.go_settings.connect(lambda: self.nav.setCurrentRow(self.NAV_SETTINGS))
        library.new_course_requested.connect(lambda: self.nav.setCurrentRow(self.NAV_WIZARD))
        library.rename_requested.connect(self._rename_course)
        library.delete_requested.connect(self._delete_course)
        library.complete_toggled.connect(self._toggle_course_complete)
        library.open_in_explorer.connect(self._open_in_explorer)
        library.backup_requested.connect(self._backup_course)
        library.restore_requested.connect(self._restore_backup)
        library.icon_requested.connect(lambda summary: self._open_icon_picker(summary))
        library.color_requested.connect(lambda summary: self._open_icon_picker(summary))

        self.wizard_page.course_created.connect(self._on_course_created)
        self.wizard_page.status_message.connect(lambda message: self.statusBar().showMessage(message, 8000))

        self.settings_page.theme_changed.connect(self._on_theme_changed)
        self.settings_page.config_saved.connect(self._on_preferences_saved)
        self.settings_page.backup_requested.connect(self._backup_library)
        self.settings_page.restore_requested.connect(self._restore_backup)
        self.settings_page.open_folder_requested.connect(self._open_folder)
        self.settings_page.status_message.connect(self.statusBar().showMessage)

        self.study_page.back_requested.connect(lambda: self.nav.setCurrentRow(self.NAV_COURSES))
        self.study_page.lesson_completed.connect(self._on_lesson_completed)
        self.notes_page.status_message.connect(lambda message: self.statusBar().showMessage(message, 10000))
        self.study_page.status_message.connect(self.statusBar().showMessage)

        self._bus.subscribe("theme:changed", lambda event: self.study_page.apply_theme_vars(event.payload))

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
        self.library_page.set_card_style(CardStyle.from_theme(ui.theme, mode))
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
        needs_api = not self._services.settings.has_api_key(self._config.active_profile)
        courses = self.library_page.refresh(needs_api_setup=needs_api)
        self._update_api_label()
        return courses

    def _update_api_label(self) -> None:
        if self._services.settings.has_api_key(self._config.active_profile):
            masked = self._services.settings.api_key_masked(self._config.active_profile)
            self.api_label.setText(f"API：已配置 {masked}")
        else:
            self.api_label.setText("API：尚未配置（去设置页填写）")

    @Slot(object)
    def _on_course_created(self, result: object) -> None:
        self.refresh_library()
        self.nav.setCurrentRow(self.NAV_COURSES)
        name = getattr(result, "name", "")
        self.statusBar().showMessage(f"《{name}》已创建，双击即可开始学习。", 10000)

    @Slot(object)
    def _rename_course(self, summary: course_service.CourseSummary) -> None:
        new_name, ok = QInputDialog.getText(self, "重命名课程", "新名称：", text=summary.name)
        if not ok or not new_name.strip():
            return
        try:
            course_service.rename_course(summary.path, new_name.strip(), index=self._services.index)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "重命名失败", str(exc))
            return
        self.refresh_library()
        self.statusBar().showMessage(f"已重命名为《{new_name.strip()}》", 8000)

    @Slot(object)
    def _delete_course(self, summary: course_service.CourseSummary) -> None:
        detail = f"《{summary.name}》\n{summary.chapters} 章 · {summary.lessons} 小节"
        answer = QMessageBox.question(
            self,
            "删除课程",
            f"确定要删除吗？\n\n{detail}\n\n课程会移动到系统回收站，可以恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        if course_service.delete_course(summary.path, index=self._services.index):
            self.refresh_library()
            self.statusBar().showMessage(f"《{summary.name}》已移入回收站", 8000)

    @Slot(object, bool)
    def _toggle_course_complete(self, summary: course_service.CourseSummary, completed: bool) -> None:
        course_service.mark_course_complete(summary.path, completed=completed)
        self.refresh_library()
        self.statusBar().showMessage(
            f"《{summary.name}》已{'标记完成' if completed else '取消完成'}（内容未改动）", 8000
        )

    @Slot()
    def _on_preferences_saved(self) -> None:
        """偏好改动后立刻生效：主题/排版/人设/生成偏好。"""
        self.apply_current_theme()
        self.refresh_library()

    @Slot(str)
    def _open_folder(self, path: str) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    @Slot(object)
    def _open_icon_picker(self, summary: course_service.CourseSummary) -> None:
        """卡片右键「换图标 / 换颜色」：预设、配色、或让 AI 生成。"""
        dialog = IconPickerDialog(
            course_dir=summary.path,
            course_name=summary.name,
            client=getattr(self._services, "client", None),
            pool=self._pool,
            parent=self,
        )
        if dialog.exec():
            self.library_page._icon_cache.pop(summary.id, None)  # noqa: SLF001 - 图标变了要清缓存
            self.refresh_library()
            self.statusBar().showMessage(f"《{summary.name}》的图标已更新", 8000)

    @Slot(object)
    def _backup_course(self, summary: course_service.CourseSummary) -> None:
        """备份单门课程为 zip（FR-13；备份包不含密钥）。"""
        from PySide6.QtWidgets import QFileDialog

        from ..services import backup_service

        target, _ = QFileDialog.getSaveFileName(
            self, "备份课程", f"{summary.name}-备份.zip", "Zip 备份 (*.zip)"
        )
        if not target:
            return
        path = backup_service.backup_course(summary.path, target)
        self.statusBar().showMessage(f"已备份到 {path.name}", 10000)

    @Slot()
    def _restore_backup(self) -> None:
        """从 zip 恢复课程（恢复到课程库目录，重名自动追加后缀）。"""
        from PySide6.QtWidgets import QFileDialog

        from ..services import restore_service

        source, _ = QFileDialog.getOpenFileName(self, "选择备份文件", "", "Zip 备份 (*.zip)")
        if not source:
            return
        try:
            restored = restore_service.restore_from_zip(source, self._result.layout.courses)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "恢复失败", str(exc))
            return
        self.refresh_library()
        self.statusBar().showMessage(f"已恢复 {len(restored)} 个文件到课程库", 10000)

    @Slot()
    def _maybe_show_first_run(self) -> None:
        """首启动引导（未配置密钥时显示一次）。

        要点：**必须等主窗口真正显示并激活之后**再弹，否则用户点在「还没显示出来的窗口」上，
        看起来就像「按钮没反应」。所以这里用 QTimer 延迟到事件循环启动后，并用模态 exec()。
        """
        if not self._result.needs_api_setup or self._first_run_shown:
            return
        self._first_run_shown = True
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()

        dialog = FirstRunDialog(has_seed_course=bool(self.library_page.course_list.count()), parent=self)
        dialog.go_settings.connect(lambda: self._jump_to(self.NAV_SETTINGS))
        dialog.go_new_course.connect(lambda: self._jump_to(self.NAV_WIZARD))
        dialog.go_backup.connect(self._backup_library)
        dialog.open_seed_course.connect(self._open_first_course)
        self._first_run_dialog = dialog
        dialog.exec()
        self.raise_()
        self.activateWindow()

    def _jump_to(self, row: int) -> None:
        """切页并确保窗口在最前面（首启引导点按钮后立刻可见）。"""
        if not self.isVisible():
            self.show()
        self.nav.setCurrentRow(row)
        self.stack.setCurrentIndex(row)
        self.raise_()
        self.activateWindow()

    @Slot()
    def _backup_library(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        from ..services import backup_service

        target, _ = QFileDialog.getSaveFileName(self, "备份课程库", "课程库备份.zip", "Zip 备份 (*.zip)")
        if not target:
            return
        path = backup_service.backup_library(self._result.layout.courses, target)
        self.statusBar().showMessage(f"已备份课程库到 {path.name}", 10000)

    @Slot()
    def _open_first_course(self) -> None:
        self._jump_to(self.NAV_COURSES)
        if self.library_page.course_list.count():
            self.library_page.course_list.setCurrentRow(0)
            summary = self.library_page.selected_course()
            if summary is not None:
                self._open_course(summary)

    @Slot(object)
    def _open_in_explorer(self, summary: course_service.CourseSummary) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(summary.path)))

    @Slot(object)
    def _open_course(self, summary: course_service.CourseSummary) -> None:
        tree = course_service.lesson_tree(summary.path)
        lesson = next((item for chapter in tree for item in chapter["lessons"]), None)
        if lesson is None or lesson.get("path") is None:
            self.statusBar().showMessage(f"《{summary.name}》还没有可读的小节，先去生成讲义。", 8000)
            return
        opener = getattr(self.study_page, "open_course", None)
        if callable(opener):
            opener(summary.path, lesson["path"])
        else:  # 兼容只实现了 load_lesson 的渲染替身
            self.study_page.load_lesson(Path(lesson["path"]))
        self.nav.setCurrentRow(self.NAV_STUDY)

    @Slot(str)
    def _on_lesson_completed(self, lesson_id: str) -> None:
        self.refresh_library()
        note_hint = ""
        lesson_dir = getattr(self.study_page, "_lesson_dir", None)
        if lesson_dir is not None and getattr(self._config.generation, "auto_note_on_complete", True):
            from ..services import notes_service

            try:
                result = notes_service.generate_notes(lesson_dir.parents[3], lesson_dir)
                note_hint = f"，笔记已生成（{result.path.name}）"
                self.notes_page.refresh()
            except Exception as exc:  # noqa: BLE001 - 笔记失败不影响完成状态
                note_hint = f"，但笔记生成失败：{exc}"
        self.statusBar().showMessage(f"{lesson_id} 已完成，进度已落盘{note_hint}。", 12000)

    @Slot(int)
    def _on_nav_changed(self, row: int) -> None:
        if 0 <= row < self.stack.count():
            self.stack.setCurrentIndex(row)
            if row == self.NAV_COURSES:
                self.refresh_library()
