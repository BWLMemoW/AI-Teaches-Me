"""M0 服务层测试（settings / course / lesson）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.bootstrap import bootstrap_workspace, layout_for
from app.core.config import LLMProfile
from app.core.errors import ConfigError
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.llm.client import ConnectionReport
from app.services import course_service, lesson_service
from app.services.settings_service import SettingsService


def _settings(tmp_path: Path) -> SettingsService:
    result = bootstrap_workspace(
        tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False
    )
    return SettingsService(
        result.config, workspace_root=tmp_path, store=SecretStore(backend=InMemoryBackend())
    )


# --- settings_service ----------------------------------------------------
def test_api_key_is_stored_in_secret_store_not_config(tmp_path: Path) -> None:
    service = _settings(tmp_path)

    masked = service.set_api_key("deepseek-default", "sk-abcdefghijklmnop")
    service.save()

    assert masked == "sk-****mnop"
    assert service.has_api_key("deepseek-default") is True
    assert service.api_key_masked("deepseek-default") == "sk-****mnop"
    saved = (tmp_path / "data" / "config.json").read_text(encoding="utf-8")
    assert "sk-abcdefghijklmnop" not in saved


def test_clear_api_key_removes_secret(tmp_path: Path) -> None:
    service = _settings(tmp_path)
    service.set_api_key("deepseek-default", "sk-abcdefghijklmnop")

    service.clear_api_key("deepseek-default")

    assert service.has_api_key("deepseek-default") is False
    assert service.api_key_masked("deepseek-default") == ""


def test_upsert_and_delete_profile(tmp_path: Path) -> None:
    service = _settings(tmp_path)

    service.upsert_profile(LLMProfile(name="local-ollama", base_url="http://localhost:11434/v1"))
    assert service.profile_names() == ["deepseek-default", "local-ollama"]

    service.upsert_profile(LLMProfile(name="local-ollama", base_url="http://localhost:11434/v2"))
    assert service.profile("local-ollama").base_url == "http://localhost:11434/v2"

    service.delete_profile("local-ollama")
    assert service.profile_names() == ["deepseek-default"]

    with pytest.raises(ConfigError):
        service.delete_profile("deepseek-default")


def test_delete_active_profile_switches_active(tmp_path: Path) -> None:
    service = _settings(tmp_path)
    service.upsert_profile(LLMProfile(name="second"))
    service.set_active_profile("second")

    service.delete_profile("second")

    assert service.config.active_profile == "deepseek-default"


def test_set_active_profile_rejects_unknown(tmp_path: Path) -> None:
    service = _settings(tmp_path)
    with pytest.raises(ConfigError):
        service.set_active_profile("ghost")


def test_theme_and_font_size_are_validated(tmp_path: Path) -> None:
    service = _settings(tmp_path)

    service.set_theme("forest")
    assert service.config.ui.theme == "forest"
    with pytest.raises(ConfigError):
        service.set_theme("rainbow")

    service.set_font_size(99)
    assert service.config.ui.font_size == 24
    service.set_font_size(1)
    assert service.config.ui.font_size == 12

    service.set_follow_system(False)
    service.set_reduce_motion(True)
    assert service.config.ui.follow_system is False
    assert service.config.ui.reduce_motion is True


def test_save_migrates_plaintext_api_key(tmp_path: Path) -> None:
    service = _settings(tmp_path)
    service.profile("deepseek-default").api_key = "sk-plaintext-should-move"

    service.save()

    assert service.profile("deepseek-default").api_key == ""
    assert service.store.get_secret("atm/deepseek-default") == "sk-plaintext-should-move"
    assert "sk-plaintext-should-move" not in (tmp_path / "data" / "config.json").read_text(
        encoding="utf-8"
    )


def test_test_connection_uses_client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict[str, object] = {}

    class _StubClient:
        def __init__(self, config, *, secret_store=None, usage=None) -> None:
            seen["config"] = config

        async def probe_connection(self, **kwargs) -> ConnectionReport:
            seen.update(kwargs)
            return ConnectionReport(ok=True, elapsed_ms=42, models=["deepseek-flash"])

    monkeypatch.setattr("app.services.settings_service.LLMClient", _StubClient)
    service = _settings(tmp_path)

    report = service.test_connection_sync(profile_name="deepseek-default", api_key="sk-temp")

    assert report.ok is True
    assert report.elapsed_ms == 42
    assert seen["profile"] == "deepseek-default"
    assert seen["api_key"] == "sk-temp"


# --- course_service ------------------------------------------------------
def test_scan_courses_finds_seed_course(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)

    courses = course_service.scan_courses(tmp_path / "Courses")

    assert [course.name for course in courses] == ["ATM 使用指南"]
    seed = courses[0]
    assert seed.seed is True
    assert seed.chapters == 1
    assert seed.lessons == 1
    assert seed.completed_lessons == 0
    assert seed.broken is False
    assert seed.progress_ratio == 0.0


def test_scan_courses_reports_broken_course(tmp_path: Path) -> None:
    broken = tmp_path / "Courses" / "坏课"
    broken.mkdir(parents=True)

    courses = course_service.scan_courses(tmp_path / "Courses")

    assert len(courses) == 1
    assert courses[0].broken is True
    assert any("course.json" in issue for issue in courses[0].issues)


def test_scan_courses_ignores_hidden_and_missing(tmp_path: Path) -> None:
    (tmp_path / "Courses" / ".library").mkdir(parents=True)
    assert course_service.scan_courses(tmp_path / "Courses") == []
    assert course_service.scan_courses(tmp_path / "nope") == []


def test_lesson_tree_merges_lesson_status(tmp_path: Path) -> None:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    course_dir = tmp_path / "Courses" / "ATM 使用指南"

    tree = course_service.lesson_tree(course_dir)
    assert tree[0]["id"] == "C1"
    assert tree[0]["lessons"][0]["status"] == "not_started"

    lesson_dir = course_service.find_lesson_dir(course_dir, "L1-1")
    assert lesson_dir is not None and lesson_dir.name.startswith("L1-1-")

    lesson_service.mark_complete(lesson_dir, mastery=3, minutes=5)
    tree = course_service.lesson_tree(course_dir)
    assert tree[0]["lessons"][0]["status"] == "completed"
    assert course_service.find_lesson_dir(course_dir, "L9-9") is None


# --- lesson_service ------------------------------------------------------
def _seed_lesson_dir(tmp_path: Path) -> Path:
    bootstrap_workspace(tmp_path, store=SecretStore(backend=InMemoryBackend()), configure_file_logging=False)
    return tmp_path / "Courses" / "ATM 使用指南" / "chapters" / "C1-三分钟上手" / "lessons" / "L1-1-先配置你的-AI"


def test_load_seed_lesson(tmp_path: Path) -> None:
    content = lesson_service.load_lesson(_seed_lesson_dir(tmp_path))

    assert content.lesson_id == "L1-1"
    assert content.title == "先配置你的 AI"
    assert content.has_blocks is True
    assert content.blocks[0]["type"] == "meta"
    assert content.blocks[-1]["type"] == "done"
    assert content.status == lesson_service.STATUS_NOT_STARTED
    assert content.issues == ()
    assert "小结" in content.markdown


def test_mark_complete_updates_lesson_and_progress(tmp_path: Path) -> None:
    lesson_dir = _seed_lesson_dir(tmp_path)

    lesson_service.mark_in_progress(lesson_dir)
    assert lesson_service.load_lesson(lesson_dir).status == lesson_service.STATUS_IN_PROGRESS

    lesson = lesson_service.mark_complete(lesson_dir, mastery=3, quiz_score=1.0, minutes=7)

    assert lesson["status"] == lesson_service.STATUS_COMPLETED
    assert lesson["time_spent_seconds"] == 420
    progress = (tmp_path / "Courses" / "ATM 使用指南" / "progress.json").read_text(encoding="utf-8")
    assert '"status": "completed"' in progress
    assert '"mastery": 3' in progress
    assert '"minutes": 7' in progress


def test_unmark_complete_keeps_content(tmp_path: Path) -> None:
    lesson_dir = _seed_lesson_dir(tmp_path)
    before = (lesson_dir / "script.md").read_text(encoding="utf-8")
    lesson_service.mark_complete(lesson_dir, mastery=2)

    lesson = lesson_service.unmark_complete(lesson_dir)

    assert lesson["status"] == lesson_service.STATUS_IN_PROGRESS
    assert (lesson_dir / "script.md").read_text(encoding="utf-8") == before


def test_load_lesson_without_blocks_falls_back_to_markdown(tmp_path: Path) -> None:
    lesson_dir = tmp_path / "lessons" / "L2-1-只有 Markdown"
    lesson_dir.mkdir(parents=True)
    (lesson_dir / "lesson.json").write_text('{"id": "L2-1", "title": "只有 Markdown"}', encoding="utf-8")
    (lesson_dir / "script.md").write_text("# 标题\n\n正文", encoding="utf-8")

    content = lesson_service.load_lesson(lesson_dir)

    assert content.has_blocks is False
    assert content.needs_markdown_fallback is True
    assert any("整段 Markdown" in issue for issue in content.issues)
    assert any("script.blocks.jsonl" in issue for issue in content.issues)


def test_mark_complete_requires_lesson_json(tmp_path: Path) -> None:
    empty = tmp_path / "lessons" / "L3-1-空"
    empty.mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        lesson_service.mark_complete(empty)


def test_blocks_to_markdown_covers_rich_blocks() -> None:
    markdown = lesson_service.blocks_to_markdown(
        [
            {"type": "text", "md": "## 小节"},
            {"type": "table", "headers": ["A", "B"], "rows": [["1", "2"]]},
            {"type": "code", "lang": "python", "code": "print(1)"},
            {"type": "summary", "points": ["要点一", "要点二"]},
            {"type": "next_preview", "md": "下节见"},
        ]
    )

    assert "## 小节" in markdown
    assert "| A | B |" in markdown
    assert "print(1)" in markdown
    assert "要点一" in markdown
    assert "下节见" in markdown
