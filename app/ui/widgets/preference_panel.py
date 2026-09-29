"""偏好面板（《项目文档》§11.2 设置信息架构、《技术文档》§3.5.5）。

补齐 M9 之前缺失的设置分区：

* 教学风格（人设选择 / 导入导出 / 一句话试讲）—— FR-14
* 生成偏好（默认深度、自动 SVG / 动画 / 笔记、测验与自评开关、流式节流）—— FR-10 / FR-18
* 视觉自检（开关与最大重画轮数）—— §8.4.1
* 外观进阶（中文字体、阅读宽度、背景图 / 纯色 / 渐变 + 模糊与暗度）—— FR-11
* 模型分配（文本任务 / 视觉任务分别指定档案）—— AC-07
* 工作区与备份（路径、备份/恢复、打开目录、从环境变量导入密钥）—— FR-13 / AC-06
* 关于与日志（版本、日志入口、合规提示）—— §14.4
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...agents.persona import Persona
from ...services.persona_service import PersonaService
from .persona_editor_dialog import PersonaEditorDialog

APP_VERSION = "0.2.0"
COMPLIANCE_NOTES = (
    "本软件不提供任何课程内容，所有内容由你配置的 AI 服务生成，请遵守所使用服务商的条款。\n"
    "上传资料仅存本地、仅用于本地检索与讲解；开启联网搜索后，检索词会发送给你所选择的搜索服务商。\n"
    "生成的讲解可能存在错误，重要决策请以权威资料为准。"
)


class _TrialSignals(QObject):
    done = Signal(str)
    failed = Signal(str)


class _TrialTask(QRunnable):
    """在工作线程里跑「一句话试讲」。"""

    def __init__(self, client, persona_body: str, signals: _TrialSignals) -> None:
        super().__init__()
        self._client = client
        self._persona_body = persona_body
        self._signals = signals

    @Slot()
    def run(self) -> None:
        async def go() -> str:
            messages: list[dict] = []
            if self._persona_body:
                messages.append({"role": "system", "content": self._persona_body})
            messages.append(
                {"role": "user", "content": "用你的风格给开场：『矩阵乘法不是逐项相乘』。80 字以内，不要输出 JSON。"}
            )
            parts: list[str] = []
            async for piece in self._client.stream_chat(messages, task="teach"):
                parts.append(piece)
            return "".join(parts)

        try:
            text = asyncio.run(go())
        except Exception as exc:  # noqa: BLE001
            self._signals.failed.emit(str(exc))
            return
        self._signals.done.emit(text)


class PreferencePanel(QWidget):
    """设置页的偏好面板（服务层不碰 Qt，这里只做读写与展示）。"""

    config_saved = Signal()
    status_message = Signal(str)
    backup_requested = Signal()
    restore_requested = Signal()
    open_folder_requested = Signal(str)
    restore_seed_requested = Signal()

    def __init__(self, service, *, client=None, courses_dir=None, thread_pool: QThreadPool | None = None, parent=None) -> None:
        super().__init__(parent)
        self._service = service
        self._client = client
        self._courses_dir = Path(courses_dir) if courses_dir else Path(service.workspace_root) / "Courses"
        self._pool = thread_pool if thread_pool is not None else QThreadPool.globalInstance()
        self._personas = PersonaService(user_dir=Path(service.workspace_root) / "personas")
        self._trial_signals = _TrialSignals()
        self._trial_signals.done.connect(self._on_trial_done)
        self._trial_signals.failed.connect(lambda message: self.trial_output.setPlainText(f"试讲失败：{message}"))

        layout = QVBoxLayout(self)
        layout.addWidget(self._build_persona_group())
        layout.addWidget(self._build_generation_group())
        layout.addWidget(self._build_review_group())
        layout.addWidget(self._build_appearance_group())
        layout.addWidget(self._build_routing_group())
        layout.addWidget(self._build_workspace_group())
        layout.addWidget(self._build_about_group())
        self.reload()

    # --- 教学风格 -------------------------------------------------------
    def _build_persona_group(self) -> QGroupBox:
        group = QGroupBox("教学风格（人设）")
        layout = QVBoxLayout(group)

        row = QHBoxLayout()
        row.addWidget(QLabel("默认人设"))
        self.persona_combo = QComboBox()
        self.persona_combo.currentIndexChanged.connect(self._on_persona_changed)
        row.addWidget(self.persona_combo, 1)
        self.create_button = QPushButton("+ 新建人设")
        self.create_button.setProperty("role", "primary")
        self.create_button.clicked.connect(self._on_create_persona)
        row.addWidget(self.create_button)
        self.import_button = QPushButton("导入人设…")
        self.import_button.clicked.connect(self._on_import_persona)
        row.addWidget(self.import_button)
        self.export_button = QPushButton("导出人设…")
        self.export_button.clicked.connect(self._on_export_persona)
        row.addWidget(self.export_button)
        layout.addLayout(row)

        self.persona_preview = QPlainTextEdit()
        self.persona_preview.setReadOnly(True)
        self.persona_preview.setFixedHeight(96)
        layout.addWidget(self.persona_preview)

        trial_row = QHBoxLayout()
        self.allow_roleplay_check = QCheckBox("允许角色扮演（仅影响表达层，不改变知识点与深度）")
        self.allow_roleplay_check.toggled.connect(self._on_allow_roleplay)
        trial_row.addWidget(self.allow_roleplay_check, 1)
        self.trial_button = QPushButton("一句话试讲")
        self.trial_button.clicked.connect(self._on_trial)
        trial_row.addWidget(self.trial_button)
        layout.addLayout(trial_row)

        self.trial_output = QPlainTextEdit()
        self.trial_output.setReadOnly(True)
        self.trial_output.setFixedHeight(56)
        self.trial_output.setPlaceholderText("试讲结果会显示在这里（不改动任何课程内容）")
        layout.addWidget(self.trial_output)
        return group

    # --- 生成偏好 -------------------------------------------------------
    def _build_generation_group(self) -> QGroupBox:
        group = QGroupBox("生成偏好")
        form = QFormLayout(group)

        self.depth_combo = QComboBox()
        for value, label in (("minimal", "极简"), ("standard", "标准"), ("deep", "深入")):
            self.depth_combo.addItem(label, value)
        self.depth_combo.currentIndexChanged.connect(self._save)
        form.addRow("默认讲解深度", self.depth_combo)

        self.auto_svg_check = QCheckBox("自动生成 SVG 图示（经渲染自检后才呈现）")
        self.auto_animation_check = QCheckBox("自动生成 HTML 微动画（沙箱禁网）")
        self.auto_note_check = QCheckBox("小节完成后自动生成笔记")
        for widget in (self.auto_svg_check, self.auto_animation_check, self.auto_note_check):
            widget.toggled.connect(self._save)
            form.addRow("", widget)

        self.quiz_check = QCheckBox("出小测验（关掉 = 不做测验，讲义里不会出现答题卡）")
        self.self_assessment_check = QCheckBox("要求自评掌握度（关掉 = 不做作业式自评，读完直接标记完成）")
        for widget in (self.quiz_check, self.self_assessment_check):
            widget.toggled.connect(self._save)
            form.addRow("", widget)

        hint_learning = QLabel("按 FR-18：关掉上面两项后，讲义第 5 段只保留小结与下节预告，进度与时长照常累计；"
                              "也可以在单个小节里「跟随课程 / 强制开 / 强制关」。")
        hint_learning.setProperty("role", "muted")
        hint_learning.setWordWrap(True)
        form.addRow("", hint_learning)

        self.throttle_spin = QSpinBox()
        self.throttle_spin.setRange(30, 60)
        self.throttle_spin.setSuffix(" ms")
        self.throttle_spin.valueChanged.connect(self._save)
        form.addRow("流式渲染节流（30–60ms）", self.throttle_spin)
        return group

    # --- 视觉自检 -------------------------------------------------------
    def _build_review_group(self) -> QGroupBox:
        group = QGroupBox("视觉自检")
        form = QFormLayout(group)

        self.review_check = QCheckBox("图示先渲染成 PNG 并自检（失败自动重画）")
        self.review_check.toggled.connect(self._save)
        form.addRow("", self.review_check)

        self.rounds_spin = QSpinBox()
        self.rounds_spin.setRange(1, 5)
        self.rounds_spin.setSuffix(" 轮")
        self.rounds_spin.valueChanged.connect(self._save)
        form.addRow("最大重画轮数", self.rounds_spin)

        hint = QLabel("默认沿用当前对话模型读图（DeepSeek 自带多模态），无需额外配置视觉模型。")
        hint.setProperty("role", "muted")
        hint.setWordWrap(True)
        form.addRow("", hint)
        return group

    # --- 外观进阶 -------------------------------------------------------
    def _build_appearance_group(self) -> QGroupBox:
        group = QGroupBox("外观与排版")
        form = QFormLayout(group)

        self.font_edit = QLineEdit()
        self.font_edit.editingFinished.connect(self._save)
        form.addRow("界面字体", self.font_edit)

        self.width_spin = QSpinBox()
        self.width_spin.setRange(600, 1200)
        self.width_spin.setSuffix(" px")
        self.width_spin.valueChanged.connect(self._save)
        form.addRow("阅读区宽度", self.width_spin)

        self.bg_type_combo = QComboBox()
        for value, label in (("gradient", "渐变"), ("color", "纯色"), ("image", "图片")):
            self.bg_type_combo.addItem(label, value)
        self.bg_type_combo.currentIndexChanged.connect(self._save)
        form.addRow("背景类型", self.bg_type_combo)

        self.bg_value_edit = QLineEdit()
        self.bg_value_edit.setPlaceholderText("纯色填 #RRGGBB；渐变填 CSS 值；图片填 assets/bg.png 之类的相对路径")
        self.bg_value_edit.editingFinished.connect(self._save)
        form.addRow("背景值", self.bg_value_edit)

        self.bg_blur_spin = QDoubleSpinBox()
        self.bg_blur_spin.setRange(0, 20)
        self.bg_blur_spin.valueChanged.connect(self._save)
        form.addRow("背景模糊", self.bg_blur_spin)

        self.bg_dim_spin = QDoubleSpinBox()
        self.bg_dim_spin.setRange(0, 0.8)
        self.bg_dim_spin.setSingleStep(0.05)
        self.bg_dim_spin.valueChanged.connect(self._save)
        form.addRow("背景暗度", self.bg_dim_spin)
        return group

    # --- 模型分配 -------------------------------------------------------
    def _build_routing_group(self) -> QGroupBox:
        group = QGroupBox("模型分配（按任务指定档案）")
        form = QFormLayout(group)

        self.teach_combo = QComboBox()
        self.outline_combo = QComboBox()
        self.vision_combo = QComboBox()
        for combo, label in ((self.teach_combo, "讲课 / 追问 / 笔记"), (self.outline_combo, "生成大纲"), (self.vision_combo, "读图自检")):
            combo.currentIndexChanged.connect(self._save)
            form.addRow(label, combo)

        hint = QLabel("留空（自动）表示跟随「当前激活档案」。")
        hint.setProperty("role", "muted")
        form.addRow("", hint)
        return group

    # --- 工作区与备份 ---------------------------------------------------
    def _build_workspace_group(self) -> QGroupBox:
        group = QGroupBox("工作区与备份")
        layout = QVBoxLayout(group)

        self.workspace_label = QLabel("")
        self.workspace_label.setWordWrap(True)
        self.workspace_label.setProperty("role", "muted")
        layout.addWidget(self.workspace_label)

        row = QHBoxLayout()
        self.backup_button = QPushButton("备份课程库…")
        self.backup_button.clicked.connect(self.backup_requested.emit)
        row.addWidget(self.backup_button)
        self.restore_button = QPushButton("恢复备份…")
        self.restore_button.clicked.connect(self.restore_requested.emit)
        row.addWidget(self.restore_button)
        self.open_workspace_button = QPushButton("打开工作区")
        self.open_workspace_button.clicked.connect(lambda: self.open_folder_requested.emit(str(self._service.workspace_root)))
        row.addWidget(self.open_workspace_button)
        self.restore_seed_button = QPushButton("恢复示例课程")
        self.restore_seed_button.clicked.connect(self.restore_seed_requested.emit)
        self.restore_seed_button.setToolTip("把《ATM 使用指南》恢复为内置版本（旧版移入回收站，可恢复）")
        row.addWidget(self.restore_seed_button)
        self.open_logs_button = QPushButton("打开日志目录")
        self.open_logs_button.clicked.connect(
            lambda: self.open_folder_requested.emit(str(Path(self._service.workspace_root) / "data" / "logs"))
        )
        row.addWidget(self.open_logs_button)
        row.addStretch(1)
        layout.addLayout(row)

        env_row = QHBoxLayout()
        self.import_env_button = QPushButton("从环境变量导入密钥（OPENAI_API_KEY / OPENAI_BASE_URL）")
        self.import_env_button.clicked.connect(self._on_import_env)
        env_row.addWidget(self.import_env_button)
        env_row.addStretch(1)
        layout.addLayout(env_row)
        return group

    # --- 关于与日志 -----------------------------------------------------
    def _build_about_group(self) -> QGroupBox:
        group = QGroupBox("关于与合规")
        layout = QVBoxLayout(group)
        version = QLabel(f"AI Teaches Me（ATM）v{APP_VERSION} · 本地优先的 AI 自学客户端")
        layout.addWidget(version)
        notes = QLabel(COMPLIANCE_NOTES)
        notes.setWordWrap(True)
        notes.setProperty("role", "muted")
        layout.addWidget(notes)
        return group

    # --- 装载 / 保存 ----------------------------------------------------
    def reload(self) -> None:
        config = self._service.config
        self._reload_personas(config.persona.default)

        generation = config.generation
        self.depth_combo.setCurrentIndex(max(0, self.depth_combo.findData(generation.default_depth)))
        for widget, value in (
            (self.auto_svg_check, generation.auto_svg),
            (self.auto_animation_check, generation.auto_animation),
            (self.auto_note_check, generation.auto_note_on_complete),
            (self.quiz_check, generation.quiz_enabled),
            (self.self_assessment_check, generation.self_assessment_enabled),
            (self.review_check, generation.svg_review.enabled),
        ):
            widget.blockSignals(True)
            widget.setChecked(bool(value))
            widget.blockSignals(False)
        self.throttle_spin.setValue(int(generation.stream_throttle_ms))
        self.rounds_spin.setValue(int(generation.svg_review.max_rounds))

        ui = config.ui
        self.font_edit.setText(ui.font_family)
        self.width_spin.setValue(int(ui.reading_width))
        self.bg_type_combo.setCurrentIndex(max(0, self.bg_type_combo.findData(ui.background.type)))
        self.bg_value_edit.setText(ui.background.value or ui.background.path or "")
        self.bg_blur_spin.setValue(float(ui.background.blur))
        self.bg_dim_spin.setValue(float(ui.background.dim))

        self._fill_profile_combos(config)
        self.workspace_label.setText(f"工作区根目录：{self._service.workspace_root}\n课程库：{self._courses_dir}")

    def _reload_personas(self, current: str | None = None) -> None:
        self.persona_combo.blockSignals(True)
        self.persona_combo.clear()
        for persona in self._personas.list_personas():
            self.persona_combo.addItem(f"{persona.avatar} {persona.name}", persona.id)
        wanted = current or self._service.config.persona.default
        index = self.persona_combo.findData(wanted)
        self.persona_combo.setCurrentIndex(index if index >= 0 else 0)
        self.persona_combo.blockSignals(False)
        self._refresh_persona_preview()

    def _refresh_persona_preview(self) -> None:
        persona = self._persona_by_id(str(self.persona_combo.currentData() or ""))
        self.persona_preview.setPlainText(f"{persona.name}\n语气：{'、'.join(persona.tone) or '—'}\n\n{persona.body[:400]}")
        self.allow_roleplay_check.blockSignals(True)
        self.allow_roleplay_check.setChecked(bool(persona.allow_roleplay))
        self.allow_roleplay_check.blockSignals(False)

    def _persona_by_id(self, persona_id: str):
        for persona in self._personas.list_personas():
            if persona.id == persona_id:
                return persona
        return self._personas.get(persona_id)

    def _fill_profile_combos(self, config) -> None:
        names = ["（自动 / 跟随当前档案）"] + config.profile_names()
        for combo, value in (
            (self.teach_combo, config.routing.teach),
            (self.outline_combo, config.routing.outline),
            (self.vision_combo, config.routing.vision_review),
        ):
            combo.blockSignals(True)
            combo.clear()
            for name in names:
                combo.addItem(name, None if name.startswith("（自动") else name)
            index = combo.findData(value)
            combo.setCurrentIndex(index if index >= 0 else 0)
            combo.blockSignals(False)

    @Slot()
    def _save(self) -> None:
        config = self._service.config
        generation = config.generation
        generation.default_depth = str(self.depth_combo.currentData() or "standard")
        generation.auto_svg = self.auto_svg_check.isChecked()
        generation.auto_animation = self.auto_animation_check.isChecked()
        generation.auto_note_on_complete = self.auto_note_check.isChecked()
        generation.quiz_enabled = self.quiz_check.isChecked()
        generation.self_assessment_enabled = self.self_assessment_check.isChecked()
        generation.svg_review.enabled = self.review_check.isChecked()
        generation.svg_review.max_rounds = int(self.rounds_spin.value())
        generation.stream_throttle_ms = int(self.throttle_spin.value())

        ui = config.ui
        ui.font_family = self.font_edit.text().strip() or ui.font_family
        ui.reading_width = int(self.width_spin.value())
        ui.background.type = str(self.bg_type_combo.currentData() or "gradient")
        value = self.bg_value_edit.text().strip()
        if ui.background.type == "image":
            ui.background.path = value or None
            ui.background.value = None
        else:
            ui.background.value = value or ui.background.value
            ui.background.path = None
        ui.background.blur = float(self.bg_blur_spin.value())
        ui.background.dim = float(self.bg_dim_spin.value())

        config.routing.teach = self.teach_combo.currentData()
        config.routing.outline = self.outline_combo.currentData()
        config.routing.vision_review = self.vision_combo.currentData()

        self._service.save()
        self.config_saved.emit()
        self.status_message.emit("偏好已保存")

    # --- 交互 -----------------------------------------------------------
    @Slot()
    def _on_persona_changed(self) -> None:
        persona_id = str(self.persona_combo.currentData() or "")
        if persona_id:
            self._service.config.persona.default = persona_id
            self._service.save()
        self._refresh_persona_preview()
        self.config_saved.emit()

    @Slot()
    def _on_allow_roleplay(self) -> None:
        self._service.config.persona.allow_roleplay = self.allow_roleplay_check.isChecked()
        self._service.save()
        self.status_message.emit("人设角色扮演开关已更新（只影响表达层）")

    @Slot()
    def _on_create_persona(self) -> None:
        """新建人设：弹窗填表 → 写入 personas/<id>.md 并立即选用。"""
        dialog = PersonaEditorDialog(
            existing_ids={persona.id for persona in self._personas.list_personas()}, parent=self
        )
        if not dialog.exec():
            return
        persona: Persona = dialog.created_persona
        try:
            self._personas.create_persona(persona)
        except Exception as exc:  # noqa: BLE001
            self.status_message.emit(f"创建失败：{exc}")
            return
        self._service.config.persona.default = persona.id
        self._service.save()
        self._reload_personas(persona.id)
        self.config_saved.emit()
        self.status_message.emit(f"已创建人设 {persona.name}（{persona.id}）并设为默认")

    @Slot()
    def _on_import_persona(self) -> None:
        source, _ = QFileDialog.getOpenFileName(self, "导入人设", "", "人设文件 (*.md)")
        if not source:
            return
        try:
            persona = self._personas.import_persona(source)
        except Exception as exc:  # noqa: BLE001
            self.status_message.emit(f"导入失败：{exc}")
            return
        self._reload_personas(persona.id)
        self.status_message.emit(f"已导入人设 {persona.name}")

    @Slot()
    def _on_export_persona(self) -> None:
        persona_id = str(self.persona_combo.currentData() or "")
        target, _ = QFileDialog.getSaveFileName(self, "导出人设", f"{persona_id}.md", "人设文件 (*.md)")
        if not target:
            return
        path = self._personas.export_persona(persona_id, target)
        self.status_message.emit(f"已导出到 {path.name}")

    @Slot()
    def _on_trial(self) -> None:
        if self._client is None:
            self.trial_output.setPlainText("没有可用的模型配置，请先在「AI 供应商与密钥」里配置并测试连接。")
            return
        persona = self._persona_by_id(str(self.persona_combo.currentData() or ""))
        self.trial_output.setPlainText("正在试讲…")
        self._pool.start(_TrialTask(self._client, persona.effective_body, self._trial_signals))

    @Slot(str)
    def _on_trial_done(self, text: str) -> None:
        self.trial_output.setPlainText(text.strip() or "（模型没有返回内容）")

    @Slot()
    def _on_import_env(self) -> None:
        api_key = os.environ.get("OPENAI_API_KEY", "").strip()
        base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
        if not api_key and not base_url:
            self.status_message.emit("环境变量里没有 OPENAI_API_KEY / OPENAI_BASE_URL")
            return
        profile = self._service.config.require_profile()
        if base_url:
            profile.base_url = base_url
        if api_key:
            self._service.set_api_key(profile.name, api_key)
        self._service.save()
        self.status_message.emit("已从环境变量导入（密钥只进凭据库，不落盘）")
        self.config_saved.emit()
