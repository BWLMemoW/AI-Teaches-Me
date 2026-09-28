"""密钥安全存储（《技术文档》§3.9、§6 安全边界；《项目文档》§14.2 与 T-07）。

硬约束：密钥只存在于内存与系统凭据库中，**永不**写入 ``course.json`` / 日志 / 导出 /
备份包 / 崩溃报告。

M0 以 keyring 为主存储（Windows 走 DPAPI 支持的凭据库）。配置文件只保留
``secret_ref`` / ``api_key_ref`` 引用名（§3.5.5 约定 1）。
``data/secrets.bin`` 的加密备份留到 M6 打包阶段再实现。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

import keyring

from ..core.config import LLMProfile

SERVICE_NAME = "atm"
ENV_API_KEY = "OPENAI_API_KEY"
ENV_BASE_URL = "OPENAI_BASE_URL"


def mask_secret(value: str | None) -> str:
    """按 §3.9 的样式打码：``sk-****abcd``（界面只显示后 4 位）。"""
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    tail = value[-4:]
    if value.startswith("sk-"):
        return f"sk-****{tail}"
    return f"****{tail}"


@runtime_checkable
class SecretBackend(Protocol):
    """凭据库后端的最小接口（便于测试替换）。"""

    def set_password(self, service: str, username: str, password: str) -> None: ...

    def get_password(self, service: str, username: str) -> str | None: ...

    def delete_password(self, service: str, username: str) -> None: ...


class KeyringBackend:
    """默认后端：系统凭据库（Windows DPAPI / 凭据管理器的 keyring 实现）。"""

    def set_password(self, service: str, username: str, password: str) -> None:
        keyring.set_password(service, username, password)

    def get_password(self, service: str, username: str) -> str | None:
        return keyring.get_password(service, username)

    def delete_password(self, service: str, username: str) -> None:
        keyring.delete_password(service, username)


class InMemoryBackend:
    """测试与无凭据库环境用的内存后端（不落盘）。"""

    def __init__(self) -> None:
        self._data: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, password: str) -> None:
        self._data[(service, username)] = password

    def get_password(self, service: str, username: str) -> str | None:
        return self._data.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        self._data.pop((service, username), None)


def keyring_backend_available() -> bool:
    """当前系统是否真的挂上了可用凭据库（对应环境自检里的 keyring 用例）。"""
    return keyring.get_keyring().__module__ != "keyring.backends.fail"


class SecretStore:
    """按引用名读写密钥。"""

    def __init__(self, *, backend: SecretBackend | None = None, service: str = SERVICE_NAME) -> None:
        self._backend = backend if backend is not None else KeyringBackend()
        self._service = service

    @property
    def service(self) -> str:
        return self._service

    def set_secret(self, ref: str, value: str) -> None:
        if not ref:
            raise ValueError("secret_ref 不能为空")
        self._backend.set_password(self._service, ref, value)

    def get_secret(self, ref: str | None) -> str | None:
        if not ref:
            return None
        try:
            return self._backend.get_password(self._service, ref)
        except Exception:
            return None

    def has_secret(self, ref: str | None) -> bool:
        return bool(self.get_secret(ref))

    def delete_secret(self, ref: str) -> bool:
        if not ref:
            return False
        try:
            self._backend.delete_password(self._service, ref)
        except Exception:
            return False
        return True

    def masked(self, ref: str | None) -> str:
        return mask_secret(self.get_secret(ref))


def ref_for_profile(profile: LLMProfile) -> str:
    """档案的默认引用名：``atm/<profile.name>``（§3.5.5）。"""
    return profile.secret_ref or f"{SERVICE_NAME}/{profile.name}"


def resolve_api_key(
    profile: LLMProfile,
    store: SecretStore,
    *,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """取密钥：环境变量优先级最高且不落盘（§3.9）。"""
    environment = os.environ if env is None else env
    from_env = (environment.get(ENV_API_KEY) or "").strip()
    if from_env:
        return from_env
    return store.get_secret(ref_for_profile(profile))


def resolve_base_url(profile: LLMProfile, *, env: Mapping[str, str] | None = None) -> str:
    """取 Base URL：环境变量 ``OPENAI_BASE_URL`` 优先（§3.9）。"""
    environment = os.environ if env is None else env
    return (environment.get(ENV_BASE_URL) or "").strip() or profile.base_url
