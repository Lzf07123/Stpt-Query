"""管理后台路由：/admin/api/query-logs 与 /admin/api/metrics（需独立 ADMIN_TOKEN）。"""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime
from typing import Optional

from fastapi import Depends, Query

from .context import (MAX_LOG_SCAN_LIMIT, REDIS_OP_TIMEOUT_SECONDS,
                      AppContext, _dependency_public_state, _log_timestamp)
from .querylog import sanitize_entry
from .trace import LOG


def register_admin_routes(app, ctx: AppContext) -> None:
    @app.get("/admin/api/query-logs")
    async def admin_query_logs(
        _: None = Depends(ctx.require_admin),
        keyword: str = Query(default="", max_length=100),
        kind: str = Query(default="", max_length=60),
        option: str = Query(default="", max_length=30),
        success: Optional[bool] = None,
        time_from: Optional[datetime] = None,
        time_to: Optional[datetime] = None,
        scan_limit: int = Query(default=0, ge=0, le=100000),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        """管理端查询历史；优先读取文件通道，无文件时降级 Redis/内存。"""
        keyword = keyword.strip().casefold()
        kind = kind.strip().casefold()
        option = option.strip().casefold()
        from_ts = time_from.timestamp() if time_from else None
        to_ts = time_to.timestamp() if time_to else None
        scanned = 0

        def timestamp(item: dict) -> float:
            return _log_timestamp(item)

        def matches(item: dict) -> bool:
            entry_time = timestamp(item)
            if from_ts is not None and entry_time < from_ts:
                return False
            if to_ts is not None and entry_time > to_ts:
                return False
            if success is not None and bool(item.get("success")) != success:
                return False
            if kind and str(item.get("kind", "")).casefold() != kind:
                return False
            if option and str(item.get("option", "")).casefold() != option:
                return False
            if not keyword:
                return True
            haystack = " ".join(str(item.get(field, "")) for field in (
                "event", "run_id", "client_ip", "username", "kind", "message",
            )).casefold()
            return keyword in haystack

        writer = app.state.file_log_writer
        parse_errors = 0
        effective_scan_limit = min(
            scan_limit if scan_limit > 0 else max(1, ctx.cfg.admin_log_scan_limit),
            MAX_LOG_SCAN_LIMIT,
        )
        if writer is not None:
            source = "file"

            def scan_file() -> tuple[list[dict], int, int]:
                nonlocal parse_errors
                matched: list[dict] = []
                seen_run_ids: set[str] = set()
                seen = 0
                for line in writer.iter_collection_recent_lines():
                    if seen >= effective_scan_limit:
                        break
                    seen += 1
                    try:
                        item = sanitize_entry(json.loads(line))
                    except ValueError:
                        parse_errors += 1
                        continue
                    if not isinstance(item, dict) or not matches(item):
                        continue
                    run_id = str(item.get("run_id") or "")
                    if run_id and run_id in seen_run_ids:
                        continue
                    if run_id:
                        seen_run_ids.add(run_id)
                    matched.append((timestamp(item), run_id, item))
                parsed = [entry for _, _, entry in matched]
                return parsed, seen, parse_errors

            entries, scanned, parse_errors = await asyncio.to_thread(scan_file)
        elif app.state.redis_history is not None:
            source = "redis"
            try:
                snapshot = await asyncio.wait_for(
                    app.state.redis_history.recent(
                        "gw:v2:query-logs", "gw:query-logs",
                        min(100, effective_scan_limit)),
                    timeout=REDIS_OP_TIMEOUT_SECONDS)
            except Exception as exc:
                LOG.warning("管理端查询日志读取 Redis 失败：%s", exc.__class__.__name__)
                snapshot = []
            entries: list[dict] = [
                sanitize_entry(item) for item in snapshot if matches(item)
            ]
        else:
            source = "memory"
            snapshot = app.state.local_history.recent("gw:v2:query-logs")
            entries = [sanitize_entry(item) for item in snapshot if matches(item)]

        entries.sort(key=lambda item: (timestamp(item), str(item.get("run_id", ""))), reverse=True)
        success_count = sum(1 for item in entries if bool(item.get("success")))
        kinds: dict[str, int] = {}
        for item in entries:
            key = str(item.get("kind") or "unknown")
            kinds[key] = kinds.get(key, 0) + 1
        page = entries[offset:offset + limit]
        return {
            "logs": page,
            "total": len(entries),
            "scanned": scanned,
            "scan_limit": effective_scan_limit,
            "scan_truncated": scanned >= effective_scan_limit,
            "source": source,
            "parse_errors": parse_errors,
            "stats": {
                "success": success_count,
                "failure": len(entries) - success_count,
                "kinds": dict(sorted(kinds.items(), key=lambda pair: (-pair[1], pair[0]))),
            },
            "pagination": {
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < len(entries),
            },
        }

    @app.get("/admin/api/metrics")
    async def admin_metrics(_: None = Depends(ctx.require_admin)) -> dict:
        """容器/宿主机资源、日志存储、应用负载与服务组件状态。"""
        from .dependencies import REDIS_DEGRADED_MESSAGE

        history = app.state.resource_monitor.snapshot()
        if not history:
            history = [await asyncio.to_thread(app.state.resource_monitor.collect)]

        window_seconds = 300
        cutoff = time.time() - window_seconds
        recent_logs = await ctx.recent_query_logs()
        recent_entries = []
        for item in recent_logs:
            try:
                occurred_at = datetime.fromisoformat(
                    str(item.get("time", "")).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                occurred_at = 0.0
            if occurred_at >= cutoff:
                recent_entries.append(sanitize_entry(item))
        elapsed_values = sorted(int(item["elapsed_ms"]) for item in recent_entries
                                if isinstance(item.get("elapsed_ms"), (int, float)))
        success_count = sum(1 for item in recent_entries if bool(item.get("success")))
        token_total = sum(int(item["analysis_usage"]) for item in recent_entries
                          if isinstance(item.get("analysis_usage"), (int, float)))
        analysis_count = sum(1 for item in recent_entries if item.get("analysis"))
        p95 = elapsed_values[math.ceil(len(elapsed_values) * 0.95) - 1] if elapsed_values else None

        file_status = (
            "disabled" if app.state.file_log_writer is None
            else app.state.file_log_writer.status
        )
        if app.state.redis is not None:
            try:
                await asyncio.wait_for(app.state.redis.ping(),
                                       timeout=REDIS_OP_TIMEOUT_SECONDS)
                app.state.dependency_health.record("redis", "ok")
            except Exception:
                app.state.dependency_health.record(
                    "redis", "degraded", REDIS_DEGRADED_MESSAGE)

        public_payload = await ctx.public_health_payload()
        # 复用 public_health 载荷解析，替代此前手写的重复状态映射
        query_proxy_status, school_status, query_proxy_redis = \
            _dependency_public_state(public_payload)
        app.state.dependency_health.record("query_proxy", query_proxy_status)
        app.state.dependency_health.record("query_proxy_redis", query_proxy_redis)
        app.state.dependency_health.record("school_service", school_status)

        dependencies = app.state.dependency_health.snapshot(
            file_log_state=file_status)
        degradations = [item for item in dependencies
                        if item["level"] in ("warning", "danger")]
        return {
            "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "latest": history[-1] if history else None,
            "history": history,
            "application": {
                "window_seconds": window_seconds,
                "requests": len(recent_entries),
                "success": success_count,
                "failure": len(recent_entries) - success_count,
                "success_rate": round(success_count / len(recent_entries) * 100, 1)
                if recent_entries else None,
                "elapsed_p95_ms": p95,
                "analysis_count": analysis_count,
                "analysis_token_total": token_total,
            },
            "services": {
                "redis": next(item["status"] for item in dependencies if item["key"] == "redis"),
                "file_log": file_status,
                "query_proxy": next(item["status"] for item in dependencies if item["key"] == "query_proxy"),
                "query_proxy_redis": next(item["status"] for item in dependencies if item["key"] == "query_proxy_redis"),
                "school_service": next(item["status"] for item in dependencies if item["key"] == "school_service"),
                "llm": next(item["status"] for item in dependencies if item["key"] == "llm"),
                "global_concurrency": ctx.cfg.global_concurrency,
                "rate_limit_per_minute": ctx.cfg.rate_limit,
            },
            "dependencies": dependencies,
            "degradations": degradations,
        }
