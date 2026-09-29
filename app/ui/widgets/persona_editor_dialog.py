"""新建 / 编辑人设对话框（《项目文档》§10.6 人设文件规范、§11.4 ``PersonaEditor``）。

表单化填写 YAML 头部的元信息 + 正文，**实时校验**并给出行号级错误；保存即写入
``personas/<id>.md``（用户人设目录），可立即在设置里选用。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ...agents.persona import Persona, PersonaFormatError, dump_persona, parse_persona

BODY_TEMPLATE = """你是一位……

说话方式：
- 

讲课习惯：
- 

禁止：
- 不要省略知识点的准确解释。
- 不要改变讲解结构与输出格式。
"""


class PersonaEditorDialog(QDialog):
    """创建人设。填写完成后返回 ``Persona``（调用方负责落盘）。"""

    def __init__(self, *, existing_ids: set[str] | None = None, parent=None) -> None:
        super().__init__(parent)
        self.existing_ids = {item.lower() for item in (existing_ids or set())}
        self.setWindowTitle("新建人设")
        self.resize(620, 520)

        layout = QVBoxLayout(self)
        intro = QLabel("人设只影响表达层：语气、称呼、比喻风格。知识点覆盖度、讲解深度与输出协议不可被人设改变。")
        intro.setWordWrap(True)
        intro.setProperty("role", "muted")
        layout.addWidget(intro)

        form = QFormLayout()
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("小写字母、数字、连字符，例如 my-teacher")
        form.addRow("id（唯一）", self.id_edit)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("显示名称，例如 温柔学姐")
        form.addRow("名称", self.name_edit)
        self.avatar_edit = QLineEdit()
        self.avatar_edit.setPlaceholderText("一个表情符号或图片路径，例如 📚")
        form.addRow("头像", self.avatar_edit)
        self.tone_edit = QLineEdit()
        self.tone_edit.setPlaceholderText("用逗号分隔，例如：温柔, 鼓励式, 有条理")
        form.addRow("语气关键词", self.tone_edit)
        self.tools_edit = QLineEdit()
        self.tools_edit.setPlaceholderText("留空 = 允许全部工具；或填 search_materials, gen_svg, make_quiz")
        form.addRow("允许的工具", self.tools_edit)
        self.version_spin = QSpinBox()
        self.version_spin.setRange(1, 999)
        form.addRow("版本", self.version_spin)
        self.roleplay_check = QCheckBox("允许角色扮演（如「学姐」「教练」这类人设）")
        self.roleplay_check.setChecked(True)
        form.addRow("", self.roleplay_check)
        layout.addLayout(form)

        layout.addWidget(QLabel("人设正文（自然语言描述这位老师：说话方式 / 讲课习惯 / 禁止事项）"))
        self.body_edit = QPlainTextEdit(BODY_TEMPLATE)
        layout.addWidget(self.body_edit, 1)

        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setProperty("role", "error")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self.reject)
        buttons.addWidget(self.cancel_button)
        self.save_button = QPushButton("保存人设")
        self.save_button.setProperty("role", "primary")
        self.save_button.clicked.connect(self._on_save)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

        for widget in (self.id_edit, self.name_edit):
            widget.textChanged.connect(self._validate_live)

    # --- 组装与校验 -----------------------------------------------------
    def build_persona(self) -> Persona:
        tone = ",".join(item.strip() for item in self.tone_edit.text().split(",") if item.strip())
        tools = ",".join(item.strip() for item in self.tools_edit.text().split(",") if item.strip())
        header = [
            "---",
            f"id: {self.id_edit.text().strip().lower()}",
            f"name: {self.name_edit.text().strip()}",
            f"avatar: {self.avatar_edit.text().strip()}",
            f"tone: [{tone}]",
            f"allow_roleplay: {'true' if self.roleplay_check.isChecked() else 'false'}",
            f"tools: [{tools}]",
            f"version: {int(self.version_spin.value())}",
            "---",
            "",
            self.body_edit.toPlainText().strip(),
            "",
        ]
        return parse_persona("\n".join(header), source="<新建人设>")

    def _validate_live(self) -> None:
        try:
            persona = self.build_persona()
        except PersonaFormatError as exc:
            self.error_label.setText(str(exc))
            return
        if persona.id in self.existing_ids:
            self.error_label.setText(f"id 已存在：{persona.id}（换一个，或在人设列表里直接编辑）")
            return
        self.error_label.setText("")

    def _on_save(self) -> None:
        try:
            persona = self.build_persona()
        except PersonaFormatError as exc:
            self.error_label.setText(str(exc))
            return
        if persona.id in self.existing_ids:
            self.error_label.setText(f"id 已存在：{persona.id}")
            return
        self.created_persona = persona
        self.accept()
