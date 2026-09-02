#!/usr/bin/env python3
"""可选 Redis 共享状态后端（多实例部署时启用）。

未配置 JWXT_REDIS_URL 时，服务继续使用 jwxt_state 中的内存实现；
配置后由 Runtime 用本模块的 Redis 实现替换会话/缓存/短码/限流/锁/信号量，
使多个副本共享同一份状态。
"""

import json
import secrets
import threading
import time
from contextlib import contextmanager
from functools import wraps

import redis

from jwxt_state import (
    KeyedLocks, RateLimiter, SessionStore, ShortCodeStore, TTLCache,
)
from jwxt_core import (
    LOG, UPSTREAM_SEM_TIMEOUT, LockTimeoutError, UpstreamBusyError,
)

_DEFAULT_PREFIX = "jwxt"
_TTL_MARGIN = 60  # Redis 键 TTL 比业务过期时间多留的余量（秒）


class RedisBackend:
    """Redis 连接与统一 key 前缀。"""

    def __init__(self, url, prefix=_DEFAULT_PREFIX,
                 connect_timeout=3.0, socket_timeout=3.0,
                 failure_cooldown=5.0):
        self.prefix = (prefix or _DEFAULT_PREFIX).strip().rstrip(":")
        self.client = redis.Redis.from_url(
            url,
            decode_responses=False,
            socket_connect_timeout=connect_timeout,
            socket_timeout=socket_timeout,
        )
        self._availability_lock = threading.Lock()
        self._available = True
        self._probing = False
        self._degraded = False
        self._last_failure_at = 0.0
        self._failure_cooldown = max(1.0, float(failure_cooldown))
        self._init_availability()
        try:
            self.client.ping()
            self.record_success()
        except redis.RedisError as e:
            self.record_failure(e)

    @classmethod
    def from_client(cls, client, prefix=_DEFAULT_PREFIX):
        """测试用：直接包装已有 client（fakeredis）。"""
        obj = cls.__new__(cls)
        obj.prefix = (prefix or _DEFAULT_PREFIX).strip().rstrip(":")
        obj.client = client
        obj._failure_cooldown = 5.0
        obj._init_availability()
        return obj

    def _init_availability(self):
        self._availability_lock = threading.Lock()
        self._available = True
        self._probing = False
        self._degraded = False
        self._last_failure_at = 0.0

    def key(self, name):
        return "%s:%s" % (self.prefix, name)

    @property
    def degraded(self):
        with self._availability_lock:
            return self._degraded

    def attempt_allowed(self):
        """熔断打开时只放行单个恢复探测请求，其余请求直接走内存降级。"""
        with self._availability_lock:
            if self._available:
                return True
            if time.monotonic() - self._last_failure_at < self._failure_cooldown:
                return False
            if self._probing:
                return False
            self._probing = True
            return True

    def record_success(self):
        with self._availability_lock:
            recovered = self._degraded
            self._available = True
            self._probing = False
            self._degraded = False
        if recovered:
            LOG.info("Redis 已恢复，退出内存降级模式")

    def record_failure(self, error):
        with self._availability_lock:
            first = not self._degraded
            self._available = False
            self._probing = False
            self._degraded = True
            self._last_failure_at = time.monotonic()
        if first:
            LOG.warning("Redis 不可用，已降级为本副本内存状态: %s", error)

    def abandon_attempt(self):
        with self._availability_lock:
            self._probing = False

    def probe(self):
        try:
            self.client.ping()
        except redis.RedisError as e:
            self.record_failure(e)
            return False
        self.record_success()
        return True

    def status_info(self, now=None):
        now = now or time.time()
        with self._availability_lock:
            return {"ok": not self._degraded, "at": int(now),
                    "mode": "redis", "degraded": self._degraded}


