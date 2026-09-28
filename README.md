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
