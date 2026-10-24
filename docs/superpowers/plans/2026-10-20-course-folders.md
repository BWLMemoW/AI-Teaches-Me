# Course Folders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Windows Explorer-style multi-level virtual folders to the course library without moving course directories.

**Architecture:** A file-backed `FolderService` owns the tree and course assignments. `LibraryService` merges assignments into course summaries and filters. `LibraryPage` adds a folder tree, breadcrumb, folder rows/cards, keyboard shortcuts, context menus, and drag-and-drop.

**Tech Stack:** Python 3.11, PySide6, pytest, pytest-qt, JSON atomic writes.

---

### Task 1: Folder tree service

**Files:**
- Create: `app/services/folder_service.py`
- Test: `tests/unit/test_folder_service.py`

- [ ] **Step 1: Write failing tests**

```python
def test_create_nested_folder_and_persist(tmp_path):
    service = FolderService(tmp_path / "Courses")
    root = service.create("数学")
    child = service.create("线性代数", parent_id=root.id)
    assert child.parent_id == root.id

    reloaded = FolderService(tmp_path / "Courses")
    assert [node.name for node in reloaded.children(root.id)] == ["线性代数"]
```

- [ ] **Step 2: Run test and confirm import failure**

Run: `python -m pytest tests/unit/test_folder_service.py -q`

- [ ] **Step 3: Implement FolderNode, load/save, create, children, path_of**

- [ ] **Step 4: Add tests for duplicate names, rename, move cycle, delete reparenting, assignments**

- [ ] **Step 5: Implement validation and tree mutation methods**

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/unit/test_folder_service.py -q`

### Task 2: Library filtering integration

**Files:**
- Modify: `app/services/course_service.py`
- Modify: `app/services/library_service.py`
- Test: `tests/unit/test_library_folders.py`

- [ ] **Step 1: Write failing tests for direct-only filtering, subtree search, unfiled courses, and folder persistence**

- [ ] **Step 2: Run tests and confirm failures**

Run: `python -m pytest tests/unit/test_library_folders.py -q`

- [ ] **Step 3: Add `folder_id` to `CourseSummary` and folder fields to `LibraryFilter`**

- [ ] **Step 4: Load folder assignments in `LibraryService.refresh()` and filter in `search()`**

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/unit/test_library_folders.py tests/unit/test_library_m1.py -q`

### Task 3: Folder tree widget and page navigation

**Files:**
- Create: `app/ui/widgets/folder_tree.py`
- Modify: `app/ui/pages/library_page.py`
- Test: `tests/unit/test_ui_folders.py`

- [ ] **Step 1: Write failing UI tests for tree contents, folder selection filtering, breadcrumb, and entering a folder**

- [ ] **Step 2: Run tests and confirm failures**

Run: `python -m pytest tests/unit/test_ui_folders.py -q`

- [ ] **Step 3: Implement `FolderTree` with 全部课程 / 未分类 / nested nodes and context signals**

- [ ] **Step 4: Implement page split layout; keep subfolders only in the left tree**

- [ ] **Step 5: Implement breadcrumb, navigate, back, up, grid/list folder rows**

- [ ] **Step 6: Run tests**

Run: `python -m pytest tests/unit/test_ui_folders.py tests/unit/test_m9_cards.py tests/unit/test_ui_m0.py -q`

### Task 4: Explorer operations

**Files:**
- Modify: `app/ui/pages/library_page.py`
- Modify: `app/ui/widgets/course_card.py`
- Modify: `app/ui/main_window.py`
- Test: `tests/unit/test_ui_folders.py`

- [ ] **Step 1: Write failing tests for new folder, rename, delete confirmation, move menu, and new-course destination**

- [ ] **Step 2: Run tests and confirm failures**

Run: `python -m pytest tests/unit/test_ui_folders.py -q`

- [ ] **Step 3: Add folder action buttons, context menus, shortcuts**

- [ ] **Step 4: Add course drag payload and tree/content drop handling**

- [ ] **Step 5: Assign newly created courses to the active folder**

- [ ] **Step 6: Run UI tests**

Run: `python -m pytest tests/unit/test_ui_folders.py tests/unit/test_ui_m1.py -q`

### Task 5: Backup and restore

**Files:**
- Modify: `app/services/backup_service.py`
- Modify: `app/services/restore_service.py`
- Test: `tests/unit/test_backup_restore_folders.py`

- [ ] **Step 1: Write failing backup/restore tests**

- [ ] **Step 2: Run tests and confirm failures**

Run: `python -m pytest tests/unit/test_backup_restore_folders.py -q`

- [ ] **Step 3: Include `.library/folders.json` in library backup**

- [ ] **Step 4: Allow `.library/folders.json` during restore with zip-slip protection**

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/unit/test_backup_restore_folders.py tests/unit/test_library_m1.py -q`

### Task 6: Documentation and full verification

**Files:**
- Modify: `README.md`
- Modify: `docs/项目文档.md`

- [ ] **Step 1: Document virtual folders and backup behavior**

- [ ] **Step 2: Run full unit suite**

Run: `python -m pytest tests/unit -q`

- [ ] **Step 3: Run integration suite**

Run: `python -m pytest tests/integration -q`

- [ ] **Step 4: Restart the ATM app**

Run: `pwsh -File scripts/restart_app.ps1 -Wait`
