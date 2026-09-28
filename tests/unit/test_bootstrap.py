"""工作区初始化与迁移检查的单元测试（《技术文档》§3.2 / §3.11、AC-51）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.bootstrap import (
    bootstrap_workspace,
    check_migrations,
    install_seed_courses,
    layout_for,
    list_seed_courses,
    needs_api_setup,
    resolve_workspace_root,
)
from app.core.config import config_file, load_config, save_config
from app.infra.secret_store import InMemoryBackend, SecretStore


def _store() -> SecretStore:
    return SecretStore(backend=InMemoryBackend())


def _bootstrap(tmp_path: Path, **kwargs):
    kwargs.setdefault("store", _store())
    kwargs.setdefault("configure_file_logging", False)
    return bootstrap_workspace(tmp_path, **kwargs)


def test_first_run_creates_workspace_and_config(tmp_path: Path) -> None:
    result = _bootstrap(tmp_path)
    layout = result.layout

    assert layout.root == tmp_path.resolve()
    assert layout.courses.is_dir()
    assert layout.personas.is_dir()
    assert layout.data.is_dir()
    assert layout.cache.is_dir()
    assert layout.logs.is_dir()
    assert layout.exports.is_dir()
    assert layout.db_file == layout.data / "library.db"
    assert config_file(tmp_path).exists()
    assert result.created_dirs
    assert result.read_only is False


def test_second_run_keeps_user_config(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    config = load_config(tmp_path)
    config.ui.theme = "forest"
    save_config(tmp_path, config)

    second = _bootstrap(tmp_path)

    assert second.config.ui.theme == "forest"
    assert second.created_dirs == []


def test_seed_course_installed_once(tmp_path: Path) -> None:
    first = _bootstrap(tmp_path)
    assert first.seeded_files
    seed_course = tmp_path / "Courses" / "ATM 使用指南"
    assert (seed_course / "course.json").exists()

    second = _bootstrap(tmp_path)
    assert second.seeded_files == []
    assert (seed_course / "script.md").exists() is False
    assert (seed_course / "chapters").is_dir()


def test_modified_seed_file_is_not_overwritten(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    target = (
        tmp_path / "Courses" / "ATM 使用指南" / "chapters" / "C1-三分钟上手"
        / "lessons" / "L1-1-先配置你的-AI" / "script.md"
    )
    target.write_text("用户改过的讲义", encoding="utf-8")

    _bootstrap(tmp_path)

    assert target.read_text(encoding="utf-8") == "用户改过的讲义"


def test_install_seed_courses_with_overwrite(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    layout.courses.mkdir(parents=True)
    install_seed_courses(layout)
    target = (
        tmp_path / "Courses" / "ATM 使用指南" / "chapters" / "C1-三分钟上手"
        / "lessons" / "L1-1-先配置你的-AI" / "script.md"
    )
    target.write_text("被改过", encoding="utf-8")

    written = install_seed_courses(layout, overwrite=True)

    assert written
    assert "先配置你的 AI" in target.read_text(encoding="utf-8")


def test_install_seed_courses_stays_inside_courses_dir(tmp_path: Path) -> None:
    source = tmp_path / "src" / "示例课程"
    (source / "chapters" / "C1-第一章").mkdir(parents=True)
    (source / "course.json").write_text('{"name": "示例课程"}', encoding="utf-8")
    (source / "chapters" / "C1-第一章" / "c.json").write_text("{}", encoding="utf-8")

    layout = layout_for(tmp_path / "ws")
    layout.courses.mkdir(parents=True)
    written = install_seed_courses(layout, source=tmp_path / "src")

    root = layout.courses.resolve()
    assert written
    assert all(str(path.resolve()).startswith(str(root)) for path in written)
    assert (layout.courses / "示例课程" / "course.json").exists()


def test_stale_tmp_files_are_cleaned_on_boot(tmp_path: Path) -> None:
    layout = layout_for(tmp_path)
    layout.data.mkdir(parents=True)
    leftover = layout.data / "config.json.tmp"
    leftover.write_text("{}", encoding="utf-8")

    result = _bootstrap(tmp_path)

    assert leftover in result.cleaned_tmp
    assert not leftover.exists()


def test_read_only_mode_when_config_is_too_new(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")

    result = _bootstrap(tmp_path)

    assert result.read_only is True
    assert any("只读" in message for message in result.messages)
    assert check_migrations(result.layout)


def test_plaintext_secret_is_migrated_on_boot(tmp_path: Path) -> None:
    path = config_file(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "llm_profiles": [
                    {"name": "deepseek-default", "api_key": "sk-plain-in-config"}
                ],
            }
        ),
        encoding="utf-8",
    )
    store = _store()

    result = _bootstrap(tmp_path, store=store)

    assert result.migrated_secrets == ["atm/deepseek-default"]
    assert store.get_secret("atm/deepseek-default") == "sk-plain-in-config"
    assert "sk-plain-in-config" not in path.read_text(encoding="utf-8")


def test_needs_api_setup_reflects_secret_store(tmp_path: Path) -> None:
    store = _store()
    result = _bootstrap(tmp_path, store=store)
    assert result.needs_api_setup is True

    store.set_secret("atm/deepseek-default", "sk-x")
    assert needs_api_setup(result.config, store=store, env={}) is False


def test_resolve_workspace_root_priority(tmp_path: Path) -> None:
    assert resolve_workspace_root(tmp_path, env={"ATM_WORKSPACE": "D:/elsewhere"}) == tmp_path.resolve()
    assert resolve_workspace_root(None, env={"ATM_WORKSPACE": str(tmp_path)}) == tmp_path.resolve()
    assert resolve_workspace_root(None, env={}).name == "AI-Teaches-Me"


def test_list_seed_courses_reports_bundled_course() -> None:
    assert list_seed_courses() == ["ATM 使用指南"]
