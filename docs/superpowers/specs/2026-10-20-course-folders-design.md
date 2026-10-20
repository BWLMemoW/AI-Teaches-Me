# 课程库文件夹管理设计

## 目标

在课程库中提供接近 Windows 资源管理器的多级文件夹体验。课程目录本身保持
`Courses/<课程名>/` 不变，文件夹只是应用层的组织方式，避免移动课程目录带来的
路径、备份和索引风险。

## 非目标

- 不把课程移动到真实子目录。
- 不做多选批量移动。
- 不做文件夹自定义颜色或图标。
- 不改变学习台、课程创建向导和课程目录结构。

## 数据模型

文件：`Courses/.library/folders.json`

```json
{
  "schema_version": 1,
  "folders": [
    {
      "id": "f-20261020-001",
      "name": "数学",
      "parent_id": null,
      "sort_index": 0,
      "created_at": "2026-10-20T10:00:00+08:00"
    }
  ],
  "assignments": {
    "course-id": "f-20261020-001"
  },
  "last_folder_id": "f-20261020-001"
}
```

约定：

- `parent_id=null` 表示根级文件夹。
- 文件夹 id 稳定，重命名和移动不改变 id。
- 课程归属以 `course.id` 为键，课程重命名不影响归属。
- 未被分配、或所属文件夹已不存在的课程，一律显示在“未分类”。
- 文件通过 `atomic_write_json` 原子写入。
- 文件损坏时返回空树并在内存中继续工作，不阻断课程库启动。

## 服务边界

新增 `app/services/folder_service.py`，只依赖标准库和 `core.paths`，不依赖 Qt。

公开接口：

```python
class FolderService:
    def load(self) -> None
    def save(self) -> None
    def folders(self) -> list[FolderNode]
    def children(self, parent_id: str | None) -> list[FolderNode]
    def create(self, name: str, parent_id: str | None = None) -> FolderNode
    def rename(self, folder_id: str, name: str) -> FolderNode
    def move(self, folder_id: str, new_parent_id: str | None) -> FolderNode
    def delete(self, folder_id: str) -> None
    def assign_course(self, course_id: str, folder_id: str | None) -> None
    def folder_of(self, course_id: str) -> str | None
    def descendant_ids(self, folder_id: str) -> set[str]
    def path_of(self, folder_id: str) -> list[FolderNode]
    def remove_missing_courses(self, course_ids: set[str]) -> int
    def set_last_folder(self, folder_id: str | None) -> None
    def last_folder(self) -> str | None
```

校验规则：

- 名称先做空白和 Windows 非法字符清洗，规则与课程名一致。
- 同级重名自动追加 `-2`、`-3`。
- 文件夹不能移动到自身或自己的后代，否则抛 `ValueError`。
- 删除文件夹时，子文件夹和课程上移到父级；根级文件夹的内容回到“未分类”。
- 删除文件夹不删除任何课程。

## 课程库服务集成

`CourseSummary` 增加 `folder_id: str = ""`。

`LibraryFilter` 增加：

- `folder_id: str | None = None`：`None` 表示“全部课程”。
- `include_descendants: bool = False`：浏览时只看直属课程；搜索时看文件夹子树。

`LibraryService.refresh()`：

1. 扫描课程。
2. 清理已不存在课程的归属记录。
3. 为每门课程合并 `folder_id`。
4. 返回带归属的摘要。

`LibraryService.search()` 在现有标签、状态、人设、资料、搜索和排序之前先按文件夹过滤。

## 界面

课程库页面改成左右布局：

- 左侧文件夹树：`全部课程`、`未分类`、多级文件夹。
- 右侧顶部：返回、上一级、可点击面包屑、新建文件夹按钮。
- 右侧内容：子文件夹在前，课程在后；网格和列表视图都保留。
- 双击文件夹进入；双击课程打开。
- 单击选中，右键菜单；`F2` 重命名，`Delete` 删除，`Ctrl+Shift+N` 新建文件夹，`Enter` 打开。
- 文件夹节点显示名称和含子级的课程数。
- 网格视图新增轻量文件夹卡片；列表视图在课程前插入文件夹行。

拖拽：

- 课程可拖到左侧文件夹树或右侧当前文件夹空白处。
- 文件夹可拖到左侧文件夹树，调整父级。
- 使用独立 MIME 类型，避免和系统文件拖拽混淆。
- 拖拽移动后立即刷新树、面包屑和课程区。

新建课程：

- 当前选中某个文件夹时，课程创建完成后自动归入该文件夹。
- 当前是“全部课程”或“未分类”时，新课程进入“未分类”。

## 备份与恢复

- `backup_library()` 额外写入 `.library/folders.json`。
- `restore_from_zip()` 允许恢复 `.library/folders.json`，并保持隐藏目录位置。
- 整库恢复时以备份中的文件夹结构为准；单课程备份不包含文件夹结构。

## 错误处理

- 文件夹名非法：界面提示，不写盘。
- 移动到自身后代：界面提示，不写盘。
- `folders.json` 解析失败：按空树启动，首次保存时重建。
- 保存失败：抛出异常给界面，状态栏显示失败原因，内存树保持可用。

## 测试

- `tests/unit/test_folder_service.py`：创建、嵌套、重名、重命名、移动、防循环、
  删除上移、持久化、课程归属、清理失效课程、上次选中文件夹。
- `tests/unit/test_library_folders.py`：直属过滤、子树搜索、未分类、排序叠加。
- `tests/unit/test_m9_cards.py` / `tests/unit/test_ui_m0.py`：文件夹树、面包屑、
  选择过滤、双击进入、课程拖拽入口。
- `tests/unit/test_backup_restore_folders.py`：整库备份包含文件夹结构，恢复后位置正确。

## 验收

- 多级文件夹可新建、重命名、删除、拖拽移动。
- 重启应用后文件夹层级和课程归属保留。
- 父文件夹只显示直属课程，搜索覆盖子树。
- 新课程进入当前文件夹。
- 删除文件夹不删除课程。
- 整库备份恢复后层级保留。
- 原有课程库、标签、搜索、排序、备份测试全部通过。
