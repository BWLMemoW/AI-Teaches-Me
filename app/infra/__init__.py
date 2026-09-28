"""基础设施层：文件读写、索引与检索、解析、密钥、沙箱、日志、搜索适配。

计划模块（见《技术文档》§2.3）：
  file_repository.py  课程目录读写（原子写）
  sqlite_index.py     索引与 FTS5（trigram）检索
  material_parser.py  PDF / PPTX / DOCX / 图片解析
  secret_store.py     DPAPI / keyring 密钥存储
  tool_executor.py    工具执行器：并行 / 超时 / 缓存 / 预算 / 不可信包装
  svg_sanitizer.py    SVG 白名单清洗与体积校验
  svg_renderer.py     SVG → PNG 离屏渲染（自检与占位图）
  sandbox.py          动画 HTML 校验与清洗
  web_search.py       搜索后端适配（千帆 / 托管 / 第三方 / SearXNG）
  logging.py          结构化脱敏日志
"""
