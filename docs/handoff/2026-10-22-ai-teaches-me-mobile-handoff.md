# AI Teaches Me 移动端项目工程交接文档

> 交接日期：2026-10-22  
> 交接对象：在新目录中启动的移动端项目  
> 当前仓库：`D:\+Coding\PyAI\Project\AI-Teaches-Me`  
> 当前分支：`main`  
> 当前 HEAD：`6e3df27`（注意：工作区还有大量未提交改动，正式交接前应先做一次干净快照或提交）

## 0. 先读这一页

这份文档用于把当前 PC 桌面版 AI Teaches Me 的工程资产、设计约束和迁移结论交给一个新项目。

结论先说清楚：

1. 可以迁移手机版，但**不是把 PySide6 桌面界面直接移植到手机**。
2. 优先推荐“PC/NAS/云端 Python 后端 + 手机伴学客户端”。
3. 如果必须手机完全离线独立使用，则优先做 Android，再考虑 iOS。
4. 课程文件格式、内容块协议、提示词、Agent 能力、网页渲染内核可以大量复用。
5. PySide6 页面、QtWebEngine 加载方式、Windows 文件系统操作、`keyring`/DPAPI 必须替换。

当前项目规模快照：

| 指标 | 数值 |
| --- | --- |
| 非 vendor 文件 | 约 222 个 |
| Python 文件 | 约 150 个 |
| UI 相关 Python 文件 | 约 32 个 |
| Service 文件 | 约 17 个 |
| 测试文件 | 约 58 个 |
| 可收集测试 | 807 项 |
| 当前验证 | 最近一次 `tests/unit + tests/integration` 全量通过 |

---

## 1. 当前产品与目标

AI Teaches Me 是一个 AI 驱动的个性化自学平台：

- 用户给出课程名、目标、资料或大纲。
- 课程总监 Agent 拆解章节、小节和知识点。
- 教师 Agent 生成讲义、例题、图示和追问。
- 课程以普通文件和 Markdown/JSON 持久化，可备份、迁移和离线阅读。
- 当前形态是 Windows 优先的桌面应用。

原始需求明确写了“本期不做移动端与浏览器 Web 版本”，但也明确要求 UI 层可替换、内容与渲染解耦：

- [项目文档.md](../项目文档.md:109)
- [技术文档.md](../技术文档.md:1519)

因此新项目不是从零发明产品，而是“复用核心资产，重做移动壳”。

---

## 2. 当前桌面版架构

当前技术栈：

- Python 3.11+
- PySide6 / Qt 6
- QtWebEngine（内嵌 Chromium）
- SQLite + FTS5 `trigram`
- 本地文件系统
- OpenAI 兼容 API
- pytest / pytest-qt

分层方向：

```text
app/ui            PySide6 页面、组件、主题、QtWebEngine 宿主
app/services      课程、讲义、笔记、答疑、导出、备份、文件夹等业务服务
app/agents        Director / Teacher / QA / Persona / Illustrator 等 Agent
app/llm           OpenAI 兼容客户端、流式事件、用量统计
app/infra         SQLite、文件仓库、密钥、日志、SVG、资料解析、沙箱
app/core          配置、路径安全、领域模型、路由、事件
app/web           lesson.html / renderer.js / theme.css / vendor 渲染资源
prompts           教师、总监、答疑、人设提示词
personas          内置人设
Courses/          用户课程数据，文件系统是唯一事实来源
data/             配置、索引、缓存、日志
```

关键原则：

1. 文件系统是课程事实来源，SQLite 是可重建缓存。
2. 写入路径必须经过路径清洗和边界校验。
3. 长任务应可取消、可恢复，生成内容完成一块就落盘。
4. UI 层不承载业务真相，服务层可无界面单测。
5. 课程内容与渲染解耦，HTML 渲染内核可被其他壳复用。

详细背景见：

- [技术文档.md](../技术文档.md:73)
- [技术文档.md](../技术文档.md:227)
- [项目文档.md](../项目文档.md:695)

---

## 3. 可以直接复用的资产

### 3.1 课程数据与内容协议

