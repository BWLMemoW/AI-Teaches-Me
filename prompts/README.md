# prompts —— 提示词与内置人设

提示词以**文件形式**管理，不硬编码在 Python 里，支持热重载与版本对比（见《项目文档》§10.1）。

```
prompts/
├─ director/    system.md · clarify.md · outline_schema.md · refine.md
├─ teacher/     skeleton.md · protocol.md · depth_minimal.md · depth_standard.md · depth_deep.md
├─ illustrator/ system.md · review.md
├─ animator/    system.md
├─ tutor/       system.md
├─ qa/          system.md
├─ notes/       system.md
├─ personas/    内置人设（6 个，见《项目文档》§10.6）
└─ shared/      safety.md · style.md · coverage.md · untrusted.md
```

装配顺序（低优先级在前，见《技术文档》§4.4.2）：

```
shared/safety.md → shared/untrusted.md → shared/style.md → teacher/skeleton.md
  → <工作区>/personas/<当前人设>.md      ← 只影响表达层
  → teacher/depth_<档位>.md → teacher/protocol.md → shared/coverage.md
```

硬边界：`skeleton.md`（教学骨架）与 `protocol.md`（NDJSON 输出协议）**不可被人设覆盖**。
人设正文命中越权规则时，装配阶段剔除该行并提示「已忽略 N 条与教学规则冲突的指令」。

> 待办：目录与文件尚未创建。提示词草案见《项目文档》§10.2（总监）/ §10.3（教师）/ §10.4（动画），属 M2 实现内容。
