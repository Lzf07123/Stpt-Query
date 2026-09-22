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
# 网络日历订阅源把长期密钥放在路径里（/cal/<token>.ics）：日志中必须整体掩码
_CALENDAR_FEED_PATH = re.compile(r"(/cal/)[A-Za-z0-9_-]{8,}(\.ics)")


def redact_feed_paths(message: str) -> str:
    """掩码订阅源路径中的长期令牌（nginx 已关闭该路径访问日志，应用日志同样不得留存）。"""
    return _CALENDAR_FEED_PATH.sub(r"\1[redacted]\2", str(message or ""))


def _redact_sensitive_log_message(message: str) -> str:
    """掩码 stdout/集中日志中可能出现的短码、会话与令牌参数。"""
    message = _SENSITIVE_LOG_QUERY.sub(r"\1***", message)
    message = _SENSITIVE_LOG_BEARER.sub(r"\1***", message)
    return redact_feed_paths(message)


class _SensitiveLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        rendered = record.getMessage()
        redacted = _redact_sensitive_log_message(rendered)
        if redacted != rendered:
            record.msg = redacted
            record.args = None
        return True


class _AccessLogRedactionFilter(logging.Filter):
    """uvicorn 访问日志把请求路径放进 record.args，需在格式化前改写参数。

    默认 ``uvicorn app.main:app`` 会记录完整 request target，若只依赖 nginx
    ``access_log off``，``/cal/<token>.ics`` 仍会进入容器 stdout 与集中日志。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 5 and isinstance(args[2], str):
            values = list(args)
            values[2] = redact_feed_paths(values[2])
            record.args = tuple(values)
        elif isinstance(args, dict):
            for key in ("path", "request_line", "request_uri"):
                value = args.get(key)
                if isinstance(value, str):
                    args[key] = redact_feed_paths(value)
        return True


def install_access_log_redaction() -> None:
    """把订阅源脱敏过滤器挂到 uvicorn 访问日志（日志器与已存在的处理器都挂）。"""
    logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(item, _AccessLogRedactionFilter) for item in logger.filters):
        logger.addFilter(_AccessLogRedactionFilter())
    for handler in list(logger.handlers):
        if not any(isinstance(item, _AccessLogRedactionFilter) for item in handler.filters):
            handler.addFilter(_AccessLogRedactionFilter())


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
    install_access_log_redaction()
