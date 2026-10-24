"""课程库按虚拟文件夹过滤与搜索子树。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.library_service import UNFILED_FOLDER_ID, LibraryFilter, LibraryService


def _copy_course(courses: Path, source: Path, name: str, course_id: str) -> None:
    target = courses / name
    shutil.copytree(source, target)
    meta_path = target / "course.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta.update({"id": course_id, "name": name, "seed": False})
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    outline_path = target / "outline.json"
    outline = json.loads(outline_path.read_text(encoding="utf-8"))
    outline["course_id"] = course_id
    outline_path.write_text(json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8")


def _workspace(tmp_path: Path) -> tuple[LibraryService, dict[str, str]]:
    result = bootstrap_workspace(
        tmp_path,
        store=SecretStore(backend=InMemoryBackend()),
        configure_file_logging=False,
    )
    courses = result.layout.courses
    seed = courses / "ATM 使用指南"
    _copy_course(courses, seed, "数学基础", "course-math")
    _copy_course(courses, seed, "高数", "course-calculus")
    _copy_course(courses, seed, "编程入门", "course-code")
    service = LibraryService(courses_dir=courses, index=None)
    folders = service.folders
    math = folders.create("数学")
    calculus = folders.create("高数", parent_id=math.id)
    code = folders.create("编程")
    folders.assign_course("course-math", math.id)
    folders.assign_course("course-calculus", calculus.id)
    folders.assign_course("course-code", code.id)
    service.refresh()
    return service, {
        "math": math.id,
        "calculus": calculus.id,
        "code": code.id,
    }


def test_refresh_merges_folder_ids(tmp_path: Path) -> None:
    service, ids = _workspace(tmp_path)
    summaries = {item.id: item for item in service.refresh()}

    assert summaries["course-math"].folder_id == ids["math"]
    assert summaries["course-calculus"].folder_id == ids["calculus"]
    assert summaries["course-code"].folder_id == ids["code"]


def test_direct_folder_filter_excludes_children(tmp_path: Path) -> None:
    service, ids = _workspace(tmp_path)

    direct = service.search(LibraryFilter(folder_id=ids["math"]))

    assert [item.id for item in direct] == ["course-math"]


def test_folder_filter_can_include_descendants(tmp_path: Path) -> None:
    service, ids = _workspace(tmp_path)

    subtree = service.search(
        LibraryFilter(folder_id=ids["math"], include_descendants=True, sort="name")
    )

    assert {item.id for item in subtree} == {"course-math", "course-calculus"}


def test_search_text_scopes_to_folder_subtree(tmp_path: Path) -> None:
    service, ids = _workspace(tmp_path)

    direct = service.search(LibraryFilter(folder_id=ids["math"], query="高数"))
    subtree = service.search(
        LibraryFilter(folder_id=ids["math"], include_descendants=True, query="高数")
    )

    assert direct == []
    assert [item.id for item in subtree] == ["course-calculus"]


def test_unfiled_folder_only_returns_unassigned_courses(tmp_path: Path) -> None:
    service, _ = _workspace(tmp_path)
    service.folders.assign_course("course-code", None)

    unfiled = service.search(LibraryFilter(folder_id=UNFILED_FOLDER_ID))

    assert {item.id for item in unfiled} == {"course-code", "course-seed-atm-guide"}


def test_refresh_prunes_assignments_for_missing_courses(tmp_path: Path) -> None:
    service, ids = _workspace(tmp_path)
    service.folders.assign_course("gone", ids["math"])

    service.refresh()

    assert service.folders.folder_of("gone") is None