建议原样复用以下文件格式：

- `course.json`：课程元信息、标签、主题、人设、图标。
- `outline.json`：章节、小节、知识点、参考来源。
- `outline.md`：人读版大纲，可由 JSON 派生。
- `progress.json`：课程进度、复习队列、最近学习。
- `lesson.json`：小节状态、版本、掌握度、收藏。
- `script.blocks.jsonl`：结构化讲义内容块。
- `script.md`：人读版讲义。
- `notebook.md`：学习笔记。
- `qa/sessions.jsonl`：答疑会话。
- `materials/`：原始资料、解析稿、资料清单。
- `assets/`、`widgets/`：图标、SVG、动画、三维资产。
- `.library/folders.json`：虚拟文件夹树与课程归属。

原则：**课程包是跨平台协议，不是桌面 UI 的内部实现。**

### 3.2 内容块协议

`ContentBlock` 支持：

- `text`
- `callout`
- `analogy`
- `diagram`
- `image`
- `svg`
- `animation`
- `code`
- `quiz`
- `table`
- `summary`
- `next_preview`
- `ref`
- `error`
- `done`

移动端应与桌面端保持同一套块协议。不要为手机另造一套 Markdown 方言。

### 3.3 渲染内核

可复用：

- `app/web/lesson.html`
- `app/web/renderer.js`
- `app/web/theme.css`
- `app/web/vendor/` 下的 markdown-it、KaTeX、highlight.js
- `app/web/embed/model3d_viewer.html`

需要替换的只是宿主：

- 桌面：QtWebEngine + `runJavaScript`
- Android：Android WebView + `evaluateJavascript`
- iOS：WKWebView + `evaluateJavaScript`

渲染器目前通过 JS API 暴露能力，Python 侧主要调用 `runJavaScript`；移动端建议统一用 JSON 字符串过桥，避免平台对复杂对象序列化的差异。参考：

- [study_page.py](../../app/ui/pages/study_page.py:106)
- [renderer.js](../../app/web/renderer.js:536)

### 3.4 Agent 与提示词

建议复用：

- `app/agents/` 中的 Director、Teacher、QA、Persona、Illustrator
- `app/llm/` 中的 OpenAI 兼容客户端、流式事件、用量统计
- `prompts/` 全部提示词
- `personas/` 内置人设
- `tests/unit/test_m*` 中与课程、讲义、答疑行为有关的契约测试

这些是产品能力，不要在新项目里重新发明。

---

## 4. 必须替换的桌面能力

| 桌面能力 | 当前位置 | 移动端替代 |
| --- | --- | --- |
| PySide6 窗口与组件 | `app/ui/pages`、`app/ui/widgets` | Flutter / React Native / 原生 Kotlin/Swift |
| QtWebEngine | `app/ui/pages/study_page.py`、`export_helper.py` | Android WebView / WKWebView |
| `QFileDialog` | 课程、资料、备份、人设导入 | 系统文件选择器、分享面板、DocumentsProvider |
| `QDesktopServices.openUrl` | 打开目录、PDF | 系统分享/预览器 |
| Windows 回收站 | `app/infra/file_repository.py` | 应用内回收站 + 平台删除/恢复 |
| `keyring` / DPAPI | `app/infra/secret_store.py` | Android Keystore / iOS Keychain |
| `QThreadPool` | 多处 UI 长任务 | 持久化任务队列 + WorkManager / BGTask |
| 桌面拖拽 | 课程库文件夹 | 长按拖拽或“移动到文件夹”菜单 |
| Windows 路径假设 | `Path`、盘符、隐藏目录 | 应用沙箱、URI、文件描述符 |

特别注意：Windows 回收站实现使用了 `ctypes.windll.shell32`，移动端不可用。不要把 `file_repository.py` 原样复制到新项目。

---

## 5. 迁移路线选择

