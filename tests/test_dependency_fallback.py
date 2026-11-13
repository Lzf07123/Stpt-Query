"""外部依赖降级：Redis 熔断、内存兜底与管理端提示契约。"""
from __future__ import annotations

import asyncio
from unittest.mock import Mock

import redis
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.jwxt_redis import (
    RedisBackend, ResilientRedisSessionStore, ResilientRedisTTLCache,
)
from app.jwxt_state import SessionStore, TTLCache


def test_redis_backend_stays_available_when_initial_ping_fails():
    client = Mock()
    client.ping.side_effect = redis.ConnectionError("redis down")

    backend = RedisBackend.from_client(client)
    assert backend.probe() is False

    assert backend.degraded is True
    assert backend.attempt_allowed() is False


def test_redis_probe_recovers_the_circuit():
    client = Mock()
    client.ping.side_effect = redis.ConnectionError("redis down")
    backend = RedisBackend.from_client(client)
    assert backend.probe() is False
    assert backend.degraded is True

    client.ping.side_effect = None
    client.ping.return_value = True

    assert backend.probe() is True
    assert backend.degraded is False


def test_session_store_falls_back_to_local_memory_on_redis_error():
    backend = RedisBackend.from_client(Mock())
    primary = Mock()
    primary.create.side_effect = redis.ConnectionError("redis down")
    store = ResilientRedisSessionStore(
        backend, 300, 100, primary=primary,
        fallback=SessionStore(300, 100))

    sid = store.create("2023000001", [], {}, pwd_hash="hash")

    assert sid
    assert store.get(sid)["username"] == "2023000001"
    assert backend.degraded is True


def test_cache_store_falls_back_to_local_memory_on_redis_error():
    backend = RedisBackend.from_client(Mock())
    primary = Mock()
    primary.set.side_effect = redis.TimeoutError("redis timeout")
    cache = ResilientRedisTTLCache(
        backend, 300, 100, primary=primary, fallback=TTLCache(300, 100))

    cache.set("grades:key", {"success": True})

    assert cache.get("grades:key") == {"success": True}
    assert backend.degraded is True


ADMIN = {"Authorization": "Bearer " + ("b" * 24)}


def test_admin_metrics_exposes_dependency_degradations(tmp_path):
    class FailingRedis:
        async def ping(self):
            raise redis.ConnectionError("redis down")

        async def aclose(self):
            return None

    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="gateway-token",
        admin_token="b" * 24,
        service_base_url="http://127.0.0.1:9",
        service_api_token="upstream-token",
        llm_api_key="",
        redis_url="redis://redis:6379/0",
        file_log_path=str(tmp_path / "queries.jsonl"),
    )
    app = create_app(cfg)
    app.state.redis = FailingRedis()

    with TestClient(app) as client:
        body = client.get("/api/v1/admin/metrics", headers=ADMIN).json()["data"]

    states = {item["key"]: item for item in body["dependencies"]}
    assert states["redis"]["status"] == "degraded"
    assert states["llm"]["status"] == "not-configured"
    assert states["query_proxy"]["status"] in ("error", "unknown")
    assert any(item["key"] == "redis" for item in body["degradations"])


def test_admin_metrics_degrades_when_redis_history_hangs(tmp_path):
    class HangingRedis:
        async def ping(self):
            raise redis.ConnectionError("redis down")

        async def recent(self, *args, **kwargs):
            await asyncio.sleep(2.5)

        async def aclose(self):
            return None

    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="gateway-token",
        admin_token="b" * 24,
        service_base_url="http://127.0.0.1:9",
        llm_api_key="",
        redis_url="redis://redis:6379/0",
        file_log_path=str(tmp_path / "queries.jsonl"),
    )
    app = create_app(cfg)
    app.state.redis = HangingRedis()
    app.state.redis_history = HangingRedis()

    with TestClient(app) as client:
        body = client.get("/api/v1/admin/metrics", headers=ADMIN).json()["data"]

    assert body["services"]["redis"] == "degraded"
    assert any(item["key"] == "redis" for item in body["degradations"])


def test_dependency_public_state_memory_mode_is_not_configured():
    """查询代理内存模式必须映射为灰色未配置，不得被布尔投影误判为红色异常。

    曾因 /health/public 的 redis 布尔投影区分不出 unknown 与 error，
    单实例部署下后台把「查询代理 Redis」显示成异常。
    """
    from app.context import _dependency_public_state

    memory_payload = {
        "proxy": {"status": "up",
                  "redis": {"ok": False, "degraded": False,
                            "error": False, "mode": "memory"}},
        "school": {"status": "down"},
    }
    _, _, redis_state = _dependency_public_state(memory_payload)
    assert redis_state == "not-configured"

    broken_payload = {
        "proxy": {"status": "up",
                  "redis": {"ok": False, "degraded": False,
                            "error": True, "mode": "redis"}},
        "school": {"status": "unknown"},
    }
    assert _dependency_public_state(broken_payload)[2] == "error"

    unknown_payload = {
        "proxy": {"status": "up",
                  "redis": {"ok": None, "mode": "redis", "starting": True}},
        "school": {"status": "unknown"},
    }
    assert _dependency_public_state(unknown_payload)[2] == "unknown"
