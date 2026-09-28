"""设计 token（《项目文档》§11.1 视觉设计语言、《技术文档》§13.7 主题系统）。

一套 token 同时驱动 **Qt 原生控件（QSS）** 与 **内嵌 HTML（CSS 变量）**，
两边的变量名由 ``CSS_PREFIX`` + 字段名（下划线转连字符）统一生成，避免样式漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, fields

CSS_PREFIX = "--atm-"

# 明暗两套配色
MODES: tuple[str, ...] = ("light", "dark")


@dataclass(frozen=True)
class ColorTokens:
    """颜色 token：背景 / 卡片 / 分隔线 / 文字四级层次（明暗各一套）。"""

    bg: str
    bg_soft: str
    surface: str
    surface_alt: str
    text: str
    text_dim: str
    text_muted: str
    primary: str
    primary_hover: str
    accent: str
    border: str
    success: str
    warning: str
    danger: str
    code_bg: str

    def as_dict(self) -> dict[str, str]:
        return {item.name: getattr(self, item.name) for item in fields(self)}


@dataclass(frozen=True)
class RadiusTokens:
    """圆角：卡片 16px / 按钮 10px / 小块 8px（§11.1）。"""

    card: int = 16
    button: int = 10
    small: int = 8


@dataclass(frozen=True)
class FontTokens:
    ui_family: str = '"Microsoft YaHei", "微软雅黑", "Segoe UI", sans-serif'
    reading_family: str = '"Source Han Serif SC", "思源宋体", serif'
    code_family: str = '"JetBrains Mono", Consolas, "Courier New", monospace'
    base_size: int = 14
    body_size: int = 16
    line_height: float = 1.75


@dataclass(frozen=True)
class MotionTokens:
    """动效：150–250ms、ease-out（§11.1）。"""

    fast_ms: int = 150
    normal_ms: int = 250
    easing: str = "ease-out"


@dataclass(frozen=True)
class Theme:
    key: str
    name: str
    light: ColorTokens
    dark: ColorTokens
    radius: RadiusTokens = RadiusTokens()
    font: FontTokens = FontTokens()
    motion: MotionTokens = MotionTokens()
    default_mode: str = "light"

    def colors(self, mode: str = "light") -> ColorTokens:
        return self.dark if mode == "dark" else self.light


def css_var_name(token: str) -> str:
    """``code_bg`` → ``--atm-code-bg``。"""
    return CSS_PREFIX + token.replace("_", "-")
