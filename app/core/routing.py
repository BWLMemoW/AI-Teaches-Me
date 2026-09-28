"""模型路由（《技术文档》§2.6、《项目文档》§7.2 的 ``app/core/routing.py``）。

规则：

1. 路由未配置时回落到 ``active_profile``；
2. 视觉任务先看档案 ``vision_model``，为空则**沿用对话模型**（DeepSeek 自带多模态读图）；
3. ``routing.web_search`` 只在 ``backend = "openai_hosted"`` 时生效；
4. 默认后端千帆是独立 HTTP 端点，不占用供应商档案，因此该路由保持 ``None``。
"""

from __future__ import annotations

from .config import AppConfig, LLMProfile
from .errors import ConfigError

ROUTE_TASKS: tuple[str, ...] = ("teach", "outline", "illustrate", "vision_review", "web_search")
VISION_TASKS: tuple[str, ...] = ("vision_review",)
HOSTED_SEARCH_BACKEND = "openai_hosted"


class ModelRouter:
    """按任务名选择供应商档案与模型。"""

    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def config(self) -> AppConfig:
        return self._config

    def profile_name_for(self, task: str, *, web_search_backend: str | None = None) -> str | None:
        if task not in ROUTE_TASKS:
            raise ValueError(f"未知路由任务：{task!r}，可选：{'/'.join(ROUTE_TASKS)}")
        if task == "web_search":
            if web_search_backend != HOSTED_SEARCH_BACKEND:
                return None
            name = self._config.routing.web_search
            if not name:
                raise ConfigError(
                    "backend=openai_hosted 必须配置 routing.web_search，"
                    "指向 capabilities.hosted_web_search = true 的档案"
                )
            return name
        return getattr(self._config.routing, task) or self._config.active_profile

    def profile_for(self, task: str, *, web_search_backend: str | None = None) -> LLMProfile | None:
        name = self.profile_name_for(task, web_search_backend=web_search_backend)
        if name is None:
            return None
        profile = self._config.profile(name)
        if profile is None:
            raise ConfigError(f"routing.{task} 指向不存在的档案：{name}")
        return profile

    def model_for(self, task: str, *, vision: bool | None = None) -> str | None:
        """取该任务应使用的模型名；``vision`` 为真时优先 ``vision_model``。"""
        profile = self.profile_for(task)
        if profile is None:
            return None
        use_vision = task in VISION_TASKS if vision is None else vision
        if use_vision and profile.vision_model:
            return profile.vision_model
        return profile.chat_model

    def can_review_vision(self, task: str = "vision_review") -> bool:
        """该任务的档案能否读图（决定走读图自检还是启发式兜底）。"""
        profile = self.profile_for(task)
        return bool(profile and profile.capabilities.vision)

    def web_search_profile(self, backend: str | None = None) -> LLMProfile | None:
        return self.profile_for("web_search", web_search_backend=backend)
