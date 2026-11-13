"""健康探针与状态路由。

- 豁免端点（不套信封，探针/抓取语义）：/health/live、/health/ready、/health、/metrics
- 统一信封端点（/api/v1/*）：/api/v1/status（公开聚合状态）、
  /api/v1/query-logs、/api/v1/service-status
"""
from __future__ import annotations

import asyncio

from fastapi import Depends, Response
from fastapi.responses import JSONResponse

from .api_semantics import ok_envelope
from .context import AppContext
from .dependencies import REDIS_DEGRADED_MESSAGE
from .querylog import sanitize_entry

# rebuild_service_status 产出中归入 data.stats 的统计键（success 计数歧义
# 消除：统一改名为 success_count）
_SERVICE_STATUS_STATS_KEYS = (
    "availability", "accepted_total", "accepted_success", "accepted_failure",
    "source", "scanned", "scan_limit", "scan_truncated", "parse_errors",
)


def _service_status_data(payload: dict) -> dict:
    """service-status 聚合结果 → 信封 data（results/total + stats）。"""
    stats = {key: payload[key] for key in _SERVICE_STATUS_STATS_KEYS
             if key in payload}
    stats["success_count"] = int(payload.get("success") or 0)
    return {
        "results": payload.get("results") or [],
        "total": payload.get("total") or 0,
        "stats": stats,
    }


def register_health_routes(app, ctx: AppContext) -> None:
    @app.get("/health/live")
    async def health_live() -> dict:
        """K8s livenessProbe：进程存活即 200（豁免端点，不套信封）。"""
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict:
        """K8s readinessProbe：可选依赖降级时保持 ready，避免查询中断（豁免端点）。"""
        query_proxy_task = asyncio.create_task(ctx.probe_query_proxy())
        checks = {
            "config": "ok",
            "redis": "not-configured",
            "file_log": "disabled" if app.state.file_log_writer is None else "ok",
            "file_log_last_error": "",
            "query_proxy": "unknown",
        }
        if app.state.redis is not None:
            try:
                await app.state.redis.ping()
                checks["redis"] = "ok"
                app.state.dependency_health.record("redis", "ok")
            except Exception:
                checks["redis"] = "degraded"
                app.state.dependency_health.record(
                    "redis", "degraded", REDIS_DEGRADED_MESSAGE)
        ctx.metrics.observe_redis(checks["redis"] == "degraded")
        checks["query_proxy"] = await query_proxy_task
        checks["file_log"] = (
            "disabled" if app.state.file_log_writer is None
            else app.state.file_log_writer.status
        )
        checks["file_log_last_error"] = (
            app.state.file_log_writer.last_error
            if app.state.file_log_writer is not None else ""
        )
        app.state.dependency_health.record("query_proxy", checks["query_proxy"])
        ready = checks["query_proxy"] != "error"
        payload = {"status": "ready" if ready else "not-ready", **checks}
        return JSONResponse(status_code=503 if not ready else 200, content=payload)

    @app.get("/metrics", include_in_schema=False)
    async def prometheus_metrics() -> Response:
        """Prometheus 抓取端点；由内网网络/服务发现访问，不经 frontend 反代（豁免端点）。"""
        return Response(content=ctx.metrics.render(),
                        media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/health")
    async def health() -> dict:
        """容器/运维健康摘要（豁免端点，不套信封）。"""
        redis_status = "not-configured"
        file_log_status = (
            "disabled" if app.state.file_log_writer is None
            else app.state.file_log_writer.status
        )
        if app.state.redis is not None:
            try:
                await app.state.redis.ping()
                redis_status = "ok"
                app.state.dependency_health.record("redis", "ok")
            except Exception:
                redis_status = "degraded"
                app.state.dependency_health.record(
                    "redis", "degraded", REDIS_DEGRADED_MESSAGE)
        return {"status": "degraded" if redis_status in ("error", "degraded") or file_log_status == "error" else "ok",
                "auth_mode": "fixed-token" if ctx.cfg.api_token else "auto-token",
                "redis": redis_status,
                "file_log": file_log_status}

    @app.get("/api/v1/status")
    async def public_status() -> JSONResponse:
        """公开站粗粒度链路状态（信封）；不暴露内部地址、错误体或凭据。"""
        payload = await ctx.public_health_payload()
        return JSONResponse(content=ok_envelope(payload))

    @app.get("/api/v1/query-logs")
    async def query_logs_endpoint(_: None = Depends(ctx.require_auth)) -> JSONResponse:
        """返回最近查询日志（新→旧）；不含密码/session/token。"""
        items = [sanitize_entry(item) for item in await ctx.recent_query_logs()]
        return JSONResponse(content=ok_envelope({"logs": items, "total": len(items)}))

    @app.get("/api/v1/service-status")
    async def service_status(
        _: None = Depends(ctx.require_auth),
    ) -> JSONResponse:
        """返回最近 100 次公开状态与全量已受理统计（信封）。

        缓存命中直接返回；并发 miss 经锁合并为一次重建（single-flight），
        避免同时全量扫描共享日志卷。统计归入 data.stats（原顶层歧义的
        success 计数更名为 success_count）。
        """
        hit = await ctx.service_status_cache_hit()
        if hit is None:
            async with ctx.service_status_lock:
                # 等锁期间可能已有请求完成重建，二次确认避免重复扫描
                hit = await ctx.service_status_cache_hit()
                if hit is None:
                    hit = await ctx.rebuild_service_status()
        return JSONResponse(content=ok_envelope(_service_status_data(hit)))
