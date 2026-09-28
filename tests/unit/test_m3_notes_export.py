"""M3 测试：笔记生成（FR-10）与导出（Markdown / PDF）。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services import export_service, lesson_service, notes_service
from app.ui.pages.notes_page import NotesPage

SEED_LESSON = Path("Courses") / "ATM 使用指南" / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


def _workspace(tmp_path: Path) -> tuple[Path, Path]:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course = tmp_path / "Courses" / "ATM 使用指南"
    return course, tmp_path / SEED_LESSON


class _FakeNotesAgent:
    def __init__(self, text: str) -> None:
        self._text = text

    async def make_notes(self, **payload):
        yield self._text


class _FailingNotesAgent:
    async def make_notes(self, **payload):
        raise RuntimeError("模型不可用")
        yield ""  # pragma: no cover


def test_local_notes_include_template_sections(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)

    result = notes_service.generate_notes(course, lesson)

    assert result.path == course / "notes" / "C1" / "L1-1.md"
    assert result.path.exists()
    text = result.path.read_text(encoding="utf-8")
    assert text.startswith("# L1-1 先配置你的 AI")
    for section in notes_service.NOTE_SECTIONS:
        assert f"## {section}" in text
    assert result.generated_by_model is False
    assert result.issues == []


def test_incomplete_notes_are_marked(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)

    result = notes_service.generate_notes(course, lesson, incomplete=True)

    assert notes_service.INCOMPLETE_NOTICE in result.text
    assert any("尚未学完" in issue for issue in result.issues)


def test_model_notes_are_used_when_available(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)
    agent = _FakeNotesAgent("# L1-1 先配置你的 AI\n\n## 本节目标\n- 接上 AI\n")

    result = notes_service.generate_notes(course, lesson, agent=agent)

    assert result.generated_by_model is True
    assert "接上 AI" in result.text


def test_model_failure_falls_back_to_local_notes(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)

    result = notes_service.generate_notes(course, lesson, agent=_FailingNotesAgent())

    assert result.generated_by_model is False
    assert result.issues and "本地结构化" in result.issues[0]
    assert "## 知识点提纲" in result.text


def test_notes_include_chat_questions(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "为什么要配置 Base URL？"})
    lesson_service.append_chat_turn(lesson, {"role": "assistant", "blocks": 1, "md": "因为端点不同。"})

    result = notes_service.generate_notes(course, lesson)

    assert "为什么要配置 Base URL？" in result.text
    assert "因为端点不同" in result.text


def test_export_markdown_contains_course_and_date(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)
    notes_service.generate_notes(course, lesson)
    lesson_service.append_chat_turn(lesson, {"role": "user", "text": "追问一句"})

    result = export_service.export_markdown(course, scope="all")

    assert result.path.parent == course / "exports"
    assert "ATM 使用指南" in result.path.name
    text = result.path.read_text(encoding="utf-8")
    assert "# ATM 使用指南" in text
    assert "L1-1 先配置你的 AI" in text
    assert "追问一句" in text
    assert "#### 笔记" in text


def test_export_scopes(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)
    notes_service.generate_notes(course, lesson)

    script_only, _ = export_service.collect_markdown(course, scope="script")
    notes_only, _ = export_service.collect_markdown(course, scope="notes")

    assert "#### 笔记" not in script_only
    assert "#### 笔记" in notes_only
    assert "先别急着学知识" not in notes_only


def test_export_pdf_uses_injected_printer(tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)
    calls: list[tuple[int, Path]] = []

    def fake_printer(html: str, target: Path) -> bool:
        calls.append((len(html), Path(target)))
        Path(target).write_bytes(b"%PDF-1.4 fake")
        return True

    result = export_service.export_pdf(course, printer=fake_printer)

    assert calls and calls[0][0] > 100
    assert result.path.exists() and result.path.suffix == ".pdf"
    assert result.size_bytes > 0


def test_export_pdf_failure_raises(tmp_path: Path) -> None:
    course, _ = _workspace(tmp_path)

    with pytest.raises(RuntimeError):
        export_service.export_pdf(course, printer=lambda html, target: False)


def test_render_html_is_offline_and_has_table(tmp_path: Path) -> None:
    html = export_service.render_html("# 标题\n\n| a | b |\n| - | - |\n| 1 | 2 |\n")

    assert "<h1" in html and "<table" in html
    assert "http://" not in html and "https://" not in html


@pytest.mark.ui
def test_notes_page_generates_and_exports(qtbot, tmp_path: Path) -> None:
    course, lesson = _workspace(tmp_path)

    def fake_printer(html: str, target) -> bool:
        Path(target).write_bytes(b"%PDF-1.4 fake")
        return True

    page = NotesPage(courses_dir=tmp_path / "Courses", pdf_printer=fake_printer)
    qtbot.addWidget(page)

    page.refresh()
    assert page.course_combo.count() == 1
    page.lesson_list.setCurrentRow(0)

    page.generate_button.click()

    note_file = course / "notes" / "C1" / "L1-1.md"
    assert note_file.exists()
    assert "本节目标" in page.preview.toPlainText()

    page.export_md_button.click()
    assert list((course / "exports").glob("*.md"))

    page.export_pdf_button.click()
    assert list((course / "exports").glob("*.pdf"))
