"""模型接入层：OpenAI 兼容统一客户端、流式解析、结构化输出、用量统计。

计划模块（见《技术文档》§4.4）：
  client.py  统一客户端（多档案切换、重试、取消）
  stream.py  SSE / NDJSON 流式解析与节流
  schema.py  结构化输出与修复重试
  usage.py   token 用量统计
"""
