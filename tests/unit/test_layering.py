"""分层红线的静态检查（《技术文档》§1.5 红线 1/2/3、§12 命名与分层约定）。

出现违规导入即失败，避免架构随时间腐化。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
APP_DIR = REPO_ROOT / "app"

LLM_SDKS = ("openai", "anthropic", "google.generativeai", "mistralai", "litellm", "ollama")
QT_PACKAGES = ("PySide6", "PySide2", "PyQt5", "PyQt6")

IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_.]+)", re.MULTILINE)


def _files(subdir: str) -> list[Path]:
    directory = APP_DIR / subdir
    return sorted(directory.rglob("*.py")) if directory.is_dir() else []


def _modules(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return {match.group(1).split(".")[0] for match in IMPORT_RE.finditer(text)}


@pytest.mark.parametrize("path", _files("ui"), ids=lambda item: str(item.relative_to(APP_DIR)))
def test_ui_must_not_import_llm_sdk(path: Path) -> None:
    imported = _modules(path)
    for sdk in LLM_SDKS:
        assert sdk.split(".")[0] not in imported, f"{path} 不得直接调用 LLM SDK（§1.5 红线 1）"


@pytest.mark.parametrize(
    "path",
    _files("services") + _files("agents") + _files("core"),
    ids=lambda item: str(item.relative_to(APP_DIR)),
)
def test_services_agents_and_core_must_not_import_qt(path: Path) -> None:
    imported = _modules(path)
    for package in QT_PACKAGES:
        assert package not in imported, f"{path} 不得导入 Qt（§1.5 红线 2，需可无界面单测）"


@pytest.mark.parametrize(
    "path", _files("infra"), ids=lambda item: str(item.relative_to(APP_DIR))
)
def test_infra_must_not_import_business_layers(path: Path) -> None:
    text = path.read_text(encoding="utf-8", errors="replace")
    for layer in ("app.services", "app.agents", "app.ui", "..services", "..agents", "..ui"):
        assert layer not in text, f"{path} 不得依赖业务层（§1.5 红线 3）"


def test_renderer_assets_are_local() -> None:
    """渲染资源必须全部本地（§1.5 红线 4；详细检查见 test_web_assets.py）。"""
    web_dir = APP_DIR / "web"
    for name in ("lesson.html", "renderer.js", "theme.css"):
        text = (web_dir / name).read_text(encoding="utf-8")
        assert "http://" not in text and "https://" not in text


def test_services_do_not_touch_files_outside_workspace() -> None:
    """服务层不得出现裸 ``Path.home()`` 之类的越界写入入口。"""
    for path in _files("services"):
        text = path.read_text(encoding="utf-8", errors="replace")
        assert "Path.home()" not in text, f"{path} 不应把用户主目录当作数据目录"
