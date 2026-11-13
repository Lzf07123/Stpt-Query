"""应用共享上下文与运行时助手：create_app 拆分后的单一共享状态载体。

AppContext 集中承载原本散落在 create_app 闭包里的共享状态（配置、信号量、
缓存、上游客户端、限流桶等）；路由模块只经 (app, ctx) 传参访问，不再闭包捕获。
可被测试运行期替换的协作者（pipeline、redis、job_store、redis_history 等）
仍统一从 ``ctx.app.state`` 晚绑定读取，与拆分前的闭包语义完全一致。
"""
from __future__ import annotations

import asyncio
import hashlib
import heapq
import ipaddress
import json
import os
import re
import secrets
import socket
import time
from collections import OrderedDict
from datetime import datetime
from threading import Lock
from typing import TYPE_CHECKING, Any, Optional

import httpx
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .classifier import classify_error
from .dependencies import REDIS_DEGRADED_MESSAGE, DependencyHealth
from .history import MemoryEventHistory, RedisEventHistory, event_score, time_fallback
from .llm import LLMClient
from .metrics import ResourceMonitor
from .notices import NoticeStore
from .pipeline import HTTPServiceClient, Pipeline
from .jwxt_core import load_config as _load_jwxt_config
from .jwxt_http import make_app as _make_jwxt_app
from .calendar.config import CalendarConfigError
from .calendar.crypto import CalendarCryptoError
from .calendar.service import (CalendarService, CalendarSettings,
                                build_service as build_calendar_service)
from .querylog import JSONLFileWriter, log_query, sanitize_entry
from .runtime_metrics import RuntimeMetrics
from .trace import LOG, new_run_id, setup_logging

if TYPE_CHECKING:  # 仅类型标注：Settings 仍是 main.py 的单一事实来源
    from .main import Settings

