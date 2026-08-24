"""FastAPI 入口：编排服务 HTTP 层。

对齐原 dify-workflow-api 网关契约（/run、/health、/service-status），
客户端只需把后端地址从 Dify 应用换成本服务即可无缝迁移。
"""
from __future__ import annotations

import json
import secrets
import time
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm import LLMClient
from .pipeline import HTTPServiceClient, Pipeline
from .schema import QueryResult, WorkflowRequest
from .trace import LOG, setup_logging


class Settings(BaseSettings):
    """运行配置：全部来自环境变量 / .env。"""

    environment: str = "development"
    api_token: str = ""
    auto_rotate_token: bool = True
    service_base_url: str = "https://school.lizf.cn"
    service_api_token: str = ""
    service_timeout: float = 60.0
    llm_base_url: str = "https://api.deepseek.com"
    llm_api_key: str = ""
    llm_model: str = "deepseek-v4-flash"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 1024
    rate_limit: int = 30
    trust_proxy: bool = False
    redis_url: str = ""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore")


def _resolve_api_token(cfg: Settings) -> str:
    if cfg.auto_rotate_token:
        return secrets.token_urlsafe(32)
    return cfg.api_token.strip()


def _validate_production(environment: str, auto_rotate_token: bool, api_token: str) -> None:
    if environment.strip().lower() in ("production", "prod"):
        if auto_rotate_token:
            raise RuntimeError(
                "生产环境禁止 AUTO_ROTATE_TOKEN=true：多实例必须设置 "
                "AUTO_ROTATE_TOKEN=false 并使用固定 API_TOKEN")
        if not api_token.strip():
            raise RuntimeError("生产环境必须配置 API_TOKEN")


def create_app(cfg: Optional[Settings] = None) -> FastAPI:
    cfg = cfg or Settings()
    _validate_production(cfg.environment, cfg.auto_rotate_token, cfg.api_token)
    setup_logging()
    is_production = cfg.environment.strip().lower() in ("production", "prod")

    api_token = _resolve_api_token(cfg)
    llm = LLMClient(
        base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, model=cfg.llm_model,
        temperature=cfg.llm_temperature, max_tokens=cfg.llm_max_tokens)
    service = HTTPServiceClient(cfg.service_base_url, cfg.service_api_token, cfg.service_timeout)
    pipeline = Pipeline(service=service, llm=llm)

    app = FastAPI(
        title="edu-query-app",
        version="0.1.0",
        description="教务查询编排服务：替代 Dify 工作流，直接编排登录/查询/渲染/分析/PDF。",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    app.state.pipeline = pipeline
    app.state.api_token = api_token
    app.state.rate_limit = max(1, cfg.rate_limit)
    app.state.trust_proxy = bool(cfg.trust_proxy)

    _redis = None
    if cfg.redis_url.strip():
        try:
            import redis
            _redis = redis.Redis.from_url(cfg.redis_url, decode_responses=True,
                                          socket_connect_timeout=3.0, socket_timeout=3.0)
            _redis.ping()
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Redis 连接失败（REDIS_URL）：%s" % exc)
    app.state.redis = _redis

    results: deque = deque(maxlen=100)
    results_lock = Lock()
    rate_hits: dict = {}
    rate_lock = Lock()

    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
        token = api_token
        if token and (credentials is None or not secrets.compare_digest(
                credentials.credentials, token)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="token required")

    def _client_ip(request: Request) -> str:
        if app.state.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    def _check_rate_limit(ip: str) -> None:
        now = time.time()
        limit = app.state.rate_limit
        with rate_lock:
            hits = [t for t in rate_hits.get(ip, []) if now - t < 60]
            if len(hits) >= limit:
                raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                    detail="请求过于频繁，请稍后再试")
            hits.append(now)
            rate_hits[ip] = hits

    def _record(success: bool, kind: str) -> None:
        item = {"success": success, "kind": kind,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        with results_lock:
            results.append(item)
        if app.state.redis is not None:
            try:
                app.state.redis.lpush("gw:service-status", json.dumps(item, ensure_ascii=False))
                app.state.redis.ltrim("gw:service-status", 0, 99)
            except Exception:
                pass

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>教务信息查询</title></head><body style='font-family:sans-serif;"
                "padding:2rem'><h1>教务信息查询服务</h1>"
                "<p>本服务已替代 Dify 工作流。请通过 POST /run 提交查询：</p>"
                "<pre>{'username': '学号', 'password': '密码', 'option': '成绩|课表', "
                "'md2pdf': false, 'check': false}</pre></body></html>")

    @app.post("/run", response_model=QueryResult)
    @app.post("/v1/workflows/run", include_in_schema=False, response_model=QueryResult)
    async def run_workflow(body: WorkflowRequest, request: Request,
                           _: None = Depends(_require_auth)) -> QueryResult:
        _check_rate_limit(_client_ip(request))
        started = time.time()
        try:
            result = await app.state.pipeline.run(body)
            _record(bool(result.get("success")), result.get("kind", "unknown"))
            return QueryResult(**result)
        except Exception as exc:  # 兜底：未知异常也返回统一 JSON
            LOG.exception("run 执行异常")
            _record(False, "internal_error")
            return QueryResult(
                success=False, kind="internal_error",
                output="服务内部异常：%s" % exc.__class__.__name__,
                meta={"elapsed_ms": int((time.time() - started) * 1000)})

    @app.get("/health/live")
    def health_live() -> dict:
        """K8s livenessProbe：进程存活即 200。"""
        return {"status": "ok"}

    @app.get("/health/ready")
    def health_ready() -> dict:
        """K8s readinessProbe：配置就绪且可选 Redis 可用时 ready。"""
        checks = {"config": "ok", "redis": "not-configured"}
        ready = True
        if app.state.redis is not None:
            try:
                app.state.redis.ping()
                checks["redis"] = "ok"
            except Exception:
                checks["redis"] = "error"
                ready = False
        return {"status": "ready" if ready else "not-ready", **checks}

    @app.get("/health")
    def health() -> dict:
        redis_status = "not-configured"
        if app.state.redis is not None:
            try:
                app.state.redis.ping()
                redis_status = "ok"
            except Exception:
                redis_status = "error"
        return {"status": "degraded" if redis_status == "error" else "ok",
                "service_base_url": cfg.service_base_url,
                "auth_mode": "fixed-token" if cfg.api_token else "auto-token",
                "redis": redis_status}

    @app.get("/service-status")
    def service_status(_: None = Depends(_require_auth)) -> dict:
        if app.state.redis is not None:
            try:
                raw = app.state.redis.lrange("gw:service-status", 0, -1) or []
            except Exception:
                raw = []
            items = []
            for r in raw:
                try:
                    item = json.loads(r)
                except ValueError:
                    continue
                if isinstance(item, dict) and "success" in item:
                    items.append(item)
        else:
            with results_lock:
                items = list(reversed(results))
        total = len(items)
        success = sum(1 for i in items if i.get("success"))
        availability = round(success / total * 100, 1) if total else None
        return {"results": items, "total": total, "success": success,
                "availability": availability}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
