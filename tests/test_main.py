"""HTTP 层测试：探针端点、鉴权、生产校验与限流。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.jobs import JobStore
from app.main import Settings, create_app
from app.pipeline import ServiceError


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
    resp = _client().get("/health/public")
    assert resp.status_code == 200
    body = resp.json()
    assert body["site"]["status"] == "up"
    assert body["proxy"]["status"] == "down"
    assert body["school"]["status"] == "unknown"
    assert "127.0.0.1:9" not in str(body)


def test_run_requires_bearer_token():
    resp = _client().post("/run", json={
        "username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 401


def test_run_with_token_reaches_pipeline():
    resp = _client().post(
        "/run",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    # 上游 127.0.0.1:9 不可达 → 应返回分类后的友好错误而非 500
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["kind"] == "login_error"
    assert body["run_id"]


def test_run_records_structured_query_log_without_password(caplog):
    import json
    import logging

    with caplog.at_level(logging.INFO, logger="edu-query-app.query"):
        resp = _client().post(
            "/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 200
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
            "/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "internal_error"
    assert body["output"] == "服务内部异常，请稍后重试"
    assert "SecretDiagnosis" not in resp.text


def test_query_logs_requires_bearer_token():
    resp = _client().get("/query-logs")
    assert resp.status_code == 401


def test_query_logs_returns_entries_after_run():
    client = _client()
    resp = client.post(
        "/run",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 200
    resp = client.get("/query-logs", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["logs"][0]["username"] == "2023000001"
    assert body["logs"][0]["kind"] == "login_error"
    assert body["logs"][0]["response_summary"].startswith("HTTP 0；响应：")
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
    assert client.post("/run", headers=headers, json=payload).status_code == 200
    assert client.post("/run", headers=headers, json=payload).status_code == 200
    assert client.post("/run", headers=headers, json=payload).status_code == 429


def test_async_jobs_require_redis():
    resp = _client().post(
        "/run/jobs",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 503
    assert "REDIS_URL" in resp.json()["detail"]


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
        "/run/jobs",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})

    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["kind"] == "login_error"
    assert detail["run_id"]
    assert detail["meta"]["lock_until"] == "2026-08-29 13:22:35"
    assert "学校风控或账号临时锁定" in detail["output"]


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
            "/run/jobs",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})

    assert resp.status_code == 401
    detail = resp.json()["detail"]
    assert detail["kind"] == "login_error"
    assert detail["meta"]["lock_until"] == "2026-08-29 13:22:35"


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
