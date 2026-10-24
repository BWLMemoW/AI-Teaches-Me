# 输出协议（NDJSON，最高优先级）

**每行一个 JSON 对象**，不要 Markdown 代码围栏、不要解释。首行必须是 `meta`，最后一行必须是 `done`。

{"type":"meta","lesson_id":"L1-2","depth":"standard","version":1}
{"type":"text","md":"## 小节标题\n\n正文（支持 Markdown 与 $公式$）"}
{"type":"callout","style":"hook","md":"一句话钩子"}
{"type":"analogy","title":"接力赛","from":"矩阵乘法","to":"接力传递","md":"解释"}
{"type":"table","headers":["步骤","说明"],"rows":[["1","先对齐行列"]]}
{"type":"code","lang":"python","code":"W = m * g"}
{"type":"quiz","question":"题干","options":["A","B","C"],"answer":1,"explain":"为什么"}
{"type":"summary","points":["要点一","要点二"]}
{"type":"next_preview","md":"下一节会讲……"}
{"type":"done","tokens":0}

字段约束（严格取自《项目文档》§18.6）：

- `callout.style` ∈ `hook` / `tip` / `warn` / `question` / `fun`
- `diagram.kind` ∈ `svg` / `mermaid` / `ascii`
- `quiz.answer` 是选项下标（从 0 开始）
- 文字块里的换行用 `\n` 转义，不要真的换行
- 工具产出的视觉内容必须**原样搬运**：`plot_function` 给的 `svg`、`model_3d` 给的 `html`（已自带 CSP 头）
  与 `svg`，只做字段搬运，不要改写、不要截断、不要重画；三维交互的 HTML 一律放 `animation` 块的 `html` 字段
- 不要输出 `done` 之外的多余字段说明；`tokens` 可填 0
