"""FastAPI 入口：编排服务 HTTP 层。

对齐原 dify-workflow-api 网关契约（/run、/health、/service-status），
客户端只需把后端地址从 Dify 应用换成本服务即可无缝迁移。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import secrets
import time
from collections import deque
from datetime import datetime
from threading import Lock
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic_settings import BaseSettings, SettingsConfigDict

from .llm import LLMClient
from .metrics import ResourceMonitor
from .pipeline import HTTPServiceClient, Pipeline
from .querylog import JSONLFileWriter, log_query, sanitize_entry
from .schema import QueryResult, WorkflowRequest
from .trace import LOG, new_run_id, setup_logging


class Settings(BaseSettings):
    """运行配置：全部来自环境变量 / .env。"""

    environment: str = "development"
    api_token: str = ""
    auto_rotate_token: bool = True
    service_base_url: str = "https://school.lizf.cn"
    service_api_token: str = ""
    service_timeout: float = 60.0
    request_timeout: float = 100.0
    concurrency_wait_timeout: float = 2.0
    global_concurrency: int = 4
    llm_base_url: str = "https://open.bigmodel.cn/api/paas/v4"
    llm_api_key: str = ""
    llm_model: str = "glm-4.5-flash"
    llm_temperature: float = 0.0
    llm_max_tokens: int = 2048
    llm_enable_thinking: bool = True
    llm_system_prompt: str = ""
    llm_timeout: float = 60.0
    rate_limit: int = 30
    trust_proxy: bool = False
    redis_url: str = ""
    admin_token: str = ""
    file_log_enabled: bool = True
    file_log_path: str = "/tmp/edu-query/queries.jsonl"
    file_log_max_bytes: int = 50 * 1024 * 1024
    file_log_backup_count: int = 7

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8",
                                      extra="ignore")


def _resolve_api_token(cfg: Settings) -> str:
    if cfg.auto_rotate_token:
        return secrets.token_urlsafe(32)
    return cfg.api_token.strip()


def _validate_production(environment: str, auto_rotate_token: bool,
                         api_token: str, admin_token: str = "") -> None:
    if environment.strip().lower() in ("production", "prod"):
        if auto_rotate_token:
            raise RuntimeError(
                "生产环境禁止 AUTO_ROTATE_TOKEN=true：多实例必须设置 "
                "AUTO_ROTATE_TOKEN=false 并使用固定 API_TOKEN")
        if not api_token.strip():
            raise RuntimeError("生产环境必须配置 API_TOKEN")
        if api_token.strip().lower() == "change-me" or len(api_token.strip()) < 16:
            raise RuntimeError(
                "生产环境必须配置足够强的固定 API_TOKEN（禁止默认值 change-me，建议至少 16 字符）")
        if not admin_token.strip() or admin_token.strip().lower() == "change-admin-me" \
                or len(admin_token.strip()) < 16:
            raise RuntimeError(
                "生产环境必须配置独立的强 ADMIN_TOKEN（禁止默认值 change-admin-me，建议至少 16 字符）")


# 对外跳转/下载透传白名单：免密登录桥接页与课表 Word 下载必须经公网入口可达
PASSTHROUGH_ALLOWED = ("/jump/go", "/get_schedule/export")


async def _raw_get(base_url: str, token: str, path_qs: str, timeout: float):
    """向上游查询代理转发 GET 并原样返回（不解析、不重定向）。"""
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        return await client.get(base_url.rstrip("/") + path_qs, headers=headers)


def _public_dependency(status: str, latency_ms: int | None = None,
                       note: str = "") -> dict:
    item: dict = {"status": status}
    if latency_ms is not None:
        item["latency_ms"] = latency_ms
    if note:
        item["note"] = note
    return item


def _service_status_item(item: Any) -> Optional[dict]:
    """将持久化事件压缩为公共状态字段，避免泄漏查询日志上下文。"""
    if not isinstance(item, dict) or "success" not in item:
        return None
    return {
        "success": bool(item.get("success")),
        "kind": str(item.get("kind") or "unknown"),
        "time": str(item.get("time") or ""),
    }


def _service_status_payload(items: list[dict]) -> dict:
    """汇总最近 100 次查询的服务状态。"""
    total = len(items)
    success = sum(1 for item in items if item["success"])
    return {
        "results": items,
        "total": total,
        "success": success,
        "availability": round(success / total * 100, 1) if total else None,
    }


def create_app(cfg: Optional[Settings] = None) -> FastAPI:
    cfg = cfg or Settings()
    _validate_production(cfg.environment, cfg.auto_rotate_token, cfg.api_token,
                         cfg.admin_token)
    setup_logging()
    is_production = cfg.environment.strip().lower() in ("production", "prod")
    api_token = _resolve_api_token(cfg)
    llm = LLMClient(
        base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, model=cfg.llm_model,
        temperature=cfg.llm_temperature, max_tokens=cfg.llm_max_tokens,
        timeout=cfg.llm_timeout, enable_thinking=cfg.llm_enable_thinking,
        system_prompt=cfg.llm_system_prompt)
    service = HTTPServiceClient(cfg.service_base_url, cfg.service_api_token, cfg.service_timeout)
    pipeline = Pipeline(service=service, llm=llm,
                        request_timeout=cfg.request_timeout)
    public_health_cache: dict = {"at": 0.0, "payload": None}

    async def _shutdown() -> None:
        await resource_monitor.stop()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await pipeline.aclose()

    app = FastAPI(
        title="edu-query-app",
        version="0.1.0",
        description="教务查询编排服务：替代 Dify 工作流，直接编排登录/查询/渲染/分析/PDF。",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
    )
    app.add_event_handler("shutdown", _shutdown)
    app.state.pipeline = pipeline
    app.state.api_token = api_token
    app.state.rate_limit = max(1, cfg.rate_limit)
    app.state.trust_proxy = bool(cfg.trust_proxy)
    file_log_writer = None
    if cfg.file_log_enabled and cfg.file_log_path.strip():
        file_log_writer = JSONLFileWriter(
            path=cfg.file_log_path,
            max_bytes=cfg.file_log_max_bytes,
            backup_count=cfg.file_log_backup_count,
        )
    app.state.file_log_writer = file_log_writer
    resource_monitor = ResourceMonitor(
        log_path=cfg.file_log_path,
        interval=2.0,
        history_size=150,
    )
    app.state.resource_monitor = resource_monitor
    query_slots = asyncio.Semaphore(max(1, cfg.global_concurrency))
    app.state.query_slots = query_slots

    _redis = None
    if cfg.redis_url.strip():
        from redis import asyncio as aioredis

        try:
            _redis = aioredis.from_url(
                cfg.redis_url,
                decode_responses=True,
                socket_connect_timeout=3.0,
                socket_timeout=3.0,
                health_check_interval=30,
            )
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Redis 连接失败（REDIS_URL）：%s" % exc)
    app.state.redis = _redis

    async def _startup() -> None:
        if app.state.file_log_writer is not None:
            try:
                await asyncio.to_thread(app.state.file_log_writer.check_access)
            except Exception as exc:
                raise RuntimeError(
                    "查询日志文件不可写（FILE_LOG_PATH）：%s" % exc.__class__.__name__
                ) from exc
        await resource_monitor.start()
        if app.state.redis is None:
            return
        try:
            await app.state.redis.ping()
        except Exception as exc:
            raise RuntimeError("Redis 连接失败（REDIS_URL）：%s" % exc) from exc

    app.add_event_handler("startup", _startup)

    results: deque = deque(maxlen=100)
    results_lock = Lock()
    query_logs: deque = deque(maxlen=100)
    query_logs_lock = Lock()
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

    def _require_admin(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
        admin_token = cfg.admin_token.strip()
        if not admin_token or credentials is None or not secrets.compare_digest(
                credentials.credentials, admin_token):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="admin area unavailable")

    def _client_ip(request: Request) -> str:
        if app.state.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    async def _check_rate_limit(ip: str) -> None:
        nonlocal rate_hits
        now = time.time()
        limit = app.state.rate_limit
        limited = False
        if app.state.redis is None:
            with rate_lock:
                if len(rate_hits) > 4096:
                    pruned = {}
                    for key, stamps in rate_hits.items():
                        recent = [t for t in stamps if now - t < 60]
                        if recent:
                            pruned[key] = recent
                    rate_hits = pruned
                hits = [t for t in rate_hits.get(ip, []) if now - t < 60]
                limited = len(hits) >= limit
                if not limited:
                    hits.append(now)
                    rate_hits[ip] = hits
        else:
            digest = hashlib.sha256(ip.encode("utf-8")).hexdigest()
            key = f"gw:rate:{digest}"
            try:
                count = int(await app.state.redis.incr(key))
                if count == 1:
                    await app.state.redis.expire(key, 60)
                limited = count > limit
            except Exception as exc:
                log_query({"event": "rate_limited", "client_ip": ip,
                           "message": "rate limit backend unavailable"})
                LOG.warning("限流后端不可用：%s", exc.__class__.__name__)
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="服务繁忙，请稍后再试") from exc
        if limited:
            log_query({"event": "rate_limited", "client_ip": ip,
                       "message": "rate limit exceeded"})
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail="请求过于频繁，请稍后再试")

    async def _record(success: bool, kind: str) -> None:
        item = {"success": success, "kind": kind,
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        with results_lock:
            results.append(item)
        if app.state.redis is not None:
            try:
                await app.state.redis.lpush("gw:service-status", json.dumps(item, ensure_ascii=False))
                await app.state.redis.ltrim("gw:service-status", 0, 99)
            except Exception:
                pass

    async def _record_query_log(entry: dict) -> None:
        """查询日志四路一致：内存、可选 Redis、可选文件和 stdout。"""
        entry = sanitize_entry(entry)
        with query_logs_lock:
            query_logs.append(entry)
        if app.state.redis is not None:
            try:
                await app.state.redis.lpush("gw:query-logs", json.dumps(entry, ensure_ascii=False))
                await app.state.redis.ltrim("gw:query-logs", 0, 99)
            except Exception:
                pass
        if app.state.file_log_writer is not None:
            app.state.file_log_writer.last_error = ""
            try:
                await asyncio.to_thread(app.state.file_log_writer.write_raw, entry)
            except Exception as exc:
                app.state.file_log_writer.last_error = exc.__class__.__name__
                LOG.warning("查询日志文件写入失败：%s", exc.__class__.__name__)
        log_query(entry)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index() -> str:
        return ("<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                "<title>教务信息查询</title></head><body style='font-family:sans-serif;"
                "padding:2rem'><h1>教务信息查询服务</h1>"
                "<p>本服务已替代 Dify 工作流。请通过 POST /run 提交查询：</p>"
                "<pre>{'username': '学号', 'password': '密码', 'option': '成绩|课表', "
                "'md2pdf': false, 'check': false}</pre></body></html>")

    def _query_log_entry(body: WorkflowRequest, client_ip: str, started: float,
                         result: dict, run_id: str = "") -> dict:
        """构造查询日志：不含 password/session/token；缺省 run_id 兜底生成。"""
        meta = result.get("meta", {})
        return {
            "event": "query",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "run_id": run_id or new_run_id(),
            "client_ip": client_ip,
            "username": body.user or body.username,
            "option": body.option,
            "semesters": body.semesters or "",
            "weeks": body.weeks,
            "md2pdf": bool(body.md2pdf),
            "check": bool(body.check),
            "success": bool(result.get("success")),
            "kind": result.get("kind") or "unknown",
            "elapsed_ms": int((time.time() - started) * 1000),
            "analysis": bool(body.check and meta.get("analysis_used")),
            "analysis_usage": meta.get("analysis_usage", "—"),
            "response_summary": meta.get("response_summary", "—"),
        }

    @app.post("/run", response_model=QueryResult)
    @app.post("/v1/workflows/run", include_in_schema=False, response_model=QueryResult)
    async def run_workflow(body: WorkflowRequest, request: Request,
                           _: None = Depends(_require_auth)) -> QueryResult:
        client_ip = _client_ip(request)
        await _check_rate_limit(client_ip)
        started = time.time()
        try:
            try:
                await asyncio.wait_for(query_slots.acquire(),
                                       timeout=max(0.001, cfg.concurrency_wait_timeout))
            except asyncio.TimeoutError as exc:
                await _record(False, "busy_error")
                await _record_query_log({
                    "event": "query",
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "client_ip": client_ip,
                    "username": body.user or body.username,
                    "option": body.option,
                    "success": False,
                    "kind": "busy_error",
                })
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="当前查询并发已达上限，请稍后再试") from exc
            try:
                try:
                    result = await asyncio.wait_for(
                        app.state.pipeline.run(body), timeout=cfg.request_timeout)
                except asyncio.TimeoutError as exc:
                    run_id = new_run_id()
                    LOG.warning("run=%s 请求超时", run_id)
                    return QueryResult(
                        success=False,
                        kind="request_error",
                        run_id=run_id,
                        output="查询超时，请稍后重试",
                        meta={"elapsed_ms": int(cfg.request_timeout * 1000)})
                await _record(bool(result.get("success")), result.get("kind", "unknown"))
                await _record_query_log(_query_log_entry(body, client_ip, started, result,
                                                         result.get("run_id", "")))
                return QueryResult(**result)
            finally:
                query_slots.release()
        except HTTPException:
            raise
        except Exception as exc:  # 兜底：未知异常也返回统一 JSON
            LOG.exception("run 执行异常")
            await _record(False, "internal_error")
            run_id = new_run_id()
            result = {"success": False, "kind": "internal_error"}
            await _record_query_log(_query_log_entry(body, client_ip, started, result, run_id))
            return QueryResult(
                success=False, kind="internal_error", run_id=run_id,
                output="服务内部异常：%s" % exc.__class__.__name__,
                meta={"elapsed_ms": int((time.time() - started) * 1000)})

    @app.get("/health/live")
    async def health_live() -> dict:
        """K8s livenessProbe：进程存活即 200。"""
        return {"status": "ok"}

    @app.get("/health/ready")
    async def health_ready() -> dict:
        """K8s readinessProbe：配置就绪且可选 Redis 可用时 ready。"""
        checks = {
            "config": "ok",
            "redis": "not-configured",
            "file_log": "disabled" if app.state.file_log_writer is None else "ok",
        }
        ready = True
        if app.state.redis is not None:
            try:
                await app.state.redis.ping()
                checks["redis"] = "ok"
            except Exception:
                checks["redis"] = "error"
                ready = False
        return {"status": "ready" if ready else "not-ready", **checks}

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
            except Exception:
                redis_status = "error"
        return {"status": "degraded" if redis_status == "error" or file_log_status == "error" else "ok",
                "auth_mode": "fixed-token" if cfg.api_token else "auto-token",
                "redis": redis_status,
                "file_log": file_log_status}

    @app.get("/health/public")
    async def public_health() -> dict:
        """公开站粗粒度链路状态；不暴露内部地址、错误体或凭据。"""
        now = time.monotonic()
        if public_health_cache["payload"] and now - public_health_cache["at"] < 15:
            return public_health_cache["payload"]

        started = time.perf_counter()
        proxy_status, school_status = "down", "unknown"
        proxy_latency: int | None = None
        school_latency: int | None = None
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    cfg.service_base_url.rstrip("/") + "/health",
                    headers={"Authorization": "Bearer " + cfg.service_api_token}
                    if cfg.service_api_token else {},
                )
            proxy_latency = int((time.perf_counter() - started) * 1000)
            if response.status_code == 200:
                upstream = response.json()
                proxy_status = "up" if upstream.get("status") == "ok" else "degraded"
                school = upstream.get("school", {})
                school_latency = school.get("latency_ms")
                if school.get("ok") is True:
                    school_status = "up"
                elif school.get("ok") is False:
                    school_status = "down"
                else:
                    school_status = "unknown"
            else:
                proxy_status = "degraded"
        except Exception:
            proxy_latency = int((time.perf_counter() - started) * 1000)

        payload = {
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "site": _public_dependency("up"),
            "proxy": _public_dependency(proxy_status, proxy_latency),
            "school": _public_dependency(school_status, school_latency),
        }
        public_health_cache["at"] = now
        public_health_cache["payload"] = payload
        return payload

    @app.get("/query-logs")
    async def query_logs_endpoint(_: None = Depends(_require_auth)) -> dict:
        """返回最近查询日志（新→旧）；不含密码/session/token。"""
        if app.state.redis is not None:
            try:
                raw = await app.state.redis.lrange("gw:query-logs", 0, -1) or []
            except Exception:
                raw = []
            items = []
            for r in raw:
                try:
                    item = json.loads(r)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
        else:
            with query_logs_lock:
                items = list(reversed(query_logs))
        return {"logs": items, "total": len(items)}

    @app.get("/admin/api/query-logs")
    async def admin_query_logs(
        _: None = Depends(_require_admin),
        keyword: str = Query(default="", max_length=100),
        kind: str = Query(default="", max_length=60),
        option: str = Query(default="", max_length=30),
        success: Optional[bool] = None,
        time_from: Optional[datetime] = None,
        time_to: Optional[datetime] = None,
        scan_limit: int = Query(default=5000, ge=0, le=100000),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=50, ge=1, le=200),
) -> dict:
        """管理端查询历史；优先读取文件通道，无文件时降级 Redis/内存。

        scan_limit=0 表示全量扫描；默认限制 newest-first 扫描行数，避免大日志
        拖高常驻内存。
        """
        keyword = keyword.strip().casefold()
        kind = kind.strip().casefold()
        option = option.strip().casefold()
        from_ts = time_from.timestamp() if time_from else None
        to_ts = time_to.timestamp() if time_to else None
        scanned = 0

        def timestamp(item: dict) -> float:
            try:
                return datetime.fromisoformat(str(item.get("time", "")).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError):
                return 0.0

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
        if writer is not None:
            source = "file"

            def scan_file() -> tuple[list[dict], int, int]:
                nonlocal parse_errors
                matched: list[dict] = []
                seen = 0
                for line in writer.iter_recent_lines():
                    if scan_limit and seen >= scan_limit:
                        break
                    seen += 1
                    try:
                        item = json.loads(line)
                    except ValueError:
                        parse_errors += 1
                        continue
                    if isinstance(item, dict) and matches(item):
                        item = sanitize_entry(item)
                        matched.append((timestamp(item), str(item.get("run_id", "")), item))
                parsed = [entry for _, _, entry in matched]
                return parsed, seen, parse_errors

            entries, scanned, parse_errors = await asyncio.to_thread(scan_file)
        elif app.state.redis is not None:
            source = "redis"
            try:
                raw = await app.state.redis.lrange("gw:query-logs", 0, -1) or []
            except Exception:
                raw = []
            entries: list[dict] = []
            for raw_item in raw:
                try:
                    item = json.loads(raw_item)
                except ValueError:
                    parse_errors += 1
                    continue
                if isinstance(item, dict) and matches(item):
                    entries.append(sanitize_entry(item))
        else:
            source = "memory"
            with query_logs_lock:
                snapshot = list(reversed(query_logs))
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
            "scan_limit": scan_limit,
            "scan_truncated": bool(scan_limit and scanned >= scan_limit),
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
    async def admin_metrics(_: None = Depends(_require_admin)) -> dict:
        """容器/宿主机资源、日志存储、应用负载与服务组件状态。"""
        history = app.state.resource_monitor.snapshot()
        if not history:
            history = [await asyncio.to_thread(app.state.resource_monitor.collect)]

        window_seconds = 300
        cutoff = time.time() - window_seconds
        with query_logs_lock:
            recent_logs = list(query_logs)
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

        redis_status = "not-configured"
        if app.state.redis is not None:
            try:
                await asyncio.wait_for(app.state.redis.ping(), timeout=1.5)
                redis_status = "ok"
            except Exception:
                redis_status = "error"
        file_status = (
            "disabled" if app.state.file_log_writer is None
            else app.state.file_log_writer.status
        )
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
                "redis": redis_status,
                "file_log": file_status,
                "global_concurrency": cfg.global_concurrency,
                "rate_limit_per_minute": cfg.rate_limit,
            },
        }

    async def _passthrough(request: Request, path: str) -> Response:
        if path.split("?")[0] not in PASSTHROUGH_ALLOWED:
            raise HTTPException(status_code=404, detail="not found")
        qs = request.url.query
        try:
            upstream = await _raw_get(cfg.service_base_url, cfg.service_api_token,
                                      path + ("?" + qs if qs else ""), cfg.service_timeout)
        except httpx.HTTPError as exc:
            LOG.warning("透传上游失败 path=%s err=%s", path, exc.__class__.__name__)
            raise HTTPException(status_code=502, detail="上游服务不可用")
        headers = {}
        for name in ("Content-Type", "Content-Disposition", "Location", "Cache-Control"):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        return Response(content=upstream.content, status_code=upstream.status_code,
                        headers=headers)

    @app.get("/jump/{rest:path}", include_in_schema=False)
    async def jump_passthrough(request: Request, rest: str) -> Response:
        """免密登录桥接页：/jump/go?code=.. 经编排后端转发查询代理。"""
        return await _passthrough(request, "/jump/" + rest)

    @app.get("/get_schedule/export", include_in_schema=False)
    async def schedule_export_passthrough(request: Request) -> Response:
        """课表 Word 下载：/get_schedule/export?code=.. 原样透传。"""
        return await _passthrough(request, "/get_schedule/export")

    @app.get("/service-status")
    async def service_status(_: None = Depends(_require_auth)) -> dict:
        """返回跨客户端共享的服务状态；文件通道用于进程重启后恢复。"""
        redis_items: list[dict] = []
        if app.state.redis is not None:
            try:
                raw = await app.state.redis.lrange("gw:service-status", 0, -1) or []
            except Exception:
                raw = []
            for r in raw:
                try:
                    item = json.loads(r)
                except ValueError:
                    continue
                view = _service_status_item(item)
                if view is not None:
                    redis_items.append(view)
            if redis_items:
                return _service_status_payload(redis_items)

        writer = app.state.file_log_writer
        if writer is not None:
            def read_persisted_status() -> list[dict]:
                items: list[dict] = []
                for line in writer.iter_recent_lines():
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(item, dict) or item.get("event") != "query":
                        continue
                    view = _service_status_item(item)
                    if view is not None:
                        items.append(view)
                    if len(items) >= 100:
                        break
                return items

            try:
                persisted_items = await asyncio.to_thread(read_persisted_status)
            except Exception as exc:
                LOG.warning("服务状态文件恢复失败：%s", exc.__class__.__name__)
            else:
                if persisted_items:
                    return _service_status_payload(persisted_items)

        with results_lock:
            items = [
                view for view in
                (_service_status_item(item) for item in reversed(results))
                if view is not None
            ]
        return _service_status_payload(items)
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
