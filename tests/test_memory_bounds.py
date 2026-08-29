"""内存上界与清理语义的回归测试。

覆盖 2026-08-29 内存审查修复：
1. RateLimiter 桶数量硬上限 + LRU 淘汰（不再无界增长/O(n^2) 重建）；
2. RedisSessionStore.sweep 同步清理本进程 _warm_events；
3. probe_school 代理异常兜底分支关闭第二个 Session。
"""
import threading

import requests

from jwxt_redis import RedisBackend, RedisSessionStore
from jwxt_state import RateLimiter, UpstreamBusyError, probe_school


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
    import jwxt_state as state

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
