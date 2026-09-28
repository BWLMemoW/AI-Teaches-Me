"""token → QSS / CSS 变量（《技术文档》§13.7、《项目文档》§11.1）。

一套 token 同时产出：

* ``build_qss``：Qt 原生控件样式表（`.setStyleSheet(...)`）；
* ``to_css_vars`` / ``css_vars_text``：内嵌页面（``app/web``）使用的 CSS 变量，
  与 ``app/web/theme.css`` 同名同义；
* ``apply_theme``：两者一起应用，并可通过事件总线广播 ``theme:changed``（§4.8）。
"""

from __future__ import annotations

from typing import Any

from ...core.config import Background
from .presets import PRESETS, SAKURA, THEME_KEYS, get_theme
from .tokens import MODES, Theme, css_var_name


def resolve_mode(theme: Theme, *, follow_system: bool, system_dark: bool | None = None) -> str:
    """决定实际使用明色还是暗色（``ui.follow_system``）。"""
    if not follow_system or system_dark is None:
        return theme.default_mode
    return "dark" if system_dark else "light"


def _background_tokens(background: Background | None) -> dict[str, str]:
    tokens: dict[str, str] = {"bg-image": "none", "bg-blur": "0", "bg-dim": "0"}
    if background is None:
        return tokens
    tokens["bg-blur"] = str(background.blur)
    tokens["bg-dim"] = str(background.dim)
    if background.type == "image" and background.path:
        tokens["bg-image"] = f'url("{background.path}")'
    elif background.type == "gradient" and background.value:
        tokens["bg-image"] = background.value
    elif background.type == "color" and background.value:
        tokens["bg-color"] = background.value
    return tokens


def to_css_vars(
    theme: Theme,
    mode: str = "light",
    background: Background | None = None,
) -> dict[str, str]:
    """把主题 + 背景解析成 CSS 变量字典（键名带 ``--atm-`` 前缀）。"""
    colors = theme.colors(mode)
    variables: dict[str, str] = {
        css_var_name(name): value for name, value in colors.as_dict().items()
    }
    variables[css_var_name("radius_card")] = f"{theme.radius.card}px"
    variables[css_var_name("radius_button")] = f"{theme.radius.button}px"
    variables[css_var_name("radius_small")] = f"{theme.radius.small}px"
    variables[css_var_name("font_ui")] = theme.font.ui_family
    variables[css_var_name("font_reading")] = theme.font.reading_family
    variables[css_var_name("font_code")] = theme.font.code_family
    variables[css_var_name("size_base")] = f"{theme.font.base_size}px"
    variables[css_var_name("size_body")] = f"{theme.font.body_size}px"
    variables[css_var_name("line_height")] = str(theme.font.line_height)
    variables[css_var_name("motion_fast")] = f"{theme.motion.fast_ms}ms"
    variables[css_var_name("motion_normal")] = f"{theme.motion.normal_ms}ms"
    variables[css_var_name("theme")] = theme.key
    variables[css_var_name("mode")] = mode
    for name, value in _background_tokens(background).items():
        variables[css_var_name(name)] = value
    return variables


def css_vars_text(
    theme: Theme,
    mode: str = "light",
    background: Background | None = None,
    *,
    selector: str = ":root",
) -> str:
    """生成一段 CSS（用于 ``app/web/theme.css`` 与页面内注入）。"""
    variables = to_css_vars(theme, mode, background)
    body = "".join(f"  {name}: {value};\n" for name, value in sorted(variables.items()))
    return f"{selector} {{\n{body}}}\n"


def theme_css(background: Background | None = None) -> str:
    """生成完整的 ``app/web/theme.css``（四套主题 × 明暗两态 + 默认变量）。"""
    header = (
        "/* 由 app/ui/theme/qss.py 生成：与 Qt 侧 token 同源，禁止手工改数值。 */\n"
        "/* 对应《技术文档》§13.7 与《项目文档》§11.1。 */\n\n"
    )
    blocks: list[str] = [
        "/* 默认（樱花 · 明色）：未设置 data-theme 时的兜底变量 */\n",
        css_vars_text(SAKURA, "light", background),
        "\n",
    ]
    for key in THEME_KEYS:
        theme = PRESETS[key]
        for mode in MODES:
            label = "明色" if mode == "light" else "暗色"
            selector = f'[data-theme="{key}"][data-mode="{mode}"]'
            blocks.append(f"/* {theme.name} · {label} */\n")
            blocks.append(css_vars_text(theme, mode, background, selector=selector))
            blocks.append("\n")
    return header + "".join(blocks)


