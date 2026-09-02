"""资源采样与热路径 I/O 缓存契约。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.main import Settings, create_app
from app.notices import NoticeCreate, NoticeStore


def _settings(tmp_path, **kwargs):
    options = {
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "test-token",
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "upstream-token",
        "llm_api_key": "",
        "file_log_enabled": False,
    }
    options.update(kwargs)
    return Settings(**options, _env_file=None)


def test_resource_monitor_uses_configured_sampling(tmp_path):
    app = create_app(_settings(
        tmp_path,
        resource_monitor_interval_seconds=7.5,
        resource_monitor_history_size=42,
    ))

    assert app.state.resource_monitor.interval == 7.5
    assert app.state.resource_monitor.samples.maxlen == 42


async def test_notice_store_reuses_snapshot_until_mutation(tmp_path, monkeypatch):
    store = NoticeStore(
        str(tmp_path / "notices.jsonl"), compact_after=8, cache_seconds=60)
    await store.startup()
    calls = 0
    original = store._read_file

    def counted_read_file():
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(store, "_read_file", counted_read_file)
    store._cache_loaded_at = 0

    await store.active()
    await store.active()
    assert calls == 1

    await store.create(NoticeCreate(content="缓存失效通知", status="active"))
    calls = 0
    await store.active()
    assert calls == 1


def test_service_status_scan_is_cached_until_ttl_expires(tmp_path, monkeypatch):
    from app.querylog import JSONLFileWriter

    path = tmp_path / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    writer.write_raw({
        "event": "query", "time": "2026-09-02T10:00:00+08:00",
        "run_id": "run-1", "username": "2023000001", "option": "成绩",
        "success": True, "kind": "grades",
    })
    app = create_app(_settings(
        tmp_path,
        file_log_enabled=True,
        file_log_path=str(path),
        service_status_cache_seconds=60,
        resource_monitor_interval_seconds=30,
    ))
    calls = 0
    original = main._read_service_status_summary

    def counted_scan(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(main, "_read_service_status_summary", counted_scan)
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        first = client.get("/service-status", headers=headers)
        second = client.get("/service-status", headers=headers)
        assert calls == 1
        assert first.json()["accepted_total"] == 1
        assert second.json() == first.json()

        app.state.service_status_cache["at"] = 0
        third = client.get("/service-status", headers=headers)
        assert calls == 2
        assert third.json() == first.json()


async def test_redis_history_failure_marks_backfill_dirty(tmp_path):
    class FailingHistory:
        async def add(self, *args, **kwargs):
            raise RuntimeError("redis unavailable")

    app = create_app(_settings(
        tmp_path,
        file_log_enabled=True,
        file_log_path=str(tmp_path / "queries.jsonl"),
        resource_monitor_interval_seconds=30,
    ))
    app.state.redis_history = FailingHistory()
    with TestClient(app) as client:
        assert app.state.history_dirty is False
        response = client.post(
            "/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"},
        )
        assert response.status_code == 200
        assert app.state.history_dirty is True


def test_service_status_count_uses_one_redis_pipeline(tmp_path):
    class FakePipeline:
        def __init__(self, redis):
            self.redis = redis
            self.operations = []

        def hincrby(self, key, field, amount):
            self.operations.append(("hincrby", key, field, amount))

        async def execute(self):
            self.redis.pipeline_runs.append(list(self.operations))
            self.operations.clear()
            return []

    class FakeRedis:
        def __init__(self):
            self.pipeline_runs = []

        def pipeline(self, transaction=True):
            assert transaction is True
            return FakePipeline(self)

        async def eval(self, *args, **kwargs):
            return 1

        async def aclose(self):
            return None

    app = create_app(_settings(tmp_path))
    app.state.redis = FakeRedis()
    with TestClient(app) as client:
        response = client.post(
            "/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"},
        )

    assert response.status_code == 200
    assert app.state.redis.pipeline_runs == [[
        ("hincrby", "gw:v2:service-status:accepted", "total", 1),
    ]]
