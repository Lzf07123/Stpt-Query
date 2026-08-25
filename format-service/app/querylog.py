"""查询日志：把每次查询记录为结构化 JSON 单行日志。

设计约束（对齐 AGENTS.md 硬性规则）：
- 只输出到 stdout，不落盘业务状态，不引入文件存储 / PV；
- 日志字段不含 password / session / token 等敏感值；
- 每个字段都可被集中日志系统（K8s/Fluentd/Promtail 等）稳定解析。
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict

LOG = logging.getLogger("edu-query-app.query")

# 结构性白名单之外的键即使误传也不会进入日志
_ALLOWED_FIELDS = {
    "event", "time", "run_id", "client_ip", "username", "option",
    "semesters", "weeks", "md2pdf", "check", "success", "kind",
    "elapsed_ms", "message",
}

_SENSITIVE_FIELDS = {"password", "session", "token", "authorization", "api_token"}


def _sanitize(entry: Dict[str, Any]) -> Dict[str, Any]:
    """仅保留白名单字段；敏感字段一律剔除（双保险）。"""
    clean: Dict[str, Any] = {}
    for key, value in entry.items():
        if key not in _ALLOWED_FIELDS or key in _SENSITIVE_FIELDS:
            continue
        clean[key] = value
    return clean


def log_query(entry: Dict[str, Any]) -> None:
    """输出一条查询日志（stdout JSON 单行）；永不抛异常打断业务。"""
    try:
        LOG.info(json.dumps(_sanitize(entry), ensure_ascii=False, default=str))
    except Exception:  # pragma: no cover - 日志失败不得影响查询主流程
        LOG.exception("查询日志写入失败")
