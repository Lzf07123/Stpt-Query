"""全站通知：文件权威存储、Redis 热备份与 HTTP 契约。"""
from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.notices import NoticeConflictError, NoticeCreate, NoticeStore, NoticeUpdate


ADMIN = {"Authorization": "Bearer " + ("b" * 24)}
GATEWAY = {"Authorization": "Bearer gateway-token"}


def _settings(tmp_path, **kwargs):
    options = {
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "gateway-token",
        "admin_token": "b" * 24,
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "upstream-token",
        "llm_api_key": "",
        "file_log_enabled": False,
        "notice_fallback_path": str(tmp_path / "notices" / "notices.jsonl"),
    }
    options.update(kwargs)
    return Settings(**options, _env_file=None)


async def _create(store: NoticeStore, content: str, status: str = "active",
                  level: str = "info"):
    return await store.create(NoticeCreate(content=content, status=status, level=level))


async def test_notice_store_crud_and_public_views(tmp_path):
    store = NoticeStore(str(tmp_path / "notices.jsonl"), compact_after=8)
    await store.startup()

    draft = await _create(store, "  草稿通知\n", status="draft")
    assert draft.content == "草稿通知"
    assert draft.status == "draft"

    active = await _create(store, "维护通知", status="active", level="warning")
    assert len(await store.active()) == 1
    assert await store.history() == []

    updated = await store.update(draft.id, NoticeUpdate(level="warning", status="active"))
    assert len(await store.active()) == 2

    archived = await store.update(active.id, NoticeUpdate(status="archived"))
    active_items = await store.active()
    history_items = await store.history()
    assert [item.id for item in active_items] == [draft.id]
    assert [item.id for item in history_items] == [active.id]
    assert archived.archived_at

    disposable = await _create(store, "待删除草稿", status="draft")
    await store.delete(disposable.id)
    items, total = await store.admin_list()
    assert total == 2
    assert {item.status for item in items} == {"active", "archived"}


async def test_notice_store_rejects_published_content_edit_and_draft_delete(tmp_path):
    store = NoticeStore(str(tmp_path / "notices.jsonl"))
    await store.startup()
    active = await _create(store, "已发布通知")
    draft = await _create(store, "待删除草稿", status="draft")

    payload = NoticeUpdate(content="尝试修改")
    with pytest.raises(NoticeConflictError):
        await store.update(active.id, payload)
    with pytest.raises(NoticeConflictError):
        await store.delete(active.id)

    await store.delete(draft.id)
    items, total = await store.admin_list()
    assert total == 1 and items[0].id == active.id


async def test_notice_active_limit_and_history_limit(tmp_path):
    store = NoticeStore(
        str(tmp_path / "notices.jsonl"), max_active=2, max_history=2,
        compact_after=4,
    )
    await store.startup()
    await _create(store, "第一条")
    await _create(store, "第二条")
    with pytest.raises(NoticeConflictError):
        await _create(store, "第三条")

    first = (await store.active())[0]
    await store.update(first.id, NoticeUpdate(status="archived"))
    await _create(store, "第三条")
    third = (await store.active())[0]
    await store.update(third.id, NoticeUpdate(status="archived"))

    history = await store.history(limit=10)
    assert len(history) == 2
    assert history[0].content == "第三条"


async def test_notice_file_replay_skips_corrupt_lines_and_compacts(tmp_path):
    path = tmp_path / "notices.jsonl"
    store = NoticeStore(str(path), compact_after=2)
    await store.startup()
    active = await _create(store, "压缩前通知")

    with path.open("a", encoding="utf-8") as handle:
        handle.write("{broken-json\n")
        handle.write(json.dumps({"id": str(uuid.uuid4()), "content": "missing-fields"}) + "\n")

    another = NoticeStore(str(path), compact_after=2)
    await another.startup()
    assert another.parse_errors == 2
    await _create(another, "压缩后通知")
    assert path.read_text(encoding="utf-8").strip().count("\n") <= 2
    assert any(item.content == active.content for item in (await another.admin_list())[0])


class _FakePipeline:
    def __init__(self, backend):
        self.backend = backend
        self.fail = False

    def delete(self, key):
        self.backend.commands.append(("delete", key))

    def hset(self, key, field, value):
        self.backend.commands.append(("hset", key, field, value))

    async def execute(self):
        if self.fail:
            raise RuntimeError("redis unavailable")
        self.backend.data = {}
        for command in self.backend.commands:
            if command[0] == "hset":
                self.backend.data[command[2]] = command[3]
        self.backend.commands.clear()


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.commands = []
        self.fail_next = False

    def pipeline(self, transaction=True):
        pipeline = _FakePipeline(self)
        pipeline.fail = self.fail_next
        self.fail_next = False
        return pipeline

    async def hgetall(self, key):
        return dict(self.data)


async def test_notice_redis_failure_does_not_block_file_write(tmp_path):
    redis = _FakeRedis()
    store = NoticeStore(str(tmp_path / "notices.jsonl"), redis=redis)
    await store.startup()
    redis.commands.clear()
    redis.fail_next = True
    record = await _create(store, "Redis 写失败也不丢通知")

    assert record.content == "Redis 写失败也不丢通知"
    assert store.redis_status == "degraded"
    assert len(await store.active()) == 1


async def test_notice_file_missing_rebuilds_from_redis(tmp_path):
    path = tmp_path / "notices.jsonl"
    redis = _FakeRedis()
    first = NoticeStore(str(path), redis=redis)
    await first.startup()
    active = await _create(first, "Redis 可重建通知")

    path.unlink()
    second = NoticeStore(str(path), redis=redis)
    await second.startup()
    assert [item.id for item in await second.active()] == [active.id]
    assert path.exists()


def test_notice_http_auth_filters_and_pagination(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/notices/active").status_code == 401
        assert client.get("/notices/history").status_code == 401
        assert client.get("/admin/api/notices").status_code == 404

        created = client.post(
            "/admin/api/notices",
            headers=ADMIN,
            json={"content": "首页维护通知", "level": "warning", "status": "active"},
        )
        assert created.status_code == 200
        notice = created.json()["notice"]
        draft = client.post(
            "/admin/api/notices", headers=ADMIN,
            json={"content": "内部草稿", "status": "draft"},
        ).json()["notice"]

        public = client.get("/notices/active", headers=GATEWAY).json()
        assert [item["id"] for item in public["notices"]] == [notice["id"]]
        assert all(item["content"] != "内部草稿" for item in public["notices"])

        history = client.get("/notices/history", headers=GATEWAY).json()
        assert history["notices"] == []

        listed = client.get(
            "/admin/api/notices?status=draft", headers=ADMIN).json()
        assert [item["id"] for item in listed["notices"]] == [draft["id"]]
        assert listed["total"] == 1

        client.patch(
            f"/admin/api/notices/{notice['id']}", headers=ADMIN,
            json={"status": "archived"})
        history = client.get(
            "/notices/history?limit=50", headers=GATEWAY).json()
        assert [item["id"] for item in history["notices"]] == [notice["id"]]

        serialized = str(client.get("/admin/api/notices", headers=ADMIN).json())
        for forbidden in ("password", "session", "token"):
            assert forbidden not in serialized


def test_notice_http_validates_single_line_content(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        long = "长" * 121
        assert client.post(
            "/admin/api/notices", headers=ADMIN, json={"content": long},
        ).status_code == 422
        normalized = client.post(
            "/admin/api/notices", headers=ADMIN, json={"content": "a\nb"},
        )
        assert normalized.status_code == 200
        assert normalized.json()["notice"]["content"] == "a b"
