"""Agent 编排层：Director / Teacher / Illustrator / Animator / Tutor / Q&A / NoteKeeper。

计划模块（见《技术文档》§2.5、§4.5）：
  base.py         Agent 基类：上下文装配 + Tool Loop
  persona.py      人设模型与装配（YAML 头 + Markdown 正文）
  director.py     课程总监
  teacher.py      教师
  tutor.py        助教（小节内追问）
  qa.py           答疑顾问（小灶，仅限本课程）
  notes.py        笔记整理
  illustrator.py  SVG 图示与重画
  animator.py     HTML 微动画
  tools.py        工具注册表（16 个工具，见《项目文档》§8.6）
"""
