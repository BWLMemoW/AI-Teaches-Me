"""内置主题（《项目文档》§11.1 / §18.6 的 ``ui.theme`` 取值）。

**v2 调色板（M12 UI 改版）**：
* 底色/卡片/分隔线一律用**中性灰白**，主题色只保留在 ``primary`` / ``accent`` 上
  —— 修掉「整屏都是淡绿/淡粉、层次糊在一起」的观感问题；
* 文字分四级灰阶（text / text_dim / text_muted / border），保证层级；
* §11.1 规定的樱花主色 ``#E86A92`` 与辅色 ``#6C8AE4`` 保持不变。
"""

from __future__ import annotations

from .tokens import ColorTokens, Theme

# --- 中性色基座（明暗各一套，四个主题共享，只有强调色不同） ---
NEUTRAL_LIGHT = dict(
    bg="#F7F7F9", bg_soft="#F1F1F4", surface="#FFFFFF", surface_alt="#F4F4F7",
    text="#1F2328", text_dim="#57606A", text_muted="#8B949E", border="#E4E4E8",
    code_bg="#E8EDF5",
)
NEUTRAL_DARK = dict(
    bg="#15171C", bg_soft="#1B1E24", surface="#20242B", surface_alt="#262B33",
    text="#E6E8EC", text_dim="#B6BCC6", text_muted="#8B93A1", border="#2E343D",
    code_bg="#0B0E13",
)


def _tokens(base: dict, *, primary: str, primary_hover: str, accent: str,
            success: str, warning: str, danger: str) -> ColorTokens:
    return ColorTokens(**base, primary=primary, primary_hover=primary_hover, accent=accent,
                       success=success, warning=warning, danger=danger)


SAKURA = Theme(
    key="sakura",
    name="樱花",
    default_mode="light",
    light=_tokens(NEUTRAL_LIGHT, primary="#E86A92", primary_hover="#D2557E", accent="#6C8AE4",
                  success="#2F9E63", warning="#D68A20", danger="#D14343"),
    dark=_tokens(NEUTRAL_DARK, primary="#F08BAE", primary_hover="#F5A3C0", accent="#8FA8F0",
                 success="#4CBF86", warning="#E0B159", danger="#F0736F"),
)

PAPER = Theme(
    key="paper",
    name="纸感",
    default_mode="light",
    light=_tokens(NEUTRAL_LIGHT, primary="#B4763B", primary_hover="#9C6431", accent="#4F7A6F",
                  success="#2F9E63", warning="#C98A22", danger="#C0554E"),
    dark=_tokens(NEUTRAL_DARK, primary="#D79A5B", primary_hover="#E2AC72", accent="#7FB3A5",
                 success="#4CBF86", warning="#D8A85A", danger="#E07B74"),
)

DEEPSPACE = Theme(
    key="deepspace",
    name="深空",
    default_mode="dark",
    light=_tokens(NEUTRAL_LIGHT, primary="#4C6BE0", primary_hover="#3A57C9", accent="#8A6BE0",
                  success="#2F9E63", warning="#C98A22", danger="#CE5A57"),
    dark=_tokens(NEUTRAL_DARK, primary="#7E97FF", primary_hover="#97ABFF", accent="#B49BFF",
                 success="#5FC48A", warning="#E0B159", danger="#F0807C"),
)

FOREST = Theme(
    key="forest",
    name="森林",
    default_mode="light",
    light=_tokens(NEUTRAL_LIGHT, primary="#3F7D4E", primary_hover="#336842", accent="#A8792E",
                  success="#2F9E63", warning="#B98A25", danger="#BF5A50"),
    dark=_tokens(NEUTRAL_DARK, primary="#6FBE84", primary_hover="#86CE99", accent="#D3A85E",
                 success="#6FBE84", warning="#D9B161", danger="#E08679"),
)

PRESETS: dict[str, Theme] = {
    SAKURA.key: SAKURA,
    PAPER.key: PAPER,
    DEEPSPACE.key: DEEPSPACE,
    FOREST.key: FOREST,
}

THEME_KEYS: tuple[str, ...] = ("deepspace", "paper", "sakura", "forest")


def get_theme(key: str) -> Theme:
    """按 key 取主题；未知 key 回落到默认「樱花」。"""
    return PRESETS.get(key, SAKURA)


def theme_choices() -> list[tuple[str, str]]:
    """（key, 中文名）列表，供设置页下拉框使用。"""
    return [(key, PRESETS[key].name) for key in THEME_KEYS]