| 方案 | 复用程度 | 离线 | 工作量 | 建议 |
| --- | --- | --- | --- | --- |
| 直接 PySide6/Qt 移植 | 表面高，实际低 | 高 | 高且高风险 | 不推荐 |
| PC/NAS/云后端 + 手机薄客户端 | 最高 | 部分 | 中 | 首选 |
| 手机独立离线 App | 中高 | 高 | 中高 | Android 优先 |
| 全原生重写 + 共享协议 | 低 | 高 | 很高 | 长期产品化 |

### 5.1 推荐方案：后端 + 伴学客户端

保留现有 Python 服务作为后端，手机做：

- 课程库和文件夹树
- 讲义阅读、公式、代码、SVG、动画
- 进度、笔记、答疑
- 课程包导入/导出
- 最近课程离线缓存

优点：

- 最大限度复用现有 Agent、服务、数据格式和渲染器。
- API Key 留在 PC/NAS/服务器，不进手机。
- 课程生成、资料解析、PDF/PPT 处理继续在性能更好的设备上跑。
- 首版能最快验证手机端是否有价值。

缺点：

- 手机依赖网络；需要用离线课程包弥补。
- 需要做认证、TLS、任务状态和断线重连。

### 5.2 离线独立 App

如果目标是手机完全脱离 PC：

- Android 优先。
- 自带 SQLite，确保 FTS5 `trigram` 可用。
- 密钥使用 Keystore。
- 课程 zip 导入应用沙箱。
- AI 生成和资料解析做成可恢复任务。
- iOS 后续再做，需单独评估 Python 嵌入、后台任务和审核。

---

## 6. 新项目建议目录

建议新仓库按“协议/后端/移动端”拆，而不是把桌面版整个复制过去：

```text
atm-mobile/
├─ docs/
│  ├─ handoff.md                 # 本文件
│  ├─ api-contract.md
│  ├─ course-package.md
│  └─ decisions.md
├─ backend/                      # 可复用 Python 服务
│  ├─ app/
│  │  ├─ agents/
│  │  ├─ core/
│  │  ├─ infra/
│  │  ├─ llm/
│  │  ├─ services/
│  │  └─ web/
│  ├─ prompts/
│  ├─ personas/
│  └─ tests/
├─ mobile/                       # Flutter / React Native / 原生壳
│  ├─ lib/ 或 app/
│  └─ test/
├─ shared/                       # JSON Schema、示例课程包、契约夹具
└─ scripts/
```

如果选择纯离线 App，则把 `backend/app` 中可移动的服务拆成共享核心，UI 目录不要复制。

---

## 7. 移动端信息架构建议

建议首版做四个主入口：

1. **课程库**  
   文件夹树放在抽屉或顶部入口；课程卡片单列/双列；长按课程出现“移动到文件夹、重命名、删除、备份”。
2. **学习台**  
   左目录树改成章节抽屉；正文全宽；底部固定“上一节 / 下一节 / 完成 / 追问”。
3. **小灶**  
   全屏聊天；流式回答；引用卡片可展开；输入框避开键盘。
4. **设置**  
   API、主题、存储、导入导出、备份恢复、离线缓存、清理空间。

移动端不要照搬桌面侧栏、右键菜单和悬停交互。触屏原则：

- 单击进入，长按上下文菜单。
- 拖拽作为增强，不能成为唯一操作路径。
- 所有关键动作都要有菜单/按钮替代。
- 课程卡片统一尺寸，文字最多两行，标签单行收纳。

---

## 8. 建议 API 契约

如果走后端 + 客户端路线，先冻结协议，再写 App。

### 8.1 认证

- 单用户本地部署：一次性配对码换取长期 Bearer Token。
- 云端部署：账号登录 + Token。
- 所有请求走 HTTPS；局域网也不要裸 HTTP。
- 不在日志中输出 API Key、Token、课程正文全文。

### 8.2 课程与文件夹

```http
GET    /api/v1/courses
GET    /api/v1/courses/{course_id}
GET    /api/v1/courses/{course_id}/outline
GET    /api/v1/courses/{course_id}/lessons/{lesson_id}
GET    /api/v1/folders
PATCH  /api/v1/courses/{course_id}/progress
POST   /api/v1/courses/import
GET    /api/v1/courses/{course_id}/export
```

