"""设置页（《项目文档》§11.2 设置、FR-02 / FR-11；M0 含「测试连接」）。

约束：网络握手必须跑在**工作线程**（《技术文档》§1.5 红线 3），因此这里用 QRunnable +
Signal 回主线程；服务层保持无 Qt。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...core.config import SEARCH_BACKENDS, LLMProfile
from ...llm.client import ConnectionReport
from ...services.settings_service import SettingsService
from ..theme.presets import theme_choices


class _ProbeSignals(QObject):
    finished = Signal(object)


class _ProbeTask(QRunnable):
    """在工作线程里做一次握手探测（主线程绝不做网络请求）。"""

    def __init__(self, service: SettingsService, payload: dict, signals: _ProbeSignals) -> None:
        super().__init__()
        self._service = service
        self._payload = payload
        self._signals = signals

    @Slot()
    def run(self) -> None:
        try:
            report = self._service.test_connection_sync(**self._payload)
        except Exception as exc:  # noqa: BLE001 - 线程内异常必须转成结果对象
            report = ConnectionReport(ok=False, error=str(exc), message=f"连接失败：{exc}")
        self._signals.finished.emit(report)


class SettingsPage(QWidget):
    """供应商档案 / 密钥 / 外观（主题、跟随系统、减少动效、字号）。"""

    theme_changed = Signal(str)
    config_saved = Signal()
    status_message = Signal(str)

    def __init__(self, service: SettingsService, *, thread_pool: QThreadPool | None = None, parent=None) -> None:
        super().__init__(parent)
        self._service = service
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._probe_signals = _ProbeSignals()
        self._probe_signals.finished.connect(self._on_probe_finished)

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_provider_group())
        layout.addWidget(self._build_appearance_group())
        layout.addWidget(self._build_search_group())
        layout.addWidget(self._build_result_group())
        layout.addStretch(1)

        self._reload_profiles()
        self._load_appearance()

    # --- 构建界面 -------------------------------------------------------
    def _build_provider_group(self) -> QGroupBox:
        group = QGroupBox("AI 供应商与密钥")
        outer = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel("档案"))
        self.profile_combo = QComboBox()
        self.profile_combo.currentIndexChanged.connect(self._on_profile_changed)
        row.addWidget(self.profile_combo, 1)
        self.add_profile_button = QPushButton("新建档案")
        self.add_profile_button.clicked.connect(self._on_add_profile)
        row.addWidget(self.add_profile_button)
        self.delete_profile_button = QPushButton("删除档案")
        self.delete_profile_button.clicked.connect(self._on_delete_profile)
        row.addWidget(self.delete_profile_button)
        outer.addLayout(row)

        form = QFormLayout()
        self.name_edit = QLineEdit()
        self.base_url_edit = QLineEdit()
        self.chat_model_edit = QLineEdit()
        self.vision_model_edit = QLineEdit()
        self.vision_model_edit.setPlaceholderText("留空 = 沿用对话模型（DeepSeek 自带读图）")
        self.temperature_spin = QDoubleSpinBox()
        self.temperature_spin.setRange(0.0, 2.0)
        self.temperature_spin.setSingleStep(0.1)
        self.max_tokens_spin = QSpinBox()
        self.max_tokens_spin.setRange(64, 200_000)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(5, 600)
        self.proxy_edit = QLineEdit()
        self.proxy_edit.setPlaceholderText("可选，如 http://127.0.0.1:7890")

        form.addRow("名称", self.name_edit)
        form.addRow("Base URL", self.base_url_edit)
        form.addRow("对话模型", self.chat_model_edit)
        form.addRow("读图模型", self.vision_model_edit)
        form.addRow("温度", self.temperature_spin)
        form.addRow("最大 token", self.max_tokens_spin)
        form.addRow("超时（秒）", self.timeout_spin)
        form.addRow("代理", self.proxy_edit)
        outer.addLayout(form)

        key_row = QHBoxLayout()
        key_row.addWidget(QLabel("API Key"))
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("留空表示不修改已保存的密钥")
        key_row.addWidget(self.api_key_edit, 1)
        self.save_key_button = QPushButton("保存密钥")
        self.save_key_button.clicked.connect(self._on_save_key)
        key_row.addWidget(self.save_key_button)
        self.clear_key_button = QPushButton("清除密钥")
        self.clear_key_button.clicked.connect(self._on_clear_key)
        key_row.addWidget(self.clear_key_button)
        outer.addLayout(key_row)

        self.key_status_label = QLabel("")
        self.key_status_label.setProperty("role", "muted")
        outer.addWidget(self.key_status_label)

        action_row = QHBoxLayout()
        self.save_button = QPushButton("保存配置")
        self.save_button.setProperty("role", "primary")
        self.save_button.clicked.connect(self._on_save)
        action_row.addWidget(self.save_button)
        self.test_button = QPushButton("测试连接")
        self.test_button.clicked.connect(self._on_test_connection)
        action_row.addWidget(self.test_button)
        action_row.addStretch(1)
        outer.addLayout(action_row)
        return group

    def _build_appearance_group(self) -> QGroupBox:
        group = QGroupBox("外观")
        form = QFormLayout(group)

        self.theme_combo = QComboBox()
        for key, name in theme_choices():
            self.theme_combo.addItem(name, key)
        self.theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        form.addRow("主题", self.theme_combo)

        self.follow_system_check = QCheckBox("跟随系统明暗")
        self.follow_system_check.toggled.connect(self._on_appearance_toggled)
        form.addRow("", self.follow_system_check)

        self.reduce_motion_check = QCheckBox("减少动效")
        self.reduce_motion_check.toggled.connect(self._on_appearance_toggled)
        form.addRow("", self.reduce_motion_check)

        self.font_size_spin = QSpinBox()
        self.font_size_spin.setRange(12, 24)
        self.font_size_spin.valueChanged.connect(self._on_appearance_toggled)
        form.addRow("正文字号", self.font_size_spin)
        return group

    def _build_search_group(self) -> QGroupBox:
        """联网与时效（FR-19：默认关闭 + 后端可插拔 + 每日预算）。"""
        group = QGroupBox("联网与时效")
        form = QFormLayout(group)

        self.search_enabled_check = QCheckBox("启用联网搜索（默认关闭）")
        self.search_enabled_check.toggled.connect(self._on_save_search)
        form.addRow("", self.search_enabled_check)

        self.search_backend_combo = QComboBox()
        for backend in SEARCH_BACKENDS:
            self.search_backend_combo.addItem(backend, backend)
        self.search_backend_combo.currentIndexChanged.connect(self._on_save_search)
        form.addRow("后端", self.search_backend_combo)

        self.search_url_edit = QLineEdit()
        self.search_url_edit.editingFinished.connect(self._on_save_search)
        form.addRow("端点", self.search_url_edit)

        self.search_model_edit = QLineEdit()
        self.search_model_edit.editingFinished.connect(self._on_save_search)
        form.addRow("模型", self.search_model_edit)

        self.search_topk_spin = QSpinBox()
        self.search_topk_spin.setRange(1, 10)
        self.search_topk_spin.valueChanged.connect(self._on_save_search)
        form.addRow("结果条数", self.search_topk_spin)

        self.search_budget_spin = QSpinBox()
        self.search_budget_spin.setRange(0, 10000)
        self.search_budget_spin.valueChanged.connect(self._on_save_search)
        form.addRow("每日预算（次）", self.search_budget_spin)

        key_row = QHBoxLayout()
        self.search_key_edit = QLineEdit()
        self.search_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.search_key_edit.setPlaceholderText("留空表示不修改已保存的密钥")
        key_row.addWidget(self.search_key_edit, 1)
        self.search_key_button = QPushButton("保存搜索密钥")
        self.search_key_button.clicked.connect(self._on_save_search_key)
        key_row.addWidget(self.search_key_button)
        form.addRow("搜索 API Key", key_row)
        self._load_search_settings()
        return group

    def _load_search_settings(self) -> None:
        model = self._service.config.searching_model
        self.search_enabled_check.blockSignals(True)
        self.search_enabled_check.setChecked(bool(model.enabled))
        self.search_enabled_check.blockSignals(False)
        index = self.search_backend_combo.findData(model.backend)
        self.search_backend_combo.blockSignals(True)
        self.search_backend_combo.setCurrentIndex(index if index >= 0 else 0)
        self.search_backend_combo.blockSignals(False)
        self.search_url_edit.setText(model.url)
        self.search_model_edit.setText(model.default_model)
        self.search_topk_spin.blockSignals(True)
        self.search_topk_spin.setValue(int(model.top_k))
        self.search_topk_spin.blockSignals(False)
        self.search_budget_spin.blockSignals(True)
        self.search_budget_spin.setValue(int(model.daily_call_budget))
        self.search_budget_spin.blockSignals(False)

    @Slot()
    def _on_save_search(self) -> None:
        model = self._service.config.searching_model
        model.enabled = bool(self.search_enabled_check.isChecked())
        model.backend = str(self.search_backend_combo.currentData() or "qianfan")
        model.url = self.search_url_edit.text().strip() or model.url
        model.default_model = self.search_model_edit.text().strip() or model.default_model
        model.top_k = int(self.search_topk_spin.value())
        model.daily_call_budget = int(self.search_budget_spin.value())
        self._service.save()
        self.status_message.emit(
            f"联网设置已保存（{'开启' if model.enabled else '关闭'} · {model.backend} · 每日 {model.daily_call_budget} 次）"
        )

    @Slot()
    def _on_save_search_key(self) -> None:
        value = self.search_key_edit.text().strip()
        if not value:
            self.status_message.emit("请输入搜索 API Key")
            return
        model = self._service.config.searching_model
        ref = model.api_key_ref or "atm/qianfan-search"
        self._service.store.set_secret(ref, value)
        model.api_key = ""
        model.api_key_ref = ref
        self._service.save()
        self.search_key_edit.clear()
        self.status_message.emit(f"搜索密钥已写入系统凭据库（{self._service.store.masked(ref)}）")

    def _build_result_group(self) -> QGroupBox:
        group = QGroupBox("连接测试结果")
        layout = QVBoxLayout(group)
        self.result_label = QLabel("尚未测试。填写 Base URL 与 API Key 后点「测试连接」。")
        self.result_label.setWordWrap(True)
        layout.addWidget(self.result_label)
        return group

    # --- 数据装载 -------------------------------------------------------
    def _reload_profiles(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        for name in self._service.profile_names():
            self.profile_combo.addItem(name, name)
        active = self._service.config.active_profile
        index = self.profile_combo.findData(active)
        self.profile_combo.setCurrentIndex(index if index >= 0 else 0)
        self.profile_combo.blockSignals(False)
        self._load_profile_into_form()

    def _load_appearance(self) -> None:
        ui = self._service.config.ui
        index = self.theme_combo.findData(ui.theme)
        self.theme_combo.blockSignals(True)
        self.theme_combo.setCurrentIndex(index if index >= 0 else 0)
        self.theme_combo.blockSignals(False)
        self.follow_system_check.blockSignals(True)
        self.follow_system_check.setChecked(ui.follow_system)
        self.follow_system_check.blockSignals(False)
        self.reduce_motion_check.blockSignals(True)
        self.reduce_motion_check.setChecked(ui.reduce_motion)
        self.reduce_motion_check.blockSignals(False)
        self.font_size_spin.blockSignals(True)
        self.font_size_spin.setValue(ui.font_size)
        self.font_size_spin.blockSignals(False)

    def _load_profile_into_form(self) -> None:
        name = self.profile_combo.currentData()
        if not name:
            return
        profile = self._service.profile(str(name))
        self.name_edit.setText(profile.name)
        self.base_url_edit.setText(profile.base_url)
        self.chat_model_edit.setText(profile.chat_model)
        self.vision_model_edit.setText(profile.vision_model or "")
        self.temperature_spin.setValue(float(profile.temperature))
        self.max_tokens_spin.setValue(int(profile.max_tokens))
        self.timeout_spin.setValue(int(profile.timeout_seconds))
        self.proxy_edit.setText(profile.proxy or "")
        self.api_key_edit.clear()
        self._refresh_key_status()

    def _collect_profile(self) -> LLMProfile:
        profile = self._service.profile(str(self.profile_combo.currentData())).model_copy()
        profile.name = self.name_edit.text().strip() or profile.name
        profile.base_url = self.base_url_edit.text().strip() or profile.base_url
        profile.chat_model = self.chat_model_edit.text().strip() or profile.chat_model
        profile.vision_model = self.vision_model_edit.text().strip() or None
        profile.temperature = float(self.temperature_spin.value())
        profile.max_tokens = int(self.max_tokens_spin.value())
        profile.timeout_seconds = int(self.timeout_spin.value())
        profile.proxy = self.proxy_edit.text().strip() or None
        return profile

    def _refresh_key_status(self) -> None:
        name = str(self.profile_combo.currentData() or "")
        masked = self._service.api_key_masked(name)
        self.key_status_label.setText(f"已保存密钥：{masked}" if masked else "尚未保存密钥")

    # --- 交互 -----------------------------------------------------------
    @Slot()
    def _on_profile_changed(self) -> None:
        self._load_profile_into_form()

    @Slot()
    def _on_add_profile(self) -> None:
        base = "new-profile"
        names = set(self._service.profile_names())
        name = base
        index = 2
        while name in names:
            name = f"{base}-{index}"
            index += 1
        self._service.upsert_profile(LLMProfile(name=name))
        self._service.save()
        self._reload_profiles()
        self.profile_combo.setCurrentIndex(self.profile_combo.findData(name))
        self.status_message.emit(f"已新建档案 {name}")

    @Slot()
    def _on_delete_profile(self) -> None:
        name = str(self.profile_combo.currentData() or "")
        try:
            self._service.delete_profile(name)
        except Exception as exc:  # noqa: BLE001 - 展示可操作提示
            self.status_message.emit(str(exc))
            return
        self._service.save()
        self._reload_profiles()
        self.status_message.emit(f"已删除档案 {name}")

    @Slot()
    def _on_save_key(self) -> None:
        name = str(self.profile_combo.currentData() or "")
        value = self.api_key_edit.text().strip()
        if not value:
            self.status_message.emit("请输入 API Key 后再保存（留空不会覆盖已保存的密钥）")
            return
        masked = self._service.set_api_key(name, value)
        self._service.save()
        self.api_key_edit.clear()
        self._refresh_key_status()
        self.status_message.emit(f"密钥已写入系统凭据库（{masked}）")

    @Slot()
    def _on_clear_key(self) -> None:
        name = str(self.profile_combo.currentData() or "")
        self._service.clear_api_key(name)
        self._service.save()
        self._refresh_key_status()
        self.status_message.emit("已清除该档案的密钥")

    @Slot()
    def _on_save(self) -> None:
        try:
            profile = self._collect_profile()
            self._service.upsert_profile(profile)
            self._service.set_active_profile(profile.name)
            self._service.save()
        except Exception as exc:  # noqa: BLE001
            self.status_message.emit(f"保存失败：{exc}")
            return
        self._reload_profiles()
        self.config_saved.emit()
        self.status_message.emit("配置已保存")

    @Slot()
    def _on_test_connection(self) -> None:
        name = str(self.profile_combo.currentData() or "")
        payload = {
            "profile_name": name,
            "api_key": self.api_key_edit.text().strip() or None,
            "base_url": self.base_url_edit.text().strip() or None,
            "model": self.chat_model_edit.text().strip() or None,
        }
        self.test_button.setEnabled(False)
        self.result_label.setText("正在握手…（在工作线程里进行，界面不会卡住）")
        self._pool.start(_ProbeTask(self._service, payload, self._probe_signals))

    @Slot(object)
    def _on_probe_finished(self, report: ConnectionReport) -> None:
        self.test_button.setEnabled(True)
        self.result_label.setText(self.format_report(report))
        self.status_message.emit("连接测试完成" if report.ok else "连接测试失败")

    @Slot(int)
    def _on_theme_changed(self, _index: int) -> None:
        key = str(self.theme_combo.currentData() or "")
        self._service.set_theme(key)
        self._service.save()
        self.theme_changed.emit(key)

    @Slot()
    def _on_appearance_toggled(self) -> None:
        self._service.set_follow_system(self.follow_system_check.isChecked())
        self._service.set_reduce_motion(self.reduce_motion_check.isChecked())
        self._service.set_font_size(int(self.font_size_spin.value()))
        self._service.save()
        self.config_saved.emit()

    # --- 展示 -----------------------------------------------------------
    @staticmethod
    def format_report(report: ConnectionReport) -> str:
        """错误必须可操作：状态码 + 人话 + 下一步（§11.5 第 7 条）。"""
        if report.ok:
            models = f"，模型 {len(report.models)} 个" if report.models else ""
            return f"✅ {report.message}（握手 {report.elapsed_ms} ms{models}）"
        status = f"HTTP {report.status} · " if report.status else ""
        return f"❌ {status}{report.message}\n建议：检查 Base URL 与 API Key，必要时换一个兼容端点。"
