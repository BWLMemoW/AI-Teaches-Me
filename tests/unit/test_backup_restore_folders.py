"""整库备份与恢复要保留虚拟文件夹层级。"""

from __future__ import annotations

import zipfile
from pathlib import Path

from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services import backup_service, restore_service
from app.services.folder_service import FolderService


def test_library_backup_contains_folder_tree(tmp_path: Path) -> None:
    result = bootstrap_workspace(
        tmp_path,
        store=SecretStore(backend=InMemoryBackend()),
        configure_file_logging=False,
    )
    courses = result.layout.courses
    folders = FolderService(courses)
    math = folders.create("数学")
    child = folders.create("线性代数", parent_id=math.id)
    folders.assign_course("course-seed-atm-guide", child.id)
    archive = tmp_path / "课程库备份.zip"

    backup_service.backup_library(courses, archive)

    with zipfile.ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    assert ".library/folders.json" in names


def test_restore_keeps_hidden_library_path_and_assignments(tmp_path: Path) -> None:
    result = bootstrap_workspace(
        tmp_path / "source",
        store=SecretStore(backend=InMemoryBackend()),
        configure_file_logging=False,
    )
    courses = result.layout.courses
    folders = FolderService(courses)
    math = folders.create("数学")
    child = folders.create("线性代数", parent_id=math.id)
    folders.assign_course("course-seed-atm-guide", child.id)
    archive = tmp_path / "课程库备份.zip"
    backup_service.backup_library(courses, archive)

    target = tmp_path / "restored"
    restored = restore_service.restore_from_zip(archive, target)

    assert target / ".library" / "folders.json" in restored
    assert not (target / "library" / "folders.json").exists()
    reloaded = FolderService(target)
    restored_child = reloaded.get(child.id)
    assert restored_child is not None
    assert restored_child.parent_id == math.id
    assert reloaded.folder_of("course-seed-atm-guide") == child.id