def build_qss(
    theme: Theme,
    mode: str = "light",
    *,
    font_size: int | None = None,
    reading_width: int | None = None,
) -> str:
    """把 token 渲染成 Qt 样式表。"""
    c = theme.colors(mode)
    base = font_size or theme.font.base_size
    small = max(11, base - 2)
    title = base + 6
    width = reading_width or 820
    return f"""
/* AI Teaches Me · {theme.name}（{mode}）—— 生成的 QSS，请勿手工改数值 */
QWidget {{
  background-color: {c.bg};
  color: {c.text};
  font-family: {theme.font.ui_family};
  font-size: {base}px;
}}
QMainWindow, QDialog {{ background-color: {c.bg}; }}
QLabel[role="title"] {{ font-size: {title}px; font-weight: 600; }}
QLabel[role="subtitle"] {{ color: {c.text_dim}; }}
QLabel[role="muted"] {{ color: {c.text_muted}; font-size: {small}px; }}
QLabel[role="error"] {{ color: {c.danger}; }}
QFrame[role="card"] {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: {theme.radius.card}px;
}}
QFrame[role="separator"] {{ background-color: {c.border}; max-height: 1px; }}
QPushButton {{
  background-color: {c.surface_alt};
  color: {c.text};
  border: 1px solid {c.border};
  border-radius: {theme.radius.button}px;
  padding: 6px 14px;
}}
QPushButton:hover {{ background-color: {c.bg_soft}; border-color: {c.primary}; }}
QPushButton:disabled {{ color: {c.text_muted}; border-color: {c.border}; }}
QPushButton[role="primary"] {{
  background-color: {c.primary};
  border-color: {c.primary};
  color: #FFFFFF;
}}
QPushButton[role="primary"]:hover {{ background-color: {c.primary_hover}; }}
QPushButton[role="danger"] {{ color: {c.danger}; border-color: {c.danger}; }}
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: {theme.radius.small}px;
  padding: 6px 8px;
  selection-background-color: {c.primary};
  selection-color: #FFFFFF;
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus {{
  border-color: {c.primary};
}}
QListWidget, QTreeWidget, QTreeView, QListView {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: {theme.radius.small}px;
}}
QListWidget::item, QTreeWidget::item {{ padding: 4px 6px; }}
QListWidget::item:selected, QTreeWidget::item:selected, QListView::item:selected {{
  background-color: {c.bg_soft};
  color: {c.text};
}}
QTabBar::tab {{ background: transparent; color: {c.text_dim}; padding: 6px 12px; }}
QTabBar::tab:selected {{ color: {c.primary}; border-bottom: 2px solid {c.primary}; }}
QStatusBar {{ color: {c.text_muted}; font-size: {small}px; }}
QToolTip {{ background-color: {c.surface}; color: {c.text}; border: 1px solid {c.border}; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{ background: {c.border}; border-radius: 5px; min-height: 24px; }}
QScrollBar::handle:vertical:hover {{ background: {c.primary}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QProgressBar {{
  border: 1px solid {c.border};
  border-radius: {theme.radius.small}px;
  text-align: center;
  background-color: {c.surface};
}}
QProgressBar::chunk {{ background-color: {c.primary}; border-radius: {theme.radius.small}px; }}
QCheckBox, QRadioButton {{ spacing: 6px; }}
QSplitter::handle {{ background-color: {c.border}; }}
/* 阅读区宽度（§11.1：正文阅读宽度可调，默认 {width}px） */
QTextBrowser[role="reading"], QWidget[role="reading"] {{ max-width: {width}px; }}
"""


def apply_theme(
    app: Any,
    theme: Theme,
    background: Background | None = None,
    *,
    mode: str = "light",
    bus: Any = None,
    font_size: int | None = None,
    reading_width: int | None = None,
) -> dict[str, str]:
    """应用主题：Qt 原生控件 + 内嵌页面变量，并广播 ``theme:changed``（§13.7）。"""
    app.setStyleSheet(build_qss(theme, mode, font_size=font_size, reading_width=reading_width))
    variables = to_css_vars(theme, mode, background)
    if bus is not None:
        bus.emit("theme:changed", variables)
    return variables
