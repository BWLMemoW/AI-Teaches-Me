"""内置主题（《项目文档》§11.1 / §18.6 的 ``ui.theme`` 取值）。

四套主题：``deepspace`` 深空 / ``paper`` 纸感 / ``sakura`` 樱花 / ``forest`` 森林，
每套都提供明暗两版配色，配合 ``ui.follow_system`` 使用。
"""

from __future__ import annotations

from .tokens import ColorTokens, Theme

SAKURA = Theme(
    key="sakura",
    name="樱花",
    default_mode="light",
    light=ColorTokens(
        bg="#FDF7F9", bg_soft="#FBEEF3", surface="#FFFFFF", surface_alt="#F7F2F4",
        text="#2B2430", text_dim="#5C5566", text_muted="#8A8394",
        primary="#E86A92", primary_hover="#D2557E", accent="#6C8AE4",
        border="#EADCE3", success="#3FA46A", warning="#D9922B", danger="#D9534F",
        code_bg="#F6F1F4",
    ),
    dark=ColorTokens(
        bg="#1B1720", bg_soft="#221D29", surface="#26212E", surface_alt="#2E2837",
        text="#F2EDF6", text_dim="#CFC7D8", text_muted="#9A93A6",
        primary="#F08BAE", primary_hover="#F5A3C0", accent="#8FA8F0",
        border="#3A3244", success="#62C08A", warning="#E5B25C", danger="#F0807C",
        code_bg="#211C28",
    ),
)

PAPER = Theme(
    key="paper",
    name="纸感",
    default_mode="light",
    light=ColorTokens(
        bg="#FBF7F0", bg_soft="#F4EEE3", surface="#FFFFFF", surface_alt="#F7F2E8",
        text="#2E2A24", text_dim="#5F584E", text_muted="#8C8478",
        primary="#B4763B", primary_hover="#9C6431", accent="#4F7A6F",
        border="#E6DCCB", success="#4C8C5A", warning="#C98A22", danger="#C0554E",
        code_bg="#F4EFE6",
    ),
    dark=ColorTokens(
        bg="#201D19", bg_soft="#262320", surface="#2B2724", surface_alt="#332E2A",
        text="#F4EFE7", text_dim="#CFC7BB", text_muted="#9C9488",
        primary="#D79A5B", primary_hover="#E2AC72", accent="#7FB3A5",
        border="#3E3833", success="#6FAF78", warning="#D8A85A", danger="#E07B74",
        code_bg="#262220",
    ),
)

DEEPSPACE = Theme(
    key="deepspace",
    name="深空",
    default_mode="dark",
    light=ColorTokens(
        bg="#F4F6FB", bg_soft="#E9EDF7", surface="#FFFFFF", surface_alt="#EEF1F9",
        text="#1B2033", text_dim="#4C5570", text_muted="#7B8499",
        primary="#4C6BE0", primary_hover="#3A57C9", accent="#8A6BE0",
        border="#DDE3F1", success="#3E9E6A", warning="#C98A22", danger="#CE5A57",
        code_bg="#EEF1F9",
    ),
    dark=ColorTokens(
        bg="#0E1120", bg_soft="#141830", surface="#171B2C", surface_alt="#1F2439",
        text="#E8ECFB", text_dim="#BFC7E4", text_muted="#8891AF",
        primary="#7E97FF", primary_hover="#97ABFF", accent="#B49BFF",
        border="#2A3149", success="#5FC48A", warning="#E0B159", danger="#F0807C",
        code_bg="#141830",
    ),
)

FOREST = Theme(
    key="forest",
    name="森林",
    default_mode="light",
    light=ColorTokens(
        bg="#F5F8F3", bg_soft="#EBF1E7", surface="#FFFFFF", surface_alt="#EFF4EC",
        text="#21291F", text_dim="#4E5A4A", text_muted="#7C877A",
        primary="#3F7D4E", primary_hover="#336842", accent="#A8792E",
        border="#DCE6D6", success="#3F7D4E", warning="#B98A25", danger="#BF5A50",
        code_bg="#EEF3EA",
    ),
    dark=ColorTokens(
        bg="#131A15", bg_soft="#18211B", surface="#1C2620", surface_alt="#243027",
        text="#E6F0E7", text_dim="#B9C9BB", text_muted="#86988A",
        primary="#6FBE84", primary_hover="#86CE99", accent="#D3A85E",
        border="#2C3A31", success="#6FBE84", warning="#D9B161", danger="#E08679",
        code_bg="#18211B",
    ),
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