课程列表建议返回：

```json
{
  "id": "course-...",
  "name": "高等数学",
  "description": "",
  "folder_id": "f-...",
  "tags": ["数学"],
  "chapters": 6,
  "lessons": 24,
  "completed_lessons": 3,
  "updated_at": "2026-10-22T10:00:00+08:00"
}
```

### 8.3 答疑与长任务

答疑优先用 SSE 或 WebSocket 流式返回：

```text
event: lesson.delta
data: {"text":"..."}

event: tool.call
data: {"name":"search_materials","status":"running"}

event: lesson.done
data: {"usage":{"prompt_tokens":0,"completion_tokens":0}}
```

长任务统一返回 Job：

```http
POST /api/v1/jobs
GET  /api/v1/jobs/{job_id}
POST /api/v1/jobs/{job_id}/cancel
```

Job 状态：`queued / running / succeeded / failed / cancelled`。手机断线后可以按 `job_id` 恢复。

### 8.4 错误信封

```json
{
  "error": {
    "code": "COURSE_NOT_FOUND",
    "message": "课程不存在",
    "retryable": false,
    "detail": null
  }
}
```

不要把 Python traceback 直接返回给 App。

---

## 9. 课程包格式

建议把“课程目录”定义为跨平台课程包：

```text
<course-name>/
├─ course.json
├─ outline.json
├─ outline.md
├─ progress.json
├─ chapters/
│  └─ C1-章节/
│     ├─ chapter.json
│     └─ lessons/
│        └─ L1-1-小节/
│           ├─ lesson.json
│           ├─ script.md
│           ├─ script.blocks.jsonl
│           └─ notebook.md
├─ materials/
├─ assets/
├─ widgets/
├─ notes/
├─ qa/
└─ exports/
```

课程包导入要求：

- 拒绝 `..`、绝对路径、盘符、符号链接逃逸。
- 限制 zip 总大小、单文件大小和文件数量。
- 对 HTML/JS/SVG 做安全检查；默认禁止联网和任意导航。
- 导入前先解压到临时目录，校验通过后再原子移动到课程库。
- 同名课程自动追加后缀，不覆盖已有课程。

`.library/folders.json` 结构：

```json
{
  "schema_version": 1,
  "folders": [
    {
      "id": "f-...",
      "name": "数学",
      "parent_id": null,
      "sort_index": 0,
      "created_at": "2026-10-22T10:00:00+08:00"
    }
  ],
  "assignments": {
    "course-id": "f-..."
  },
  "last_folder_id": "f-..."
}
```

注意：当前整库备份已经能带 `folders.json`，但标签和智能视图还没有完整进入整库备份。做移动迁移前应补齐这一点。

---

## 10. 搜索与索引

桌面版用 SQLite FTS5：

```sql
CREATE VIRTUAL TABLE material_chunks USING fts5(
  material_id UNINDEXED,
  page UNINDEXED,
  heading,
  text,
  source UNINDEXED,
  tokenize='trigram'
);
```

移动端必须确认：

- Android 自带 SQLite 是否启用 FTS5。
- 是否支持 `trigram`。
- 中文查询 < 3 字是否回退 `LIKE`。
- 课程包导入后能否在合理时间内重建索引。

建议：

1. 自带 SQLite 版本，避免依赖系统 SQLite 编译选项。
2. 课程和资料文件是事实来源，索引随时可删重建。
3. 首版允许“无全文检索，只搜课程名/标签/章节标题”，后续再补 FTS5。
4. 大资料包在后台建索引，显示进度，不阻塞阅读。

参考实现：

- [sqlite_index.py](../../app/infra/sqlite_index.py:1)

---

## 11. 密钥与安全

桌面版使用 `keyring`，Windows 下走 DPAPI。移动端不可复用该实现。

要求：

- Android：Android Keystore + EncryptedSharedPreferences 或等价方案。
- iOS：Keychain。
- 后端方案：API Key 只存服务器，手机只保存配对 Token。
- 导出、日志、崩溃报告不得包含密钥。
- 课程 HTML/JS 是不可信内容，必须在沙箱中渲染。
- WebView 禁用任意导航、下载、弹窗和外部网络，只加载受控本地目录。
- 课程包导入时检查 zip-slip、脚本、外链和超大文件。

