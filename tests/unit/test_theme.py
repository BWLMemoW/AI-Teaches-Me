"""主题系统的单元测试（《项目文档》§11.1、§18.6；《技术文档》§13.7）。"""

from __future__ import annotations

from pathlib import Path

from app.core.config import Background
from app.ui.theme.presets import PRESETS, THEME_KEYS, get_theme, theme_choices
from app.ui.theme.qss import build_qss, css_vars_text, resolve_mode, theme_css, to_css_vars
from app.ui.theme.tokens import MODES, css_var_name

THEME_CSS_PATH = Path(__file__).resolve().parents[2] / "app" / "web" / "theme.css"


def test_presets_match_documented_keys() -> None:
    assert THEME_KEYS == ("deepspace", "paper", "sakura", "forest")
    assert set(PRESETS) == set(THEME_KEYS)
    assert [PRESETS[key].name for key in THEME_KEYS] == ["深空", "纸感", "樱花", "森林"]


def test_sakura_matches_doc_primary_colors() -> None:
    assert PRESETS["sakura"].light.primary == "#E86A92"
    assert PRESETS["sakura"].light.accent == "#6C8AE4"


def test_every_theme_has_both_modes_with_same_fields() -> None:
    for theme in PRESETS.values():
        assert set(theme.light.as_dict()) == set(theme.dark.as_dict())
        assert theme.default_mode in MODES
        assert theme.radius.card == 16
        assert theme.radius.button == 10
        assert theme.radius.small == 8


def test_get_theme_falls_back_to_sakura() -> None:
    assert get_theme("nope").key == "sakura"
    assert get_theme("forest").key == "forest"


def test_theme_choices_order_and_labels() -> None:
    assert theme_choices()[2] == ("sakura", "樱花")
    assert len(theme_choices()) == 4


def test_css_vars_cover_every_color_token() -> None:
    for theme in PRESETS.values():
        for mode in MODES:
            variables = to_css_vars(theme, mode)
            for name, value in theme.colors(mode).as_dict().items():
                assert variables[css_var_name(name)] == value


def test_css_vars_include_layout_and_font_tokens() -> None:
    variables = to_css_vars(PRESETS["paper"], "dark")

    assert variables["--atm-radius-card"] == "16px"
    assert variables["--atm-radius-button"] == "10px"
    assert variables["--atm-radius-small"] == "8px"
    assert variables["--atm-line-height"] == "1.75"
    assert variables["--atm-mode"] == "dark"
    assert variables["--atm-theme"] == "paper"
    assert variables["--atm-font-code"]


def test_background_tokens_by_type() -> None:
    gradient = Background(type="gradient", value="linear-gradient(180deg, #fff, #000)")
    assert to_css_vars(PRESETS["sakura"], "light", gradient)["--atm-bg-image"].startswith(
        "linear-gradient"
    )

    image = Background(type="image", path="assets/bg.png", blur=6, dim=0.25)
    variables = to_css_vars(PRESETS["sakura"], "light", image)
    assert variables["--atm-bg-image"] == 'url("assets/bg.png")'
    assert variables["--atm-bg-blur"] == "6.0"
    assert variables["--atm-bg-dim"] == "0.25"

    color = Background(type="color", value="#101010")
    assert to_css_vars(PRESETS["sakura"], "light", color)["--atm-bg-color"] == "#101010"

    assert to_css_vars(PRESETS["sakura"], "light")["--atm-bg-image"] == "none"


def test_build_qss_uses_token_values() -> None:
    qss = build_qss(PRESETS["sakura"], "light")

    assert "#E86A92" in qss
    assert "border-radius: 16px" in qss
    assert "--atm" not in qss


def test_build_qss_differs_between_themes_and_modes() -> None:
    sakura_light = build_qss(PRESETS["sakura"], "light")

    assert sakura_light != build_qss(PRESETS["sakura"], "dark")
    assert sakura_light != build_qss(PRESETS["forest"], "light")


def test_resolve_mode_follows_system_setting() -> None:
    sakura = PRESETS["sakura"]

    assert resolve_mode(sakura, follow_system=False, system_dark=True) == "light"
    assert resolve_mode(sakura, follow_system=True, system_dark=True) == "dark"
    assert resolve_mode(sakura, follow_system=True, system_dark=False) == "light"
    assert resolve_mode(sakura, follow_system=True, system_dark=None) == sakura.default_mode
    assert resolve_mode(PRESETS["deepspace"], follow_system=True, system_dark=None) == "dark"


def test_theme_css_file_is_generated_from_tokens() -> None:
    text = THEME_CSS_PATH.read_text(encoding="utf-8")

    assert text.startswith("/* 由 app/ui/theme/qss.py 生成")
    assert css_vars_text(PRESETS["sakura"], "light") in text
    for key in THEME_KEYS:
        theme = PRESETS[key]
        for mode in MODES:
            selector = f'[data-theme="{key}"][data-mode="{mode}"]'
            assert css_vars_text(theme, mode, selector=selector) in text


def test_theme_css_helper_matches_file() -> None:
    text = THEME_CSS_PATH.read_text(encoding="utf-8")
    assert len(theme_css()) == len(text)
