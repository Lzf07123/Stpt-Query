"""服务状态持久化与跨客户端共享契约。"""
from __future__ import annotations

import json
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.querylog import JSONLFileWriter


HEADERS = {"Authorization": "Bearer test-token"}


def _cfg(tmp_path, **kwargs) -> Settings:
    options = {
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "test-token",
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "x",
        "llm_api_key": "",
        "file_log_enabled": True,
        "file_log_path": str(tmp_path / "queries.jsonl"),
    }
    options.update(kwargs)
    return Settings(**options)


def _write_query_event(writer: JSONLFileWriter, *, success: bool, kind: str,
                       username: str = "2023000001") -> None:
    writer.write_raw({
        "event": "query",
        "time": "2026-08-28T10:00:00+08:00",
        "run_id": "run-1",
        "client_ip": "192.168.1.2",
        "username": username,
        "option": "成绩",
        "success": success,
        "kind": kind,
    })


def test_service_status_restores_persistent_query_events_without_log_fields(tmp_path):
    cfg = _cfg(tmp_path)
    writer = JSONLFileWriter(cfg.file_log_path)
    _write_query_event(writer, success=False, kind="login_error")
    _write_query_event(writer, success=True, kind="grades", username="2023000002")

    with TestClient(create_app(cfg)) as client:
        response = client.get("/service-status", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["success"] == 1
    assert body["availability"] == 50.0
    assert body["results"][0]["kind"] == "grades"
    assert body["results"][1]["kind"] == "login_error"
    assert all(set(item) == {"success", "kind", "time"} for item in body["results"])
    assert "2023000001" not in response.text
    assert "2023000002" not in response.text
    assert "192.168.1.2" not in response.text


def test_service_status_restores_shared_file_after_restart(tmp_path):
    cfg = _cfg(tmp_path)
    writer = JSONLFileWriter(cfg.file_log_path)
    _write_query_event(writer, success=True, kind="grades")

    with TestClient(create_app(cfg)) as first:
        assert first.get("/service-status", headers=HEADERS).json()["total"] == 1
    with TestClient(create_app(cfg)) as second:
        body = second.get("/service-status", headers=HEADERS).json()

    assert body["total"] == 1
    assert body["results"][0]["success"] is True
    assert body["results"][0]["kind"] == "grades"


def test_redis_service_status_is_shared_before_file_and_memory(tmp_path):
    class FakeAsyncRedis:
        def __init__(self):
            self.values: dict[str, list[str]] = {}

        async def incr(self, key: str) -> int:
            return 1

        async def lpush(self, key: str, value: str) -> int:
            self.values.setdefault(key, []).insert(0, value)
            return len(self.values[key])

        async def ltrim(self, key: str, start: int, stop: int) -> bool:
            self.values[key] = self.values[key][start:stop + 1]
            return True

        async def expire(self, key: str, seconds: int) -> bool:
            assert seconds == 60
            return True

        async def ping(self) -> bool:
            return True

        async def lrange(self, key: str, start: int, stop: int) -> list[str]:
            return self.values.get(key, [])[start:stop + 1]

    cfg = _cfg(tmp_path, file_log_enabled=False)
    app = create_app(cfg)
    redis = FakeAsyncRedis()
    app.state.redis = redis
    client = TestClient(app)

    assert client.post(
        "/run", headers=HEADERS,
        json={"username": "2023000001", "password": "pw", "option": "成绩"},
    ).status_code == 200
    body = client.get("/service-status", headers=HEADERS).json()

    assert body["total"] == 1
    assert body["results"][0]["success"] is False
    assert body["results"][0]["kind"] == "login_error"
    assert json.loads(redis.values["gw:service-status"][0])["success"] is False
