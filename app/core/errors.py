"""统一异常类型（可重试 / 不可重试）。

出处：《技术文档》§4.4.1（错误分类与重试策略）、§1.5（红线 7 密钥不落明文）、
《项目文档》§13.1「LLM 统一客户端」与 §15.2 的故障类用例（T-01/T-02）。

分层约定：本模块只依赖标准库，不导入 Qt、不导入 LLM SDK；
各家 SDK 的异常由 app/llm/client.py 翻译成本模块的异常类型。
"""

from __future__ import annotations

from typing import Final

# 《技术文档》§4.4.1 错误类别表
AUTH_STATUSES: Final[frozenset[int]] = frozenset({401, 403})
RETRYABLE_STATUSES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

# 超时 / 连接类异常的类名（openai 与 httpx 的写法都列上，避免耦合具体 SDK）
TIMEOUT_NAMES: Final[frozenset[str]] = frozenset(
    {
        "APITimeoutError",
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadTimeout",
        "WriteTimeout",
        "PoolTimeout",
        "ReadError",
        "RemoteProtocolError",
        "TimeoutException",
        "TimeoutError",
        "ConnectionError",
        "ConnectionResetError",
    }
)
AUTH_NAMES: Final[frozenset[str]] = frozenset({"AuthenticationError", "PermissionDeniedError"})
RATE_LIMIT_NAMES: Final[frozenset[str]] = frozenset({"RateLimitError"})


class AtmError(Exception):
    """项目内所有自定义异常的基类。"""

    retryable: bool = False
    user_message: str = "发生未知错误"

    def __init__(self, message: str = "", *, detail: str = "") -> None:
        super().__init__(message or self.user_message)
        self.detail = detail

    def explain(self) -> str:
        """给用户看的一句话（界面展示用，不直接把英文堆栈丢给用户）。"""
        return self.user_message


class InvalidName(AtmError):
    """课程名 / 文件名非法（空、Windows 保留名、含路径分隔符等）。"""

    user_message = "名称不合法，请换一个名字"


class PathEscape(AtmError):
    """解析后的路径落在工作区根目录之外。"""

    user_message = "路径越界，已拒绝写入"


class ConfigError(AtmError, ValueError):
    """配置缺失、字段非法或取值不在枚举内。"""

    user_message = "配置有问题"


class ConfigTooNew(ConfigError):
    """配置文件版本高于应用支持的版本（《项目文档》§18.6：进入只读模式）。"""

    user_message = "配置文件由更新版本的应用创建，请升级应用后再写入"


class Cancelled(AtmError):
    """用户主动取消长任务（取消后保留已生成内容）。"""

    user_message = "已取消"


class ProviderError(AtmError):
    """模型供应商相关错误的基类。"""

    status: int | None = None

    def __init__(self, message: str = "", *, detail: str = "", status: int | None = None) -> None:
        super().__init__(message, detail=detail)
        self.status = status


class AuthError(ProviderError):
    """401 / 403：配置问题，重试无用。"""

    retryable = False
    user_message = "API Key 或 Base URL 可能有问题，已停止重试，请到设置页检查"


class RetryableError(ProviderError):
    """429 / 5xx / 网络超时：可指数退避重试。"""

    retryable = True
    user_message = "模型服务暂时不可用，正在重试"


class FatalError(ProviderError):
    """其它不可重试的调用失败（含其它 4xx）。"""

    retryable = False
    user_message = "模型服务返回了无法处理的错误"


class TruncatedOutput(ProviderError):
    """模型输出被 ``max_tokens`` 顶掉：正文为空，或只吐了半截。

    典型现场是推理型模型（先思考几千 token）：思考与正文共用同一份预算，
    预算不够时 ``finish_reason="length"`` 且正文缺失／被截断。
    """

    retryable = False
    user_message = "模型这次没说完就被 token 上限截断了，请在设置里调大 max_tokens"


def status_of(exc: BaseException) -> int | None:
    """尽最大努力从异常里取出 HTTP 状态码。"""
    for attr in ("status_code", "status", "http_status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, int):
            return value
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    return code if isinstance(code, int) else None


def classify_exception(exc: BaseException) -> ProviderError:
    """把任意调用异常映射成项目内的错误类型（幂等：传入自身类型直接返回）。"""
    if isinstance(exc, ProviderError):
        return exc

    name = type(exc).__name__
    status = status_of(exc)
    detail = f"{name}: {exc}"
    body = getattr(getattr(exc, "response", None), "text", "") or ""
    if body:
        detail += " | 服务端返回：" + body[:200]

    if name in AUTH_NAMES or status in AUTH_STATUSES:
        return AuthError(
            f"模型服务拒绝了本次请求（{status or name}）", detail=detail, status=status
        )
    if name in RATE_LIMIT_NAMES or status == 429 or (status is not None and 500 <= status < 600):
        return RetryableError(
            f"模型服务限流或暂时故障（{status or name}）", detail=detail, status=status
        )
    if name in TIMEOUT_NAMES:
        return RetryableError("请求超时或连接失败", detail=detail, status=status)
    if status is not None:
        return FatalError(f"模型服务返回错误（{status}）", detail=detail, status=status)
    return FatalError("模型调用失败", detail=detail)


def is_retryable(exc: BaseException) -> bool:
    """是否应当重试（asyncio.CancelledError 不在此列，调用方需自行向上抛）。"""
    return classify_exception(exc).retryable
