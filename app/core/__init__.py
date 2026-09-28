"""核心层：领域模型、配置、错误类型、路径安全、模型路由、事件总线。

计划模块（见《技术文档》§2.3）：
  models.py   领域模型 Course / Chapter / Lesson / KnowledgePoint / ContentBlock
  config.py   配置读写与默认值
  errors.py   统一异常类型（可重试 / 不可重试）
  paths.py    路径解析与安全清洗（防路径穿越）
  routing.py  模型路由：按任务选择供应商档案
  events.py   应用内事件总线（Signal 封装）
"""
