"""设置服务（《技术文档》§2.3 / §4.2；UI 只通过它读写配置与密钥）。

约束：本模块**不导入 Qt**（§1.5 红线 2），测试与离线场景都能直接用。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from ..core.config import (
    AppConfig,
    GenerationConfig,
    LLMProfile,
    THEMES,
    extract_plaintext_secrets,
    load_config,
    save_config,
)
from ..core.errors import ConfigError
from ..infra.secret_store import SecretStore, mask_secret, ref_for_profile
from ..llm.client import ConnectionReport, LLMClient
from ..llm.usage import UsageTracker


class SettingsService:
    """配置与密钥的事务边界（§2.4：服务层负责事务，UI 不直接碰文件与凭据库）。"""

    def __init__(
        self,
        config: AppConfig,
        *,
        workspace_root: str | Path,
        store: SecretStore | None = None,
        usage: UsageTracker | None = None,
    ) -> None:
        self._config = config
        self._workspace_root = Path(workspace_root)
        self._store = store if store is not None else SecretStore()
        self._usage = usage if usage is not None else UsageTracker()

    # --- 读 -------------------------------------------------------------
    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def config(self) -> AppConfig:
        return self._config

    @property
    def store(self) -> SecretStore:
        return self._store

    def reload(self) -> AppConfig:
        self._config = load_config(self._workspace_root)
        return self._config

    def profile_names(self) -> list[str]:
        return self._config.profile_names()

    def profile(self, name: str | None = None) -> LLMProfile:
        return self._config.require_profile(name)

    def theme_key(self) -> str:
        return self._config.ui.theme

    def generation(self) -> GenerationConfig:
        return self._config.generation

    # --- 写 -------------------------------------------------------------
    def save(self) -> Path:
        """保存配置；顺带把任何明文密钥迁进凭据库（T-42），确保磁盘上无明文。"""
        extract_plaintext_secrets(self._config, self._store)
        return save_config(self._workspace_root, self._config)

    def set_active_profile(self, name: str) -> None:
        if self._config.profile(name) is None:
            raise ConfigError(f"找不到模型档案：{name}")
        self._config.active_profile = name

    def upsert_profile(self, profile: LLMProfile) -> None:
        existing = self._config.profile(profile.name)
        if existing is None:
            self._config.llm_profiles.append(profile)
            return
        index = self._config.llm_profiles.index(existing)
        self._config.llm_profiles[index] = profile

    def delete_profile(self, name: str) -> None:
        target = self._config.profile(name)
        if target is None:
            raise ConfigError(f"找不到模型档案：{name}")
        if len(self._config.llm_profiles) <= 1:
            raise ConfigError("至少要保留一个模型档案")
        self._config.llm_profiles.remove(target)
        self._store.delete_secret(ref_for_profile(target))
        if self._config.active_profile == name:
            self._config.active_profile = self._config.llm_profiles[0].name

    def set_theme(self, key: str) -> None:
        if key not in THEMES:
            raise ConfigError(f"未知主题：{key}")
        self._config.ui.theme = key

    def set_follow_system(self, value: bool) -> None:
        self._config.ui.follow_system = bool(value)

    def set_reduce_motion(self, value: bool) -> None:
        self._config.ui.reduce_motion = bool(value)

    def set_font_size(self, size: int) -> None:
        self._config.ui.font_size = max(12, min(24, int(size)))

    # --- 密钥 -----------------------------------------------------------
    def set_api_key(self, profile_name: str | None, value: str) -> str:
        """写入密钥到系统凭据库（不落明文），返回打码后的展示串。"""
        profile = self._config.require_profile(profile_name)
        ref = ref_for_profile(profile)
        profile.secret_ref = ref
        if value:
            self._store.set_secret(ref, value)
            return mask_secret(value)
        self._store.delete_secret(ref)
        return ""

    def clear_api_key(self, profile_name: str | None = None) -> None:
        profile = self._config.require_profile(profile_name)
        self._store.delete_secret(ref_for_profile(profile))

    def api_key_masked(self, profile_name: str | None = None) -> str:
        profile = self._config.require_profile(profile_name)
        return self._store.masked(ref_for_profile(profile))

    def has_api_key(self, profile_name: str | None = None) -> bool:
        profile = self._config.require_profile(profile_name)
        return self._store.has_secret(ref_for_profile(profile))

    def migrate_plaintext_secrets(self) -> list[str]:
        return extract_plaintext_secrets(self._config, self._store)

    # --- 测试连接（FR-02 / AC-04） --------------------------------------
    async def test_connection(
        self,
        *,
        profile_name: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> ConnectionReport:
        client = LLMClient(self._config, secret_store=self._store, usage=self._usage)
        return await client.probe_connection(
            profile=profile_name, api_key=api_key, base_url=base_url, model=model
        )

    def test_connection_sync(self, **kwargs: object) -> ConnectionReport:
        """同步包装（供工作线程调用；主线程不得阻塞等待，见红线 3）。"""
        return asyncio.run(self.test_connection(**kwargs))  # type: ignore[arg-type]