参考：

- [secret_store.py](../../app/infra/secret_store.py:1)
- [技术文档.md](../技术文档.md:2626)

---

## 12. 渲染与动画

复用现有 `lesson.html` / `renderer.js` 时，需要定义移动桥接协议：

```js
window.ATM.init();
window.ATM.render(blocks);
window.ATM.getState();
window.ATMNative.postMessage(JSON.stringify({ type: "ready" }));
```

移动原生侧：

- Android：`addJavascriptInterface` 或 `WebMessageListener`。
- iOS：`WKScriptMessageHandler`。
- 所有复杂对象序列化为 JSON 字符串。
- 不要让网页直接访问任意原生 API。

必须测试：

- KaTeX 字体是否从本地加载。
- Markdown 表格、代码高亮。
- SVG/HTML 动画。
- 三维查看器 WebGL。
- 深色/浅色主题。
- 手机横竖屏。
- 低端机滚动性能。

---

## 13. 后台任务

手机不能假设 AI 生成任务一直活着。

建议把任务模型做成：

```text
Job
├─ id
├─ type: generate_outline | generate_lesson | parse_material | export_pdf
├─ course_id
├─ lesson_id
├─ status
├─ progress
├─ created_at
├─ updated_at
└─ last_error
```

平台策略：

- Android：WorkManager + 前台服务（长任务显示通知）。
- iOS：BGProcessingTask + 可恢复任务；不要承诺无限时长。
- 客户端断线后重新连接任务状态。
- 生成内容完成一块就落盘，失败保留已有内容。
- 取消操作必须幂等。

---

## 14. PC 与手机的数据关系

### 14.1 首版：只读同步 / 手动导入

最简单：

- PC 负责创建和生成课程。
- 手机导入课程包，离线阅读。
- 手机进度导出为 `progress.json` 或增量文件。
- 用户手动选择“以手机进度为准 / 以 PC 进度为准”。

### 14.2 第二阶段：增量同步

建议先做“单用户、最后写入优先”的同步，不做 CRDT：

- 每门课、每小节记录 `updated_at`。
- 同一小节冲突时保留较新状态，并把旧版本写入 `history/`。
- 删除使用 tombstone，避免设备离线后把已删课程复活。
- 笔记冲突时可生成“冲突副本”，不要静默覆盖。
- 文件夹移动属于元数据，冲突时以最近一次操作为准。

---

## 15. 里程碑与验收

### M0：新仓库基线

- 新目录、README、交接文档、API 契约、课程包契约。
- CI 能跑 Python 核心测试和移动端空壳测试。
- 明确 Android/iOS 范围和离线范围。

### M1：后端与课程包

- 课程列表、文件夹树、课程详情、讲义读取 API。
- 课程 zip 导入/导出。
- 种子课程可导入。
- 路径安全、zip-slip、同名课程测试通过。

### M2：移动伴学 MVP

- 课程库、文件夹树、课程阅读。
- 进度、笔记、最近学习。
- 离线缓存最近课程。
- 渲染器在 Android WebView 中通过视觉验收。

### M3：答疑与流式输出

- SSE/WebSocket 流式答疑。
- 断线重连。
- 引用卡片、工具调用状态。
- API Key 不出现在客户端日志。

### M4：离线课程包

- 手机导入课程包。
- 本地索引/搜索。
- 离线阅读、笔记、进度。
- 导出进度回传 PC。

### M5：手机创建课程

- 资料上传、解析、目录生成。
- 后台 Job、取消、恢复。
- 课程落盘与备份。

### M6：iOS

- WKWebView 渲染。
- Keychain。
- 后台任务限制。
- App Store 审核与隐私说明。

---

