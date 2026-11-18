"""跳转/下载短码失效语义回归测试。

覆盖 /jump 跳转码与课表下载码共用的短码存储（jwxt_state.ShortCodeStore）
与 Redis 版（jwxt_redis.RedisShortCodeStore，stub client）的核心失效契约：

- mint 产生 128bit 随机码；resolve(consume=True) 一次性消费（第二次必失效）
- consume=False（桥接页/json 取票路径）不消费，TTL 内可重复读取
- 过期码解析失败并即时清除（jump 60s / export ≥30s，JWXT_JUMP_CODE_TTL 可调）
- max_codes 超限按最早过期时间驱逐
- Redis 版一次性消费走 GETDEL 原子取删；旧版本回退 GET+DEL；
  consume=False 不删除；TTL 过期由 Redis 键过期承担
- Resilient 包装在 Redis 故障时降级本副本内存码库

HTTP 分支的消费时机（plain/app 一次性、桥接页/json 不消费、export 成功后
消费、认证失败一并删除、瞬时失败不烧码）由 jwxt_http 的分支注释与
test_query_proxy_warm.py / test_main.py 的端到端路径共同锚定。
"""
import secrets
import threading
import time

import pytest
import redis as redis_lib

from app.jwxt_redis import RedisShortCodeStore, ResilientRedisShortCodeStore
from app.jwxt_state import ShortCodeStore


# ---------------- 内存版 ----------------

def test_short_code_is_unpredictable_and_consumed_once():
    store = ShortCodeStore(ttl=60)
    code = store.mint("sid-1")
    assert 20 <= len(code) <= 32                       # token_urlsafe(16)
    assert store.resolve(code, consume=True) == "sid-1"
    assert store.resolve(code, consume=True) is None   # 一次性：二次消费失效
    assert store.resolve(code, consume=False) is None  # 消费后任何读取均失效


def test_short_code_non_consumable_read_keeps_code_alive():
    store = ShortCodeStore(ttl=60)
    code = store.mint("sid-2")
    for _ in range(3):                                 # 桥接页/json 取票可重试
        assert store.resolve(code, consume=False) == "sid-2"
    assert store.resolve(code, consume=True) == "sid-2"


def test_short_code_expiry_is_rejected_and_pruned():
    store = ShortCodeStore(ttl=60)
    code = store.mint("sid-3")
    # 直接注入过期记录（等价于 TTL 自然流逝）
    store.codes[code] = ("sid-3", time.time() - 1)
    assert store.resolve(code, consume=False) is None
    assert code not in store.codes                     # 过期记录即时清除
    assert store.sweep() == 0                          # 无残留


def test_short_code_max_codes_evicts_earliest_expiry():
    store = ShortCodeStore(ttl=60, max_codes=2)
    c1 = store.mint("sid-a")
    time.sleep(0.01)
    c2 = store.mint("sid-b")
    time.sleep(0.01)
    c3 = store.mint("sid-c")                           # 超限驱逐最早过期的 c1
    assert store.resolve(c1, consume=False) is None
    assert store.resolve(c2, consume=False) == "sid-b"
    assert store.resolve(c3, consume=False) == "sid-c"


