"""统一记录外部依赖状态，供健康检查与管理后台降级提示使用。"""
from __future__ import annotations

from datetime import datetime
from threading import Lock
from typing import Dict, List, Literal


DependencyState = Literal["ok", "degraded", "error", "not-configured", "unknown"]

_DEFAULT_MESSAGES = {
    "redis": {
        "ok": "共享队列与历史正常",
        "degraded": "Redis 不可用，查询继续；限流、历史与异步任务降级",
        "error": "Redis 不可用",
        "not-configured": "未配置 Redis，仅支持单副本历史与同步查询",
    },
    "query_proxy": {
        "ok": "查询代理可达",
        "degraded": "查询代理部分能力异常，已执行可用性降级",
        "error": "查询代理不可达",
        "unknown": "尚未探测查询代理",
    },
    "query_proxy_redis": {
        "ok": "查询代理共享状态正常",
        "degraded": "查询代理 Redis 不可用，已降级为本副本内存状态",
        "error": "查询代理 Redis 不可用",
        "not-configured": "查询代理未配置 Redis",
        "unknown": "尚未探测查询代理共享状态",
    },
    "school_service": {
        "ok": "学校服务可达",
        "degraded": "学校服务响应异常",
        "error": "学校服务不可达",
        "unknown": "尚未探测学校服务",
    },
    "llm": {
        "ok": "成绩分析 LLM 可用",
        "degraded": "成绩分析 LLM 不可用，已降级为纯成绩表",
        "error": "成绩分析 LLM 不可用",
        "not-configured": "未配置 LLM，成绩分析保持关闭",
    },
    "file_log": {
        "ok": "查询日志文件通道正常",
        "degraded": "查询日志文件通道异常，已降级为内存与 stdout",
        "error": "查询日志文件通道异常",
        "disabled": "查询日志文件通道未启用",
    },
}

_LEVELS = {
    "ok": "success",
    "degraded": "warning",
    "error": "danger",
    "not-configured": "info",
    "unknown": "info",
}

_DISPLAY_NAMES = {
    "redis": "编排 Redis",
    "query_proxy": "查询代理",
    "query_proxy_redis": "查询代理 Redis",
    "school_service": "学校服务",
    "llm": "成绩分析 LLM",
    "file_log": "文件日志",
}


class DependencyHealth:
    """线程安全保存最近一次外部依赖状态和降级说明。"""

    def __init__(self, initial: Dict[str, DependencyState]) -> None:
        self._lock = Lock()
        self._states = dict(initial)

    def record(self, key: str, state: DependencyState,
               message: str | None = None) -> None:
        if key not in _DISPLAY_NAMES:
            return
        with self._lock:
            self._states[key] = state

    def snapshot(self, file_log_state: str | None = None,
                 checked_at: str | None = None) -> List[dict]:
        with self._lock:
            states = dict(self._states)
        if file_log_state is not None:
            states["file_log"] = (
                "ok" if file_log_state == "ok"
                else "disabled" if file_log_state == "disabled"
                else "error"
            )
        checked_at = checked_at or datetime.now().astimezone().isoformat(
            timespec="seconds")
        items = []
        for key in _DISPLAY_NAMES:
            state = states.get(key, "unknown")
            message = _DEFAULT_MESSAGES.get(key, {}).get(state, "状态未知")
            items.append({
                "key": key,
                "display": _DISPLAY_NAMES[key],
                "status": state,
                "level": _LEVELS.get(state, "info"),
                "message": message,
            })
        return items
