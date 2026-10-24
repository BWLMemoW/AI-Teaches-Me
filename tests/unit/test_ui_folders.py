"""课程库文件夹树、导航与课程移动。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from app.bootstrap import bootstrap_workspace
from app.infra.secret_store import InMemoryBackend, SecretStore
from app.services.library_service import UNFILED_FOLDER_ID, LibraryService
from app.ui.pages.library_page import LibraryPage
from app.ui.widgets.course_card import CourseCard
from app.ui.widgets.folder_tree import ALL_FOLDERS_ID


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


def _page(tmp_path: Path, qtbot) -> tuple[LibraryPage, LibraryService, dict[str, str]]:
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
    math = service.folders.create("数学")
    calculus = service.folders.create("高数", parent_id=math.id)
    code = service.folders.create("编程")
    service.folders.assign_course("course-math", math.id)
    service.folders.assign_course("course-calculus", calculus.id)
    service.folders.assign_course("course-code", code.id)
    page = LibraryPage(courses, service=service)
    qtbot.addWidget(page)
    page.refresh()
    return page, service, {
        "math": math.id,
        "calculus": calculus.id,
        "code": code.id,
    }


def test_tree_contains_special_nodes_and_nested_folders(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)

    assert page.folder_tree.topLevelItem(0).text(0) == "全部课程"
    assert page.folder_tree.topLevelItem(1).text(0) == "未分类"
    math_item = page.folder_tree.topLevelItem(2)
    assert math_item.text(0).startswith("数学")
    assert math_item.child(0).text(0).startswith("高数")
    assert math_item.data(0, page.folder_tree.FOLDER_ROLE) == ids["math"]


def test_navigate_to_folder_filters_direct_courses(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)

    page.navigate(ids["math"])

    assert {card.summary.id for card in page.current_cards()} == {"course-math"}
    assert "数学" in page.breadcrumb_label.text()


def test_search_inside_folder_covers_descendants(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)
    page.navigate(ids["math"])

    page.search_edit.setText("高数")

    assert {card.summary.id for card in page.current_cards()} == {"course-calculus"}


def test_unfiled_node_shows_only_unassigned_courses(tmp_path: Path, qtbot) -> None:
    page, _, _ = _page(tmp_path, qtbot)

    page.navigate(UNFILED_FOLDER_ID)

    assert {card.summary.id for card in page.current_cards()} == {"course-seed-atm-guide"}


def test_move_course_to_folder_refreshes_tree(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)

    page.move_course_to_folder("course-code", ids["math"])
    page.navigate(ids["math"])

    assert {card.summary.id for card in page.current_cards()} == {"course-code", "course-math"}


def test_new_course_destination_follows_current_folder(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)

    page.navigate(ids["calculus"])
    assert page.target_folder_for_new_course() == ids["calculus"]

    page.navigate(ALL_FOLDERS_ID)
    assert page.target_folder_for_new_course() is None


def test_assign_new_course_uses_current_folder(tmp_path: Path, qtbot) -> None:
    page, service, ids = _page(tmp_path, qtbot)
    page.navigate(ids["code"])

    page.assign_new_course("course-math")

    assert service.folders.folder_of("course-math") == ids["code"]


def test_drop_on_current_folder_moves_course_into_it(tmp_path: Path, qtbot) -> None:
    page, service, ids = _page(tmp_path, qtbot)
    page.navigate(ids["math"])

    page._on_course_dropped_to_current("course-code")

    assert service.folders.folder_of("course-code") == ids["math"]


def test_create_folder_under_current_folder(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)
    page.navigate(ids["math"])

    created = page.create_folder("概率论")

    assert created.parent_id == ids["math"]
    assert page.folder_tree.current_folder_id() == created.id


def test_view_mode_switches_between_grid_and_list(tmp_path: Path, qtbot) -> None:
    page, _, _ = _page(tmp_path, qtbot)

    assert page.card_scroll.isHidden() is False
    assert page.course_list.isHidden() is True

    page.view_mode_combo.setCurrentIndex(1)

    assert page.card_scroll.isHidden() is True
    assert page.course_list.isHidden() is False


def test_content_area_only_lists_courses_not_child_folders(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)
    page.navigate(ids["math"])

    assert len(page.current_cards()) == 1
    assert page.card_grid.count() == 1

    page.view_mode_combo.setCurrentIndex(1)
    assert page.course_list.count() == 1


def test_course_cards_use_one_uniform_size(tmp_path: Path, qtbot) -> None:
    page, _, ids = _page(tmp_path, qtbot)
    page.move_course_to_folder("course-code", ids["math"])
    page.move_course_to_folder("course-calculus", ids["math"])
    page.navigate(ids["math"])

    cards = page.current_cards()
    assert len(cards) == 3
    assert {card.width() for card in cards} == {CourseCard.CARD_WIDTH}
    assert {card.height() for card in cards} == {CourseCard.CARD_HEIGHT}
