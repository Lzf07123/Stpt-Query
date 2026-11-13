"""HTTP 层测试：探针端点、鉴权、生产校验与限流。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobStore
from app.main import Settings, create_app
from app.pipeline import ServiceError
from app.schema import SessionWorkflowRequest


def _client() -> TestClient:
    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
    )
    return TestClient(create_app(cfg))


def test_liveness():
    resp = _client().get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_without_redis():
    resp = _client().get("/health/ready")
    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "not-ready"
    assert body["redis"] == "not-configured"
    assert body["query_proxy"] == "error"


def test_file_log_path_is_isolated_per_instance(tmp_path):
    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
        file_log_enabled=True,
        file_log_path=str(tmp_path / "{instance_id}" / "queries.jsonl"),
        instance_id="replica/one",
    )

    app = create_app(cfg)

    assert app.state.file_log_writer.path == str(
        tmp_path / "replica_one" / "queries.jsonl")


def test_public_health_reports_coarse_dependencies():
    resp = _client().get("/api/v1/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["code"] == "OK"
    data = body["data"]
    assert data["site"]["status"] == "up"
    assert data["proxy"]["status"] == "down"
    assert data["school"]["status"] == "unknown"
    assert "127.0.0.1:9" not in str(body)


def test_run_requires_bearer_token():
    resp = _client().post("/api/v1/run", json={
        "username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "request.unauthorized"


def test_run_with_token_reaches_pipeline():
    resp = _client().post(
        "/api/v1/run",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    # 上游 127.0.0.1:9 不可达 → 真实状态码 + 分类错误信封，而非 500
    assert resp.status_code == 502
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "school.upstream.unavailable"
    assert body["data"]["kind"] == "login"
    assert body["data"]["diagnosis"]["report"]
    assert body["meta"]["trace_id"]


def test_run_records_structured_query_log_without_password(caplog):
    import json
    import logging

    with caplog.at_level(logging.INFO, logger="edu-query-app.query"):
        resp = _client().post(
            "/api/v1/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 502
    query_records = [
        r for r in caplog.records
        if getattr(r, "name", "") == "edu-query-app.query"
        and '"event": "query"' in r.getMessage()
    ]
    assert query_records, "应输出一条结构化查询日志"
    data = json.loads(query_records[0].getMessage())
    assert data["username"] == "2023000001"
    assert data["option"] == "成绩"
    assert data["success"] is False and data["kind"] == "login_error"
    assert data["run_id"]
    assert data["analysis"] is False
    assert data["analysis_usage"] == "—"
    assert data["response_summary"].startswith("HTTP 0；响应：")
    assert "password" not in query_records[0].getMessage()


def test_run_internal_error_hides_exception_class():
    class FailingPipeline:
        async def run(self, request):
            raise RuntimeError("SecretDiagnosis")

    app = create_app(Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
    ))
    app.state.pipeline = FailingPipeline()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal.error"
    assert body["message"] == "服务内部异常，请稍后重试"
    assert body["data"]["kind"] == "internal"
    assert "SecretDiagnosis" not in resp.text


def test_query_logs_requires_bearer_token():
    resp = _client().get("/api/v1/query-logs")
    assert resp.status_code == 401


def test_query_logs_returns_entries_after_run():
    client = _client()
    resp = client.post(
        "/api/v1/run",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 502
    resp = client.get("/api/v1/query-logs",
                      headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["total"] >= 1
    assert data["logs"][0]["username"] == "2023000001"
    assert data["logs"][0]["kind"] == "login_error"
    assert data["logs"][0]["response_summary"].startswith("HTTP 0；响应：")
    serialized = str(body)
    for forbidden in ("password", "session", "token"):
        assert forbidden not in serialized


class _FakeUpstream:
    def __init__(self, content, status_code, headers):
        self.content = content
        self.status_code = status_code
        self.headers = headers


def test_jump_passthrough_forwards_and_keeps_token(monkeypatch):
    captured = {}

    async def fake_raw(base_url, token, path_qs, timeout, client):
        captured["base_url"] = base_url
        captured["token"] = token
        captured["path_qs"] = path_qs
        return _FakeUpstream(b"<html>jump-bridge</html>", 200,
                             {"Content-Type": "text/html; charset=utf-8"})

    monkeypatch.setattr("app.main._raw_get", fake_raw)
    resp = _client().get("/jump/go?code=abc123")
    assert resp.status_code == 200
    assert resp.content == b"<html>jump-bridge</html>"
    assert resp.headers["content-type"] == "text/html; charset=utf-8"
    assert captured["path_qs"] == "/jump/go?code=abc123"
    assert captured["token"] == "x"


def test_schedule_export_passthrough_copies_disposition(monkeypatch):
    async def fake_raw(base_url, token, path_qs, timeout, client):
        return _FakeUpstream(b"DOCBINARY", 200, {
            "Content-Type": "application/msword",
            "Content-Disposition": "attachment; filename=\"schedule.doc\"",
        })

    monkeypatch.setattr("app.main._raw_get", fake_raw)
    resp = _client().get("/get_schedule/export?code=xyz")
    assert resp.status_code == 200
    assert resp.content == b"DOCBINARY"
    assert "attachment" in resp.headers["content-disposition"]


def test_passthrough_rejects_unlisted_path():
    # /jump 基路径不在白名单（仅 /jump/go），返回 404
    resp = _client().get("/jump")
    assert resp.status_code == 404


def test_production_rejects_default_weak_token():
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        create_app(Settings(_env_file=None, environment="production",
                            auto_rotate_token=False, api_token="change-me"))


def test_production_rejects_short_token():
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        create_app(Settings(_env_file=None, environment="production",
                            auto_rotate_token=False, api_token="short-token"))


def test_production_rejects_weak_admin_token():
    with pytest.raises(RuntimeError, match="ADMIN_TOKEN"):
        create_app(Settings(
            _env_file=None,
            environment="production", auto_rotate_token=False,
            api_token="a" * 24, admin_token="admin-token"))


def test_production_accepts_strong_fixed_tokens():
    strong = "a" * 24
    cfg = Settings(_env_file=None,
                   environment="production", auto_rotate_token=False, api_token=strong,
                   admin_token="b" * 24,
                   service_base_url="http://127.0.0.1:9", service_api_token="x")
    app = create_app(cfg)
    assert app.state.api_token == strong


def test_rate_limit_returns_429_by_client_ip():
    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
        rate_limit=2,
    )
    client = TestClient(create_app(cfg))
    headers = {"Authorization": "Bearer test-token"}
    payload = {"username": "2023000001", "password": "pw", "option": "成绩"}
    assert client.post("/api/v1/run", headers=headers, json=payload).status_code == 502
    assert client.post("/api/v1/run", headers=headers, json=payload).status_code == 502
    resp = client.post("/api/v1/run", headers=headers, json=payload)
    assert resp.status_code == 429
    assert resp.json()["code"] == "request.rate_limited"


def test_async_jobs_require_redis():
    resp = _client().post(
        "/api/v1/run/jobs",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["code"] == "capacity.jobs_disabled"
    assert "REDIS_URL" in body["message"]


class _FakeAsyncRedis:
    def __init__(self):
        self.counters = {}

    async def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    async def expire(self, key, seconds):
        return True

    async def zcard(self, key):
        return 0

    async def aclose(self):
        return None


class _FailingLoginService:
    def __init__(self, exc):
        self.exc = exc

    async def post(self, path, payload):
        raise self.exc


def _async_jobs_client_with_login_failure(exc):
    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
    )
    app = create_app(cfg)
    fake_redis = _FakeAsyncRedis()
    app.state.redis = fake_redis
    app.state.job_store = JobStore(fake_redis)
    app.state.redis_history = None
    app.state.pipeline.service = _FailingLoginService(exc)
    return TestClient(app)


def test_async_login_failure_preserves_school_lock_time():
    exc = ServiceError(
        401,
        {"data": {"code": "USERLOCK", "data": "2026-08-29 13:22:35"}},
        "上游服务 HTTP 401",
    )
    client = _async_jobs_client_with_login_failure(exc)
    resp = client.post(
        "/api/v1/run/jobs",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})

    # 统一语义：学校风控/锁定 → 403（注册表 school.cas.locked）
    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "school.cas.locked"
    assert body["data"]["kind"] == "login"
    assert body["meta"]["trace_id"]
    assert body["meta"]["lock_until"] == "2026-08-29 13:22:35"
    assert "学校风控或账号临时锁定" in body["data"]["diagnosis"]["report"]


def test_async_login_success_without_session_stays_structured():
    class _NoSessionLoginService:
        async def post(self, path, payload):
            if path != "/login":
                return {}
            return {
                "success": False,
                "data": {"code": "USERLOCK", "data": "2026-08-29 13:22:35"},
            }

    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
    )
    app = create_app(cfg)
    fake_redis = _FakeAsyncRedis()
    app.state.redis = fake_redis
    app.state.job_store = JobStore(fake_redis)
    app.state.redis_history = None
    app.state.pipeline.service = _NoSessionLoginService()

    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/run/jobs",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})

    assert resp.status_code == 403
    body = resp.json()
    assert body["code"] == "school.cas.locked"
    assert body["data"]["kind"] == "login"
    assert body["meta"]["lock_until"] == "2026-08-29 13:22:35"


async def test_async_worker_links_pipeline_phases_to_job_status():
    import asyncio
    import time

    from app.schema import JobStatusResponse

    job_id = "0" * 32
    calls = []

    class FakeAsyncRedis:
        async def ping(self):
            return True

        async def aclose(self):
            return None

        async def lpush(self, key, value):
            return 1

        async def ltrim(self, key, start, stop):
            return True

    class FakeJobStore:
        def __init__(self):
            self.claimed = False
            self.doc = {
                "state": "queued", "phase": "queued", "phase_started_at": time.time()
            }

        async def claim(self, ttl_seconds):
            if self.claimed:
                await asyncio.sleep(0.05)
                return None
            self.claimed = True
            return job_id, {
                "username": "2023000001", "option": "成绩", "check": True
            }, "short-lived-session", "127.0.0.1", time.time()

        async def mark_phase(self, job_id, phase, ttl_seconds):
            calls.append(phase)
            self.doc["phase"] = phase
            return True

        async def mark_running(self, job_id, started_at, ttl_seconds):
            self.doc["state"] = "running"
            return True

        async def complete(self, job_id, result, ttl_seconds):
            self.doc.update({"state": "success", "result": result})

        async def status(self, job_id):
            return JobStatusResponse(
                job_id=job_id,
                state=self.doc["state"],
                phase=self.doc["phase"],
                enqueued_at=time.time(),
            )

    class PhasePipeline:
        async def run(self, body, session=None, progress_cb=None):
            await progress_cb("querying")
            await progress_cb("analyzing")
            return {"success": True, "kind": "grades", "run_id": "run-1"}

    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
        job_workers=1,
    )
    app = create_app(cfg)
    fake_redis = FakeAsyncRedis()
    fake_store = FakeJobStore()
    app.state.redis = fake_redis
    app.state.job_store = fake_store
    app.state.pipeline = PhasePipeline()

    with TestClient(app):
        for _ in range(50):
            if fake_store.doc.get("state") == "success":
                break
            await asyncio.sleep(0.02)
        status = await app.state.job_store.status(job_id)

    assert status is not None
    assert status.state == "success"
    assert status.phase == "analyzing"
    assert calls == ["dispatching", "querying", "querying", "analyzing"]


def test_memory_rate_allow_writes_back_filtered_timestamps():
    """限流桶写回归约：被限流也写回过滤结果；空桶删 key；LRU 硬上限保持。"""
    from collections import OrderedDict
    from threading import Lock

    from app.context import MAX_RATE_KEYS, _memory_rate_allow

    hits: OrderedDict = OrderedDict()
    lock = Lock()
    now = 1000.0

    assert _memory_rate_allow(hits, lock, "ip-a", now, 2) is False
    assert hits["ip-a"] == [1000.0]
    assert _memory_rate_allow(hits, lock, "ip-a", now + 1, 2) is False
    assert _memory_rate_allow(hits, lock, "ip-a", now + 2, 2) is True
    # 被限流的请求不追加新时间戳，但过滤结果仍写回
    assert hits["ip-a"] == [1000.0, 1001.0]

    # 过滤：60 秒外的旧时间戳被丢弃，桶清空后重新开始计数
    assert _memory_rate_allow(hits, lock, "ip-a", now + 61, 2) is False
    assert hits["ip-a"] == [1061.0]

    # 被限流路径同样写回：过旧条目不会滞留内存（修复点）
    hits["ip-c"] = [now - 5, now - 4, now - 120]
    assert _memory_rate_allow(hits, lock, "ip-c", now, 2) is True
    assert hits["ip-c"] == [now - 5, now - 4]

    # 空桶（全部过期且无新增）不驻留内存
    hits["ip-b"] = [now - 120]
    assert _memory_rate_allow(hits, lock, "ip-b", now + 100, 0) is True
    assert "ip-b" not in hits

    # LRU 硬上限：超出 4096 后淘汰最久未使用 key
    lru_hits: OrderedDict = OrderedDict()
    for index in range(MAX_RATE_KEYS):
        lru_hits[f"ip-{index:05d}"] = [now]
    lru_hits.move_to_end("ip-00000")
    assert _memory_rate_allow(lru_hits, lock, "ip-new", now, 1) is False
    assert len(lru_hits) == MAX_RATE_KEYS
    assert "ip-00001" not in lru_hits
    assert "ip-new" in lru_hits


async def test_run_releases_query_slot_before_logging(monkeypatch):
    """槽位释放先于日志写入：pipeline 落定即释放，日志/日历收尾在槽位之外。"""
    import httpx

    import app.context as context_module

    events = []
    app = create_app(Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
        global_concurrency=1,
        file_log_enabled=False,
    ))
    slots = app.state.query_slots

    class RecordingPipeline:
        async def run(self, body):
            events.append(("pipeline", slots.locked()))
            return {"success": True, "kind": "grades", "run_id": "run-1",
                    "output": "ok", "meta": {}}

    def fake_log_query(entry):
        events.append(("log", slots.locked()))

    import app.context as context_module
    monkeypatch.setattr(context_module, "log_query", fake_log_query)
    app.state.pipeline = RecordingPipeline()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    # 槽位持有期间跑 pipeline（locked=True），日志写入时槽位已释放（locked=False）
    assert events == [("pipeline", True), ("log", False)]


async def test_job_worker_idle_backoff_grows_and_resets():
    """空轮询指数退避（0.2s→1s 封顶），领到任务后重置回 0.2s。"""
    import time as time_module

    app = create_app(Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
        job_workers=1,
        file_log_enabled=False,
    ))

    class PingableRedis:
        async def ping(self):
            return True

        async def aclose(self):
            return None

    class BackoffJobStore:
        def __init__(self):
            self.claim_times = []
            self.completed = []

        async def claim(self, ttl_seconds):
            self.claim_times.append(time_module.monotonic())
            if len(self.claim_times) == 5:
                return ("0" * 32, SessionWorkflowRequest(
                    username="2023000001", option="成绩"),
                    "short-lived-session", "127.0.0.1", time_module.time())
            return None

        async def mark_phase(self, job_id, phase, ttl_seconds):
            return True

        async def mark_running(self, job_id, started_at, ttl_seconds):
            return True

        async def complete(self, job_id, result, ttl_seconds):
            self.completed.append(job_id)

        async def reap_stale(self, stale_after_seconds, ttl_seconds):
            return 0

    class InstantPipeline:
        async def run(self, body, session=None, progress_cb=None):
            return {"success": True, "kind": "grades", "run_id": "run-1",
                    "output": "ok", "meta": {}}

    store = BackoffJobStore()
    app.state.redis = PingableRedis()
    app.state.job_store = store
    app.state.pipeline = InstantPipeline()

    with TestClient(app):
        for _ in range(200):
            if len(store.claim_times) >= 7:
                break
            await asyncio.sleep(0.05)

    assert len(store.claim_times) >= 7, "worker 应持续空轮询并完成一次任务"
    assert store.completed, "第 5 次 claim 应交付一个任务"
    gaps = [store.claim_times[i + 1] - store.claim_times[i]
            for i in range(min(6, len(store.claim_times) - 1))]
    # 期望退避序列：0.2 → 0.4 → 0.8 → 1.0 →(交付后立即可 claim) → 0.2（重置）
    assert 0.15 <= gaps[0] < 0.5, "首次空轮询约 0.2s：%s" % gaps
    assert gaps[1] > gaps[0] * 1.5, "第二次空轮询应约 0.4s（指数增长）：%s" % gaps
    assert gaps[2] > gaps[1] * 1.5, "第三次空轮询应约 0.8s：%s" % gaps
    assert gaps[2] <= gaps[3] < 2.0, "退避封顶 1s：%s" % gaps
    assert gaps[4] < 0.5, "任务交付后下一次 claim 立即执行：%s" % gaps
    assert gaps[5] < gaps[3] * 0.5, \
        "任务完成后空轮询间隔重置回 0.2s 量级（不再继续 1s 封顶）：%s" % gaps