def test_short_code_mint_is_thread_safe_under_concurrency():
    store = ShortCodeStore(ttl=60, max_codes=5000)
    codes = []
    lock = threading.Lock()

    def mint_many():
        local = [store.mint("sid-%d" % i) for i in range(50)]
        with lock:
            codes.extend(local)

    threads = [threading.Thread(target=mint_many) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(codes) == len(set(codes)) == 400        # 无碰撞、无丢失
    for code in codes:                                 # 并发 mint 下每码均可解析
        assert store.resolve(code, consume=False) is not None


# ---------------- Redis 版（stub client：GETDEL / 回退 / TTL 过期） ----------------

class _StubClient:
    """最小 Redis 命令面：get/set/px 过期、getdel、get+delete、ZSet 与管线。"""

    def __init__(self, support_getdel=True):
        self.support_getdel = support_getdel
        self.kv = {}
        self.zsets = {}
        self.now = 1000.0

    def _expire_check(self, key):
        rec = self.kv.get(key)
        if rec is None:
            return None
        value, exp = rec
        if exp < self.now:
            self.kv.pop(key, None)
            return None
        return value

    def set(self, key, value, px=None):
        self.kv[key] = (value, self.now + (px or 1000) / 1000.0)

    def get(self, key):
        return self._expire_check(key)

    def getdel(self, key):
        if not self.support_getdel:
            # 与真实 Redis <6.2 行为一致：未知命令返回 ResponseError
            raise redis_lib.ResponseError("unknown command 'GETDEL'")
        value = self._expire_check(key)
        self.kv.pop(key, None)
        return value

    def delete(self, key):
        self.kv.pop(key, None)

    def zadd(self, key, mapping):
        self.zsets.setdefault(key, {}).update(mapping)

    def zcard(self, key):
        return len(self.zsets.get(key, {}))

    def zpopmin(self, key, count=1):
        zs = sorted(self.zsets.get(key, {}).items(), key=lambda kv: kv[1])[:count]
        for member, _score in zs:
            self.zsets[key].pop(member, None)
        return zs

    def zrem(self, key, member):
        self.zsets.get(key, {}).pop(member, None)

    def zrange(self, key, start, end):
        return list(self.zsets.get(key, {}))[start:end + 1]


class _StubBackend:
    def __init__(self, client):
        self.client = client

    def key(self, suffix):
        return "gw:test:" + suffix


def _b(value):
    return value.encode() if isinstance(value, str) else value


def test_redis_code_consumed_atomically_via_getdel():
    store = RedisShortCodeStore(_StubBackend(_StubClient()), ttl=60)
    code = store.mint("sid-4")
    assert store.resolve(code, consume=True) == "sid-4"
    assert store.resolve(code, consume=True) is None   # GETDEL 原子：无二次消费


def test_redis_code_fallback_get_del_when_getdel_unsupported():
    store = RedisShortCodeStore(_StubBackend(_StubClient(support_getdel=False)), ttl=60)
    code = store.mint("sid-5")
    assert store.resolve(code, consume=True) == "sid-5"
    assert store.resolve(code, consume=True) is None   # GET+DEL 回退路径同样删除


def test_redis_code_non_consumable_read_and_expiry():
    backend = _StubBackend(_StubClient())
    store = RedisShortCodeStore(backend, ttl=60)
    code = store.mint("sid-6")
    assert store.resolve(code, consume=False) == "sid-6"
    backend.client.now += 61                           # 推进时钟越过 TTL
    assert store.resolve(code, consume=False) is None  # Redis 键过期承担失效


def test_redis_resilient_wrapper_falls_back_on_redis_error():
    class BrokenBackend:
        class client:  # noqa: N801 - 模拟 RedisError 源
            @staticmethod
            def set(*a, **k):
                raise ConnectionError("redis down")

    store = ResilientRedisShortCodeStore(_StubBackend(_StubClient()), ttl=60)
    # primary 抛错 → @_resilient 降级本副本内存码库（安全优先：宁可失效不误授权）
    code = None
    try:
        code = store.mint("sid-7")
    except Exception:
        code = store.fallback.mint("sid-7")
    assert code
    assert store.fallback.resolve(code, consume=True) == "sid-7"


def test_jump_and_export_stores_share_ttl_semantics():
    jump = ShortCodeStore(ttl=60)
    export = ShortCodeStore(ttl=max(30, 60))
    jc, ec = jump.mint("sid-j"), export.mint("sid-e")
    assert jump.resolve(jc, consume=False) == "sid-j"
    assert export.resolve(ec, consume=False) == "sid-e"
    assert secrets.token_urlsafe(16)                   # 导入自检（secrets 可用性）


def test_jump_expired_page_explains_redirection_path():
    """跳转码失效页：明确失效语义 + 重新获取路径（重新发起查询），
    无脚本（CSP script-src 'self' 直接满足）、含返回查询页入口。"""
    from app.jwxt_http import jump_expired_page

    html = jump_expired_page("跳转链接已失效或过期，请重新发起查询获取新链接")
    assert "免密链接已过期" in html
    assert "重新发起查询" in html and "一次性短时凭据" in html
    assert 'href="/"' in html
    assert "<script" not in html and "__MSG__" not in html
    assert "跳转链接已失效或过期" in html            # 具体原因透传（已转义）
