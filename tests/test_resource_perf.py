"""资源采样与热路径 I/O 缓存契约。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.context as context
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
    original = context._read_service_status_summary

    def counted_scan(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(context, "_read_service_status_summary", counted_scan)
    # 密封化（抖动治理）：本单测曾被两个环境依赖间歇性击穿——
    # ① 真实墙钟：TTL 判定依赖 monotonic 推进；② 文件 stat：_jsonl_signature
    # 读 st_mtime_ns，macOS Docker 挂载层（virtiofs/gRPC-FUSE）对同一未变文件
    # 的两次 stat 可能返回不同 mtime_ns，签名被误判变化 → 多扫一次 →
    # 「重复请求命中缓存」断言偶发失败（全量约 1/16，单测无法复现）。
    # 生产代码逻辑无误：mtime+size 签名在真实卷上是正确的脏检查手段。
    # 单测改为注入可控时钟 + 可控签名，把三条失效分支全部钉死，
    # 失败时自诊断输出（命中分支名），杜绝环境噪声混入。
    clock = {"monotonic": 1000.0}
    monkeypatch.setattr(context.time, "monotonic", lambda: clock["monotonic"])
    signature = {"value": (111, 4096)}
    monkeypatch.setattr(context, "_jsonl_signature", lambda path: signature["value"])
    with TestClient(app) as client:
        headers = {"Authorization": "Bearer test-token"}
        first = client.get("/api/v1/service-status", headers=headers)
        second = client.get("/api/v1/service-status", headers=headers)
        assert calls == 1, "重复请求未命中缓存（calls=%d）" % calls
        assert first.json()["data"]["stats"]["accepted_total"] == 1
        assert second.json() == first.json()

        # 分支一：TTL 到期（显式推进时钟，不再 hack cache["at"]）
        clock["monotonic"] += 61
        third = client.get("/api/v1/service-status", headers=headers)
        assert calls == 2, "TTL 到期未触发重扫（calls=%d）" % calls
        assert third.json() == first.json()

        # 分支二：文件签名变化（另一副本/worker 追加写）→ TTL 未到期也必须重扫
        signature["value"] = (112, 4608)
        fourth = client.get("/api/v1/service-status", headers=headers)
        assert calls == 3, "签名变化未触发重扫（calls=%d）" % calls
        assert fourth.json() == first.json()

        # 分支三：签名稳定时即便时钟继续走，TTL 内仍命中
        clock["monotonic"] += 30
        fifth = client.get("/api/v1/service-status", headers=headers)
        assert calls == 3, "TTL 内仍被重扫（calls=%d，签名=%r 时钟=%.1f）" % (
            calls, signature["value"], clock["monotonic"])
        assert fifth.json() == first.json()


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
            "/api/v1/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"},
        )
        # 上游不可达 → 502（信封语义）；查询日志仍应写入并标记回填脏位
        assert response.status_code == 502
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
            "/api/v1/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"},
        )

    assert response.status_code == 502
    assert app.state.redis.pipeline_runs == [[
        ("hincrby", "gw:v2:service-status:accepted", "total", 1),
    ]]


async def test_service_status_concurrent_misses_single_flight(tmp_path, monkeypatch):
    """并发 miss 经锁合并重建：只扫一次共享日志卷，所有请求拿到一致载荷。"""
    import asyncio

    import httpx

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
    original = context._read_service_status_summary

    def slow_scan(*args, **kwargs):
        nonlocal calls
        calls += 1
        import time as time_module
        time_module.sleep(0.15)  # 放大扫描窗口，制造并发 miss
        return original(*args, **kwargs)

    monkeypatch.setattr(context, "_read_service_status_summary", slow_scan)

    transport = httpx.ASGITransport(app=app)
    headers = {"Authorization": "Bearer test-token"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        responses = await asyncio.gather(*[
            client.get("/api/v1/service-status", headers=headers) for _ in range(3)
        ])

    assert all(response.status_code == 200 for response in responses)
    assert calls == 1, "并发 miss 应合并为一次文件扫描（single-flight）"
    payloads = [response.json()["data"] for response in responses]
    assert payloads[0] == payloads[1] == payloads[2]
    assert payloads[0]["stats"]["accepted_total"] == 1
