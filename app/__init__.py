"""AI Teaches Me 应用包。

分层与依赖方向（见《技术文档》§2.1）::

    UI → 应用服务 → Agent / 领域 → 基础设施

约束：
  - ``app/ui/`` 不得直接 import LLM SDK（如 openai）；
  - ``app/services/`` 不得 import Qt，保证可无界面单测；
  - ``app/infra/`` 只做技术细节，不含业务规则。
"""
