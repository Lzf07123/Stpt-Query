"""跨层安全与过载保护契约。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import re
import redis
from fastapi.testclient import TestClient

from app.main import Settings, create_app


HEADERS = {"Authorization": "Bearer security-token"}
PAYLOAD = {"username": "2023000001", "password": "pw", "option": "成绩"}


def _cfg(**kwargs):
    options = {
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "security-token",
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "upstream-token",
        "llm_api_key": "",
        "rate_limit": 100,
    }
    options.update(kwargs)
    return Settings(**options, _env_file=None)


async def test_global_concurrency_rejects_when_no_slot_is_free():
    class BusyPipeline:
        def __init__(self):
            self.active = 0

        async def run(self, request):
            self.active += 1
            try:
                await asyncio.sleep(0.15)
            finally:
                self.active -= 1

    pipeline = BusyPipeline()
    app = create_app(_cfg(global_concurrency=1, concurrency_wait_timeout=0))
    app.state.pipeline = pipeline
    await app.state.query_slots.acquire()
    with TestClient(app) as client:
        assert client.post("/run", headers=HEADERS, json=PAYLOAD).status_code == 503
        app.state.query_slots.release()
        assert client.post("/run", headers=HEADERS, json=PAYLOAD).status_code == 200


def test_redis_rate_limiter_degrades_to_local_memory():
    class FakeAsyncRedis:
        async def incr(self, key):
            raise redis.ConnectionError("redis down")

        async def expire(self, key, seconds):
            raise redis.ConnectionError("redis down")

    fake_redis = FakeAsyncRedis()
    app = create_app(_cfg(rate_limit=2))
    app.state.redis = fake_redis
    client = TestClient(app)

    assert client.post("/run", headers=HEADERS, json=PAYLOAD).status_code == 200
    assert client.post("/run", headers=HEADERS, json=PAYLOAD).status_code == 200
    states = {item["key"]: item["status"]
              for item in app.state.dependency_health.snapshot()}
    assert states["redis"] == "degraded"
    assert client.post("/run", headers=HEADERS, json=PAYLOAD).status_code == 429


def test_edge_hides_sensitive_query_strings_and_sets_csp():
    config = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    assert 'location = /jump/go {' in config
    assert 'access_log off;' in config
    assert '"$request_method $uri $server_protocol"' in config
    assert "img-src 'self' data:" in config


def test_query_log_file_uses_persistent_named_volume():
    compose = open("docker-compose.yml", encoding="utf-8").read()
    volume_section = compose.split("\nvolumes:\n", 1)[1]
    assert "format-query-logs:/var/log/edu-query" in compose
    assert "name: edu-query-app_format-query-logs-persistent" in volume_section
    assert "type: tmpfs" not in volume_section


def test_runtime_memory_guardrails_are_configured():
    compose = open("docker-compose.yml", encoding="utf-8").read()
    assert "mem_limit: ${JWXT_MEM_LIMIT:-512m}" in compose
    assert "mem_limit: ${FORMAT_MEM_LIMIT:-256m}" in compose
    assert "mem_limit: ${FRONTEND_MEM_LIMIT:-64m}" in compose
    assert compose.count("MALLOC_ARENA_MAX=${MALLOC_ARENA_MAX:-2}") == 2
    nginx = open("frontend/nginx.conf", encoding="utf-8").read()
    assert "worker_processes auto;" in nginx


def test_inline_script_hashes_are_covered_by_csp():
    config = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    declared_hashes = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", config))
    for page in ("frontend/static/index.html", "frontend/static/admin.html"):
        html = open(page, encoding="utf-8").read()
        inline_scripts = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
        assert inline_scripts
        for script in inline_scripts:
            digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
            assert f"sha256-{digest}" in declared_hashes
