"""应用入口。

职责（见《技术文档》§2.3、§12）：
  1. 初始化 QApplication；
  2. 由 ``app.bootstrap`` 组装服务容器（工作区初始化 / 迁移检查 / 配置 / 主题 / 日志）；
  3. 显示主窗口。

当前状态：**仅工程骨架，M0 尚未实现**。实现顺序与出口标准见：
  - 《技术文档》§2.8（进程与线程）、§4.2（UI ↔ 应用服务）
  - 《项目文档》§16.1（M0：能配置 API 并完成一次流式对话、主题可切换、示例课程可离线阅读）
"""

from __future__ import annotations


def main() -> int:
    """启动应用。骨架阶段直接抛出未实现错误，避免误以为已经可用。"""
    raise NotImplementedError(
        "M0 尚未实现：请先完成 core / llm 层与主窗口骨架，"
        "见 docs/项目文档.md §16.1 与 docs/技术文档.md §2"
    )


if __name__ == "__main__":
    raise SystemExit(main())
