"""结构化脱敏日志（《技术文档》§2.3 / §3.2；《项目文档》§14.2 与 T-07）。

规则：``Authorization`` 头、``api_key`` 字段与疑似密钥长串一律替换为 ``***``；
日志按天切割、默认保留 30 天（``privacy.log_retention_days``）。
"""

from __future__ import annotations

import logging
import logging.handlers
import re
from pathlib import Path

REDACTED = "***"
LOGGER_NAME = "atm"

# 依次应用；带分组的前两个规则保留前缀（authorization / api_key），只抹掉值
_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?[A-Za-z0-9._\-]{6,}"), True),
    (re.compile(r"(?i)(api[_-]?key\"?\s*[:=]\s*\"?)[A-Za-z0-9._\-]{6,}"), True),
    (re.compile(r"\bsk-[A-Za-z0-9._\-]{6,}"), False),
    (re.compile(r"\b(?:sk|pk|rk|key)-[A-Za-z0-9]{16,}"), False),
)


def redact(text: str) -> str:
    """把文本里的疑似密钥替换为 ``***``。"""
    result = text
    for pattern, has_prefix in _PATTERNS:
        if has_prefix:
            result = pattern.sub(lambda match: match.group(1) + REDACTED, result)
        else:
            result = pattern.sub(REDACTED, result)
    return result


class RedactingFilter(logging.Filter):
    """在写盘前对整条日志做脱敏（符合 T-07：输出文件无明文命中）。"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            rendered = record.getMessage()
        except Exception:
            return True
        record.msg = redact(rendered)
        record.args = ()
        return True


def configure_logging(
    log_dir: str | Path,
    *,
    level: int = logging.INFO,
    retention_days: int = 30,
    name: str = LOGGER_NAME,
) -> logging.Logger:
    """配置按天切割的脱敏文件日志，返回 logger（可重复调用，不会叠加 handler）。"""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    handler = logging.handlers.TimedRotatingFileHandler(
        filename=str(directory / "app.log"),
        when="midnight",
        backupCount=max(1, int(retention_days)),
        encoding="utf-8",
        delay=False,
    )
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(handler)
    return logger


def get_logger(name: str = LOGGER_NAME) -> logging.Logger:
    return logging.getLogger(name)


def shutdown_logging(logger: logging.Logger | None = None) -> None:
    """关闭并摘除 handler（测试与退出时用，确保文件句柄释放）。"""
    target = logger or logging.getLogger(LOGGER_NAME)
    for handler in list(target.handlers):
        handler.flush()
        handler.close()
        target.removeHandler(handler)
