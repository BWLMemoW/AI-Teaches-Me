"""token → QSS / CSS 变量（《技术文档》§13.7、《项目文档》§11.1）。

一套 token 同时产出：

* ``build_qss``：Qt 原生控件样式表（`.setStyleSheet(...)`）；
* ``to_css_vars`` / ``css_vars_text``：内嵌页面（``app/web``）使用的 CSS 变量，
  与 ``app/web/theme.css`` 同名同义；
* ``apply_theme``：两者一起应用，并可通过事件总线广播 ``theme:changed``（§4.8）。
"""

from __future__ import annotations

from pathlib import Path
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


def _rgba(hex_color: str, alpha: float) -> str:
    """``#RRGGBB`` → ``rgba(r, g, b, a)``（QSS 不做透明度混合，只能自己算）。"""
    text = str(hex_color).lstrip("#")
    if len(text) != 6:
        return hex_color
    r, g, b = (int(text[index : index + 2], 16) for index in (0, 2, 4))
    return f"rgba({r}, {g}, {b}, {alpha:.2f})"


def build_qss(
    theme: Theme,
    mode: str = "light",
    *,
    font_size: int | None = None,
    reading_width: int | None = None,
) -> str:
    """把 token 渲染成 Qt 样式表（v2：中性底 + 单一强调色 + 统一间距与控件高度）。"""
    c = theme.colors(mode)
    base = font_size or theme.font.base_size
    small = max(11, base - 1)
    title = base + 7
    width = reading_width or 820
    tint = lambda alpha: _rgba(c.primary, alpha)  # noqa: E731 - 局部小助手
    check_icon = (Path(__file__).resolve().parents[1] / "assets" / "icons" / "check.svg").as_posix()
    return f"""
/* AI Teaches Me · {theme.name}（{mode}）· 生成的 QSS v2，请勿手工改数值 */
QWidget {{
  color: {c.text};
  font-family: {theme.font.ui_family};
  font-size: {base}px;
}}
QMainWindow, QDialog, QStackedWidget, QScrollArea, QAbstractScrollArea, QSplitter {{
  background-color: {c.bg};
}}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
/* 文字控件一律透明：否则全局底色会在每行文字后刷出一条底纹 */
QLabel {{ background-color: transparent; }}
QCheckBox, QRadioButton {{ background-color: transparent; }}
QLabel[role="title"] {{ font-size: {title}px; font-weight: 600; }}
QLabel[role="subtitle"] {{ color: {c.text_dim}; font-weight: 600; }}
QLabel[role="muted"] {{ color: {c.text_muted}; font-size: {small}px; }}
QLabel[role="error"] {{ color: {c.danger}; }}

/* 分组：不用描边方框，只用「标题 + 细分隔线」表达层级 */
QGroupBox {{
  border: none;
  border-top: 1px solid {c.border};
  margin-top: 20px;
  padding-top: 10px;
  font-weight: 600;
  color: {c.text_dim};
}}
QGroupBox::title {{
  subcontrol-origin: margin;
  subcontrol-position: top left;
  left: 0px;
  padding: 0 4px 0 0;
}}
QFrame[role="card"] {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: {theme.radius.card}px;
}}
QFrame[role="separator"] {{ background-color: {c.border}; max-height: 1px; }}

/* 按钮：默认（描边）/ 主按钮（实心）/ 幽灵 / 危险 */
QPushButton {{
  background-color: {c.surface};
  color: {c.text};
  border: 1px solid {c.border};
  border-radius: {theme.radius.button}px;
  padding: 5px 14px;
  min-height: 22px;
}}
QPushButton:hover {{ background-color: {c.surface_alt}; border-color: {c.primary}; }}
QPushButton:pressed {{ background-color: {c.bg_soft}; }}
QPushButton:disabled {{ color: {c.text_muted}; border-color: {c.border}; background-color: {c.surface_alt}; }}
QPushButton[role="primary"] {{ background-color: {c.primary}; border-color: {c.primary}; color: #FFFFFF; font-weight: 600; }}
QPushButton[role="primary"]:hover {{ background-color: {c.primary_hover}; }}
QPushButton[role="ghost"] {{ background-color: transparent; border-color: transparent; color: {c.text_dim}; }}
QPushButton[role="ghost"]:hover {{ background-color: {c.surface_alt}; color: {c.text}; }}
QPushButton[role="danger"] {{ color: {c.danger}; border-color: {_rgba(c.danger, 0.45)}; background-color: transparent; }}
QPushButton[role="danger"]:hover {{ background-color: {_rgba(c.danger, 0.10)}; }}

/* 输入控件：统一高度与焦点环 */
QLineEdit, QPlainTextEdit, QTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: {theme.radius.small}px;
  padding: 5px 8px;
  min-height: 22px;
  selection-background-color: {tint(0.25)};
  selection-color: {c.text};
}}
QLineEdit:focus, QPlainTextEdit:focus, QTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
  border-color: {c.primary};
}}
QLineEdit:disabled, QPlainTextEdit:disabled, QComboBox:disabled {{ background-color: {c.surface_alt}; color: {c.text_muted}; }}

/* 复选框 / 单选框：必须显式画指示器，否则在自定义 QSS 下会变成看不清的空框 */
QCheckBox::indicator, QRadioButton::indicator {{
  width: 15px; height: 15px;
  border: 1px solid {c.border};
  border-radius: 4px;
  background-color: {c.surface};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c.primary}; }}
QCheckBox::indicator:checked {{
  background-color: {c.primary};
  border-color: {c.primary};
  image: url({check_icon});
}}
QRadioButton::indicator:checked {{ background-color: {c.primary}; border: 4px solid {c.surface}; outline: 1px solid {c.primary}; }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ background-color: {c.surface_alt}; border-color: {c.border}; }}
QComboBox::drop-down {{ border: none; width: 22px; }}
QComboBox QAbstractItemView {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: 8px;
  selection-background-color: {tint(0.16)};
  selection-color: {c.text};
  padding: 4px;
}}

/* 左侧导航 */
QListWidget#NavList {{
  background-color: {c.surface_alt};
  border: none;
  border-radius: 12px;
  padding: 8px;
  outline: none;
}}
QListWidget#NavList::item {{
  height: 34px;
  border-radius: 8px;
  padding-left: 12px;
  color: {c.text_dim};
}}
QListWidget#NavList::item:hover {{ background-color: {tint(0.08)}; color: {c.text}; }}
QListWidget#NavList::item:selected {{ background-color: {tint(0.14)}; color: {c.primary}; font-weight: 600; }}

/* 追问抽屉：气泡式对话（M23）—— 用户在右（主色浅底），老师在左（面板色底） */
QScrollArea#ChatHistory {{
  background-color: {c.bg_soft};
  border: 1px solid {c.border};
  border-radius: 12px;
}}
QWidget#ChatHistoryHost {{ background: transparent; }}
QFrame#ChatBubble {{ border-radius: 14px; padding: 0px; }}
QFrame#ChatBubble[role="assistant"] {{
  background-color: {c.surface};
  border: 1px solid {c.border};
}}
QFrame#ChatBubble[role="user"] {{
  background-color: {tint(0.14)};
  border: 1px solid {tint(0.26)};
}}
QLabel#ChatBubbleText {{ background: transparent; }}

/* 列表 / 树 */
QListWidget, QTreeWidget, QTreeView, QListView {{
  background-color: {c.surface};
  border: 1px solid {c.border};
  border-radius: 10px;
  padding: 4px;
  outline: none;
}}
QTreeWidget::item, QListView::item {{ height: 26px; border-radius: 6px; }}
QListWidget::item:selected, QTreeWidget::item:selected, QListView::item:selected {{
  background-color: {tint(0.12)};
  color: {c.text};
}}

/* 标签页与菜单 */
QTabWidget::pane {{ border: 1px solid {c.border}; border-radius: 12px; top: 6px; }}
QTabBar::tab {{ padding: 7px 16px; margin-right: 4px; border-radius: 8px; color: {c.text_dim}; }}
QTabBar::tab:hover {{ background-color: {c.surface_alt}; color: {c.text}; }}
QTabBar::tab:selected {{ background-color: {tint(0.14)}; color: {c.primary}; font-weight: 600; }}
QMenu {{ background-color: {c.surface}; border: 1px solid {c.border}; border-radius: 10px; padding: 6px; }}
QMenu::item {{ padding: 6px 18px; border-radius: 6px; }}
QMenu::item:selected {{ background-color: {tint(0.14)}; color: {c.text}; }}

/* 进度与滚动条 */
QProgressBar {{ background-color: {c.bg_soft}; border: none; border-radius: 3px; text-align: center; }}
QProgressBar::chunk {{ background-color: {c.primary}; border-radius: 3px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {_rgba(c.text_muted, 0.45)}; border-radius: 5px; min-height: 28px; }}
QScrollBar::handle:vertical:hover {{ background: {_rgba(c.text_muted, 0.70)}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {_rgba(c.text_muted, 0.45)}; border-radius: 5px; min-width: 28px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

QStatusBar {{ background-color: {c.surface}; color: {c.text_muted}; border-top: 1px solid {c.border}; font-size: {small}px; }}
QSplitter::handle {{ background-color: {c.border}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QToolTip {{ background-color: {c.surface}; color: {c.text}; border: 1px solid {c.border}; padding: 4px 6px; }}
QCheckBox, QRadioButton {{ spacing: 7px; }}

/* 图标选择网格：选中态用主色描边 + 主色浅底 */
QToolButton {{
  border: 1px solid transparent;
  border-radius: 10px;
  padding: 6px 4px;
  color: {c.text_dim};
  background-color: transparent;
  font-size: {small}px;
}}
QToolButton:hover {{ background-color: {c.surface_alt}; border-color: {c.border}; color: {c.text}; }}
QToolButton:checked {{ background-color: {tint(0.12)}; border-color: {c.primary}; color: {c.primary}; font-weight: 600; }}
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