def _resilient(operation):
    """让 Redis 操作在连接故障时调用同名内存降级实现。"""
    def decorator(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if not self.backend.attempt_allowed():
                return getattr(self.fallback, operation)(*args, **kwargs)
            try:
                result = func(self, *args, **kwargs)
            except redis.RedisError as exc:
                self.backend.record_failure(exc)
                return getattr(self.fallback, operation)(*args, **kwargs)
            except Exception:
                self.backend.abandon_attempt()
                raise
            self.backend.record_success()
            return result
        return wrapper
    return decorator


class ResilientRedisSessionStore:
    """Redis 会话存储，故障时降级为本副本内存会话。"""

    def __init__(self, backend, ttl, max_sessions, primary=None, fallback=None):
        self.backend = backend
        self.primary = primary or RedisSessionStore(backend, ttl, max_sessions)
        self.fallback = fallback or SessionStore(ttl, max_sessions)

    @_resilient("create")
    def create(self, username, cookies, portal, warm_evt=None, pwd_hash=None):
        return self.primary.create(username, cookies, portal,
                                   warm_evt=warm_evt, pwd_hash=pwd_hash)

    @_resilient("find_reusable")
    def find_reusable(self, username, pwd_hash, max_age):
        return self.primary.find_reusable(username, pwd_hash, max_age)

    @_resilient("refresh")
    def refresh(self, sid, cookies, portal):
        return self.primary.refresh(sid, cookies, portal)

    @_resilient("touch")
    def touch(self, sid):
        return self.primary.touch(sid)

    @_resilient("get")
    def get(self, sid):
        return self.primary.get(sid)

    @_resilient("sweep")
    def sweep(self, now=None):
        return self.primary.sweep(now)

    @_resilient("status")
    def status(self, sid):
        return self.primary.status(sid)

    @_resilient("invalidate")
    def invalidate(self, sid):
        return self.primary.invalidate(sid)

    @_resilient("count")
    def count(self):
        return self.primary.count()

    @_resilient("sessions")
    def sessions(self):
        return self.primary.sessions()


class ResilientRedisTTLCache:
    """Redis TTL 缓存，故障时降级为本副本进程内缓存。"""

    def __init__(self, backend, ttl=0, max_items=2000,
                 primary=None, fallback=None):
        self.backend = backend
        self.primary = primary or RedisTTLCache(backend, ttl, max_items)
        self.fallback = fallback or TTLCache(ttl, max_items)

    @_resilient("get")
    def get(self, key):
        return self.primary.get(key)

    @_resilient("get_payload")
    def get_payload(self, key):
        return self.primary.get_payload(key)

    @_resilient("delete")
    def delete(self, key):
        return self.primary.delete(key)

    @_resilient("set")
    def set(self, key, value, ttl=None, payload=None):
        return self.primary.set(key, value, ttl=ttl, payload=payload)

    @_resilient("sweep")
    def sweep(self, now=None):
        return self.primary.sweep(now)


class ResilientRedisShortCodeStore:
    """Redis 短码存储，故障时降级为本副本进程内短码。"""

    def __init__(self, backend, ttl=60, max_codes=10000,
                 primary=None, fallback=None):
        self.backend = backend
        self.primary = primary or RedisShortCodeStore(
            backend, ttl, max_codes)
        self.fallback = fallback or ShortCodeStore(ttl, max_codes)

    @_resilient("mint")
    def mint(self, sid):
        return self.primary.mint(sid)

    @_resilient("resolve")
    def resolve(self, code, consume=True):
        return self.primary.resolve(code, consume=consume)

    @_resilient("sweep")
    def sweep(self, now=None):
        return self.primary.sweep(now)


RedisResilientJumpCodeStore = ResilientRedisShortCodeStore


class ResilientRedisRateLimiter:
    """Redis 限流器，故障时降级为本副本限流。"""

    def __init__(self, backend, limit_per_min=0, primary=None, fallback=None):
        self.backend = backend
        self.primary = primary or RedisRateLimiter(backend, limit_per_min)
        self.fallback = fallback or RateLimiter(limit_per_min)

    @_resilient("allow")
    def allow(self, key):
        return self.primary.allow(key)

    @_resilient("sweep")
    def sweep(self, now=None):
        return self.primary.sweep(now)


class ResilientRedisKeyedLocks:
    """Redis 锁，获取失败时降级为本副本互斥锁。"""

    def __init__(self, backend, lease_ms=30000, primary=None, fallback=None):
        self.backend = backend
        self.primary = primary or RedisKeyedLocks(backend, lease_ms)
        self.fallback = fallback or KeyedLocks()

    @contextmanager
    def lock(self, key, timeout=None):
        if not self.backend.attempt_allowed():
            with self.fallback.lock(key, timeout=timeout):
                yield
            return
        manager = self.primary.lock(key, timeout=timeout)
        try:
            manager.__enter__()
        except redis.RedisError as exc:
            self.backend.record_failure(exc)
            with self.fallback.lock(key, timeout=timeout):
                yield
            return
        except Exception:
            self.backend.abandon_attempt()
            raise
        self.backend.record_success()
        try:
            yield
        finally:
            try:
                manager.__exit__(None, None, None)
            except redis.RedisError as exc:
                self.backend.record_failure(exc)


class LocalSemaphore:
    """Redis 信号量的本副本降级实现。"""

    def __init__(self, limit):
        self._semaphore = threading.BoundedSemaphore(max(1, int(limit)))

    def acquire(self, timeout=None):
        if timeout is None:
            return self._semaphore.acquire()
        return self._semaphore.acquire(timeout=max(0.0, float(timeout)))

    def release(self):
        self._semaphore.release()


class ResilientRedisSemaphore:
    """Redis 上游信号量，故障时降级为本副本并发上限。"""

    def __init__(self, backend, limit, timeout=UPSTREAM_SEM_TIMEOUT,
                 lease=30.0, primary=None, fallback=None):
        self.backend = backend
        self.primary = primary or RedisSemaphore(
            backend, limit, timeout=timeout, lease=lease)
        self.fallback = fallback or LocalSemaphore(limit)
        self._fallback_holds = 0
        self._holds_lock = threading.Lock()

    def acquire(self, timeout=None):
        if not self.backend.attempt_allowed():
            if self.fallback.acquire(timeout=timeout):
                with self._holds_lock:
                    self._fallback_holds += 1
                return True
            return False
        try:
            result = self.primary.acquire(timeout=timeout)
        except redis.RedisError as exc:
            self.backend.record_failure(exc)
            if self.fallback.acquire(timeout=timeout):
                with self._holds_lock:
                    self._fallback_holds += 1
                return True
            return False
        except Exception:
            self.backend.abandon_attempt()
            raise
        self.backend.record_success()
        return result

    def release(self):
        with self._holds_lock:
            if self._fallback_holds > 0:
                self._fallback_holds -= 1
                self.fallback.release()
                return
        try:
            self.primary.release()
        except redis.RedisError as exc:
            self.backend.record_failure(exc)


def _b(value):
    """把 Redis 返回的 bytes 解码为 str（非 bytes 原样返回）。"""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


class RedisSessionStore:
    """Redis 版会话存储，接口与 jwxt_state.SessionStore 一致。

    数据形态：
    - Hash `prefix:session:<sid>`：username / cookies(JSON) / portal(JSON) / at / exp / pwd_hash
    - Set  `prefix:user:<username>`：同账号会话索引（复用查询）
    - ZSet `prefix:session-index`：按创建时间排序，超限时淘汰最旧

    warm_evt（threading.Event）无法跨进程共享，保存在签发进程本地；
    其他副本首次使用时由 _ensure_warmed 按需同步补齐。
    """

    def __init__(self, backend, ttl, max_sessions):
        self.backend = backend
        self.ttl = max(60, int(ttl))
        self.max_sessions = max(1, int(max_sessions))
        self._warm_events = {}
        self._local_lock = threading.Lock()

    def _key(self, sid):
        return self.backend.key("session:" + sid)

    def _user_key(self, username):
        return self.backend.key("user:" + str(username))

    def _index_key(self):
        return self.backend.key("session-index")

    def _drop(self, sid):
        """从 Redis 同步移除会话与索引（幂等）。"""
        client = self.backend.client
        raw_user = client.hget(self._key(sid), "username")
        user = _b(raw_user) if raw_user is not None else None
        pipe = client.pipeline()
        pipe.delete(self._key(sid))
        if user:
            pipe.srem(self._user_key(user), sid)
        pipe.zrem(self._index_key(), sid)
        pipe.execute()
        with self._local_lock:
            self._warm_events.pop(sid, None)

    def create(self, username, cookies, portal, warm_evt=None, pwd_hash=None):
        sid = secrets.token_urlsafe(24)
        now = time.time()
        client = self.backend.client
        mapping = {
            "username": str(username),
            "cookies": json.dumps(cookies or [], ensure_ascii=False),
            "portal": json.dumps(portal or {}, ensure_ascii=False),
            "at": "%.6f" % now,
            "exp": "%.6f" % (now + self.ttl),
            "pwd_hash": pwd_hash or "",
        }
        key = self._key(sid)
        client.hset(key, mapping=mapping)
        client.expire(key, self.ttl + _TTL_MARGIN)
        client.sadd(self._user_key(username), sid)
        client.expire(self._user_key(username), self.ttl + _TTL_MARGIN)
        client.zadd(self._index_key(), {sid: now})
        if warm_evt is not None:
            with self._local_lock:
                self._warm_events[sid] = warm_evt
        count = client.zcard(self._index_key())
        if count > self.max_sessions:
            evicted = client.zpopmin(self._index_key(), count=count - self.max_sessions)
            for member, _score in evicted:
                self._drop(_b(member))
        return sid

    def _record(self, raw):
        """把 Redis Hash 字段还原为内存会话记录 dict。"""
        if not raw:
            return None
        rec = {
            "username": _b(raw.get(b"username", b"")),
            "cookies": json.loads(_b(raw.get(b"cookies", b"[]")) or "[]"),
            "portal": json.loads(_b(raw.get(b"portal", b"{}")) or "{}"),
            "at": float(_b(raw.get(b"at", b"0")) or 0),
            "exp": float(_b(raw.get(b"exp", b"0")) or 0),
            "pwd_hash": _b(raw.get(b"pwd_hash", b"")) or None,
        }
        return rec

    def find_reusable(self, username, pwd_hash, max_age):
        """查找可复用同账号会话：密码摘要一致、未过期、创建时间在 max_age 内。"""
        if not username or not pwd_hash:
            return None
        client = self.backend.client
        now = time.time()
        sids = client.smembers(self._user_key(username)) or set()
        if not sids:
            return None
        pipe = client.pipeline()
        for sid in sids:
            pipe.hgetall(self._key(_b(sid)))
        rows = pipe.execute()
        best, best_at = None, -1
        for sid, row in zip(sids, rows):
            rec = self._record(row)
            if rec is None:
                continue
            if (rec.get("pwd_hash") == pwd_hash
                    and rec["exp"] > now
                    and (now - rec.get("at", 0)) <= max_age
                    and rec.get("at", 0) > best_at):
                best, best_at = _b(sid), rec.get("at", 0)
        return best

    def refresh(self, sid, cookies, portal):
        """后台预热完成后更新会话数据（Cookie/门户信息），不延长有效期。

        与内存实现语义一致：只有 touch()（成功使用）才会滑动续期；
        Redis 键 TTL 由 create/touch 维护，刷新只更新字段。
        """
        client = self.backend.client
        key = self._key(sid)
        if not client.exists(key):
            return False
        pipe = client.pipeline()
        if cookies is not None:
            pipe.hset(key, "cookies", json.dumps(cookies, ensure_ascii=False))
        if portal is not None:
            pipe.hset(key, "portal", json.dumps(portal, ensure_ascii=False))
        pipe.execute()
        return True

    def touch(self, sid):
        client = self.backend.client
        key = self._key(sid)
        if not client.exists(key):
            return False
        now = time.time()
        client.hset(key, "exp", "%.6f" % (now + self.ttl))
        client.expire(key, self.ttl + _TTL_MARGIN)
        return True

    def get(self, sid):
        if not sid:
            return None
        client = self.backend.client
        raw = client.hgetall(self._key(sid))
        rec = self._record(raw)
        if rec is None:
            return None
        if rec["exp"] < time.time():
            self._drop(sid)
            return None
        with self._local_lock:
            evt = self._warm_events.get(sid)
        if evt is not None:
            rec["warm_evt"] = evt
        return rec

    def sweep(self, now=None):
        """清理 session-index 中会话键已消失的过期残留成员。"""
        client = self.backend.client
        sids = client.zrange(self._index_key(), 0, -1) or []
        if not sids:
            return 0
        pipe = client.pipeline()
        for sid in sids:
            pipe.exists(self._key(_b(sid)))
        rows = pipe.execute()
        dead = [sid for sid, ok in zip(sids, rows) if not ok]
        if dead:
            pipe2 = client.pipeline()
            for sid in dead:
                pipe2.zrem(self._index_key(), sid)
            pipe2.execute()
            # Redis 键自然过期时同步清理本进程预热事件，避免本地字典随登录次数无界增长
            with self._local_lock:
                for sid in dead:
                    self._warm_events.pop(_b(sid), None)
        return len(dead)

    def status(self, sid):
        rec = self.get(sid)
        if rec is None:
            return {"valid": False}
        return {"valid": True, "username": rec["username"],
                "expires_in": max(0, int(rec["exp"] - time.time())),
                "expires_at": int(rec["exp"])}

    def invalidate(self, sid):
        client = self.backend.client
        existed = bool(client.exists(self._key(sid)))
        self._drop(sid)
        return existed

    def count(self):
        return self.backend.client.zcard(self._index_key())

    def sessions(self):
        """返回 {sid: 会话记录}（供调试/测试使用）。"""
        client = self.backend.client
        sids = client.zrange(self._index_key(), 0, -1) or []
        out = {}
        for sid in sids:
            sid = _b(sid)
            rec = self.get(sid)
            if rec is not None:
                out[sid] = rec
        return out


class RedisTTLCache:
    """Redis 版 TTL 缓存，接口与 jwxt_state.TTLCache 一致。

    数据形态：`prefix:cache:<key>` 存 JSON 值，`prefix:cache:<key>:payload`
    存预序列化响应字节；TTL 由 Redis 管理。ttl<=0 表示关闭缓存；
    max_items 仅作接口兼容（容量由 Redis 内存与 TTL 控制）。
    """

    def __init__(self, backend, ttl=0, max_items=2000):
        self.backend = backend
        self.ttl = max(0, int(ttl))
        self.max_items = max(1, int(max_items))

    def _key(self, key):
        return self.backend.key("cache:" + str(key))

    def _payload_key(self, key):
        return self.backend.key("cache:" + str(key) + ":payload")

    def get(self, key):
        if not self.ttl or not key:
            return None
        raw = self.backend.client.get(self._key(key))
        if raw is None:
            return None
        try:
            return json.loads(_b(raw))
        except (ValueError, TypeError):
            return None

    def get_payload(self, key):
        if not self.ttl or not key:
            return None
        return self.backend.client.get(self._payload_key(key))

    def delete(self, key):
        if not key:
            return
        pipe = self.backend.client.pipeline()
        pipe.delete(self._key(key))
        pipe.delete(self._payload_key(key))
        pipe.execute()

    def set(self, key, value, ttl=None, payload=None):
        if not self.ttl or not key:
            return
        ttl = self.ttl if ttl is None else max(1, int(ttl))
        pipe = self.backend.client.pipeline()
        pipe.set(self._key(key), json.dumps(value, ensure_ascii=False), px=ttl * 1000)
        if payload is not None:
            pipe.set(self._payload_key(key), payload, px=ttl * 1000)
        else:
            # 新写入不带 payload 时必须清掉旧 payload，避免返回过期响应体
            pipe.delete(self._payload_key(key))
        pipe.execute()

    def sweep(self, now=None):
        """TTL 由 Redis 管理；保留接口供后台 sweeper 调用。"""
        return 0


class RedisShortCodeStore:
    """Redis 版短时随机码存储，接口与 jwxt_state.ShortCodeStore 一致。

    数据形态：`prefix:code:<code>` 存 sid（TTL），`prefix:code-index` ZSet
    按过期时间排序用于超限淘汰；一次性消费用 Redis 6.2+ 的 GETDEL 原子取删，
    旧版本回退 GET+DEL。
    """

    def __init__(self, backend, ttl=60, max_codes=10000):
        self.backend = backend
        self.ttl = max(1, int(ttl))
        self.max_codes = max(1, int(max_codes))

    def _key(self, code):
        return self.backend.key("code:" + str(code))

    def _index_key(self):
        return self.backend.key("code-index")

    def mint(self, sid):
        code = secrets.token_urlsafe(16)
        now = time.time()
        client = self.backend.client
        client.set(self._key(code), str(sid), px=self.ttl * 1000)
        client.zadd(self._index_key(), {code: now + self.ttl})
        count = client.zcard(self._index_key())
        if count > self.max_codes:
            evicted = client.zpopmin(self._index_key(), count=count - self.max_codes)
            for member, _score in evicted:
                client.delete(self._key(_b(member)))
        return code

    def resolve(self, code, consume=True):
        if not code:
            return None
        client = self.backend.client
        if consume:
            # Redis 6.2+ 用 GETDEL 原子取并删；旧版本回退 GET+DEL。
            try:
                raw = client.getdel(self._key(code))
            except redis.ResponseError as exc:
                if "unknown command" not in str(exc).lower():
                    raise
                raw = client.get(self._key(code))
                if raw is not None:
                    client.delete(self._key(code))
            if raw is not None:
                client.zrem(self._index_key(), code)
        else:
            raw = client.get(self._key(code))
        if raw is None:
            return None
        return _b(raw)

    def sweep(self, now=None):
        """清理 code-index 中短码键已消失的过期残留成员。"""
        client = self.backend.client
        members = client.zrange(self._index_key(), 0, -1) or []
        if not members:
            return 0
        pipe = client.pipeline()
        for member in members:
            pipe.exists(self._key(_b(member)))
        rows = pipe.execute()
        dead = [member for member, ok in zip(members, rows) if not ok]
        if dead:
            pipe2 = client.pipeline()
            for member in dead:
                pipe2.zrem(self._index_key(), member)
            pipe2.execute()
        return len(dead)


RedisJumpCodeStore = RedisShortCodeStore  # 兼容旧名称


class RedisRateLimiter:
    """Redis 版令牌桶限流，接口与 jwxt_state.RateLimiter 一致。

    用 WATCH/MULTI 乐观锁保证多副本下同一 key 的计数一致；
    limit=0 表示不限。
    """

    def __init__(self, backend, limit_per_min=0):
        """limit_per_min=0 表示不限流；令牌按 limit/60 每秒补充。"""
        self.backend = backend
        self.limit = max(0, int(limit_per_min))
        self.rate = (self.limit / 60.0) if self.limit else 0.0

    def _key(self, key):
        return self.backend.key("rate:" + str(key))

    def allow(self, key):
        if not self.limit:
            return True
        client = self.backend.client
        k = self._key(key)
        now = time.time()
        while True:
            try:
                pipe = client.pipeline()
                pipe.watch(k)
                row = pipe.hmget(k, "tokens", "last")
                tokens = float(row[0]) if row[0] is not None else float(self.limit)
                last = float(row[1]) if row[1] is not None else now
                tokens = min(float(self.limit), tokens + (now - last) * self.rate)
                allowed = tokens >= 1.0
                if allowed:
                    tokens -= 1.0
                pipe.multi()
                pipe.hset(k, mapping={"tokens": tokens, "last": now})
                pipe.pexpire(k, 120000)
                pipe.execute()
                return allowed
            except redis.WatchError:
                # 乐观锁冲突：短暂退避后重试，避免高并发下空转 CPU
                time.sleep(0.002)
                continue

    def sweep(self, now=None):
        """TTL 由 Redis 管理；保留接口供后台 sweeper 调用。"""
        return 0


class RedisKeyedLocks:
    """Redis 版按 key 互斥锁，接口与 jwxt_state.KeyedLocks 一致。

    加锁用 SET NX PX（带租约，进程崩溃后自动过期）；
    释放用 WATCH/MULTI 校验 token 后删除，避免误删他人锁。
    """

    def __init__(self, backend, lease_ms=30000):
        self.backend = backend
        self._lease_ms = max(1000, int(lease_ms))

    def _key(self, key):
        return self.backend.key("lock:" + str(key))

    @contextmanager
    def lock(self, key, timeout=None):
        k = self._key(key)
        token = secrets.token_hex(8)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self.backend.client.set(k, token, nx=True, px=self._lease_ms):
                break
            if deadline is not None and time.monotonic() >= deadline:
                raise LockTimeoutError("等待登录锁超时：%s" % key)
            time.sleep(0.02)
        try:
            yield
        finally:
            self._release(k, token)

    def _release(self, key, token):
        client = self.backend.client
        while True:
            try:
                pipe = client.pipeline()
                pipe.watch(key)
                if not secrets.compare_digest(_b(pipe.get(key)), token):
                    pipe.unwatch()
                    return
                pipe.multi()
                pipe.delete(key)
                pipe.execute()
                return
            except redis.WatchError:
                # 乐观锁冲突：短暂退避后重试
                time.sleep(0.002)
                continue


class RedisSemaphore:
    """Redis 版全局信号量（上游并发上限）。

    计数键带租约 TTL，持有者崩溃后槽位自动释放；
    获取/释放用 WATCH/MULTI 乐观锁保证多副本计数一致。
    """

    def __init__(self, backend, limit, timeout=UPSTREAM_SEM_TIMEOUT, lease=30.0):
        self.backend = backend
        self.limit = max(1, int(limit))
        self.timeout = max(0.1, float(timeout))
        self.lease = max(5.0, float(lease))

    def _key(self):
        return self.backend.key("upstream:counter")

    def acquire(self, timeout=None):
        client = self.backend.client
        k = self._key()
        wait = self.timeout if timeout is None else max(0.0, float(timeout))
        deadline = time.monotonic() + wait
        while True:
            try:
                pipe = client.pipeline()
                pipe.watch(k)
                cur = int(pipe.get(k) or 0)
                if cur < self.limit:
                    pipe.multi()
                    pipe.incr(k)
                    pipe.expire(k, int(self.lease))
                    pipe.execute()
                    return True
                pipe.unwatch()
            except redis.WatchError:
                # 乐观锁冲突：短暂退避后重试
                time.sleep(0.002)
                continue
            if time.monotonic() >= deadline:
                raise UpstreamBusyError(
                    "上游并发已达上限（等待 %.1fs 超时），请稍后重试" % wait)
            time.sleep(0.02)

    def release(self):
        client = self.backend.client
        k = self._key()
        while True:
            try:
                pipe = client.pipeline()
                pipe.watch(k)
                cur = int(pipe.get(k) or 0)
                if cur <= 0:
                    pipe.unwatch()
                    return
                pipe.multi()
                pipe.decr(k)
                pipe.execute()
                return
            except redis.WatchError:
                # 乐观锁冲突：短暂退避后重试
                time.sleep(0.002)
                continue
