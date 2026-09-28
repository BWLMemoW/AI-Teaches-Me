# tests —— 测试分层

对应《项目文档》§15.1，目录与层级一一对应：

| 目录 | 层级 | 范围 | 目标 |
| --- | --- | --- | --- |
| `unit/` | 单元测试 | 领域模型、路径清洗、块解析、主题 token、进度计算 | 覆盖率 ≥ 80%（核心模块 ≥ 90%） |
| `integration/` | 集成测试 | 课程创建全流程、资料解析、落盘与重建索引 | 主链路必过 |
| `golden/` | 提示词回放 | 固定输入 → 检查输出协议与质量规则 | 提示词改动不劣化 |
| `fixtures/` | 测试素材 | 样例课程、样例 PDF/PPTX、录制式 LLM 桩 | — |

约定：

1. **无网络回归**：测试一律不得访问真实端点，用 `@pytest.mark.no_network` 标记（见《项目文档》§15.3），
   LLM 调用走录制式桩（pytest + httpx MockTransport）。
2. 契约测试（各家 OpenAI 兼容端点的重试与错误码映射）放 `integration/`；
   渲染测试（QWebEngine 截图比对）与 UI 测试（pytest-qt）放 `integration/`。
3. 临时文件一律用 pytest 的 `tmp_path`，不要写入真实 `Courses/`。
