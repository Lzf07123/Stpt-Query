"""健康探针、服务状态与查询日志路由（/health*、/metrics、/query-logs、/service-status）。"""
from __future__ import annotations

import asyncio

from fastapi import Depends, Response
from fastapi.responses import JSONResponse

from .context import AppContext
from .dependencies import REDIS_DEGRADED_MESSAGE
from .querylog import sanitize_entry


def register_health_routes(app, ctx: AppContext) -> None:
    @app.get("/health/live")
    async def health_live() -> dict:
        """K8s livenessProbe：进程存活即 200。"""
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict:
        """K8s readinessProbe：可选依赖降级时保持 ready，避免查询中断。"""
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
        """Prometheus 抓取端点；由内网网络/服务发现访问，不经 frontend 反代。"""
        return Response(content=ctx.metrics.render(),
                        media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/health")
    async def health() -> dict:
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

    @app.get("/health/public")
    async def public_health() -> dict:
        """公开站粗粒度链路状态；不暴露内部地址、错误体或凭据。"""
        return await ctx.public_health_payload()

    @app.get("/query-logs")
    async def query_logs_endpoint(_: None = Depends(ctx.require_auth)) -> dict:
        """返回最近查询日志（新→旧）；不含密码/session/token。"""
        items = [sanitize_entry(item) for item in await ctx.recent_query_logs()]
        return {"logs": items, "total": len(items)}

    @app.get("/service-status")
    async def service_status(
        _: None = Depends(ctx.require_auth),
    ) -> dict:
        """返回最近 100 次公开状态与全量已受理统计。

        缓存命中直接返回；并发 miss 经锁合并为一次重建（single-flight），
        避免同时全量扫描共享日志卷。
        """
        hit = await ctx.service_status_cache_hit()
        if hit is not None:
            return hit
        async with ctx.service_status_lock:
            # 等锁期间可能已有请求完成重建，二次确认避免重复扫描
            hit = await ctx.service_status_cache_hit()
            if hit is not None:
                return hit
            return await ctx.rebuild_service_status()
