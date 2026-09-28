# 输出协议（严格 JSON，最高优先级）

**只输出一个 JSON 对象**，不要解释、不要 Markdown 代码围栏、不要多余文字。

```json
{
  "schema_version": 1,
  "learning_goals": ["..."],
  "estimated_hours": 12.5,
  "chapters": [
    {
      "id": "C1",
      "title": "章标题",
      "summary": "章摘要（1–2 句）",
      "lessons": [
        {
          "id": "L1-1",
          "title": "小节标题",
          "objectives": ["..."],
          "estimated_minutes": 12,
          "prerequisites": [],
          "source_refs": ["m1#p12"],
          "knowledge_points": [
            {"id": "kp-001", "title": "知识点", "summary": "一句话解释", "difficulty": "beginner", "must_master": true}
          ]
        }
      ]
    }
  ],
  "assessment_plan": ["每章末 5 题自测"]
}
```

字段枚举必须严格取自《项目文档》§18.6：`difficulty` 只能 `beginner` / `intermediate` / `advanced`。
