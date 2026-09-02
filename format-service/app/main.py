"""FastAPI 入口：编排服务 HTTP 层。

对齐原 dify-workflow-api 网关契约（/run、/health、/service-status），
客户端只需把后端地址从 Dify 应用换成本服务即可无缝迁移。
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import ipaddress
import json
import math
import os
import re
import secrets
import socket
import time
from collections import OrderedDict, deque
from datetime import datetime
from contextlib import asynccontextmanager
from threading import Lock
from typing import Any, Optional

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic_settings import BaseSettings, SettingsConfigDict

from .classifier import classify_error
from .dependencies import DependencyHealth
from .history import MemoryEventHistory, RedisEventHistory, event_score, time_fallback
from .jobs import JobStore, QueueFullError
from .llm import LLMClient
from .metrics import ResourceMonitor
from .notices import (NoticeCreate, NoticeError, NoticeStore,
                      NoticeUpdate)
from .pipeline import HTTPServiceClient, Pipeline, ServiceError
from .querylog import JSONLFileWriter, log_query, sanitize_entry
from .runtime_metrics import RuntimeMetrics
from .schema import (JobSubmissionResponse, JobStatusResponse, QueryResult,
                     SessionWorkflowRequest, WorkflowRequest)
from .trace import LOG, new_run_id, setup_logging
from .render import extract_session


class Settings(BaseSettings):
    """运行配置：全部来自环境变量 / .env。"""

    environment: str = "development"
    api_token: str = ""
    auto_rotate_token: bool = True
    service_base_url: str = "https://school.lizf.cn"
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


# 对外跳转/下载透传白名单：免密登录桥接页与课表 PDF 下载必须经公网入口可达
PASSTHROUGH_ALLOWED = ("/jump/go", "/get_schedule/export")
# 内存限流桶的硬上限：超出后按 LRU 淘汰最久未使用的来源 key，防止高基数来源撑爆内存
MAX_RATE_KEYS = 4096
# 后台回填最多扫描的 JSONL 行数；更早日志由 stdout/集中日志平台长期保存
MAX_HISTORY_SYNC_SCAN_LINES = 20_000
MAX_LOG_SCAN_LIMIT = 100_000
RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def _raw_get(base_url: str, token: str, path_qs: str, timeout: float,
                   client: Optional[httpx.AsyncClient] = None):
    """向上游查询代理转发 GET 并原样返回（不解析、不重定向）。"""
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    if client is None:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as shared:
            client = shared
    return await client.get(
        base_url.rstrip("/") + path_qs, headers=headers, timeout=timeout)


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
    """汇总最近窗口服务状态；accepted_* 由调用方补充全量计数。"""
    total = len(items)
    success = sum(1 for item in items if item["success"])
    return {
        "results": items,
        "total": total,
        "success": success,
        "availability": round(success / total * 100, 1) if total else None,
    }


def _accepted_status_counts(
    aggregate_total: Optional[int],
    aggregate_success: Optional[int],
    scan_total: int,
    scan_success: int,
) -> tuple[int, int]:
    """Redis 是低代价聚合源，但文件历史能在聚合键缺失时补齐总量。"""
    if aggregate_total is not None and aggregate_total >= scan_total:
        return aggregate_total, min(aggregate_success or 0, aggregate_total)
    return scan_total, scan_success


def _jsonl_signature(path: str | None) -> tuple[int, int] | None:
    """用纳秒 mtime + size 判断 JSONL 是否可能已变化。"""
    if not path:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _log_timestamp(item: dict) -> float:
    try:
        return datetime.fromisoformat(
            str(item.get("time", "")).replace("Z", "+00:00")
        ).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _read_service_status_summary(
    writer: JSONLFileWriter,
    recent_limit: int = 100,
    scan_limit: int = 10000,
) -> dict:
    """扫描共享日志卷，返回公开状态窗口与窗口内受理统计。"""
    window: list[tuple[float, str, int, dict]] = []
    seen_run_ids: set[str] = set()
    accepted_total = 0
    accepted_success = 0
    scanned = 0
    parse_errors = 0
    for line in writer.iter_collection_recent_lines():
        if scan_limit and scanned >= max(0, scan_limit):
            break
        scanned += 1
        try:
            item = sanitize_entry(json.loads(line))
        except (OSError, ValueError, TypeError):
            parse_errors += 1
            continue
        if item.get("event") != "query":
            continue
        view = _service_status_item(item)
        if view is None:
            continue
        run_id = str(item.get("run_id") or "")
        if run_id and run_id in seen_run_ids:
            continue
        if run_id:
            seen_run_ids.add(run_id)
        accepted_total += 1
        if view["success"]:
            accepted_success += 1
        heapq.heappush(
            window, (_log_timestamp(item), run_id, scanned, view))
        if len(window) > recent_limit:
            heapq.heappop(window)

    window.sort(key=lambda value: (value[0], value[1], value[2]), reverse=True)
    items = [view for _, _, _, view in window]
    return {
        "items": items,
        "accepted_total": accepted_total,
        "accepted_success": accepted_success,
        "scanned": scanned,
        "parse_errors": parse_errors,
    }


def _dependency_public_state(payload: dict) -> tuple[str, str, str]:
    proxy = payload.get("proxy") if isinstance(payload, dict) else None
    school = payload.get("school") if isinstance(payload, dict) else None
    redis_info = proxy.get("redis") if isinstance(proxy, dict) else None
    proxy_status = str((proxy or {}).get("status") or "unknown")
    school_status = str((school or {}).get("status") or "unknown")
    redis_state = "unknown"
    if isinstance(redis_info, dict):
        if redis_info.get("ok") is True:
            redis_state = "ok"
        elif redis_info.get("degraded") is True:
            redis_state = "degraded"
        elif redis_info.get("ok") is False:
            redis_state = "error"
    return proxy_status, school_status, redis_state


def _parse_trusted_proxy_cidrs(value: str) -> list[ipaddress.IPv4Network | ipaddress.IPv6Network]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in value.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        try:
            networks.append(ipaddress.ip_network(candidate, strict=False))
        except ValueError:
            LOG.warning("忽略无效的编排层受信代理网段：%s", candidate)
    return networks


def _read_recent_file_logs(writer: JSONLFileWriter, limit: int = 100,
                           scan_limit: int = MAX_HISTORY_SYNC_SCAN_LINES) -> list[dict]:
    """读取最新到最旧的脱敏查询日志，最多保留 limit 条。"""
    entries: list[dict] = []
    seen_run_ids: set[str] = set()
    scanned = 0
    for line in writer.iter_recent_lines():
        scanned += 1
        if scanned > max(0, scan_limit):
            break
        try:
            item = sanitize_entry(json.loads(line))
        except (OSError, ValueError, TypeError):
            continue
        if item.get("event") != "query":
            continue
        run_id = str(item.get("run_id") or "")
        if run_id and run_id in seen_run_ids:
            continue
        if run_id:
            seen_run_ids.add(run_id)
        entries.append(item)
        if len(entries) >= max(0, limit):
            break
    return entries


def _seed_local_history(local_history: MemoryEventHistory,
                        entries: list[dict]) -> None:
    """把 JSONL 中最新→最旧的事件恢复到本实例有界历史。"""
    for index, entry in enumerate(entries):
        event_id = str(entry.get("run_id") or "")
        score = event_score(entry, time_fallback() - index) - index * 0.000001
        local_history.add("gw:v2:query-logs", entry, event_id, score)


async def _sync_history_from_file(writer: JSONLFileWriter,
                                  local_history: MemoryEventHistory,
                                  redis_history: Optional[RedisEventHistory],
                                  ) -> bool:
    """恢复本实例 WAL；Redis 写入按 run_id/记录成员幂等去重。"""
    entries = await asyncio.to_thread(_read_recent_file_logs, writer)
    _seed_local_history(local_history, entries)
    if redis_history is None or not entries:
        return bool(entries)

    file_events = []
    for index, entry in enumerate(entries):
        event_id = str(entry.get("run_id") or "")
        score = event_score(entry, time_fallback() - index)
        file_events.append((entry, event_id, score))
    return await redis_history.seed_newest("gw:v2:query-logs", file_events)


def create_app(cfg: Optional[Settings] = None) -> FastAPI:
    cfg = cfg or Settings()
    instance_id = cfg.instance_id.strip() or socket.gethostname()
    file_instance_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", instance_id)[:128] or "instance"
    _validate_production(cfg.environment, cfg.auto_rotate_token, cfg.api_token,
                         cfg.admin_token)
    setup_logging()
    is_production = cfg.environment.strip().lower() in ("production", "prod")
    api_token = _resolve_api_token(cfg)
    dependency_health = DependencyHealth(initial={
        "redis": "not-configured" if not cfg.redis_url.strip() else "unknown",
        "query_proxy": "unknown",
        "query_proxy_redis": "unknown",
        "school_service": "unknown",
        "llm": "not-configured" if not cfg.llm_api_key.strip() else "unknown",
        "file_log": "disabled" if not (cfg.file_log_enabled and cfg.file_log_path.strip()) else "unknown",
    })
    llm = LLMClient(
        base_url=cfg.llm_base_url, api_key=cfg.llm_api_key, model=cfg.llm_model,
        temperature=cfg.llm_temperature, max_tokens=cfg.llm_max_tokens,
        timeout=cfg.llm_timeout, enable_thinking=cfg.llm_enable_thinking,
        system_prompt=cfg.llm_system_prompt)
    service = HTTPServiceClient(cfg.service_base_url, cfg.service_api_token, cfg.service_timeout)
    llm_slots = asyncio.Semaphore(max(1, cfg.llm_concurrency))
    pdf_slots = asyncio.Semaphore(max(1, cfg.pdf_concurrency))
    login_slots = asyncio.Semaphore(max(1, min(8, cfg.global_concurrency)))
    pipeline = Pipeline(service=service, llm=llm,
                        request_timeout=cfg.request_timeout,
                        llm_semaphore=llm_slots, pdf_semaphore=pdf_slots,
                        dependency_health=dependency_health,
                        metrics=RuntimeMetrics())
    metrics = pipeline.metrics
    assert metrics is not None
    public_health_cache: dict = {"at": 0.0, "payload": None}
    service_status_cache: dict = {
        "at": 0.0, "payload": None, "aggregate_total": None,
        "aggregate_success": None, "file_signature": None,
    }
    upstream_client = httpx.AsyncClient(
        timeout=cfg.service_timeout,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        follow_redirects=False,
    )

    async def _shutdown() -> None:
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
        await resource_monitor.stop()
        if app.state.file_log_writer is not None:
            app.state.file_log_writer.close()
        if app.state.redis is not None:
            await app.state.redis.aclose()
        await upstream_client.aclose()
        await pipeline.aclose()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        await _startup()
        try:
            yield
        finally:
            await _shutdown()

    app = FastAPI(
        title="edu-query-app",
        version="0.1.0",
        description="教务查询编排服务：替代 Dify 工作流，直接编排登录/查询/渲染/分析/PDF。",
        docs_url=None if is_production else "/docs",
        redoc_url=None if is_production else "/redoc",
        openapi_url=None if is_production else "/openapi.json",
        lifespan=lifespan,
    )
    app.state.pipeline = pipeline
    app.state.api_token = api_token
    app.state.rate_limit = max(1, cfg.rate_limit)
    app.state.trust_proxy = bool(cfg.trust_proxy)
    app.state.trusted_proxy_cidrs = _parse_trusted_proxy_cidrs(
        cfg.trusted_proxy_cidrs)
    app.state.metrics = metrics
    file_log_path = cfg.file_log_path.replace("{instance_id}", file_instance_id)
    if "{instance_id}" in cfg.file_log_path:
        path_prefix = cfg.file_log_path[:cfg.file_log_path.index("{instance_id}")]
        file_log_collection_root = os.path.dirname(path_prefix)
    else:
        file_log_collection_root = os.path.dirname(file_log_path)
    file_log_writer = None
    if cfg.file_log_enabled and file_log_path.strip():
        file_log_writer = JSONLFileWriter(
            path=file_log_path,
            max_bytes=cfg.file_log_max_bytes,
            backup_count=cfg.file_log_backup_count,
            collection_root=file_log_collection_root,
        )
    app.state.file_log_writer = file_log_writer
    resource_monitor = ResourceMonitor(
        log_path=file_log_path,
        interval=cfg.resource_monitor_interval_seconds,
        history_size=cfg.resource_monitor_history_size,
    )
    app.state.resource_monitor = resource_monitor
    app.state.dependency_health = dependency_health
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
    app.state.job_store = JobStore(_redis) if _redis is not None else None
    app.state.redis_history = RedisEventHistory(_redis) if _redis is not None else None
    app.state.local_history = MemoryEventHistory()
    app.state.history_dirty = True
    app.state.service_status_cache = service_status_cache
    app.state.instance_id = instance_id
    app.state.notice_store = NoticeStore(
        path=cfg.notice_fallback_path,
        redis=_redis,
        max_active=cfg.notice_active_max,
        max_history=cfg.notice_history_max,
        compact_after=cfg.notice_compact_after,
    )
    metrics.observe_redis(False)

    async def _startup() -> None:
        if app.state.file_log_writer is not None:
            try:
                await asyncio.to_thread(app.state.file_log_writer.check_access)
            except Exception as exc:
                raise RuntimeError(
                    "查询日志文件不可写（FILE_LOG_PATH）：%s" % exc.__class__.__name__
                ) from exc
        await resource_monitor.start()
        await app.state.notice_store.startup()
        if app.state.file_log_writer is not None:
            try:
                if await _sync_history_from_file(
                        app.state.file_log_writer, app.state.local_history,
                        app.state.redis_history):
                    LOG.info("已从 JSONL 恢复最近 100 条查询历史")
            except Exception as exc:
                LOG.warning("查询历史文件同步失败：%s",
                            exc.__class__.__name__)
            app.state.history_dirty = False

        if app.state.redis is None:
            return
        try:
            await app.state.redis.ping()
        except Exception as exc:
            LOG.warning("Redis 暂不可用，查询与本地 WAL 继续工作：%s",
                        exc.__class__.__name__)

        async def sync_history_periodically() -> None:
            while True:
                await asyncio.sleep(max(1.0, cfg.history_sync_interval_seconds))
                if app.state.file_log_writer is None or not app.state.history_dirty:
                    continue
                try:
                    await _sync_history_from_file(
                        app.state.file_log_writer, app.state.local_history,
                        app.state.redis_history)
                    app.state.history_dirty = False
                except Exception as exc:
                    LOG.warning("查询历史后台回填失败：%s", exc.__class__.__name__)

        app.state.history_sync_task = asyncio.create_task(
            sync_history_periodically(), name="history-sync")

        async def sync_notice_backup() -> None:
            while True:
                await asyncio.sleep(max(10.0, cfg.history_sync_interval_seconds))
                if not app.state.notice_store.redis_dirty:
                    continue
                try:
                    await app.state.notice_store.sync_redis()
                except Exception:
                    app.state.notice_store.redis_status = "degraded"

        app.state.notice_backup_task = asyncio.create_task(
            sync_notice_backup(), name="notice-backup")

        async def reap_stale_jobs() -> None:
            while True:
                try:
                    stale_after = max(
                        cfg.job_stale_after_seconds, cfg.request_timeout + 30)
                    await app.state.job_store.reap_stale(
                        stale_after, cfg.job_result_ttl_seconds)
                except Exception as exc:
                    LOG.warning("异步任务过期清理失败：%s", exc.__class__.__name__)
                await asyncio.sleep(30)

        async def run_one_job() -> None:
            while True:
                claimed = None
                try:
                    claimed = await app.state.job_store.claim(cfg.job_result_ttl_seconds)
                except Exception as exc:
                    LOG.warning("异步任务领取失败：%s", exc.__class__.__name__)
                    await asyncio.sleep(1)
                    continue
                if claimed is None:
                    await asyncio.sleep(0.2)
                    continue

                job_id, body, session, client_ip, started_at = claimed

                async def report_phase(phase: str) -> None:
                    try:
                        await app.state.job_store.mark_phase(
                            job_id, phase, cfg.job_result_ttl_seconds)
                    except Exception:
                        LOG.warning("异步 job=%s 阶段更新失败", job_id)

                try:
                    await report_phase("dispatching")
                    dispatch_wait_started = time.perf_counter()
                    async with query_slots:
                        metrics.observe_concurrency_wait(
                            "query", time.perf_counter() - dispatch_wait_started)
                        started_at = time.time()
                        await report_phase("querying")
                        if not await app.state.job_store.mark_running(
                                job_id, started_at, cfg.job_result_ttl_seconds):
                            continue
                        result = await asyncio.wait_for(
                            app.state.pipeline.run(
                                body, session=session, progress_cb=report_phase),
                            timeout=cfg.request_timeout)
                except asyncio.TimeoutError:
                    result = {
                        "success": False, "kind": "request_error", "run_id": job_id,
                        "output": "查询超时，请稍后重试",
                        "meta": {"elapsed_ms": int(cfg.request_timeout * 1000)},
                    }
                except Exception as exc:
                    LOG.exception("异步 job=%s 执行异常", job_id)
                    result = {
                        "success": False, "kind": "internal_error", "run_id": job_id,
                        "output": "服务内部异常，请稍后重试",
                        "meta": {"elapsed_ms": int((time.time() - started_at) * 1000)},
                    }

                try:
                    await app.state.job_store.complete(
                        job_id, result, cfg.job_result_ttl_seconds)
                    await _record_query_log(_query_log_entry(
                        body, client_ip, started_at, result, result.get("run_id", "")))
                except Exception as exc:
                    LOG.exception("异步 job=%s 状态写入失败", job_id)

        async def start_job_workers() -> None:
            app.state.job_worker_tasks = [
                asyncio.create_task(run_one_job(), name=f"query-job-worker-{index}")
                for index in range(max(1, cfg.job_workers))
            ]
            app.state.job_reaper_task = asyncio.create_task(
                reap_stale_jobs(), name="query-job-reaper")

        await start_job_workers()

    rate_hits = OrderedDict()
    rate_lock = Lock()

    def _memory_rate_allow(ip: str, now: float, limit: int) -> bool:
        """内存限流：每 key 最多 limit 个 60 秒时间戳；桶数量有硬上限并按 LRU 淘汰。"""
        with rate_lock:
            hits = rate_hits.get(ip)
            if hits is not None:
                rate_hits.move_to_end(ip)
                hits = [t for t in hits if now - t < 60]
            else:
                hits = []
            limited = len(hits) >= limit
            if not limited:
                hits.append(now)
                rate_hits[ip] = hits
                if len(rate_hits) > MAX_RATE_KEYS:
                    rate_hits.popitem(last=False)
            return limited

    bearer = HTTPBearer(auto_error=False)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            metrics.observe_request(
                getattr(request.scope.get("route", None), "path", "unmatched"),
                500, time.perf_counter() - started)
            raise
        metrics.observe_request(
            getattr(request.scope.get("route", None), "path", "unmatched"),
            response.status_code, time.perf_counter() - started)
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
        remote_ip = request.client.host if request.client else "unknown"
        if app.state.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded and remote_ip != "unknown":
                try:
                    remote_address = ipaddress.ip_address(remote_ip)
                except ValueError:
                    return remote_ip
                if any(remote_address in network
                       for network in app.state.trusted_proxy_cidrs):
                    return forwarded.split(",")[0].strip()
        return remote_ip

    async def _check_rate_limit(ip: str) -> None:
        now = time.time()
        limit = app.state.rate_limit
        limited = False
        redis_unavailable = False
        if app.state.redis is None:
            limited = _memory_rate_allow(ip, now, limit)
        else:
            digest = hashlib.sha256(ip.encode("utf-8")).hexdigest()
            key = f"gw:rate:{digest}"
            try:
                count = int(await asyncio.wait_for(
                    app.state.redis.eval(
                        RATE_LIMIT_SCRIPT, 1, key, 60), timeout=1.5))
                limited = count > limit
            except Exception as exc:
                LOG.warning("限流后端不可用：%s", exc.__class__.__name__)
                app.state.dependency_health.record(
                    "redis", "degraded",
                    "Redis 不可用，查询继续；限流、历史与异步任务降级")
                metrics.observe_redis(True)
                redis_unavailable = True

        if redis_unavailable:
            limited = _memory_rate_allow(ip, now, limit)
        elif app.state.redis is not None:
            app.state.dependency_health.record("redis", "ok")
            metrics.observe_redis(False)
        if limited:
            log_query({"event": "rate_limited", "client_ip": ip,
                       "message": "rate limit exceeded"})
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail="请求过于频繁，请稍后再试")

    async def _record_query_log(entry: dict) -> None:
        """查询日志四路一致：内存、可选 Redis、可选文件和 stdout。"""
        entry = sanitize_entry(entry)
        entry["run_id"] = str(entry.get("run_id") or new_run_id())
        score = event_score(entry, time.time())
        app.state.local_history.add("gw:v2:query-logs", entry, entry["run_id"], score)
        if entry.get("event") == "query":
            await _record_service_status_count(bool(entry.get("success")))
        if app.state.file_log_writer is not None:
            app.state.file_log_writer.last_error = ""
            try:
                await asyncio.to_thread(app.state.file_log_writer.write_raw, entry)
            except Exception as exc:
                app.state.file_log_writer.last_error = exc.__class__.__name__
                LOG.warning("查询日志文件写入失败：%s", exc.__class__.__name__)
        if app.state.redis_history is not None:
            try:
                await asyncio.wait_for(
                    app.state.redis_history.add(
                        "gw:v2:query-logs", entry, entry["run_id"], score),
                    timeout=1.5)
                app.state.dependency_health.record("redis", "ok")
            except Exception as exc:
                app.state.history_dirty = True
                app.state.dependency_health.record(
                    "redis", "degraded",
                    "Redis 不可用，查询继续；限流、历史与异步任务降级")
                LOG.warning("查询日志写入 Redis 失败：%s", exc.__class__.__name__)
        log_query(entry)

    async def _record_service_status_count(success: bool) -> None:
        if app.state.redis is None:
            return
        key = "gw:v2:service-status:accepted"
        try:
            pipeline = app.state.redis.pipeline(transaction=True)
            pipeline.hincrby(key, "total", 1)
            if success:
                pipeline.hincrby(key, "success", 1)
            await asyncio.wait_for(pipeline.execute(), timeout=1.5)
        except Exception as exc:
            LOG.warning("服务状态聚合计数写入失败：%s", exc.__class__.__name__)

    async def _service_status_counts() -> tuple[Optional[int], Optional[int]]:
        if app.state.redis is None:
            return None, None
        try:
            counts = await asyncio.wait_for(
                app.state.redis.hgetall("gw:v2:service-status:accepted"),
                timeout=1.5,
            )
            if not counts:
                return None, None
            total = max(0, int(counts.get("total", 0)))
            success = max(0, int(counts.get("success", 0)))
            return total, min(success, total)
        except Exception:
            return None, None

    async def _recent_query_logs(limit: int = 100) -> list[dict]:
        """读取跨副本热日志；Redis 不可用时降级本实例历史。"""
        if app.state.redis_history is not None:
            try:
                return await asyncio.wait_for(
                    app.state.redis_history.recent(
                        "gw:v2:query-logs", "gw:query-logs", limit),
                    timeout=1.5)
            except Exception as exc:
                app.state.dependency_health.record(
                    "redis", "degraded",
                    "Redis 不可用，查询继续；限流、历史与异步任务降级")
                LOG.warning("查询日志读取 Redis 失败，降级本实例历史：%s",
                            exc.__class__.__name__)
        return app.state.local_history.recent("gw:v2:query-logs", limit)

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

    def _login_failure_result(exc: ServiceError, run_id: str = "") -> dict:
        """登录失败时保留学校侧风控信号，避免异步入口丢失结构化分类。"""
        result = classify_error("login", exc.status_code, exc.body,
                                error_message=str(exc))
        if run_id:
            result["run_id"] = run_id
        return result

    @app.post("/run/jobs", response_model=JobSubmissionResponse)
    async def submit_run_job(body: WorkflowRequest, request: Request,
                             _: None = Depends(_require_auth)) -> JobSubmissionResponse:
        client_ip = _client_ip(request)
        await _check_rate_limit(client_ip)
        if app.state.redis is None or app.state.job_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="异步查询未启用：必须配置 REDIS_URL")

        pending_count = await app.state.job_store.pending_count()
        if pending_count >= cfg.job_pending_limit:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前排队人数已达上限，请稍后再试")

        started = time.time()
        login_run_id = new_run_id()
        try:
            async with login_slots:
                login_body = await app.state.pipeline.service.post("/login", {
                    "username": body.username, "password": body.password})
        except ServiceError as exc:
            failure = _login_failure_result(exc, login_run_id)
            await _record_query_log(_query_log_entry(
                body, client_ip, started,
                failure, failure.get("run_id", "")))
            if exc.status_code in (401, 403):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=failure) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=failure) from exc

        session = extract_session(login_body).get("session", "")
        if not session:
            failure = classify_error("login", body=login_body)
            failure["run_id"] = login_run_id
            await _record_query_log(_query_log_entry(
                body, client_ip, started, failure, failure.get("run_id", "")))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=failure)

        internal_request = SessionWorkflowRequest(**body.model_dump())
        try:
            job_id, deduplicated, position = await app.state.job_store.enqueue(
                internal_request, session, client_ip,
                max(1, cfg.job_pending_limit), cfg.job_result_ttl_seconds)
        except QueueFullError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前排队人数已达上限，请稍后再试") from exc
        except Exception as exc:
            LOG.warning("异步任务入队失败：%s", exc.__class__.__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="排队服务暂时不可用，请稍后再试") from exc

        return JobSubmissionResponse(
            job_id=job_id,
            deduplicated=deduplicated,
            position=position,
            poll_url=f"/run/jobs/{job_id}",
        )

    @app.get("/run/jobs/{job_id}", response_model=JobStatusResponse)
    async def get_run_job(job_id: str, response: Response,
                          _: None = Depends(_require_auth)) -> JobStatusResponse:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="任务不存在或已过期")
        if app.state.redis is None or app.state.job_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="异步查询未启用：必须配置 REDIS_URL")
        try:
            job_status = await app.state.job_store.status(job_id)
        except Exception as exc:
            LOG.warning("异步任务状态查询失败：%s", exc.__class__.__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="任务状态暂时不可用，请稍后再试") from exc
        if job_status is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="任务不存在或已过期")
        response.headers["Cache-Control"] = "no-store"
        return job_status

    @app.post("/run", response_model=QueryResult)
    @app.post("/v1/workflows/run", include_in_schema=False, response_model=QueryResult)
    async def run_workflow(body: WorkflowRequest, request: Request,
                           _: None = Depends(_require_auth)) -> QueryResult:
        client_ip = _client_ip(request)
        await _check_rate_limit(client_ip)
        started = time.time()
        try:
            try:
                wait_started = time.perf_counter()
                await asyncio.wait_for(query_slots.acquire(),
                                       timeout=max(0.001, cfg.concurrency_wait_timeout))
            except asyncio.TimeoutError as exc:
                busy_run_id = new_run_id()
                await _record_query_log({
                    "event": "query",
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "client_ip": client_ip,
                    "username": body.user or body.username,
                    "option": body.option,
                    "run_id": busy_run_id,
                    "success": False,
                    "kind": "busy_error",
                })
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="当前查询并发已达上限，请稍后再试") from exc
            try:
                metrics.observe_concurrency_wait(
                    "query", time.perf_counter() - wait_started)
                try:
                    result = await asyncio.wait_for(
                        app.state.pipeline.run(body), timeout=cfg.request_timeout)
                except asyncio.TimeoutError as exc:
                    run_id = new_run_id()
                    LOG.warning("run=%s 请求超时", run_id)
                    await _record_query_log(_query_log_entry(
                        body, client_ip, started,
                        {"success": False, "kind": "request_error", "meta": {}},
                        run_id))
                    return QueryResult(
                        success=False,
                        kind="request_error",
                        run_id=run_id,
                        output="查询超时，请稍后重试",
                        meta={"elapsed_ms": int(cfg.request_timeout * 1000)})
                await _record_query_log(_query_log_entry(body, client_ip, started, result,
                                                         result.get("run_id", "")))
                return QueryResult(**result)
            finally:
                query_slots.release()
        except HTTPException:
            raise
        except Exception as exc:  # 兜底：未知异常也返回统一 JSON
            LOG.exception("run 执行异常")
            run_id = new_run_id()
            result = {"success": False, "kind": "internal_error"}
            await _record_query_log(_query_log_entry(body, client_ip, started, result, run_id))
            return QueryResult(
                success=False, kind="internal_error", run_id=run_id,
                output="服务内部异常，请稍后重试",
                meta={"elapsed_ms": int((time.time() - started) * 1000)})

    @app.get("/health/live")
    async def health_live() -> dict:
        """K8s livenessProbe：进程存活即 200。"""
        return {"status": "ok"}

    async def _probe_query_proxy() -> str:
        """ready 探测只关心查询代理可达性，不暴露上游错误详情。"""
        try:
            response = await upstream_client.get(
                cfg.service_base_url.rstrip("/") + "/health",
                headers={"Authorization": "Bearer " + cfg.service_api_token}
                if cfg.service_api_token else {},
                timeout=2.0,
            )
            if response.status_code != 200:
                return "error"
            payload = response.json()
            return "ok" if payload.get("status") == "ok" else "degraded"
        except Exception:
            return "error"

    @app.get("/health/ready")
    async def health_ready() -> dict:
        """K8s readinessProbe：可选依赖降级时保持 ready，避免查询中断。"""
        query_proxy_task = asyncio.create_task(_probe_query_proxy())
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
                    "redis", "degraded",
                    "Redis 不可用，查询继续；限流、历史与异步任务降级")
        metrics.observe_redis(checks["redis"] == "degraded")
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
        return Response(content=metrics.render(), media_type="text/plain; version=0.0.4; charset=utf-8")

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
                    "redis", "degraded",
                    "Redis 不可用，查询继续；限流、历史与异步任务降级")
        return {"status": "degraded" if redis_status in ("error", "degraded") or file_log_status == "error" else "ok",
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
        proxy_redis_state = "unknown"
        proxy_latency: int | None = None
        school_latency: int | None = None
        try:
            response = await upstream_client.get(
                cfg.service_base_url.rstrip("/") + "/health",
                headers={"Authorization": "Bearer " + cfg.service_api_token}
                if cfg.service_api_token else {},
                timeout=5.0,
            )
            proxy_latency = int((time.perf_counter() - started) * 1000)
            if response.status_code == 200:
                upstream = response.json()
                proxy_status = "up" if upstream.get("status") == "ok" else "degraded"
                upstream_redis = upstream.get("redis")
                if isinstance(upstream_redis, dict):
                    if upstream_redis.get("ok") is True:
                        proxy_redis_state = "ok"
                    elif upstream_redis.get("degraded") is True:
                        proxy_redis_state = "degraded"
                    elif upstream_redis.get("ok") is False:
                        proxy_redis_state = "error"
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
        except httpx.TimeoutException:
            proxy_latency = int((time.perf_counter() - started) * 1000)
            proxy_status = "degraded"
        except Exception:
            proxy_latency = int((time.perf_counter() - started) * 1000)
            proxy_status = "down"

        query_proxy_state = {
            "up": "ok", "degraded": "degraded"}.get(proxy_status, "error")
        app.state.dependency_health.record(
            "query_proxy", query_proxy_state)
        app.state.dependency_health.record(
            "query_proxy_redis", proxy_redis_state)
        school_state = {
            "up": "ok", "down": "error", "degraded": "degraded",
        }.get(school_status, "unknown")
        app.state.dependency_health.record("school_service", school_state)

        payload = {
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "site": _public_dependency("up"),
            "proxy": {
                **_public_dependency(proxy_status, proxy_latency),
                "redis": {
                    "ok": proxy_redis_state in ("ok", "degraded"),
                    "degraded": proxy_redis_state in ("degraded", "error"),
                },
            },
            "school": _public_dependency(school_status, school_latency),
        }
        public_health_cache["at"] = now
        public_health_cache["payload"] = payload
        return payload

    @app.get("/query-logs")
    async def query_logs_endpoint(_: None = Depends(_require_auth)) -> dict:
        """返回最近查询日志（新→旧）；不含密码/session/token。"""
        items = [sanitize_entry(item) for item in await _recent_query_logs()]
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
            scan_limit if scan_limit > 0 else max(1, cfg.admin_log_scan_limit),
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
                    timeout=1.5)
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
    async def admin_metrics(_: None = Depends(_require_admin)) -> dict:
        """容器/宿主机资源、日志存储、应用负载与服务组件状态。"""
        history = app.state.resource_monitor.snapshot()
        if not history:
            history = [await asyncio.to_thread(app.state.resource_monitor.collect)]

        window_seconds = 300
        cutoff = time.time() - window_seconds
        recent_logs = await _recent_query_logs()
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
                await asyncio.wait_for(app.state.redis.ping(), timeout=1.5)
                app.state.dependency_health.record("redis", "ok")
            except Exception:
                app.state.dependency_health.record(
                    "redis", "degraded",
                    "Redis 不可用，查询继续；限流、历史与异步任务降级")

        public_payload = await public_health()
        public_proxy = public_payload.get("proxy", {})
        proxy_status_value = str(public_proxy.get("status") or "unknown")
        query_proxy_status = {
            "up": "ok", "degraded": "degraded",
        }.get(proxy_status_value, "error")
        public_proxy_redis = public_proxy.get("redis", {})
        query_proxy_redis = "unknown"
        if isinstance(public_proxy_redis, dict):
            if public_proxy_redis.get("ok") is True:
                query_proxy_redis = "ok"
            elif public_proxy_redis.get("degraded") is True:
                query_proxy_redis = "degraded"
            elif public_proxy_redis.get("ok") is False:
                query_proxy_redis = "error"
        public_school = public_payload.get("school", {})
        school_status_value = str(public_school.get("status") or "unknown")
        school_status = {
            "up": "ok", "degraded": "degraded", "down": "error",
        }.get(school_status_value, "unknown")
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
                "global_concurrency": cfg.global_concurrency,
                "rate_limit_per_minute": cfg.rate_limit,
            },
            "dependencies": dependencies,
            "degradations": degradations,
        }

    async def _passthrough(request: Request, path: str) -> Response:
        if path.split("?")[0] not in PASSTHROUGH_ALLOWED:
            raise HTTPException(status_code=404, detail="not found")
        qs = request.url.query
        try:
            upstream = await _raw_get(cfg.service_base_url, cfg.service_api_token,
                                      path + ("?" + qs if qs else ""),
                                      cfg.service_timeout, upstream_client)
        except httpx.HTTPError as exc:
            LOG.warning("透传上游失败 path=%s err=%s", path, exc.__class__.__name__)
            raise HTTPException(status_code=502, detail="上游服务不可用")
        headers = {}
        for name in ("Content-Type", "Content-Disposition", "Location",
                     "Cache-Control", "X-PDF-Cache-Hit"):
            if name in upstream.headers:
                headers[name] = upstream.headers[name]
        if path.split("?")[0] == "/get_schedule/export" and upstream.status_code == 200:
            metrics.observe_pdf_cache(
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

    @app.get("/service-status")
    async def service_status(
        _: None = Depends(_require_auth),
    ) -> dict:
        """返回最近 100 次公开状态与全量已受理统计。"""
        writer = app.state.file_log_writer
        now = time.monotonic()
        aggregate_total, aggregate_success = await _service_status_counts()
        current_signature = _jsonl_signature(writer.path if writer else None)
        if (
            writer is not None
            and cfg.service_status_cache_seconds > 0
            and service_status_cache["payload"] is not None
            and now - service_status_cache["at"] < cfg.service_status_cache_seconds
            and service_status_cache["aggregate_total"] == aggregate_total
            and service_status_cache["aggregate_success"] == aggregate_success
            and service_status_cache["file_signature"] == current_signature
        ):
            return service_status_cache["payload"]
        if writer is not None:
            scan = await asyncio.to_thread(
                _read_service_status_summary, writer, 100,
                cfg.service_status_scan_limit)
            items = scan["items"]
            accepted_total, accepted_success = _accepted_status_counts(
                aggregate_total, aggregate_success,
                scan["accepted_total"], scan["accepted_success"])
            source = "file"
            scanned = scan["scanned"]
            parse_errors = scan["parse_errors"]
            scan_limit = cfg.service_status_scan_limit
        else:
            raw_items = await _recent_query_logs(limit=100)
            all_items = [view for view in map(_service_status_item, raw_items)
                         if view is not None]
            items = all_items
            if aggregate_total is not None:
                accepted_total = aggregate_total
                accepted_success = aggregate_success or 0
            else:
                accepted_total = len(all_items)
                accepted_success = sum(
                    1 for view in all_items if view["success"])
            source = "redis" if app.state.redis_history is not None else "memory"
            scanned = len(raw_items)
            parse_errors = 0
            scan_limit = cfg.service_status_scan_limit

        payload = _service_status_payload(items)
        if writer is not None:
            service_status_cache.update({
                "at": time.monotonic(), "payload": payload,
                "aggregate_total": aggregate_total,
                "aggregate_success": aggregate_success,
                "file_signature": current_signature,
            })
        payload.update({
            "accepted_total": accepted_total,
            "accepted_success": accepted_success,
            "accepted_failure": accepted_total - accepted_success,
            "source": source,
            "scanned": scanned,
            "scan_limit": scan_limit,
            "scan_truncated": bool(writer and scanned >= scan_limit),
            "parse_errors": parse_errors,
        })
        return payload

    def _public_notice(item) -> dict:
        return {
            "id": item.id,
            "content": item.content,
            "level": item.level,
            "published_at": item.published_at,
        }

    @app.get("/notices/active", dependencies=[Depends(_require_auth)])
    async def active_notices() -> dict:
        try:
            items = await app.state.notice_store.active()
        except NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return {"notices": [_public_notice(item) for item in items]}

    @app.get("/notices/history")
    async def notice_history(
        limit: int = Query(default=50, ge=1, le=100),
        _auth: None = Depends(_require_auth),
    ) -> dict:
        try:
            items = await app.state.notice_store.history(limit)
        except NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return {
            "notices": [
                {
                    "id": item.id,
                    "content": item.content,
                    "level": item.level,
                    "published_at": item.published_at,
                    "archived_at": item.archived_at,
                }
                for item in items
            ]
        }

    @app.get("/admin/api/notices")
    async def admin_notices(
        _: None = Depends(_require_admin),
        notice_status: str = Query(default="", alias="status", pattern="^(|draft|active|archived)$"),
        keyword: str = Query(default="", max_length=100),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=20, ge=1, le=100),
    ) -> dict:
        try:
            items, total = await app.state.notice_store.admin_list(
                notice_status, keyword, offset, limit)
        except NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return {
            "notices": [item.model_dump() for item in items],
            "total": total,
            "pagination": {
                "offset": offset,
                "limit": limit,
                "has_more": offset + limit < total,
            },
            "storage": {
                "status": app.state.notice_store.status,
                "redis": app.state.notice_store.redis_status,
                "parse_errors": app.state.notice_store.parse_errors,
                "last_error": app.state.notice_store.last_error,
            },
        }

    @app.post("/admin/api/notices")
    async def create_notice(
        payload: NoticeCreate,
        _: None = Depends(_require_admin),
    ) -> dict:
        try:
            item = await app.state.notice_store.create(payload)
        except NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return {"notice": item.model_dump()}

    @app.patch("/admin/api/notices/{notice_id}")
    async def update_notice(
        notice_id: str,
        payload: NoticeUpdate,
        _: None = Depends(_require_admin),
    ) -> dict:
        try:
            item = await app.state.notice_store.update(notice_id, payload)
        except NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return {"notice": item.model_dump()}

    @app.delete("/admin/api/notices/{notice_id}")
    async def delete_notice(
        notice_id: str,
        _: None = Depends(_require_admin),
    ) -> dict:
        try:
            await app.state.notice_store.delete(notice_id)
        except NoticeError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail)
        return {"deleted": True}

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=False)
