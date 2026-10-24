"""M7 收尾测试：工具接入 / 人设白名单 / 联网接线 / 迁移与恢复 / 渲染虚拟化。"""

from __future__ import annotations

import asyncio
import json
import zipfile
from pathlib import Path

import pytest

from app.agents.illustrator import SvgSpec
from app.agents.tools import build_registry
from app.bootstrap import Services, bootstrap_workspace, build_services
from app.core.migrations import check_course, migrate_course
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.restore_service import restore_from_zip

GOOD_SVG = (
    '<svg viewBox="0 0 100 60" xmlns="http://www.w3.org/2000/svg">'
    '<title>图</title><desc>说明</desc><text x="5" y="20">矩阵</text></svg>'
)


class _FakeClient:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def stream_chat(self, messages, **kwargs):
        text = self._responses.pop(0) if self._responses else ""
        for index in range(0, len(text), 53):
            yield text[index : index + 53]


class _FakeIllustrator:
    def __init__(self) -> None:
        self.saved: list[str] = []

    async def make_svg(self, spec: SvgSpec):
        from app.agents.illustrator import SvgResult

        return SvgResult(ok=True, svg=GOOD_SVG, png=b"PNG", rounds=1)

    def save_asset(self, lesson_dir, spec: SvgSpec, result):
        self.saved.append(str(lesson_dir))
        return {"svg": "diagram-1.svg", "png": "diagram-1.png"}


def test_registry_exposes_full_tool_table() -> None:
    names = set(build_registry().names())

    assert {
        "search_materials", "read_material_slice", "read_outline", "get_lesson_kps", "read_image",
        "gen_svg", "render_svg", "review_svg_image", "refine_svg", "gen_animation",
        "cite_from_lesson", "get_progress", "save_note", "make_notes", "make_quiz", "web_search",
        # M32：确定性视觉工具（函数绘图 + 三维简单建模）
        "plot_function", "model_3d",
    } <= names
    assert len(names) == 18


def test_persona_whitelist_limits_tool_schemas() -> None:
    registry = build_registry()

    schemas = registry.schemas(["read_outline", "get_lesson_kps"])

    assert [item["function"]["name"] for item in schemas] == ["get_lesson_kps", "read_outline"]


def test_gen_svg_tool_uses_illustrator_and_can_fall_back() -> None:
    illustrator = _FakeIllustrator()
    registry = build_registry(illustrator=illustrator, lesson_dir=Path("."))
    executor = __import__("app.infra.tool_executor", fromlist=["ToolExecutor"]).ToolExecutor(registry)

    ok = asyncio.run(executor.run({"name": "gen_svg", "arguments": json.dumps({"spec": "矩阵乘法"})}))

    assert ok["ok"] is True
    assert ok["result"]["ok"] is True
    assert illustrator.saved

    empty = asyncio.run(executor.run({"name": "gen_svg", "arguments": json.dumps({"spec": "x"})}))
    assert empty["ok"] is True  # 同一上下文里仍可用


def test_build_services_wires_search_and_personas(tmp_path: Path) -> None:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    services = build_services(result, store=SecretStore(backend=InMemoryBackend()))

    assert services.search is not None
    assert services.search.enabled is False  # 默认关闭（AC-67）
    assert services.personas is not None
    assert len(services.personas.list_personas()) >= 6
    services.index.close()


def test_migrate_course_backs_up_and_bumps_version(tmp_path: Path) -> None:
    course = tmp_path / "Courses" / "旧课"
    course.mkdir(parents=True)
    legacy = course / "course.json"
    legacy.write_text(json.dumps({"name": "旧课"}), encoding="utf-8")

    notes = migrate_course(course)

    assert notes and "v0 → v1" in notes[0]
    assert (course / "course.json.bak").exists()
    migrated = json.loads(legacy.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == 1

    future = course / "outline.json"
    future.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    assert check_course(course)


def test_restore_from_zip_blocks_path_traversal(tmp_path: Path) -> None:
    archive_path = tmp_path / "backup.zip"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("../evil.txt", "bad")
        archive.writestr("课程/course.json", "{}")

    restored = restore_from_zip(archive_path, tmp_path / "target")

    root = (tmp_path / "target").resolve()
    assert restored
    assert all(str(path.resolve()).startswith(str(root)) for path in restored)
    assert not (tmp_path / "evil.txt").exists()


def test_renderer_and_page_declare_virtualization_and_lightbox() -> None:
    html = Path("app/web/lesson.html").read_text(encoding="utf-8")
    js = Path("app/web/renderer.js").read_text(encoding="utf-8")

    assert "content-visibility: auto" in html
    assert "atm-lightbox" in html and "atm-lightbox" in js
    assert "deferMount" in js and "IntersectionObserver" in js
    assert "[data-mode=\"dark\"] .atm-image" in html


@pytest.mark.ui
def test_first_run_dialog_emits_actions(qtbot) -> None:
    from app.ui.widgets.first_run_dialog import FirstRunDialog

    dialog = FirstRunDialog(has_seed_course=True)
    qtbot.addWidget(dialog)

    with qtbot.waitSignal(dialog.go_settings, timeout=3000):
        dialog.settings_button.click()

    dialog2 = FirstRunDialog(has_seed_course=False)
    qtbot.addWidget(dialog2)
    assert dialog2.seed_button.isEnabled() is False
