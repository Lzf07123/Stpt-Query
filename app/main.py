"""FastAPI 入口：编排服务 HTTP 层（组装与启动编排）。

对齐原 dify-workflow-api 网关契约（/run、/health、/service-status），
客户端只需把后端地址从 Dify 应用换成本服务即可无缝迁移。

create_app 只负责装配：共享状态由 app.context.AppContext 承载，
路由按域拆分在 app.routes_*（run/jobs/health/admin/notices），
异步任务 worker 在 app.jobs_worker；行为契约与拆分前完全一致。
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic_settings import BaseSettings, SettingsConfigDict

from .calendar.routes import build_router as build_calendar_router
from .calendar.scheduler import CalendarScheduler
from .context import (REDIS_OP_TIMEOUT_SECONDS,  # noqa: F401  兼容旧导入面
                      AppContext, _JOB_ID_RE,  # noqa: F401
                      _memory_rate_allow,  # noqa: F401
                      _next_worker_idle_delay,  # noqa: F401
                      _parse_trusted_proxy_cidrs,  # noqa: F401
                      _raw_get,  # noqa: F401
                      _read_service_status_summary,  # noqa: F401
                      _sync_history_from_file,  # noqa: F401
                      WORKER_IDLE_DELAY_CAP,  # noqa: F401
                      WORKER_IDLE_DELAY_FLOOR,  # noqa: F401
                      build_app_context)
from .history import MemoryEventHistory, RedisEventHistory
from .jobs import JobStore
from .jobs_worker import start_job_workers
from .pipeline import ServiceError  # noqa: F401  兼容旧导入面
from .routes_admin import register_admin_routes
from .routes_health import register_health_routes
from .routes_jobs import register_job_routes
from .routes_notices import register_notice_routes
from .routes_run import register_run_routes
from .trace import LOG


class Settings(BaseSettings):
    """运行配置：全部来自环境变量 / .env。"""

    environment: str = "development"
    api_token: str = ""
    auto_rotate_token: bool = True
    # 留空 = 单体模式：查询代理以内嵌 ASGI 应用运行在同一进程内；
    # 显式配置地址则回退为 HTTP 调用（测试与过渡期直连使用）
    service_base_url: str = ""
    service_api_token: str = ""
    service_timeout: float = 60.0
    request_timeout: float = 100.0
    resource_monitor_interval_seconds: float = 5.0
    resource_monitor_history_size: int = 90
    concurrency_wait_timeout: float = 2.0
    global_concurrency: int = 4
    job_workers: int = 2
    job_pending_limit: int = 500
    job_result_ttl_seconds: int = 900
    job_stale_after_seconds: int = 200
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4.5-flash"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048
    llm_enable_thinking: bool = True
    llm_system_prompt: str = ""
    llm_timeout: float = 60.0
    llm_concurrency: int = 8
    pdf_concurrency: int = 2
    rate_limit: int = 30
    trust_proxy: bool = False
    trusted_proxy_cidrs: str = "172.16.0.0/12"
    redis_url: str = ""
    instance_id: str = ""
    history_sync_interval_seconds: float = 60.0
    admin_token: str = ""
    service_status_cache_seconds: float = 3.0
    file_log_enabled: bool = True
    file_log_path: str = "/tmp/edu-query/queries.jsonl"
    file_log_max_bytes: int = 50 * 1024 * 1024
    file_log_backup_count: int = 7
    service_status_scan_limit: int = 10000
    admin_log_scan_limit: int = 10000
    notice_fallback_path: str = "/var/lib/edu-query/notices/notices.jsonl"
    notice_active_max: int = 10
    notice_history_max: int = 500
    notice_compact_after: int = 2000
    # ---- 网络日历订阅（可选；启用需要固定主密钥）----
    calendar_enabled: bool = False
    calendar_master_key: str = ""
    calendar_db_path: str = "/var/lib/edu-query/calendar/calendar.db"
    calendar_config_path: str = "config/calendar.json"
    calendar_public_base_url: str = ""
    calendar_default_refresh_seconds: int = 43200
    calendar_min_refresh_seconds: int = 3600
    calendar_max_failures: int = 3
    calendar_pause_hours: int = 24
    calendar_max_per_owner: int = 5
    calendar_ttl_days: int = 210
    calendar_scheduler_interval_seconds: int = 60

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore")


# 对外跳转/下载透传白名单：免密登录桥接页与课表 PDF 下载必须经公网入口可达
PASSTHROUGH_ALLOWED = ("/jump/go", "/get_schedule/export")


def create_app(cfg: Optional[Settings] = None) -> FastAPI:
    cfg = cfg or Settings()
    ctx = build_app_context(cfg)

    async def _shutdown() -> None:
        calendar_task = getattr(app.state, "calendar_scheduler_task", None)
        if calendar_task is not None:
            calendar_task.cancel()
        for task in getattr(app.state, "job_worker_tasks", []):
            task.cancel()
        history_sync = getattr(app.state, "history_sync_task", None)
        if history_sync is not None:
            history_sync.cancel()
        notice_backup = getattr(app.state, "notice_backup_task", None)
        if notice_backup is not None:
            notice_backup.cancel()
        reaper = getattr(app.state, "job_reaper_task", None)
        if reaper is not None:
            reaper.cancel()
        tasks = list(getattr(app.state, "job_worker_tasks", []))
        if reaper is not None:
            tasks.append(reaper)
        if history_sync is not None:
            tasks.append(history_sync)
        if notice_backup is not None:
            tasks.append(notice_backup)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await ctx.resource_monitor.stop()
        if app.state.file_log_writer is not None:
            app.state.file_log_writer.close()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await ctx.upstream_client.aclose()
        await ctx.pipeline.aclose()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await ctx.startup(app)
        # 与拆分前一致：历史回填/通知备份任务在 ctx.startup 内创建，随后是任务 worker 与 reaper
        start_job_workers(app, ctx)
        calendar_stop = asyncio.Event()
        calendar_scheduler = (
            CalendarScheduler(ctx.calendar_service,
                              interval=cfg.calendar_scheduler_interval_seconds)
            if ctx.calendar_service is not None else None)
        if calendar_scheduler is not None:
            app.state.calendar_scheduler_task = asyncio.create_task(
                calendar_scheduler.run(calendar_stop))
        try:
            if ctx.internal_jwxt_app is None:
                yield
            else:
                # 内嵌查询代理的 lifespan：拉起健康巡检与缓存清理线程
                async with ctx.internal_jwxt_app.router.lifespan_context(
                        ctx.internal_jwxt_app):
                    yield
        finally:
            calendar_stop.set()
            await _shutdown()

    app = FastAPI(
        title="edu-query-app",
        version="0.1.0",
        description="教务查询编排服务：替代 Dify 工作流，直接编排登录/查询/渲染/分析/PDF。",
        docs_url=None if ctx.is_production else "/docs",
        redoc_url=None if ctx.is_production else "/redoc",
        openapi_url=None if ctx.is_production else "/openapi.json",
        lifespan=lifespan,
    )
    ctx.app = app

    # ---- app.state 镜像：与拆分前逐项一致（测试与诊断依赖这些名字）----
    app.state.pipeline = ctx.pipeline
    # 单体模式下内嵌的查询代理应用（HTTP 兼容模式为 None），供诊断与测试使用
    app.state.embedded_jwxt_app = ctx.internal_jwxt_app
    app.state.api_token = ctx.api_token
    app.state.rate_limit = max(1, cfg.rate_limit)
    app.state.trust_proxy = bool(cfg.trust_proxy)
    app.state.trusted_proxy_cidrs = _parse_trusted_proxy_cidrs(
        cfg.trusted_proxy_cidrs)
    app.state.metrics = ctx.metrics
    app.state.file_log_writer = ctx.file_log_writer
    app.state.resource_monitor = ctx.resource_monitor
    app.state.dependency_health = ctx.dependency_health
    app.state.query_slots = ctx.query_slots
    app.state.redis = ctx.redis_client
    app.state.job_store = JobStore(ctx.redis_client) if ctx.redis_client is not None else None
    app.state.redis_history = (
        RedisEventHistory(ctx.redis_client) if ctx.redis_client is not None else None)
    app.state.local_history = MemoryEventHistory()
    app.state.history_dirty = True
    app.state.service_status_cache = ctx.service_status_cache
    app.state.instance_id = ctx.instance_id
    app.state.notice_store = ctx.notice_store
    app.state.rate_hits = ctx.rate_hits
    ctx.metrics.observe_redis(False)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            ctx.metrics.observe_request(
                getattr(request.scope.get("route", None), "path", "unmatched"),
                500, time.perf_counter() - started)
            raise
        ctx.metrics.observe_request(
            getattr(request.scope.get("route", None), "path", "unmatched"),
            response.status_code, time.perf_counter() - started)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    # ---- 按域注册路由 ----
    register_job_routes(app, ctx)
    register_run_routes(app, ctx)
    register_health_routes(app, ctx)
    register_admin_routes(app, ctx)
    register_notice_routes(app, ctx)

    async def _passthrough(request: Request, path: str) -> Response:
        if path.split("?")[0] not in PASSTHROUGH_ALLOWED:
            raise HTTPException(status_code=404, detail="not found")
        qs = request.url.query
        try:
            upstream = await _raw_get(ctx.service_base_url, ctx.service_api_token,
                                      path + ("?" + qs if qs else ""),
                                      cfg.service_timeout, ctx.upstream_client)
        except httpx.HTTPError as exc:
            LOG.warning("透传上游失败 path=%s err=%s", path, exc.__class__.__name__)
            raise HTTPException(status_code=502, detail="上游服务不可用")
        headers = {}
        for name in ("Content-Type", "Content-Disposition", "Location",
                     "Cache-Control", "X-PDF-Cache-Hit"):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        if path.split("?")[0] == "/get_schedule/export" and upstream.status_code == 200:
            ctx.metrics.observe_pdf_cache(
                upstream.headers.get("X-PDF-Cache-Hit", "0") == "1")
        return Response(content=upstream.content, status_code=upstream.status_code,
                        headers=headers)

    @app.get("/jump/{rest:path}", include_in_schema=False)
    async def jump_passthrough(request: Request, rest: str) -> Response:
        """免密登录桥接页：/jump/go?code=.. 经编排后端转发查询代理。"""
        return await _passthrough(request, "/jump/" + rest)

    @app.get("/get_schedule/export", include_in_schema=False)
    async def schedule_export_passthrough(request: Request) -> Response:
        """课表 PDF 下载：/get_schedule/export?code=.. 原样透传。"""
        return await _passthrough(request, "/get_schedule/export")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>教务信息查询</title></head><body style='font-family:sans-serif;"
                "padding:2rem'><h1>教务信息查询服务</h1>"
                "<p>本服务已替代 Dify 工作流。请通过 POST /run 提交查询：</p>"
                "<pre>{'username': '学号', 'password': '密码', 'option': '成绩|课表', "
                "'md2pdf': false, 'check': false}</pre></body></html>")

    if ctx.calendar_service is not None:
        app.state.calendar_service = ctx.calendar_service
        app.include_router(build_calendar_router(ctx.calendar_service, ctx.require_auth,
                                                 ctx.require_admin,
                                                 ctx.calendar_rate_limit))

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
