"""服务状态持久化与多实例共享契约。"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.history import MemoryEventHistory, RedisEventHistory
from app.main import Settings, create_app, _sync_history_from_file
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
    return Settings(**options, _env_file=None)


def _write_query_event(writer: JSONLFileWriter, *, success: bool, kind: str,
                       username: str = "2023000001",
                       run_id: str = "run-1") -> None:
    writer.write_raw({
        "event": "query",
        "time": "2026-08-28T10:00:00+08:00",
        "run_id": run_id,
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
    _write_query_event(writer, success=True, kind="grades", username="2023000002",
                       run_id="run-2")

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


def test_service_status_restores_local_file_after_restart(tmp_path):
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


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.operations = []

    def zadd(self, key, values, nx=False):
        self.operations.append(("zadd", key, values, nx))

    def zremrangebyrank(self, key, start, stop):
        self.operations.append(("zremrangebyrank", key, start, stop))

    async def execute(self):
        self.redis.transactions.append(list(self.operations))
        for operation, *args in self.operations:
            if operation == "zadd":
                key, values, nx = args
                members = self.redis.zsets.setdefault(key, [])
                for member, score in values.items():
                    if nx and any(item[0] == member for item in members):
                        continue
                    members[:] = [(item_member, item_score)
                                  for item_member, item_score in members
                                  if item_member != member]
                    members.append((member, score))
            else:
                key, start, stop = args
                self.redis.zsets[key].sort(key=lambda item: (item[1], item[0]))
                del self.redis.zsets[key][start:stop + 1]
        return True


class FakeAsyncRedis:
    def __init__(self):
        self.zsets: dict[str, list[tuple[str, float]]] = {}
        self.lists: dict[str, list[str]] = {}
        self.locks = {}
        self.transactions = []

    async def incr(self, key):
        self.lists[key] = self.lists.get(key, [])
        count = len(self.lists[key]) + 1
        self.lists[key].append(str(count))
        return count

    async def expire(self, key, seconds):
        assert seconds == 60
        return True

    async def ping(self):
        return True

    async def set(self, key, value, nx=False, px=None):
        if nx and key in self.locks:
            return False
        self.locks[key] = value
        return True

    async def get(self, key):
        return self.locks.get(key)

    async def eval(self, script, numkeys, key, *args):
        if self.locks.get(key) == args[0]:
            del self.locks[key]
        return 1

    async def lrange(self, key, start, stop):
        return self.lists.get(key, [])[start:stop + 1]

    async def zrange(self, key, start, stop, desc=False, withscores=False):
        rows = sorted(self.zsets.get(key, []), key=lambda item: (item[1], item[0]))
        if desc:
            rows = list(reversed(rows))
        rows = rows[start:stop + 1 if stop >= 0 else None]
        if withscores:
            return rows
        return [member for member, _ in rows]

    def pipeline(self, transaction=True):
        assert transaction is True
        return FakePipeline(self)

    async def aclose(self):
        return None


async def test_redis_records_status_and_query_logs_with_run_ids(tmp_path):
    redis = FakeAsyncRedis()
    cfg = _cfg(tmp_path, file_log_enabled=False, redis_url="redis://redis:6379/0")
    app = create_app(cfg)
    app.state.redis = redis
    app.state.redis_history = RedisEventHistory(redis)
    client = TestClient(app)

    assert client.post(
        "/run", headers=HEADERS,
        json={"username": "2023000001", "password": "pw", "option": "成绩"},
    ).status_code == 200
    body = client.get("/service-status", headers=HEADERS).json()

    assert body["total"] == 1
    assert body["results"][0]["success"] is False
    assert body["results"][0]["kind"] == "login_error"
    assert "run_id" not in body["results"][0]
    assert len(redis.zsets["gw:v2:service-status"]) == 1
    assert len(redis.zsets["gw:v2:query-logs"]) == 1
    status_event = json.loads(redis.zsets["gw:v2:service-status"][0][0])
    query_event = json.loads(redis.zsets["gw:v2:query-logs"][0][0])
    assert status_event["run_id"] == query_event["run_id"]


async def test_redis_startup_backfills_empty_history_from_jsonl(tmp_path):
    writer = JSONLFileWriter(tmp_path / "queries.jsonl")
    for index in range(102):
        writer.write_raw({
            "event": "query",
            "time": f"2026-08-28T10:{index // 60:02d}:{index % 60:02d}+08:00",
            "run_id": f"run-{index:03d}",
            "username": f"2023000{index:03d}",
            "password": "secret",
            "option": "成绩",
            "success": True,
            "kind": "grades",
        })

    redis = FakeAsyncRedis()
    local = MemoryEventHistory()
    history = RedisEventHistory(redis)

    assert await _sync_history_from_file(writer, local, history, "instance-1") is True
    assert len(local.recent("gw:v2:query-logs")) == 100
    assert len(redis.zsets["gw:v2:query-logs"]) == 100
    assert len(redis.zsets["gw:v2:service-status"]) == 100
    newest = json.loads(redis.zsets["gw:v2:query-logs"][-1][0])
    oldest = json.loads(redis.zsets["gw:v2:query-logs"][0][0])
    assert newest["run_id"] == "run-101"
    assert oldest["run_id"] == "run-002"
    assert "password" not in newest
    assert all(len({operation[1] for operation in transaction}) == 1
               for transaction in redis.transactions)


async def test_redis_backfill_replay_does_not_duplicate_records(tmp_path):
    writer = JSONLFileWriter(tmp_path / "queries.jsonl")
    _write_query_event(writer, success=True, kind="grades", run_id="run-1")
    _write_query_event(writer, success=False, kind="login_error", run_id="run-2")

    redis = FakeAsyncRedis()
    local = MemoryEventHistory()
    history = RedisEventHistory(redis)

    assert await _sync_history_from_file(
        writer, local, history, "instance-1") is True
    query_members = list(redis.zsets["gw:v2:query-logs"])
    status_members = list(redis.zsets["gw:v2:service-status"])

    assert await _sync_history_from_file(
        writer, local, history, "instance-1") is True
    assert redis.zsets["gw:v2:query-logs"] == query_members
    assert redis.zsets["gw:v2:service-status"] == status_members
    assert len(redis.zsets["gw:v2:query-logs"]) == 2
    assert len(redis.zsets["gw:v2:service-status"]) == 2
    assert len(local.recent("gw:v2:query-logs")) == 2


async def test_redis_history_sync_is_owner_locked():
    redis = FakeAsyncRedis()
    redis.locks["gw:history-sync-lock"] = "instance-other"
    history = RedisEventHistory(redis)
    writer = JSONLFileWriter(str(Path("/dev/null")))

    assert await _sync_history_from_file(
        writer, MemoryEventHistory(), history, "instance-1") is False
    assert not redis.transactions


async def test_redis_history_merges_versioned_and_legacy_records():
    versioned = json.dumps({
        "event": "query", "run_id": "run-a", "success": True, "kind": "grades",
        "time": "2026-08-28T10:02:00+08:00", "_record_id": "a",
    }, separators=(",", ":"))
    legacy_a = json.dumps({
        "event": "query", "run_id": "run-a", "success": False, "kind": "login_error",
        "time": "2026-08-28T10:00:00+08:00",
    }, separators=(",", ":"))
    legacy_b = json.dumps({
        "event": "query", "run_id": "run-b", "success": True, "kind": "grades",
        "time": "2026-08-28T10:01:00+08:00",
    }, separators=(",", ":"))

    class LegacyRedis:
        def __init__(self):
            self.zsets = {"gw:v2:query-logs": [(versioned, 1787882520.0)]}
            self.lists = {"gw:query-logs": [legacy_a, legacy_b]}

        async def zrange(self, *args, **kwargs):
            rows = self.zsets["gw:v2:query-logs"]
            return rows if kwargs.get("withscores") else [row[0] for row in rows]

        async def lrange(self, key, start, stop):
            rows = self.lists[key]
            if stop < 0:
                return rows[start:] if start >= 0 else rows[:stop]
            return rows[start:stop + 1]

    items = await RedisEventHistory(LegacyRedis()).recent(
        "gw:v2:query-logs", "gw:query-logs")

    assert [item["run_id"] for item in items] == ["run-a", "run-b"]
    assert items[0]["success"] is True
    assert "_record_id" not in items[0]
    assert "_history_member" not in items[1]


def test_memory_history_is_bounded_and_deduplicated():
    history = MemoryEventHistory(limit=2)
    for index in range(4):
        history.add("status", {"success": True, "kind": str(index)}, str(index), index)
    history.add("status", {"success": True, "kind": "updated"}, "3", 99)

    assert [item["kind"] for item in history.recent("status")] == ["updated", "2"]
