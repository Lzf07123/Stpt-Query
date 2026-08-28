"""异步任务队列的安全与状态契约。"""
from __future__ import annotations

import json

import pytest

from app.jobs import JobStore, QueueFullError
from app.schema import SessionWorkflowRequest


class _FakeLock:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


class _FakePipeline:
    def __init__(self, redis):
        self.redis = redis

    def set(self, key, value, ex=None):
        self.redis.data[key] = value

    def zadd(self, key, mapping):
        self.redis.zsets.setdefault(key, {}).update(mapping)

    async def execute(self):
        return []


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.zsets = {}

    def lock(self, *args, **kwargs):
        return _FakeLock()

    def pipeline(self, transaction=True):
        return _FakePipeline(self)

    async def get(self, key):
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.data[key] = value

    async def delete(self, *keys):
        for key in keys:
            self.data.pop(key, None)

    async def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    async def zrem(self, key, member):
        self.zsets.setdefault(key, {}).pop(member, None)

    async def zcard(self, key):
        return len(self.zsets.get(key, {}))

    async def zrank(self, key, member):
        members = sorted(self.zsets.get(key, {}), key=self.zsets[key].get)
        try:
            return members.index(member)
        except ValueError:
            return None


async def test_enqueue_never_persists_or_publishes_password():
    request = SessionWorkflowRequest(
        username="2023000001",
        password="secret-password",
        option="成绩",
    )
    redis = _FakeRedis()
    store = JobStore(redis)

    job_id, deduplicated, position = await store.enqueue(
        request, "short-lived-session", "127.0.0.1",
        pending_limit=1, ttl_seconds=900,
    )
    serialized = json.dumps(redis.data, ensure_ascii=False)

    assert deduplicated is False
    assert position == 1
    assert "secret-password" not in serialized
    assert '"password"' not in serialized
    assert "short-lived-session" in redis.data[f"{store.payload_prefix}:{job_id}"]

    status = await store.status(job_id)
    assert status is not None
    assert status.state == "queued"
    assert status.phase == "queued"
    assert status.phase_label == "排队中"
    assert status.phase_started_at is not None
    assert status.position == 1
    assert "secret-password" not in status.model_dump_json()


async def test_phase_transitions_are_persisted_and_completed_on_success():
    request = SessionWorkflowRequest(
        username="2023000001", password="secret-password", option="成绩")
    redis = _FakeRedis()
    store = JobStore(redis)
    job_id, _, _ = await store.enqueue(
        request, "short-lived-session", "127.0.0.1", 1, 900)

    assert await store.mark_phase(job_id, "dispatching", 900)
    assert await store.mark_phase(job_id, "querying", 900)
    assert await store.mark_running(job_id, 123.0, 900)
    await store.complete(job_id, {"success": True, "kind": "grades"}, 900)

    status = await store.status(job_id)
    assert status is not None
    assert status.state == "success"
    assert status.phase == "done"
    assert status.phase_label == "完成"
    assert "secret-password" not in json.dumps(redis.data, ensure_ascii=False)


async def test_failure_preserves_last_reached_phase():
    request = SessionWorkflowRequest(
        username="2023000001", password="secret-password", option="成绩")
    redis = _FakeRedis()
    store = JobStore(redis)
    job_id, _, _ = await store.enqueue(
        request, "short-lived-session", "127.0.0.1", 1, 900)
    await store.mark_phase(job_id, "querying", 900)

    await store.fail(job_id, "grades_error", "查询失败", 900)

    status = await store.status(job_id)
    assert status is not None
    assert status.state == "failed"
    assert status.phase == "querying"
    assert status.phase_label == "查询成绩/课表"


async def test_enqueue_respects_pending_limit():
    grades = SessionWorkflowRequest(
        username="2023000001", password="secret-password", option="成绩")
    schedule = SessionWorkflowRequest(
        username="2023000001", password="secret-password", option="课表")
    redis = _FakeRedis()
    store = JobStore(redis)

    await store.enqueue(grades, "session-1", "127.0.0.1", 1, 900)

    with pytest.raises(QueueFullError):
        await store.enqueue(schedule, "session-2", "127.0.0.1", 1, 900)
