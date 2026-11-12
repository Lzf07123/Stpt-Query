"""内存上界与清理语义的回归测试。

覆盖 2026-08-29 内存审查修复：
1. RateLimiter 桶数量硬上限 + LRU 淘汰（不再无界增长/O(n^2) 重建）；
2. RedisSessionStore.sweep 同步清理本进程 _warm_events；
3. probe_school 代理异常兜底分支关闭第二个 Session。

覆盖 2026-11 会话/缓存审查修复：
4. SessionStore 满载按最后活跃时间淘汰（touch 刷新活跃度，最活跃老会话不被先踢）；
5. TTLCache 满载不再整表重建（OrderedDict 原地惰性过期 + 按插入序淘汰）。
"""
import threading
import time

import requests

from app.jwxt_redis import RedisBackend, RedisSessionStore
from app.jwxt_state import (
    RateLimiter, SessionStore, TTLCache, UpstreamBusyError, probe_school,
)


def test_rate_limiter_has_hard_cap_and_obeys_token_semantics():
    limiter = RateLimiter(limit_per_min=3, max_buckets=100)
    assert [limiter.allow("same-key") for _ in range(3)] == [True, True, True]
    assert limiter.allow("same-key") is False

    for i in range(1000):
        limiter.allow(f"ip-{i}")
    assert len(limiter.buckets) == 100


def test_rate_limiter_evicts_least_recently_used_key():
    limiter = RateLimiter(limit_per_min=30, max_buckets=100)
    for i in range(100):
        limiter.allow(f"ip-{i}")
    limiter.allow("ip-0")  # touch：移到 LRU 尾部
    for i in range(100, 199):
        limiter.allow(f"ip-{i}")
    assert "ip-0" in limiter.buckets
    assert "ip-1" not in limiter.buckets
    assert len(limiter.buckets) == 100


def test_session_store_evicts_least_recently_active_session():
    """满载淘汰依据最后活跃时间：持续活跃的老会话不被最早创建的会话顶掉。"""
    store = SessionStore(3600, max_sessions=2)
    s1 = store.create("u1", [], {})
    s2 = store.create("u2", [], {})
    time.sleep(0.01)
    assert store.touch(s1) is True  # s1 活跃：lat 刷新
    s3 = store.create("u3", [], {})  # 满载：应淘汰未活跃的 s2（而非 s1）
    assert store.get(s1) is not None
    assert store.get(s2) is None
    assert store.get(s3) is not None


def test_session_store_evicts_oldest_when_no_touch():
    """无活跃度差异时退化为按创建序淘汰（与旧行为一致）。"""
    store = SessionStore(3600, max_sessions=2)
    s1 = store.create("u1", [], {})
    s2 = store.create("u2", [], {})
    s3 = store.create("u3", [], {})
    assert store.get(s1) is None
    assert store.get(s2) is not None
    assert store.get(s3) is not None


def test_ttlcache_evicts_oldest_inserted_when_full():
    """TTLCache 满载淘汰按插入序最旧一条；过期项优先惰性清理。"""
    cache = TTLCache(ttl=300, max_items=2)
    cache.set("k1", {"v": 1})
    cache.set("k2", {"v": 2})
    cache.set("k3", {"v": 3})  # 超限：k1 被淘汰
    assert cache.get("k1") is None
    assert cache.get("k2") == {"v": 2}
    assert cache.get("k3") == {"v": 3}

    cache.set("k2", {"v": 22})  # 原地更新
    cache.set("k4", {"v": 4})   # 超限：按插入序淘汰 k3
    assert cache.get("k3") is None
    assert cache.get("k2") == {"v": 22}
    assert cache.get("k4") == {"v": 4}
    assert len(cache.data) == 2


def test_ttlcache_json_cache_stores_value_once_and_serializes_lazily():
    """JSON 类缓存（不传 payload）只存 value dict：不再 dict+bytes 双存。

    get_payload 命中时按需序列化，字节与写入时显式序列化完全一致
    （ensure_ascii=False + UTF-8），响应体不变。
    """
    import json as _json

    cache = TTLCache(ttl=300, max_items=8)
    value = {"success": True, "rows": [{"course": "数学"}], "count": 1}
    cache.set("grades|2023000001", value)

    stored = cache.data["grades|2023000001"]
    assert stored[1] is value          # value 原样保存
    assert stored[2] is None           # 不再保存预序列化副本

    payload = cache.get_payload("grades|2023000001")
    assert payload == _json.dumps(value, ensure_ascii=False).encode("utf-8")
    assert cache.get("grades|2023000001") == value


def test_ttlcache_explicit_payload_still_preferred():
    """显式传 payload（PDF 缓存：value 是小 dict、字节即主体）时按原样存取。"""
    cache = TTLCache(ttl=300, max_items=8)
    cache.set("pdf|k", {"semester": "sem"}, payload=b"%PDF-1.7\n")
    assert cache.get("pdf|k") == {"semester": "sem"}
    assert cache.get_payload("pdf|k") == b"%PDF-1.7\n"


def test_ttlcache_expired_entries_purged_in_place_on_full_set():
    """满载写入时先惰性清理过期项，清理后不再淘汰未过期条目。"""
    cache = TTLCache(ttl=300, max_items=2)
    cache.set("a", 1, ttl=1)
    cache.set("b", 2)
    # 强制把 a 变成已过期
    cache.data["a"] = (time.time() - 1, cache.data["a"][1], cache.data["a"][2])
    cache.set("c", 3)  # 满载：先清掉过期的 a，b 保留，无需淘汰
    assert cache.get("a") is None
    assert cache.get("b") == 2
    assert cache.get("c") == 3
    assert len(cache.data) == 2


class _FakePipeline:
    def __init__(self):
        self.exists_count = 0
        self.zrem_calls = []

    def exists(self, key):
        self.exists_count += 1
        return self

    def zrem(self, key, member):
        self.zrem_calls.append((key, member))
        return self

    def execute(self):
        return [0] * self.exists_count


class _FakeRedisClient:
    """仅覆盖 RedisSessionStore.sweep 使用的命令。"""

    def __init__(self, index_key, sids):
        self.index_key = index_key
        self.sids = list(sids)

    def zrange(self, key, start, end):
        assert key == self.index_key
        return list(self.sids)

    def pipeline(self):
        return _FakePipeline()


def test_redis_session_store_sweep_cleans_local_warm_events():
    client = _FakeRedisClient("jwxt:session-index", [b"s1", b"s2"])
    backend = RedisBackend.from_client(client)
    store = RedisSessionStore(backend, ttl=300, max_sessions=100)
    store._warm_events["s1"] = threading.Event()
    store._warm_events["s2"] = threading.Event()

    assert store.sweep() == 2
    assert not store._warm_events


def test_probe_school_closes_fallback_session_on_busy(monkeypatch):
    from app import jwxt_state as state

    sessions = []

    class FakeSession:
        def __init__(self):
            self.verify = True
            self.trust_env = True
            self.headers = {}
            self.closed = False
            sessions.append(self)

        def mount(self, *args, **kwargs):
            pass

        def close(self):
            self.closed = True

        def get(self, *args, **kwargs):
            if len(sessions) == 1:
                raise requests.exceptions.ProxyError("proxy")
            raise UpstreamBusyError("busy")

    monkeypatch.setattr(state.requests, "Session", FakeSession)
    result = state.probe_school(timeout=1)

    assert result.get("busy") is True
    assert len(sessions) == 2
    assert all(session.closed for session in sessions)
