"""运行 ID 与日志：替代 Dify 系统变量 sys.workflow_run_id。"""
from __future__ import annotations

import logging
import re
import uuid

LOG = logging.getLogger("edu-query-app")
_SENSITIVE_LOG_QUERY = re.compile(
    r"(?i)([?&](?:password|passwd|pwd|authorization|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|secret|session|token|jump[_-]?code|code)=)[^&\s]+"
)
_SENSITIVE_LOG_BEARER = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}")


def _redact_sensitive_log_message(message: str) -> str:
    """掩码 stdout/集中日志中可能出现的短码、会话与令牌参数。"""
    message = _SENSITIVE_LOG_QUERY.sub(r"\1***", message)
    return _SENSITIVE_LOG_BEARER.sub(r"\1***", message)


class _SensitiveLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = _redact_sensitive_log_message(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = None
        return True


def new_run_id() -> str:
    """每次查询生成 32 位十六进制运行 ID，贯穿日志与响应，便于排查定位。"""
    return uuid.uuid4().hex


def setup_logging(level: int = logging.INFO) -> None:
    """初始化日志；密码/session/token 等敏感字段由上游与各模块脱敏处理。"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    loggers = [logging.getLogger()]
    loggers.extend(
        logger for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    for logger in loggers:
        for handler in logger.handlers:
            if not any(isinstance(item, _SensitiveLogFilter) for item in handler.filters):
                handler.addFilter(_SensitiveLogFilter())
