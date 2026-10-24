"""课程库虚拟文件夹：树结构、归属、移动与持久化。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.folder_service import FolderService


def _service(tmp_path: Path) -> FolderService:
    return FolderService(tmp_path / "Courses")


def test_create_nested_folder_and_persist(tmp_path: Path) -> None:
    service = _service(tmp_path)
    root = service.create("数学")
    child = service.create("线性代数", parent_id=root.id)

    assert child.parent_id == root.id
    reloaded = _service(tmp_path)
    assert [node.name for node in reloaded.children(root.id)] == ["线性代数"]


def test_duplicate_sibling_names_get_suffix(tmp_path: Path) -> None:
    service = _service(tmp_path)
    first = service.create("数学")
    second = service.create("数学")
    other = service.create("数学", parent_id=first.id)

    assert second.name == "数学-2"
    assert other.name == "数学"


def test_duplicate_long_names_stay_unique(tmp_path: Path) -> None:
    service = _service(tmp_path)
    long_name = "学" * 80

    first = service.create(long_name)
    second = service.create(long_name)

    assert second.name != first.name
    assert len(second.name) <= 80


def test_rename_keeps_sibling_names_unique(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.create("数学")
    second = service.create("编程")

    renamed = service.rename(second.id, "数学")

    assert renamed.name == "数学-2"
    assert renamed.id == second.id


def test_move_rejects_self_and_descendant(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parent = service.create("数学")
    child = service.create("线性代数", parent_id=parent.id)
    grandchild = service.create("矩阵", parent_id=child.id)

    with pytest.raises(ValueError):
        service.move(parent.id, parent.id)
    with pytest.raises(ValueError):
        service.move(parent.id, grandchild.id)

    moved = service.move(grandchild.id, parent.id)
    assert moved.parent_id == parent.id
    assert [node.name for node in service.children(child.id)] == []


def test_delete_reparents_children_and_courses(tmp_path: Path) -> None:
    service = _service(tmp_path)
    parent = service.create("数学")
    doomed = service.create("代数", parent_id=parent.id)
    child = service.create("线性代数", parent_id=doomed.id)
    service.assign_course("course-1", doomed.id)

    service.delete(doomed.id)

    assert child.parent_id == parent.id
    assert service.folder_of("course-1") == parent.id
    assert all(node.id != doomed.id for node in service.folders())


def test_delete_root_folder_returns_courses_to_unfiled(tmp_path: Path) -> None:
    service = _service(tmp_path)
    root = service.create("数学")
    child = service.create("代数", parent_id=root.id)
    service.assign_course("course-1", root.id)
    service.assign_course("course-2", child.id)

    service.delete(root.id)

    assert child.parent_id is None
    assert service.folder_of("course-1") is None
    assert service.folder_of("course-2") == child.id


def test_assignments_survive_reload_and_missing_courses_are_pruned(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = service.create("数学")
    service.assign_course("course-1", folder.id)
    service.assign_course("course-2", folder.id)

    reloaded = _service(tmp_path)
    assert reloaded.folder_of("course-1") == folder.id

    removed = reloaded.remove_missing_courses({"course-1"})

    assert removed == 1
    assert reloaded.folder_of("course-2") is None


def test_last_folder_is_remembered_and_validated(tmp_path: Path) -> None:
    service = _service(tmp_path)
    folder = service.create("数学")
    service.set_last_folder(folder.id)
    assert _service(tmp_path).last_folder() == folder.id

    service.set_last_folder("missing")
    assert service.last_folder() is None


def test_corrupt_file_starts_with_empty_tree(tmp_path: Path) -> None:
    library = tmp_path / "Courses" / ".library"
    library.mkdir(parents=True)
    (library / "folders.json").write_text("{not json", encoding="utf-8")

    service = _service(tmp_path)

    assert service.folders() == []
    service.create("数学")
    payload = json.loads((library / "folders.json").read_text(encoding="utf-8"))
    assert payload["folders"][0]["name"] == "数学"


def test_invalid_folder_name_is_rejected(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(Exception):
        service.create("数学/代数")
