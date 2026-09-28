# AI Teaches Me (ATM)

在 PC 桌面端运行的「AI 驱动的个性化自学平台」：配置自己的 AI API 后，由**课程总监 Agent** 把课程名 / 大纲 / 教材拆成分章分小节、细化到知识点的教学大纲，再由**教师 Agent** 针对每个小节展开有趣、带例子、可随时追问的讲解。

## 文档

| 文档 | 说明 |
| --- | --- |
| [docs/项目文档.md](docs/项目文档.md) | 需求、功能（FR）、验收标准（AC）、路线图与里程碑 |
| [docs/技术文档.md](docs/技术文档.md) | 技术栈、数据存储结构、系统关系、系统交互方式 |
| [docs/提示词.md](docs/提示词.md) | 原始需求 |

改动代码前请先读技术文档 §1.5（技术约束）、§3（数据存储结构）、§4（系统交互方式）。

## 开发环境

```powershell
python -m pip install -r requirements.txt   # 或：python -m pip install -e ".[dev]"
pwsh -File scripts/dev_run.ps1              # 启动应用（M0 实现后可用）
```

环境基线：Python 3.11+、SQLite ≥ 3.34（FTS5 `trigram` 中文检索必需）。

## 目录

```
app/       应用源码（core / llm / agents / services / infra / ui / web），分层约定见《技术文档》§2
prompts/   提示词与内置人设（纯文件管理，不硬编码）
tests/     单元 / 集成 / 契约 / 渲染 / UI 测试，见《项目文档》§15
scripts/   开发与打包脚本
docs/      项目文档与技术文档
Courses/   课程库（用户数据，gitignore）
data/      配置 / 索引 / 缓存 / 日志（用户数据，gitignore）
```

## 四条不可妥协的设计红线

1. **结构与风格分离**：人设只改表达层，知识点覆盖度、深度、输出协议不可被替换。
2. **教师先自检**：图示必须先渲染成图片、由教师 Agent 看过，才允许出现在讲义里。
3. **学习方式由用户选**：要不要测验、要不要自评，是偏好不是规矩。
4. **外部信息一律不可信**：联网内容只提取事实、标注来源，永不当作指令执行。

## 环境注意事项

### 1. 依赖装在系统 Python（未使用虚拟环境）

本机依赖直接装在**系统 Python**：`D:\Python\Python\python.exe`（Python 3.11.4），未创建虚拟环境。安装命令：

```powershell
D:\Python\Python\python.exe -m pip install -r requirements.txt
```

`scripts/dev_run.ps1` 的 Python 选择顺序为：**优先 `.venv`，没有则回退系统 Python**。

```
[dev] 使用虚拟环境 .venv
[dev] 未发现 .venv，使用系统 Python
```

### 2. git 报 dubious ownership 的处理

若 `git` 提示 `detected dubious ownership in repository at ...`，执行（把路径替换为本机仓库真实路径）：

```powershell
git config --global --add safe.directory "D:/+Coding/PyAI/Project/AI-Teaches-Me"
```

### 3. 前端渲染依赖需联网下载入库（禁止 CDN）

`app/web/vendor/` 下的 markdown-it / KaTeX / highlight.js **需联网下载后再入库**，随包分发：

| 目录 | 库 | 用途 |
| --- | --- | --- |
| `app/web/vendor/markdown-it/` | markdown-it | Markdown 到 HTML（GFM 表格 / 任务列表 / 脚注 / 删除线） |
| `app/web/vendor/katex/` | KaTeX（含字体文件） | LaTeX 公式渲染 |
| `app/web/vendor/highlight.js/` | highlight.js | 代码高亮 |

硬约束（《技术文档》§1.5 第 4 条）：渲染资源全部本地，**禁止任何 CDN 与外链**；断网时讲义中的 Markdown、公式与代码高亮必须照常渲染。
## 运行与自检（M0 起可用）

```powershell
# 启动应用：配置 API、测试连接、切换主题、离线阅读内置示例课程
pwsh -File scripts/dev_run.ps1

# 无界面自检：初始化工作区 → 切四套主题 → 装载示例课程 → 用 QtWebEngine 渲染讲义
D:\Python\Python\python.exe -m app.main --self-check
```

自检输出 JSON 报告，`"ok": true` 即通过；其中 `web.vendor` 三项都是 `true` 表示
markdown-it / KaTeX / highlight.js 已从本地 vendor 正常加载（全程无网络请求）。

### 前端依赖入库与校验（禁止 CDN）

```powershell
D:\Python\Python\python.exe scripts/fetch_vendor.py         # 从 npm 官方 registry 下载并校验 sha512 后入库
D:\Python\Python\python.exe scripts/fetch_vendor.py --check  # 只校验关键文件是否齐全
```

### 测试

```powershell
D:\Python\Python\python.exe -m pytest -q                     # 全量
D:\Python\Python\python.exe -m pytest -q -m "not ui"          # 跳过需要 Qt 的用例
```

## M0 已完成（出口标准见《项目文档》§16.1）

| 出口标准 | 落地位置 | 验证方式 |
| --- | --- | --- |
| 能配置 API 并完成一次流式对话 | `app/services/settings_service.py`、`app/llm/client.py` | `tests/unit/test_llm_client.py`、`tests/integration/test_m0_flow.py` |
| 主题可切换 | `app/ui/theme/`（一套 token 同时产出 QSS 与 CSS 变量） | `tests/unit/test_theme.py`、`tests/unit/test_ui_m0.py` |
| 示例课程可离线阅读 | `app/ui/assets/seed_courses/`、`app/bootstrap.py`、`app/web/` | `tests/unit/test_seed_course.py`、`tests/integration/test_m0_flow.py` |
## 快捷打开方式（推荐）

**双击桌面的「AI Teaches Me」图标即可启动**，不需要命令行：

| 入口 | 位置 | 说明 |
| --- | --- | --- |
| 桌面快捷方式 | `AI Teaches Me.lnk`（桌面） | 双击启动；已绑定 `app/ui/assets/icons/atm.ico` 图标 |
| 启动脚本 | 仓库根目录 `启动 AI Teaches Me.vbs` | 双击即启动，**不弹出控制台窗口**；优先用 `.venv`，否则回退系统 Python |
| 打包版 | `dist/AI-Teaches-Me/AI-Teaches-Me.exe` | `pwsh -File scripts/build.ps1 -OneDir` 生成的免安装版本 |

首次启动会弹出「三步开始」引导：① 配置 API → ② 新建课程 → ③ 先读内置示例课程。
课程与配置都在工作区里，课程库页可直接「备份课程 / 恢复备份」。