## 16. 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 直接移植 PySide6 | 移动端不可用、工期失控 | 新壳 + 复用服务 |
| QtWebEngine 不可用 | 讲义不能渲染 | WebView + 现有 renderer.js |
| 手机 SQLite 无 FTS5 | 中文搜索失效 | 自带 SQLite / 首版简化搜索 |
| Python 嵌入困难 | Android/iOS 打包复杂 | 后端方案或原生重写服务 |
| 后台任务被系统挂起 | 生成中断 | Job 持久化 + 前台服务/可恢复任务 |
| API Key 泄露 | 用户损失 | Keychain/Keystore，后端保管 |
| 课程包路径攻击 | 任意文件写入 | safe_join、zip-slip 防护、大小限制 |
| 同步冲突 | 进度/笔记丢失 | 最后写入优先 + 冲突副本 |
| 大 PDF/PPT 内存不足 | 解析崩溃 | 后端处理，手机只存结果 |
| 动画/3D 性能差 | 体验差 | 低端机降级/静态替代 |

---

## 17. 新项目首先做的 10 件事

1. 确定“伴学客户端”还是“完全离线独立 App”。
2. 确定 Android 优先还是双端同步开发。
3. 建立新仓库目录和 CI。
4. 冻结课程包格式与 API 契约。
5. 复制 `app/core`、`app/services`、`app/agents`、`app/llm`、`app/infra` 中可复用部分。
6. 复制 `app/web`、`prompts`、`personas` 和种子课程。
7. 做课程 zip 导入/导出和路径安全测试。
8. 做一个最小移动壳：课程列表 + 讲义 WebView。
9. 接进度、笔记和离线缓存。
10. 再接答疑流式与长任务。

不要先做：

- 不要先写手机端课程生成全套 UI。
- 不要先做跨设备实时同步。
- 不要先做复杂动画编辑器。
- 不要把 Windows 文件系统代码直接复制过去。

---

## 18. 交接检查清单

- [ ] 当前仓库已做干净提交或完整打包，未提交改动不会丢。
- [ ] 确认新项目使用的课程格式版本。
- [ ] 确认种子课程已复制。
- [ ] 确认 `prompts/`、`personas/` 已复制。
- [ ] 确认渲染资源和 vendor 资源已复制。
- [ ] 确认 API Key 不进入新仓库。
- [ ] 确认 `Courses/`、`data/`、`logs/` 不进入源码分发。
- [ ] 确认移动端不依赖 Windows 路径。
- [ ] 确认课程包导入有 zip-slip 防护。
- [ ] 确认移动端有离线失败提示。
- [ ] 确认 UI 采用触屏操作，不依赖右键和悬停。
- [ ] 确认 LGPL / 第三方许可证合规。

---

## 19. 需要主人先拍板的问题

1. 手机版首版是“看课 + 复习 + 答疑”，还是必须能创建课程？
2. 先做 Android，还是 Android + iOS 一起做？
3. 是否接受 PC/NAS 作为后端，还是手机必须完全独立？
4. 课程和进度是否需要自动同步？
5. 是否允许手机上传 PDF/PPT 并在手机本地解析？
6. 是否要把当前代码提交成干净基线，再复制到新目录？

建议默认答案：

- 首版：看课 + 复习 + 答疑。
- 平台：Android 优先。
- 架构：PC/NAS 后端 + 手机伴学客户端。
- 离线：支持已下载课程离线阅读。
- 生成：首版留在 PC/后端。
- 同步：先手动导入导出，再做增量同步。

---

## 20. 参考文件

- [项目文档.md](../项目文档.md)
- [技术文档.md](../技术文档.md)
- [课程库文件夹设计](../superpowers/specs/2026-10-20-course-folders-design.md)
- [课程库文件夹实现计划](../superpowers/plans/2026-10-20-course-folders.md)
- [渲染器](../../app/web/renderer.js)
- [SQLite 索引](../../app/infra/sqlite_index.py)
- [密钥存储](../../app/infra/secret_store.py)
- [备份服务](../../app/services/backup_service.py)
- [恢复服务](../../app/services/restore_service.py)

这份文档可以直接复制到新项目的 `docs/handoff.md`，再按新项目实际情况补充 API 和移动端实现细节喵。
