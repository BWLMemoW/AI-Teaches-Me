"""模型路由的单元测试（《技术文档》§2.6 路由关系与四条规则）。"""

from __future__ import annotations

import pytest

from app.core.config import Capabilities, LLMProfile, default_config
from app.core.errors import ConfigError
from app.core.routing import ROUTE_TASKS, VISION_TASKS, ModelRouter


def _router_with_extra_profile() -> tuple[ModelRouter, LLMProfile]:
    config = default_config("D:/ws")
    extra = LLMProfile(
        name="hosted-profile",
        chat_model="hosted-chat",
        vision_model="hosted-vision",
        capabilities=Capabilities(vision=True, hosted_web_search=True),
    )
    config.llm_profiles.append(extra)
    return ModelRouter(config), extra


def test_defaults_fall_back_to_active_profile() -> None:
    router = ModelRouter(default_config())

    assert router.profile_name_for("teach") == "deepseek-default"
    assert router.profile_for("outline") is not None
    assert router.profile_for("outline").chat_model == "deepseek-flash"


def test_explicit_route_wins_over_active_profile() -> None:
    router, extra = _router_with_extra_profile()
    router.config.routing.teach = extra.name

    assert router.profile_for("teach") is extra
    assert router.profile_for("illustrate").name == "deepseek-default"


def test_route_to_missing_profile_raises() -> None:
    router = ModelRouter(default_config())
    router.config.routing.teach = "ghost"

    with pytest.raises(ConfigError):
        router.profile_for("teach")


def test_unknown_task_raises_value_error() -> None:
    router = ModelRouter(default_config())
    with pytest.raises(ValueError):
        router.profile_name_for("cooking")


def test_web_search_route_is_unused_for_standalone_backends() -> None:
    router, _ = _router_with_extra_profile()

    for backend in ("qianfan", "tavily", "serper", "exa", "brave", "searxng", None):
        assert router.profile_for("web_search", web_search_backend=backend) is None


def test_web_search_route_required_for_hosted_backend() -> None:
    router, extra = _router_with_extra_profile()

    with pytest.raises(ConfigError):
        router.profile_for("web_search", web_search_backend="openai_hosted")

    router.config.routing.web_search = extra.name
    assert router.web_search_profile("openai_hosted") is extra


def test_vision_model_falls_back_to_chat_model() -> None:
    router = ModelRouter(default_config())
    profile = router.config.require_profile()
    assert profile.vision_model is None
    assert router.model_for("vision_review") == "deepseek-flash"

    profile.vision_model = "deepseek-vl"
    assert router.model_for("vision_review") == "deepseek-vl"


def test_text_task_ignores_vision_model() -> None:
    router = ModelRouter(default_config())
    router.config.require_profile().vision_model = "deepseek-vl"

    assert router.model_for("teach") == "deepseek-flash"
    assert router.model_for("teach", vision=False) == "deepseek-flash"


def test_can_review_vision_follows_capabilities() -> None:
    router = ModelRouter(default_config())
    assert router.can_review_vision() is True

    router.config.require_profile().capabilities.vision = False
    assert router.can_review_vision() is False


def test_route_task_constants_match_doc() -> None:
    assert ROUTE_TASKS == ("teach", "outline", "illustrate", "vision_review", "web_search")
    assert VISION_TASKS == ("vision_review",)