# 内存限流桶的硬上限：超出后按 LRU 淘汰最久未使用的来源 key，防止高基数来源撑爆内存
MAX_RATE_KEYS = 4096
# 后台回填最多扫描的 JSONL 行数；更早日志由 stdout/集中日志平台长期保存
MAX_HISTORY_SYNC_SCAN_LINES = 20_000
MAX_LOG_SCAN_LIMIT = 100_000
# 编排 Redis 单次操作（限流/日志/聚合/探活）的统一等待上限：
# Redis 短暂不可用时快速降级，不拖慢查询主链路
REDIS_OP_TIMEOUT_SECONDS = 1.5
RATE_LIMIT_SCRIPT = """
local count = redis.call('INCR', KEYS[1])
if count == 1 or redis.call('TTL', KEYS[1]) < 0 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""
# /run/jobs/{job_id} 的任务 ID 形态：32 位十六进制（每请求 fullmatch 复用）
_JOB_ID_RE = re.compile(r"[0-9a-f]{32}")
# 异步任务 worker 空轮询退避：从 0.2s 指数增长，1s 封顶；领到任务即重置
WORKER_IDLE_DELAY_FLOOR = 0.2
WORKER_IDLE_DELAY_CAP = 1.0


def _next_worker_idle_delay(current: float) -> float:
    """worker 空手后下一次轮询前的等待时长（指数退避，封顶 1s）。"""
    return min(WORKER_IDLE_DELAY_CAP, max(WORKER_IDLE_DELAY_FLOOR, current) * 2)


def _import_pdf_module() -> None:
    """导入 app.pdf 触发 reportlab CID 中文字体注册（首次较慢）。"""
    from . import pdf  # noqa: F401  # 延迟导入：仅在启动预热与真正生成 PDF 时加载


def _memory_rate_allow(rate_hits: "OrderedDict[str, list[float]]", rate_lock: Lock,
                       ip: str, now: float, limit: int) -> bool:
    """内存限流：每 key 最多 limit 个 60 秒时间戳；桶数量有硬上限并按 LRU 淘汰。

    无论是否被限流都写回过滤后的时间戳，避免过旧条目滞留内存；
    桶清空则删除 key（LRU 硬上限语义保持不变）。
    """
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
        if hits:
            rate_hits[ip] = hits
            if len(rate_hits) > MAX_RATE_KEYS:
                rate_hits.popitem(last=False)
        else:
            rate_hits.pop(ip, None)
        return limited


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
    """从 /health/public 载荷解析依赖态，返回 (query_proxy, school_service, query_proxy_redis)。

    供 admin_metrics 复用；映射口径与 public_health 记录 dependency_health 时保持一致。
    查询代理内存模式（mode=memory）映射为 not-configured，不再被布尔投影误判为 error。
    """
    proxy = payload.get("proxy") if isinstance(payload, dict) else None
    school = payload.get("school") if isinstance(payload, dict) else None
    redis_info = proxy.get("redis") if isinstance(proxy, dict) else None
    proxy_status = str((proxy or {}).get("status") or "unknown")
    school_status = str((school or {}).get("status") or "unknown")
    redis_state = "unknown"
    if isinstance(redis_info, dict):
        if redis_info.get("mode") == "memory":
            redis_state = "not-configured"
        elif redis_info.get("ok") is True:
            redis_state = "ok"
        elif redis_info.get("degraded") is True:
            redis_state = "degraded"
        elif redis_info.get("error") is True:
            redis_state = "error"
    query_proxy_state = {
        "up": "ok", "degraded": "degraded"}.get(proxy_status, "error")
    school_state = {
        "up": "ok", "down": "error", "degraded": "degraded",
    }.get(school_status, "unknown")
    return query_proxy_state, school_state, redis_state


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


def _resolve_api_token(cfg: "Settings") -> str:
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


class AppContext:
    """create_app 的共享状态载体：路由与后台任务只经它访问协作对象。"""

    def __init__(self, cfg: "Settings") -> None:
        self.cfg = cfg
        self.app: Optional[Any] = None  # create_app 组装完成后回填（晚绑定状态镜像）
        self.instance_id = ""
        self.file_instance_id = ""
        self.is_production = False
        self.api_token = ""
        self.internal_jwxt_app: Optional[Any] = None
        self.service_base_url = ""
        self.service_api_token = ""
        self.calendar_service: Optional[CalendarService] = None
        self.llm_slots: Optional[asyncio.Semaphore] = None
        self.pdf_slots: Optional[asyncio.Semaphore] = None
        self.login_slots: Optional[asyncio.Semaphore] = None
        self.query_slots: Optional[asyncio.Semaphore] = None
        self.pipeline: Optional[Pipeline] = None
        self.metrics: Optional[RuntimeMetrics] = None
        self.public_health_cache: dict = {"at": 0.0, "payload": None}
        self.service_status_cache: dict = {
            "at": 0.0, "payload": None, "aggregate_total": None,
            "aggregate_success": None, "file_signature": None,
        }
        self.service_status_lock: Optional[asyncio.Lock] = None
        self.upstream_client: Optional[httpx.AsyncClient] = None
        self.rate_hits: "OrderedDict[str, list[float]]" = OrderedDict()
        self.rate_lock = Lock()
        self.bearer = HTTPBearer(auto_error=False)
        self.dependency_health: Optional[DependencyHealth] = None
        self.resource_monitor: Optional[ResourceMonitor] = None
        self.file_log_path = ""
        self.file_log_writer: Optional[JSONLFileWriter] = None
        self.redis_client: Optional[Any] = None
        self.notice_store: Optional[NoticeStore] = None
        self.require_auth: Any = None
        self.require_admin: Any = None

    # ---- 请求期共享助手（与拆分前闭包行为逐行对应）----

    def client_ip(self, request: Request) -> str:
        remote_ip = request.client.host if request.client else "unknown"
        state = self.app.state
        if state.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for")
            if forwarded and remote_ip != "unknown":
                try:
                    remote_address = ipaddress.ip_address(remote_ip)
                except ValueError:
                    return remote_ip
                if any(remote_address in network
                       for network in state.trusted_proxy_cidrs):
                    return forwarded.split(",")[0].strip()
        return remote_ip

    def query_log_entry(self, body, client_ip: str, started: float,
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

    def login_failure_result(self, exc, run_id: str = "") -> dict:
        """登录失败时保留学校侧风控信号，避免异步入口丢失结构化分类。"""
        result = classify_error("login", exc.status_code, exc.body,
                                error_message=str(exc))
        if run_id:
            result["run_id"] = run_id
        return result

    async def check_rate_limit(self, ip: str) -> None:
        now = time.time()
        limit = self.app.state.rate_limit
        limited = False
        redis_unavailable = False
        if self.app.state.redis is None:
            limited = _memory_rate_allow(self.rate_hits, self.rate_lock, ip, now, limit)
        else:
            digest = hashlib.sha256(ip.encode("utf-8")).hexdigest()
            key = f"gw:rate:{digest}"
            try:
                count = int(await asyncio.wait_for(
                    self.app.state.redis.eval(
                        RATE_LIMIT_SCRIPT, 1, key, 60),
                    timeout=REDIS_OP_TIMEOUT_SECONDS))
                limited = count > limit
            except Exception as exc:
                LOG.warning("限流后端不可用：%s", exc.__class__.__name__)
                self.app.state.dependency_health.record(
                    "redis", "degraded", REDIS_DEGRADED_MESSAGE)
                self.metrics.observe_redis(True)
                redis_unavailable = True

        if redis_unavailable:
            limited = _memory_rate_allow(self.rate_hits, self.rate_lock, ip, now, limit)
        elif self.app.state.redis is not None:
            self.app.state.dependency_health.record("redis", "ok")
            self.metrics.observe_redis(False)
        if limited:
            log_query({"event": "rate_limited", "client_ip": ip,
                       "message": "rate limit exceeded"})
            raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                                detail="请求过于频繁，请稍后再试")

    async def record_query_log(self, entry: dict) -> None:
        """查询日志四路一致：内存、可选 Redis、可选文件和 stdout。"""
        entry = sanitize_entry(entry)
        entry["run_id"] = str(entry.get("run_id") or new_run_id())
        score = event_score(entry, time.time())
        self.app.state.local_history.add(
            "gw:v2:query-logs", entry, entry["run_id"], score)
        if entry.get("event") == "query":
            await self.record_service_status_count(bool(entry.get("success")))
        if self.app.state.file_log_writer is not None:
            self.app.state.file_log_writer.last_error = ""
            try:
                await asyncio.to_thread(
                    self.app.state.file_log_writer.write_raw, entry)
            except Exception as exc:
                self.app.state.file_log_writer.last_error = exc.__class__.__name__
                LOG.warning("查询日志文件写入失败：%s", exc.__class__.__name__)
        if self.app.state.redis_history is not None:
            try:
                await asyncio.wait_for(
                    self.app.state.redis_history.add(
                        "gw:v2:query-logs", entry, entry["run_id"], score),
                    timeout=REDIS_OP_TIMEOUT_SECONDS)
                self.app.state.dependency_health.record("redis", "ok")
            except Exception as exc:
                self.app.state.history_dirty = True
                self.app.state.dependency_health.record(
                    "redis", "degraded", REDIS_DEGRADED_MESSAGE)
                LOG.warning("查询日志写入 Redis 失败：%s", exc.__class__.__name__)
        log_query(entry)

    async def record_service_status_count(self, success: bool) -> None:
        if self.app.state.redis is None:
            return
        key = "gw:v2:service-status:accepted"
        try:
            pipeline = self.app.state.redis.pipeline(transaction=True)
            pipeline.hincrby(key, "total", 1)
            if success:
                pipeline.hincrby(key, "success", 1)
            await asyncio.wait_for(pipeline.execute(),
                                   timeout=REDIS_OP_TIMEOUT_SECONDS)
        except Exception as exc:
            LOG.warning("服务状态聚合计数写入失败：%s", exc.__class__.__name__)

    async def service_status_counts(self) -> tuple[Optional[int], Optional[int]]:
        if self.app.state.redis is None:
            return None, None
        try:
            counts = await asyncio.wait_for(
                self.app.state.redis.hgetall("gw:v2:service-status:accepted"),
                timeout=REDIS_OP_TIMEOUT_SECONDS,
            )
            if not counts:
                return None, None
            total = max(0, int(counts.get("total", 0)))
            success = max(0, int(counts.get("success", 0)))
            return total, min(success, total)
        except Exception:
            return None, None

    async def recent_query_logs(self, limit: int = 100) -> list[dict]:
        """读取跨副本热日志；Redis 不可用时降级本实例历史。"""
        if self.app.state.redis_history is not None:
            try:
                return await asyncio.wait_for(
                    self.app.state.redis_history.recent(
                        "gw:v2:query-logs", "gw:query-logs", limit),
                    timeout=REDIS_OP_TIMEOUT_SECONDS)
            except Exception as exc:
                self.app.state.dependency_health.record(
                    "redis", "degraded", REDIS_DEGRADED_MESSAGE)
                LOG.warning("查询日志读取 Redis 失败，降级本实例历史：%s",
                            exc.__class__.__name__)
        return self.app.state.local_history.recent("gw:v2:query-logs", limit)

    async def probe_query_proxy(self) -> str:
        """ready 探测只关心查询代理可达性，不暴露上游错误详情。"""
        try:
            response = await self.upstream_client.get(
                self.service_base_url + "/health",
                headers={"Authorization": "Bearer " + self.service_api_token}
                if self.service_api_token else {},
                timeout=2.0,
            )
            if response.status_code != 200:
                return "error"
            payload = response.json()
            return "ok" if payload.get("status") == "ok" else "degraded"
        except Exception:
            return "error"

    async def public_health_payload(self) -> dict:
        """公开站粗粒度链路状态；不暴露内部地址、错误体或凭据。"""
        now = time.monotonic()
        if self.public_health_cache["payload"] and \
                now - self.public_health_cache["at"] < 15:
            return self.public_health_cache["payload"]

        started = time.perf_counter()
        proxy_status, school_status = "down", "unknown"
        proxy_redis_state = "unknown"
        upstream_redis: dict | None = None
        proxy_latency: int | None = None
        school_latency: int | None = None
        try:
            response = await self.upstream_client.get(
                self.service_base_url + "/health",
                headers={"Authorization": "Bearer " + self.service_api_token}
                if self.service_api_token else {},
                timeout=5.0,
            )
            proxy_latency = int((time.perf_counter() - started) * 1000)
            if response.status_code == 200:
                upstream = response.json()
                proxy_status = "up" if upstream.get("status") == "ok" else "degraded"
                upstream_redis = upstream.get("redis")
                if isinstance(upstream_redis, dict):
                    if upstream_redis.get("mode") == "memory":
                        # 查询代理运行在内存模式：这是“未配置”而非故障
                        proxy_redis_state = "not-configured"
                    elif upstream_redis.get("ok") is True:
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
        self.app.state.dependency_health.record(
            "query_proxy", query_proxy_state)
        self.app.state.dependency_health.record(
            "query_proxy_redis", proxy_redis_state)
        school_state = {
            "up": "ok", "down": "error", "degraded": "degraded",
        }.get(school_status, "unknown")
        self.app.state.dependency_health.record("school_service", school_state)

        payload = {
            "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "site": _public_dependency("up"),
            "proxy": {
                **_public_dependency(proxy_status, proxy_latency),
                "redis": {
                    # 三态布尔 + mode：布尔投影必须能区分 ok/degraded/error/unknown，
                    # 否则 error 会被下游 _dependency_public_state 误读回 error/unknown 之外的状态
                    "ok": proxy_redis_state == "ok",
                    "degraded": proxy_redis_state == "degraded",
                    "error": proxy_redis_state == "error",
                    "mode": (upstream_redis or {}).get("mode")
                    if isinstance(upstream_redis, dict) else None,
                },
            },
            "school": _public_dependency(school_status, school_latency),
        }
        self.public_health_cache["at"] = now
        self.public_health_cache["payload"] = payload
        return payload

    async def service_status_cache_hit(self) -> Optional[dict]:
        """文件扫描缓存命中检查；聚合计数与文件签名一致且未过期才复用。"""
        writer = self.app.state.file_log_writer
        cache = self.service_status_cache
        if writer is None or self.cfg.service_status_cache_seconds <= 0 \
                or cache["payload"] is None:
            return None
        aggregate_total, aggregate_success = await self.service_status_counts()
        current_signature = _jsonl_signature(writer.path)
        if (
            time.monotonic() - cache["at"] >= self.cfg.service_status_cache_seconds
            or cache["aggregate_total"] != aggregate_total
            or cache["aggregate_success"] != aggregate_success
            or cache["file_signature"] != current_signature
        ):
            return None
        return cache["payload"]

    async def rebuild_service_status(self) -> dict:
        writer = self.app.state.file_log_writer
        cache = self.service_status_cache
        aggregate_total, aggregate_success = await self.service_status_counts()
        current_signature = _jsonl_signature(writer.path if writer else None)
        if writer is not None:
            scan = await asyncio.to_thread(
                _read_service_status_summary, writer, 100,
                self.cfg.service_status_scan_limit)
            items = scan["items"]
            accepted_total, accepted_success = _accepted_status_counts(
                aggregate_total, aggregate_success,
                scan["accepted_total"], scan["accepted_success"])
            source = "file"
            scanned = scan["scanned"]
            parse_errors = scan["parse_errors"]
            scan_limit = self.cfg.service_status_scan_limit
        else:
            raw_items = await self.recent_query_logs(limit=100)
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
            source = "redis" if self.app.state.redis_history is not None else "memory"
            scanned = len(raw_items)
            parse_errors = 0
            scan_limit = self.cfg.service_status_scan_limit

        payload = _service_status_payload(items)
        # 先补全完整响应字段，再存入缓存：缓存对象不再被原地改写，
        # 并发读者不会观察到半成品载荷
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
        if writer is not None:
            cache.update({
                "at": time.monotonic(), "payload": payload,
                "aggregate_total": aggregate_total,
                "aggregate_success": aggregate_success,
                "file_signature": current_signature,
            })
        return payload

    async def calendar_rate_limit(self, request: Request) -> None:
        """日历接口复用 /run 的按 IP 限流（Redis 可用时走 Redis，否则内存桶）。"""
        await self.check_rate_limit(self.client_ip(request))

    async def startup(self, app) -> None:
        """lifespan 启动序：文件日志检查 → 资源采样 → PDF 预热 → 通知 → 历史恢复 →
        Redis 探活 → 历史回填/通知备份后台任务。（异步任务 worker 由 main 随后启动）"""
        if app.state.file_log_writer is not None:
            try:
                await asyncio.to_thread(app.state.file_log_writer.check_access)
            except Exception as exc:
                raise RuntimeError(
                    "查询日志文件不可写（FILE_LOG_PATH）：%s" % exc.__class__.__name__
                ) from exc
        await self.resource_monitor.start()
        # PDF 模块预热：首次导入注册 CID 字体，放线程池执行，消除首单 PDF 尾延迟
        try:
            await asyncio.to_thread(_import_pdf_module)
        except Exception as exc:
            LOG.warning("PDF 模块预热失败：%s", exc.__class__.__name__)
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
                await asyncio.sleep(max(1.0, self.cfg.history_sync_interval_seconds))
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
                await asyncio.sleep(max(10.0, self.cfg.history_sync_interval_seconds))
                if not app.state.notice_store.redis_dirty:
                    continue
                try:
                    await app.state.notice_store.sync_redis()
                except Exception:
                    app.state.notice_store.redis_status = "degraded"

        app.state.notice_backup_task = asyncio.create_task(
            sync_notice_backup(), name="notice-backup")


def build_app_context(cfg: "Settings") -> AppContext:
    """构建共享上下文：原 create_app 前置装配段的一比一迁移。"""
    _validate_production(cfg.environment, cfg.auto_rotate_token, cfg.api_token,
                         cfg.admin_token)
    setup_logging()
    ctx = AppContext(cfg)
    ctx.instance_id = cfg.instance_id.strip() or socket.gethostname()
    ctx.file_instance_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", ctx.instance_id)[:128] or "instance"
    ctx.is_production = cfg.environment.strip().lower() in ("production", "prod")
    ctx.api_token = _resolve_api_token(cfg)
    ctx.dependency_health = DependencyHealth(initial={
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
    internal_jwxt_app: Optional[Any] = None
    upstream_transport: Optional[httpx.AsyncBaseTransport] = None
    if cfg.service_base_url.strip():
        ctx.service_base_url = cfg.service_base_url.rstrip("/")
        ctx.service_api_token = cfg.service_api_token
    else:
        # 单体模式：内嵌查询代理，进程内直连，无需网络与内部令牌
        jwxt_cfg = _load_jwxt_config()
        jwxt_cfg.token = secrets.token_urlsafe(32)
        internal_jwxt_app = _make_jwxt_app(jwxt_cfg, start_background=True)
        ctx.service_base_url = "http://jwxt.internal"
        ctx.service_api_token = jwxt_cfg.token
        upstream_transport = httpx.ASGITransport(app=internal_jwxt_app)
    ctx.internal_jwxt_app = internal_jwxt_app
    service = HTTPServiceClient(ctx.service_base_url, ctx.service_api_token,
                                cfg.service_timeout, transport=upstream_transport)
    # 日历订阅：仅在启用且配置合法时构建；配置非法直接阻止启动（宁可失败，不生成错误日历）
    if cfg.calendar_enabled:
        try:
            ctx.calendar_service = build_calendar_service(CalendarSettings(
                enabled=True,
                master_key=cfg.calendar_master_key,
                db_path=cfg.calendar_db_path,
                config_path=cfg.calendar_config_path,
                public_base_url=cfg.calendar_public_base_url,
                default_refresh=cfg.calendar_default_refresh_seconds,
                min_refresh=cfg.calendar_min_refresh_seconds,
                max_failures=cfg.calendar_max_failures,
                pause_hours=cfg.calendar_pause_hours,
                max_per_owner=cfg.calendar_max_per_owner,
                ttl_days=cfg.calendar_ttl_days,
            ), service)
        except (CalendarConfigError, CalendarCryptoError) as exc:
            raise RuntimeError("日历订阅配置非法：%s" % exc) from exc
    ctx.llm_slots = asyncio.Semaphore(max(1, cfg.llm_concurrency))
    ctx.pdf_slots = asyncio.Semaphore(max(1, cfg.pdf_concurrency))
    ctx.login_slots = asyncio.Semaphore(max(1, min(8, cfg.global_concurrency)))
    ctx.pipeline = Pipeline(service=service, llm=llm,
                            request_timeout=cfg.request_timeout,
                            llm_semaphore=ctx.llm_slots,
                            pdf_semaphore=ctx.pdf_slots,
                            dependency_health=ctx.dependency_health,
                            metrics=RuntimeMetrics())
    ctx.metrics = ctx.pipeline.metrics
    assert ctx.metrics is not None
    ctx.upstream_client = httpx.AsyncClient(
        timeout=cfg.service_timeout,
        limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        follow_redirects=False,
        transport=upstream_transport,
    )
    ctx.query_slots = asyncio.Semaphore(max(1, cfg.global_concurrency))
    ctx.service_status_lock = asyncio.Lock()

    ctx.file_log_path = cfg.file_log_path.replace("{instance_id}", ctx.file_instance_id)
    file_log_writer = None
    if cfg.file_log_enabled and ctx.file_log_path.strip():
        if "{instance_id}" in cfg.file_log_path:
            path_prefix = cfg.file_log_path[:cfg.file_log_path.index("{instance_id}")]
            file_log_collection_root = os.path.dirname(path_prefix)
        else:
            file_log_collection_root = os.path.dirname(ctx.file_log_path)
        file_log_writer = JSONLFileWriter(
            path=ctx.file_log_path,
            max_bytes=cfg.file_log_max_bytes,
            backup_count=cfg.file_log_backup_count,
            collection_root=file_log_collection_root,
        )
    ctx.resource_monitor = ResourceMonitor(
        log_path=ctx.file_log_path,
        interval=cfg.resource_monitor_interval_seconds,
        history_size=cfg.resource_monitor_history_size,
        # 单体默认只有 app + frontend；配置外部 Redis 时才把它计入期望服务
        expected_services=("app", "frontend", "redis") if cfg.redis_url.strip()
        else ("app", "frontend"),
    )

    redis_client = None
    if cfg.redis_url.strip():
        from redis import asyncio as aioredis

        try:
            redis_client = aioredis.from_url(
                cfg.redis_url,
                decode_responses=True,
                socket_connect_timeout=3.0,
                socket_timeout=3.0,
                health_check_interval=30,
            )
        except Exception as exc:  # pragma: no cover
            raise RuntimeError("Redis 连接失败（REDIS_URL）：%s" % exc)
    ctx.redis_client = redis_client
    ctx.file_log_writer = file_log_writer
    ctx.notice_store = NoticeStore(
        path=cfg.notice_fallback_path,
        redis=redis_client,
        max_active=cfg.notice_active_max,
        max_history=cfg.notice_history_max,
        compact_after=cfg.notice_compact_after,
    )

    # 鉴权依赖：小闭包仅封装 Depends(bearer) 的注入签名，共享状态仍来自 ctx
    bearer = ctx.bearer
    api_token = ctx.api_token

    def require_auth(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
        if api_token and (credentials is None or not secrets.compare_digest(
                credentials.credentials, api_token)):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="token required")

    def require_admin(credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer)) -> None:
        admin_token = cfg.admin_token.strip()
        if not admin_token or credentials is None or not secrets.compare_digest(
                credentials.credentials, admin_token):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="admin area unavailable")

    ctx.require_auth = require_auth
    ctx.require_admin = require_admin
    return ctx
