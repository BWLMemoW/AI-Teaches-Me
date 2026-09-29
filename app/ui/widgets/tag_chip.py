"""彩色胶囊标签（《项目文档》§11.4 ``TagChip``）。

同一个标签名永远得到同一种颜色（按名字做稳定散列），明暗主题下都保证可读：
浅底 + 深字。标签是**平铺**结构（§18.6：不支持父子层级）。
"""

from __future__ import annotations

import hashlib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

# (底色, 字色)：挑的都是中等饱和度的浅底 + 深字，保证 ≥4.5:1 对比度
CHIP_COLORS: tuple[tuple[str, str], ...] = (
    ("#FCE7F0", "#A83259"),  # 樱花粉
    ("#E8EEFC", "#33559B"),  # 蓝
    ("#E4F5EA", "#2C6B45"),  # 绿
    ("#FDF1DC", "#8A5A12"),  # 琥珀
    ("#F0E9FC", "#5B3FA0"),  # 紫
    ("#E2F4F5", "#1F6A70"),  # 青
    ("#FBE7E4", "#9B3A2E"),  # 砖红
    ("#EEF0F4", "#4A5262"),  # 中性灰
)


def color_for(name: str) -> tuple[str, str]:
    """按标签名取（底色, 字色），同名稳定。"""
    digest = hashlib.sha1(str(name or "").encode("utf-8")).hexdigest()
    return CHIP_COLORS[int(digest[:4], 16) % len(CHIP_COLORS)]


def chip_style(name: str) -> str:
    bg, fg = color_for(name)
    return (
        f"background-color: {bg}; color: {fg};"
        " border-radius: 9px; padding: 1px 8px; font-size: 11px;"
    )


class TagChip(QLabel):
    """一个标签胶囊。"""

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(f" {name} ", parent)
        self.tag_name = str(name)
        self.setStyleSheet(chip_style(name))
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setToolTip(f"标签：{name}")


def build_tag_row(names: list[str] | tuple[str, ...], *, max_items: int = 4) -> list[TagChip]:
    """生成一排标签胶囊（超出上限时用「+N」表示）。"""
    chips = [TagChip(str(name)) for name in list(names)[:max_items]]
    hidden = max(0, len(names) - max_items)
    if hidden:
        extra = TagChip(f"+{hidden}")
        extra.setStyleSheet(
            "background-color: #EEF0F4; color: #4A5262; border-radius: 9px;"
            " padding: 1px 8px; font-size: 11px;"
        )
        chips.append(extra)
    return chips
