#!/usr/bin/env python3
"""状态与缓存模块：会话存储、限流、TTL 缓存、短码、健康与指标。"""

import json
import secrets
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from urllib.parse import quote

import requests

from app import school_endpoints as _ep
from app.jwxt_core import (
    J, BASE, UA, TIMEOUT, VERIFY_TLS, LOG, JUMP_PAGES,
    LOGIN_REUSE, WARM_WAIT, VERSION, MAX_SESSIONS, JUMP_CODE_TTL, SchoolError,
    SessionInvalidError, TokenError, UpstreamBusyError, LockTimeoutError,
    LATENCY_BUCKETS_MS, to_webvpn, WarmPendingError,
    dump_session_cookies, dump_portal, load_cookies,
    set_trace_id, _shared_adapter, _upstream_slot, _sanitize_url, _has_session_hint,
    _is_auth_failure, _is_transient_error, _METRICS, _METRICS_LOCK,
)
from app.school_contract import (
    CAS_BUNDLE_JS_RE, CAS_COOKIE_DOMAIN, CAS_REDIRECT_URL_RE,
    CAS_RSA_EXPONENT_RE, CAS_RSA_MODULUS_RE, CAS_TGT_COOKIE_NAME, LAYER_CAS,
    LAYER_MGMT, LAYER_PORTAL, LAYER_STUDENT, LAYER_WEBVPN,
    LOGIN_PARAM_EXPECTED_MASTER, PORTAL_LOGIN_PARAM, PORTAL_REDIRECT_TO_CAS,
    SCHOOL_CONTRACT_VERSION, VPN_BASE, WEBVPN_COOKIE_PATH, is_webvpn_url,
)

class ServerState:
    def __init__(self):
        self.started = time.time()
        self.lock = threading.Lock()
        self.net = None            # 学校可达性探测缓存 {at, ok, latency_ms, ...}
        self.redis = None          # Redis 连通性探测缓存 {at, ok}（Redis 模式）
        self.deep = None           # 最近一次真实登录+接口探测结果
        self.school_layers = None  # 最近一次无凭据分层探测缓存 {at, layers}（可选周期）
        self.last_real = None      # 最近一次业务查询结果
        self.last_deep_start = 0   # 真实探测节流时间戳
        self.metrics_lock = threading.Lock()
        self.metrics = {}          # endpoint -> {total, ok, err, latency_sum, cache_hit}
        self.last_reals = {}       # endpoint -> 最近一次 {at, status, ok, duration_ms}

    def record(self, endpoint, ok, duration_ms, cache_hit=False, status_code=200):
        """记录一次请求指标（/metrics 导出）。"""
        with self.metrics_lock:
            # 只统计已知/高频端点，避免任意 404 路径让 metrics 无限增长
            if endpoint not in self.metrics and len(self.metrics) >= 200:
                return
            m = self.metrics.setdefault(endpoint, {
                "total": 0, "ok": 0, "err": 0, "latency_sum": 0.0, "cache_hit": 0,
                "latency_buckets": [0] * len(LATENCY_BUCKETS_MS)})
            m["total"] += 1
            m["latency_sum"] += duration_ms
            for i, b in enumerate(LATENCY_BUCKETS_MS):
                if duration_ms <= b:
                    m["latency_buckets"][i] += 1
            if ok:
                m["ok"] += 1
            else:
                m["err"] += 1
            if cache_hit:
                m["cache_hit"] += 1
            self.last_reals[endpoint] = {
                "at": int(time.time()),
                "status": status_code,
                "ok": ok,
                "duration_ms": round(duration_ms, 1),
                "cache_hit": cache_hit,
            }

class SessionStore:
    """内存会话存储：/login 成功后签发短随机 session ID，会话数据保存在进程内存中，
    不落盘、不加密、重启即失效。ID 为 192 位随机数，不可猜测，持有即有权使用。
    会话记录额外保存密码单向摘要（内存）用于同账号复用校验，不保存明文密码。
    维护 username -> sid 集合索引，避免同账号复用查询退化为全表扫描。"""

    def __init__(self, ttl, max_sessions=MAX_SESSIONS):
        self.ttl = max(60, int(ttl))
        self.max_sessions = max(1, int(max_sessions))
        self.lock = threading.RLock()   # create() 持锁期间会再调用 sweep()，需可重入
        self.sessions = {}
        self.by_user = {}

    def _drop(self, sid):
        """从 sessions 与 username 索引中同步移除会话（幂等）。"""
        rec = self.sessions.pop(sid, None)
        if rec is None:
            return
        user = rec.get("username")
        sids = self.by_user.get(user)
        if sids is not None:
            sids.discard(sid)
            if not sids:
                self.by_user.pop(user, None)

    def create(self, username, cookies, portal, warm_evt=None, pwd_hash=None):
        sid = secrets.token_urlsafe(24)
        now = time.time()
        with self.lock:
            self.sweep(now)
            rec = {
                "username": username,
                "cookies": cookies,
                "portal": portal or {},
                "at": now,
                "lat": now,   # 最后活跃时间（touch 时更新），满载淘汰依据
                "exp": now + self.ttl,
                "pwd_hash": pwd_hash,
            }
            if warm_evt is not None:
                rec["warm_evt"] = warm_evt
            self.sessions[sid] = rec
            self.by_user.setdefault(username, set()).add(sid)
            if len(self.sessions) > self.max_sessions:
                # 按最后活跃时间淘汰（LRU）：最活跃的老会话不再因创建早被先踢
                oldest = min(self.sessions, key=lambda k: self.sessions[k].get(
                    "lat", self.sessions[k].get("at", 0)))
                self._drop(oldest)
        return sid

    def find_reusable(self, username, pwd_hash, max_age):
        """查找可复用的同账号会话：用户名+密码摘要一致、未过期、签发时间在 max_age 内。
        返回最“新”的一个 sid；找不到返回 None。"""
        if not username or not pwd_hash or not LOGIN_REUSE:
            return None
        now = time.time()
        with self.lock:
            self.sweep(now)
            best, best_at = None, -1
            for sid in self.by_user.get(username, ()):
                s = self.sessions.get(sid)
                if s is None:
                    continue
                if (s.get("pwd_hash") == pwd_hash
                        and s["exp"] > now and (now - s.get("at", 0)) <= max_age
                        and s.get("at", 0) > best_at):
                    best, best_at = sid, s.get("at", 0)
            return best

    def refresh(self, sid, cookies, portal):
        """后台预热完成后更新会话数据（Cookie/门户信息），不延长有效期。"""
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return False
            if cookies is not None:
                s["cookies"] = cookies
            if portal is not None:
                s["portal"] = portal
            return True

    def touch(self, sid):
        """滑动续期：成功使用后把过期时间顺延为 now+ttl，并刷新最后活跃时间。

        与学校会话生命周期对齐：活跃会话不会被服务端 TTL 提前掐断，
        闲置会话按 SESSION_TTL 自然过期，过期后由调用方重新 /login。
        最后活跃时间同时作为满载淘汰依据（而非创建时间）。
        """
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return False
            now = time.time()
            s["exp"] = now + self.ttl
            s["lat"] = now
            return True

    def get(self, sid):
        if not sid:
            return None
        now = time.time()
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return None
            if s["exp"] < now:
                self._drop(sid)
                return None
            return s

    def sweep(self, now=None):
        now = now or time.time()
        with self.lock:
            expired = [k for k, v in self.sessions.items() if v["exp"] < now]
            for k in expired:
                self._drop(k)
            return len(expired)

    def status(self, sid):
        s = self.get(sid)
        if s is None:
            return {"valid": False}
        return {"valid": True, "username": s["username"],
                "expires_in": max(0, int(s["exp"] - time.time())),
                "expires_at": int(s["exp"])}

    def invalidate(self, sid):
        """主动清除指定会话（如学校端强制下线/未登录时），
        使后续 /login 同账号复用不再返回死会话。"""
        with self.lock:
            existed = sid in self.sessions
            self._drop(sid)
            return existed


class BodyTooLarge(Exception):
    pass


class KeyedLocks:
    """按 key 互斥的锁集合（引用计数，key 空闲后自动清理）。

    用于同账号并发登录互斥：避免两个请求同时向学校创建会话，
    触发“已在其他地方登录”互相顶下线。
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}

    @contextmanager
    def lock(self, key, timeout=None):
        with self._guard:
            entry = self._locks.get(key)
            if entry is None:
                entry = [threading.Lock(), 0]
                self._locks[key] = entry
            entry[1] += 1
        if timeout is None:
            acquired = entry[0].acquire()
        else:
            acquired = entry[0].acquire(timeout=timeout)
        if not acquired:
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(key, None)
            raise LockTimeoutError("等待登录锁超时：%s" % key)
        try:
            yield
        finally:
            entry[0].release()
            with self._guard:
                entry[1] -= 1
                if entry[1] == 0:
                    self._locks.pop(key, None)


class ConcurrencyLimiter:
    """进程内并发请求上限：超出时返回统一 JSON 503（替代 uvicorn 纯文本 503）。"""

    def __init__(self, limit=0):
        self.limit = max(0, int(limit))
        self._active = 0
        self._lock = threading.Lock()

    def try_acquire(self):
        if not self.limit:
            return True
        with self._lock:
            if self._active >= self.limit:
                return False
            self._active += 1
            return True

    def release(self):
        if not self.limit:
            return
        with self._lock:
            self._active = max(0, self._active - 1)


class RateLimiter:
    """令牌桶限流：容量 limit，按 limit/60 每秒补充（limit=0 表示不限）。

    相比固定窗口，令牌桶在分钟边界不会“瞬间清零重来”，可平滑突发流量；
    idle 一段时间后仍允许最多 limit 次突发，适合本服务的调用场景。

    桶数量有硬上限（MAX_BUCKETS）：达到上限时按 LRU 淘汰最久未使用的 key，
    避免大量不同来源 key 让进程内存无界增长；淘汰是 O(1)，不会退化为全表重建。
    """

    MAX_BUCKETS = 10000

    def __init__(self, limit_per_min=0, max_buckets=MAX_BUCKETS):
        self.limit = max(0, int(limit_per_min))
        self.rate = (self.limit / 60.0) if self.limit else 0.0
        self.max_buckets = max(1, int(max_buckets))
        self.lock = threading.Lock()
        self.buckets = OrderedDict()

    def allow(self, key):
        if not self.limit:
            return True
        now = time.time()
        with self.lock:
            b = self.buckets.get(key)
            if b is None:
                b = [float(self.limit), now]
                self.buckets[key] = b
                if len(self.buckets) > self.max_buckets:
                    self.buckets.popitem(last=False)
            else:
                tokens, last = b
                tokens = min(float(self.limit), tokens + (now - last) * self.rate)
                b[0] = tokens
                b[1] = now
                self.buckets.move_to_end(key)
            if b[0] >= 1.0:
                b[0] -= 1.0
                return True
            return False

    def _prune(self, now):
        """兼容旧调用：仅做一次轻量清理，不再对全表做 O(n) 重建。"""
        if len(self.buckets) > self.max_buckets:
            self.sweep(now)

    def sweep(self, now=None):
        """清理超过 2 分钟未活动的 key（由后台 sweeper 周期调用）。"""
        if not self.limit:
            return 0
        now = now or time.time()
        cutoff = now - 120
        with self.lock:
            dead = [k for k, b in self.buckets.items() if b[1] < cutoff]
            for k in dead:
                self.buckets.pop(k, None)
            return len(dead)


class TTLCache:
    """线程安全的进程内短 TTL 缓存（不落盘、重启即失效）。

    用于缓存成绩/课表查询结果：数据在一天内基本不变，短 TTL 可显著减少
    对学校上游的重复查询压力。ttl<=0 表示关闭缓存。

    内存模式：JSON 类缓存 set() 不传 payload（只存 value dict），
    get_payload() 命中时按需序列化——同一份结果不再同时保存 dict 与 bytes
    两份，避免大响应（ grades/schedule 默认各 max_items=1000）的内存放大；
    PDF 等二进制缓存仍可显式传 payload（value 只是元数据、字节流即主体）。
    满载时先惰性清理过期项（OrderedDict 原地删除，不再整表重建），
    仍超限按插入序淘汰最旧一条。
    """

    def __init__(self, ttl=0, max_items=2000):
        self.ttl = max(0, int(ttl))
        self.max_items = max(1, int(max_items))
        self.lock = threading.Lock()
        self.data = OrderedDict()

    def get(self, key):
        if not self.ttl or not key:
            return None
        now = time.time()
        with self.lock:
            hit = self.data.get(key)
            if hit is None:
                return None
            if hit[0] <= now:
                del self.data[key]
                return None
            return hit[1]

    def get_payload(self, key):
        """取缓存项对应的序列化响应体；未显式存储 payload 时按需从 value 序列化。"""
        if not self.ttl or not key:
            return None
        now = time.time()
        with self.lock:
            hit = self.data.get(key)
            if hit is None or hit[0] <= now:
                return None
            payload = hit[2]
            if payload is None:
                try:
                    payload = json.dumps(hit[1], ensure_ascii=False).encode("utf-8")
                except (TypeError, ValueError):
                    return None
            return payload

    def delete(self, key):
        """主动删除缓存项（如会话失效时废弃对应跳转缓存）。"""
        if not key:
            return
        with self.lock:
            self.data.pop(key, None)

    def set(self, key, value, ttl=None, payload=None):
        if not self.ttl or not key:
            return
        ttl = self.ttl if ttl is None else max(1, int(ttl))
        with self.lock:
            if len(self.data) >= self.max_items:
                now = time.time()
                # 先惰性清理过期项（原地删除，避免旧实现的整表 dict 重建）；
                # 仍超限则按插入序淘汰最旧的一条
                dead = [k for k, v in self.data.items() if v[0] <= now]
                for k in dead:
                    self.data.pop(k, None)
                if len(self.data) >= self.max_items:
                    self.data.popitem(last=False)
            self.data[key] = (time.time() + ttl, value, payload)

    def sweep(self, now=None):
        if not self.ttl:
            return 0
        now = now or time.time()
        with self.lock:
            dead = [k for k, v in self.data.items() if v[0] <= now]
            for k in dead:
                self.data.pop(k, None)
            return len(dead)


class ShortCodeStore:
    """短时随机码存储（/jump 跳转码、课表下载码共用）。

    码本身不包含长期有效的 session，避免把令牌直接暴露在 URL/聊天/浏览器历史里。
    resolve(consume=True) 为一次性消费；桥接页/json 取票等需要重试的场景
    用 consume=False，在 TTL 内可重复读取。
    """

    def __init__(self, ttl=JUMP_CODE_TTL, max_codes=10000):
        self.ttl = max(1, int(ttl))
        self.max_codes = max(1, int(max_codes))
        self.lock = threading.Lock()
        self.codes = {}

    def mint(self, sid):
        code = secrets.token_urlsafe(16)
        now = time.time()
        with self.lock:
            self._prune(now)
            if len(self.codes) >= self.max_codes:
                oldest = min(self.codes, key=lambda k: self.codes[k][1])
                self.codes.pop(oldest, None)
            self.codes[code] = (sid, now + self.ttl)
        return code

    def resolve(self, code, consume=True):
        if not code:
            return None
        now = time.time()
        with self.lock:
            rec = self.codes.get(code)
            if rec is None:
                return None
            sid, exp = rec
            if exp < now:
                self.codes.pop(code, None)
                return None
            if consume:
                self.codes.pop(code, None)
            return sid

    def _prune(self, now):
        dead = [k for k, v in self.codes.items() if v[1] < now]
        for k in dead:
            self.codes.pop(k, None)

    def sweep(self, now=None):
        now = now or time.time()
        with self.lock:
            dead = [k for k, v in self.codes.items() if v[1] < now]
            for k in dead:
                self.codes.pop(k, None)
            return len(dead)


JumpCodeStore = ShortCodeStore  # 兼容旧名称


def _restore_portal(j, claims, require_stu=True):
    """从令牌恢复学生门户地址，使复用会话无需重新走一遍 SSO。
    stu 存的是原始地址（短）时经 to_webvpn 还原；旧令牌已存 WebVPN URL 则直接用。
    mgmt/cas 旧令牌可能携带，仅兼容读取（复用期不使用）。
    require_stu=False 时允许 stu 尚未就绪（/login 已返回、后台预热未完成），
    由 _ensure_warmed 在首次使用时补齐。"""
    portal = claims.get("portal") or {}
    stu = portal.get("stu")
    if not stu:
        if require_stu:
            raise TokenError("令牌缺少门户信息，请重新 POST /login 获取新令牌")
        j.stu = None
        j.stu_frag = None
    else:
        if not is_webvpn_url(stu):
            stu = to_webvpn(stu)
        j.stu = stu.split("#")[0]
        j.stu_frag = portal.get("frag") or stu.partition("#")[2] or None
    j.mgmt = portal.get("mgmt")
    j.cas = portal.get("cas")
    j._tgt = portal.get("tgt")


def _ensure_warmed(sessions, sid, c):
    """确保会话已具备学生门户地址（stu）：后台预热完成则直接可用；
    未完成/失败时按需同步补齐，保证 /login 快速返回后首次查询仍可用。"""
    if (c.get("portal") or {}).get("stu"):
        return
    evt = c.get("warm_evt")
    if evt is not None and not evt.is_set():
        evt.wait(WARM_WAIT)
    c2 = sessions.get(sid)
    if c2 is not None and (c2.get("portal") or {}).get("stu"):
        return
    LOG.info("会话预热未在 %ss 内完成，按需同步补齐 session=***",
             WARM_WAIT)
    j = None
    try:
        j = _session_j(sessions, sid, require_stu=False)
        if not getattr(j, "mgmt", None):
            j.jwxt()
        j.student()
        sessions.refresh(sid, dump_session_cookies(j), dump_portal(j))
    except TokenError:
        raise
    except Exception as e:
        if _is_transient_error(e):
            raise WarmPendingError("会话预热门户暂未完成，请稍后重试")
        raise TokenError("session 无效或已过期，请先 POST /login 获取新 session（%s）" % e)
    finally:
        if j is not None:
            j.close()


def _session_j(sessions, sid, require_stu=True):
    """从内存会话恢复 j（可选“等待/补齐学生门户”语义），会话无效时抛 TokenError。"""
    c = sessions.get(sid) if sessions is not None else None
    if c is None:
        raise TokenError("session 无效或已过期，请先 POST /login 获取新 session")
    if require_stu:
        _ensure_warmed(sessions, sid, c)
        c = sessions.get(sid)
        if c is None:
            raise TokenError("session 无效或已过期，请先 POST /login 获取新 session")
    j = J(c["username"], "")
    load_cookies(j, c["cookies"])
    _restore_portal(j, c, require_stu=require_stu)
    return j


def query_with_session(sessions, sid, fn):
    """用内存会话执行查询 fn(u, p, j)。会话不携带密码，学校会话失效时不自动重登，
    由调用方重新 POST /login 获取新 session；会话无效时抛 TokenError（由 handler 统一 401）。
    查询成功后滑动续期（touch），活跃会话不会被服务端 TTL 提前掐断。"""
    j = _session_j(sessions, sid, require_stu=True)
    try:
        res = fn(j.u, "", j)
        sessions.touch(sid)
        return res
    finally:
        j.close()


def with_session_j(sessions, sid, fn):
    """从内存会话恢复 j 并执行 fn(j)；会话不存在/过期时抛出 TokenError。
    成功执行后滑动续期（touch），与 query_with_session 保持一致的续期语义。"""
    j = _session_j(sessions, sid, require_stu=True)
    try:
        res = fn(j)
        sessions.touch(sid)
        return res
    finally:
        j.close()


def _probe_session(sessions, sid):
    """/login 复用前轻量探测：用会话 Cookie 调门户会话校验接口，确认学校侧会话仍有效。

    认证类失败（未登录/强制下线/登录校验失败）即清除内存会话并返回 False；
    瞬时网络错误保留会话，避免上游抖动把有效会话误杀。"""
    try:
        with_session_j(sessions, sid, lambda j: j.verify())
        return True
    except Exception as e:
        if _is_auth_failure(e):
            LOG.debug("复用会话探测失败，清除并重新登录: %s", e)
            sessions.invalidate(sid)
        else:
            LOG.debug("复用会话探测失败（瞬时错误，保留会话）: %s", e)
        return False


def _warm_background(sessions, sid, j, evt, app_list=None, trace=None):
    """/login 返回后后台预热门户（管理端地址 jwxt + 学生端 student()），完成后刷新会话；
    失败不阻塞主流程，首次查询时 _ensure_warmed 会按需同步补齐。"""
    if trace:
        set_trace_id(trace)
    try:
        if not getattr(j, "mgmt", None):
            j.jwxt(app_list)
        j.student()
        sessions.refresh(sid, dump_session_cookies(j), dump_portal(j))
    except Exception as e:
        LOG.warning("后台预热学生门户失败 session=*** error=%s", e)
    finally:
        evt.set()
        j.close()


def _jump_target(stu, page, frag=None):
    """由学生门户地址 stu + 页面标识构造教务系统直达 URL（复用会话，无需重新登录）。

    page 为 JUMP_PAGES 中的别名时按映射拼接；否则按教务系统内相对路径/锚点
    （支持 "/path"、"#/hash"、"?query"）原样拼接；绝对地址一律拒绝。
    frag 为学生门户原始地址自带的 hash 片段（如 #/jwxt/js），page=home 时补回。
    """
    raw = (page or "home").strip()
    if not raw:
        raw = "home"
    low = raw.lower()
    if low.startswith(("http://", "https://", "//")):
        raise ValueError("page 不能是绝对地址，请使用教务系统内相对路径/锚点或页面别名")
    suffix = JUMP_PAGES.get(low)
    if suffix is None:
        suffix = raw if raw[0] in ("/", "#", "?") else "/" + raw
    base = stu.split("#")[0]
    if suffix:
        return base.rstrip("/") + suffix
    if frag:
        # WebVPN 根路径需要保留斜杠，否则 404；hash 片段由浏览器路由处理
        return base.rstrip("/") + "/" + "#" + frag.lstrip("#")
    return base.rstrip("/")


def _fetch_tgt(j):
    """从 WebVPN 虚拟 Cookie 中取回 CAS TGT：会话里没存 TGT 时的兜底。"""
    r = j.req("GET", BASE + WEBVPN_COOKIE_PATH + "?domain=%s&path=/"
              % quote(CAS_COOKIE_DOMAIN), timeout=TIMEOUT)
    for part in r.text.split(";"):
        name, _, val = part.strip().partition("=")
        if name == CAS_TGT_COOKIE_NAME and val:
            return val
    return None


def mint_st(j, service):
    """用会话中的 CAS TGT 换取指定 service 的新票据（免密跳转用，不触碰密码）。

    标准 CAS REST：POST /v1/tickets/{TGT}?service=.. 返回纯文本 ST；
    联奕扩展可能返回 JSON {ticket}。TGT 缺失时先从 WebVPN 虚拟 Cookie 兜底取回。
    """
    tgt = getattr(j, "_tgt", None)
    if not tgt:
        tgt = _fetch_tgt(j)
    if not tgt:
        raise SessionInvalidError("会话缺少 CAS TGT，无法签发免密跳转票据（请重新 POST /login）")
    if not getattr(j, "cas", None):
        raise SessionInvalidError("会话缺少 CAS 地址，无法签发免密跳转票据（请重新 POST /login）")
    url = j.cas + _ep.render("cas.tickets_redeem", tgt=quote(tgt, safe=""))
    r = j.req("POST", url, timeout=TIMEOUT,
              headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                       "X-Requested-With": "XMLHttpRequest",
                       "Accept": "application/json, text/plain, */*"},
              data={"service": service})
    txt = (r.text or "").strip()
    if txt.startswith("{"):
        try:
            obj = json.loads(txt)
        except Exception:
            obj = None
        if isinstance(obj, dict):
            if obj.get("ticket"):
                return str(obj["ticket"])
            if r.status_code in (401, 403) or _has_session_hint(txt):
                raise SessionInvalidError("CAS 换票失败（HTTP %s）: %s" % (
                    r.status_code, json.dumps(obj, ensure_ascii=False)[:200]))
            raise SchoolError("CAS 换票失败（HTTP %s）: %s" % (r.status_code,
                                                              json.dumps(obj, ensure_ascii=False)[:200]))
    if r.status_code in (401, 403) or _has_session_hint(txt):
        raise SessionInvalidError("CAS 换票失败（HTTP %s）: %s" % (r.status_code, txt[:200] or "(空响应)"))
    if r.status_code != 200 or not txt:
        raise SchoolError("CAS 换票失败（HTTP %s）: %s" % (r.status_code, txt[:200] or "(空响应)"))
    return txt


def probe_school(timeout=4):
    t0 = time.time()
    try:
        s = requests.Session()
        s.verify = VERIFY_TLS
        s.headers["User-Agent"] = UA
        ad = _shared_adapter()
        s.mount("https://", ad)
        s.mount("http://", ad)
        try:
            with _upstream_slot():
                s.get(BASE + "/", timeout=timeout, allow_redirects=True)
        finally:
            s.close()
    except UpstreamBusyError as e:
        # 槽位繁忙不代表学校不可达：标记 busy，避免 /health 误报 degraded
        return {"ok": None, "busy": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, e)}
    except requests.exceptions.ProxyError:
        s = requests.Session()
        s.trust_env = False
        try:
            with _upstream_slot():
                s.get(BASE + "/", timeout=timeout, verify=VERIFY_TLS, allow_redirects=True)
        except UpstreamBusyError as e:
            return {"ok": None, "busy": True,
                    "latency_ms": int((time.time() - t0) * 1000),
                    "error": "%s: %s" % (type(e).__name__, e)}
        except Exception as e:
            return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                    "error": "%s: %s" % (type(e).__name__, e)}
        finally:
            s.close()
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": True, "latency_ms": int((time.time() - t0) * 1000)}


def probe_layers(timeout=6):
    """无凭据分层探测 WebVPN/CAS/门户三层连通性与契约事实（不登录、只读 GET）。

    用于学校侧升级后的漂移定位：即使登录链路失败，三层也能独立给出结论；
    不触碰学生服务（需要会话），教务层结论由 deep_check 补充。任一步失败不
    中断后续探测，一次扫描给出全部层的漂移证据（checks 逐条判定契约事实）。

    返回 {"at", "layers": {layer: {ok, latency_ms, status?, busy?, error?}},
          "checks": [{name, layer, ok, detail?, error?}], "error"?}。
    """
    out = {"at": int(time.time()), "layers": {}, "checks": [], "error": ""}

    def _err(e):
        return "%s: %s" % (type(e).__name__, e)

    def _mark(layer, ms, ok=None, status=None, error=None, busy=False):
        item = {"ok": ok, "latency_ms": int(ms)}
        if status is not None:
            item["status"] = status
        if busy:
            item["busy"] = True
        if error:
            item["error"] = str(error)[:200]
        out["layers"][layer] = item

    def _check(name, layer, ok, detail=None, error=None):
        item = {"name": name, "layer": layer, "ok": bool(ok) if ok is not None else None}
        if detail:
            item["detail"] = str(detail)[:160]
        if error:
            item["error"] = str(error)[:200]
        out["checks"].append(item)

    s = requests.Session()
    s.verify = VERIFY_TLS
    s.headers["User-Agent"] = UA
    ad = _shared_adapter()
    s.mount("https://", ad)
    s.mount("http://", ad)

    # 1) WebVPN 边缘可达（与 probe_school 同判据：不抛异常即可达）
    t = time.time()
    try:
        with _upstream_slot():
            r = s.get(VPN_BASE + "/", timeout=timeout, allow_redirects=True)
        ms = (time.time() - t) * 1000
        _mark(LAYER_WEBVPN, ms, ok=True, status=r.status_code)
        _check("webvpn.edge_reachable", LAYER_WEBVPN, True, detail="HTTP %s" % r.status_code)
    except UpstreamBusyError as e:
        ms = (time.time() - t) * 1000
        _mark(LAYER_WEBVPN, ms, error=_err(e), busy=True)
        _check("webvpn.edge_reachable", LAYER_WEBVPN, None, error=_err(e))
    except Exception as e:
        ms = (time.time() - t) * 1000
        _mark(LAYER_WEBVPN, ms, ok=False, error=_err(e))
        _check("webvpn.edge_reachable", LAYER_WEBVPN, False, error=_err(e))

    # 2) 门户登录参数（公开接口）：可达且响应结构符合契约
    t = time.time()
    master = None
    try:
        with _upstream_slot():
            r = s.get(VPN_BASE + PORTAL_LOGIN_PARAM, timeout=timeout,
                      headers={"Accept": "application/json, text/plain, */*"})
        ms = (time.time() - t) * 1000
        try:
            d = (r.json() or {}).get("data") or {}
            master = (d.get("auth") or {}).get("master")
        except Exception:
            master = None
        portal_ok = master == LOGIN_PARAM_EXPECTED_MASTER
        _mark(LAYER_PORTAL, ms, ok=portal_ok, status=r.status_code,
              error=None if portal_ok else "门户登录参数结构漂移（auth.master=%s）" % master)
        _check("portal.auth_master", LAYER_PORTAL, portal_ok, detail="auth.master=%s" % master)
    except Exception as e:
        ms = (time.time() - t) * 1000
        _mark(LAYER_PORTAL, ms, ok=False, error=_err(e))
        _check("portal.auth_master", LAYER_PORTAL, False, error=_err(e))

    # 3) 门户 → CAS 链接（门户跳转页提取）
    t = time.time()
    cas_url = None
    try:
        with _upstream_slot():
            r = s.get(VPN_BASE + PORTAL_REDIRECT_TO_CAS, timeout=timeout)
        ms = (time.time() - t) * 1000
        m = CAS_REDIRECT_URL_RE.search(r.text or "")
        cas_url = m.group(1).replace("&amp;", "&") if m else None
        _check("portal.cas_link", LAYER_PORTAL, bool(cas_url),
               detail=cas_url or "未提取到 CAS 登录链接（门户改版信号）")
    except Exception as e:
        ms = (time.time() - t) * 1000
        _check("portal.cas_link", LAYER_PORTAL, False, error=_err(e))

    # 4) CAS 登录页 + bundle JS + RSA 公钥提取（模数必须实测，无兜底）
    if cas_url:
        t = time.time()
        try:
            with _upstream_slot():
                r = s.get(cas_url, timeout=timeout, allow_redirects=False)
            ms = (time.time() - t) * 1000
            _mark(LAYER_CAS, ms, ok=r.status_code < 500, status=r.status_code)
            _check("cas.login_page", LAYER_CAS, r.status_code < 500,
                   detail="HTTP %s" % r.status_code)
        except Exception as e:
            ms = (time.time() - t) * 1000
            _mark(LAYER_CAS, ms, ok=False, error=_err(e))
            _check("cas.login_page", LAYER_CAS, False, error=_err(e))
            r = None
        m_js = CAS_BUNDLE_JS_RE.search((getattr(r, "text", "") or "")) if r is not None else None
        _check("cas.bundle_js", LAYER_CAS, bool(m_js),
               detail=(m_js.group(1)[:120] if m_js else None),
               error=None if m_js else "登录页未定位到 bundle JS（认证产品升级信号）")
        if m_js is not None:
            src = m_js.group(1)
            if src.startswith("/"):
                src = VPN_BASE + src
            try:
                with _upstream_slot():
                    js = s.get(src, timeout=timeout)
                m_e = CAS_RSA_EXPONENT_RE.search(js.text or "")
                m_m = CAS_RSA_MODULUS_RE.search(js.text or "")
                _check("cas.rsa_exponent", LAYER_CAS, bool(m_e),
                       detail=(m_e.group(1) if m_e else None),
                       error=None if m_e else "bundle JS 中未提取到公钥指数")
                _check("cas.rsa_modulus", LAYER_CAS, bool(m_m),
                       detail=(str(m_m.group(1))[:16] + "…" if m_m else None),
                       error=None if m_m else "bundle JS 中未提取到公钥模数")
            except Exception as e:
                _check("cas.rsa_exponent", LAYER_CAS, False, error=_err(e))
                _check("cas.rsa_modulus", LAYER_CAS, False, error=_err(e))
    else:
        _mark(LAYER_CAS, 0, ok=None, error="缺少 CAS 地址（门户层未提供跳转链接）")
        _check("cas.login_page", LAYER_CAS, None, error="缺少 CAS 地址（门户层未提供跳转链接）")
    try:
        s.close()  # 共享连接池由 _SharedPoolAdapter 保护，会话关闭不销毁连接
    except Exception:
        pass
    return out


def deep_check(username, password, state=None, timeout=40):
    """真实账号全链路探测：先行无凭据分层探测（webvpn/cas/portal，约 5 个只读
    GET），再逐阶段 登录→教务发现→SSO→学生接口；每阶段记录 ok/layer/error，
    用于定位学校侧升级后的漂移层。

    兼容字段语义不变（checked_at/ok/latency_ms/stages/semester/error/username）；
    additive 新增：stages 条目的 ok/layer（与失败时的 error）、layers
    （webvpn/cas/portal/jwxt 三态 ok：True/False/None=未测）、contract_version。
    """
    t0 = time.time()
    stages = [{"stage": "start", "at_ms": 0, "ok": True}]
    result = {"checked_at": int(time.time()), "ok": False, "latency_ms": 0,
              "stages": stages, "semester": "", "error": ""}
    # 分层探测先行：登录失败时 webvpn/cas/portal 三层仍能独立给出结论
    try:
        probe = probe_layers(timeout=8)
    except Exception as e:
        probe = {"layers": {}, "checks": [],
                 "error": "%s: %s" % (type(e).__name__, e)}
    layers = {}
    for name in ("webvpn", "cas", "portal"):
        info = dict((probe.get("layers") or {}).get(name) or {})
        layers[name] = {"ok": info.get("ok"), "latency_ms": info.get("latency_ms")}
        if info.get("error"):
            layers[name]["error"] = str(info["error"])[:200]

    def step(fn, stage, layer):
        out = None
        try:
            out = fn()
        except Exception as e:
            stages.append({"stage": stage, "at_ms": int((time.time() - t0) * 1000),
                           "ok": False, "layer": layer,
                           "error": "%s: %s" % (type(e).__name__, e)})
            raise
        stages.append({"stage": stage, "at_ms": int((time.time() - t0) * 1000),
                       "ok": True, "layer": layer})
        return out

    j = None
    failed_layer = None
    try:
        j = J(username, password)
        # login 覆盖 WebVPN/CAS/门户三段握手：单标签归 portal，失败层由先行分层探测区分
        step(j.login, "login", LAYER_PORTAL)
        step(j.jwxt, "jwxt", LAYER_MGMT)
        step(j.student, "sso_student", LAYER_MGMT)
        sem = step(j.current, "api_current_semester", LAYER_STUDENT)
        result.update({"ok": True, "semester": sem,
                       "latency_ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        failed = next((s for s in reversed(stages)
                       if s.get("ok") is False and s.get("layer")), None)
        failed_layer = failed.get("layer") if failed else None
        stages.append({"stage": "failed", "at_ms": int((time.time() - t0) * 1000),
                       "ok": False})
        result.update({"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                       "error": "%s: %s" % (type(e).__name__, e)})
    finally:
        if j is not None:
            j.close()
    # 教务层结论：全链路成功=True；失败落在教务阶段=False；登录即败（教务未测）=None
    layers["jwxt"] = {
        "ok": (True if result["ok"]
               else False if failed_layer in (LAYER_MGMT, LAYER_STUDENT)
               else None),
    }
    result["layers"] = layers
    result["contract_version"] = SCHOOL_CONTRACT_VERSION
    result["stages"] = stages
    result["username"] = username
    if state is not None:
        with state.lock:
            state.deep = dict(result)
    return result


def _health_probe_loop(state, interval, stop_event=None, redis_backend=None,
                       layer_interval=0):
    """后台周期探测学校可达性并刷新 state.net，/health 只读缓存立即返回。

    stop_event 用于 FastAPI lifespan 优雅停机：置位后线程在下一轮退出。
    Redis 模式下同时探测 Redis 连通性，避免 /health 在 Redis 故障时误报 ok。
    layer_interval > 0 时按该间隔（秒）额外执行无凭据分层探测并写入
    state.school_layers（默认 0 = 关闭，保护学校侧；/health 的 school.layers
    仅在此开启或 health/deep 实测后才有值）。
    """
    last_layers = 0.0
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        net = probe_school()
        net["at"] = int(time.time())
        if layer_interval and layer_interval > 0 \
                and time.time() - last_layers >= max(60, int(layer_interval)):
            last_layers = time.time()
            try:
                probe = probe_layers(timeout=8)
            except Exception as e:
                probe = {"layers": {}, "error": "%s: %s" % (type(e).__name__, e)}
            with state.lock:
                state.school_layers = {"at": int(time.time()),
                                       "layers": dict(probe.get("layers") or {})}
        if redis_backend is not None:
            redis_ok = redis_backend.probe()
            with state.lock:
                state.redis = redis_backend.status_info()
        with state.lock:
            state.net = net
        if stop_event is not None and stop_event.wait(max(2, int(interval))):
            return


def health_payload(state, server, now=None):
    """组装 /health 响应体（主端口与独立健康端口共用同一实现）。

    学校可达性由后台探测线程持续刷新；健康检查只读缓存、绝不阻塞，
    缓存缺失时按“服务正常、学校状态未知”返回，待后台线程首轮填充。
    """
    now = now or time.time()
    with state.lock:
        cached = state.net
    if cached is None:
        # 冷缓存：健康检查路径上不做同步网络探测。学校可达性由后台探测线程首轮填充，
        # 避免学校网络/DNS 变慢时 /health 阻塞，进而让编排层 /health/ready 误报 not-ready。
        net = {"ok": None, "busy": True, "latency_ms": 0, "starting": True}
    else:
        net = dict(cached)
    net.pop("at", None)
    with state.lock:
        deep = dict(state.deep) if state.deep else None
        last = dict(state.last_real) if state.last_real else None
        cached_redis = dict(state.redis) if getattr(state, "redis", None) else None
        layer_cache = dict(state.school_layers) \
            if getattr(state, "school_layers", None) else None
    if deep is not None:
        deep.pop("username", None)
    backend = getattr(server, "_redis_backend", None)
    if backend is not None:
        if cached_redis is None:
            # 冷缓存：健康路径不做同步探测（backend.probe() 可能阻塞一个
            # socket_timeout），先返回“未知”占位；首测交由后台探测线程
            # （_health_probe_loop）填充 state.redis，下一轮起读缓存。
            redis_info = {"ok": None, "mode": "redis", "starting": True}
        else:
            redis_info = dict(cached_redis)
            redis_info.pop("at", None)
    else:
        redis_info = {"ok": None, "mode": "memory"}
    if net.get("ok") is True:
        status = "ok"
    elif net.get("busy"):
        # 探测因槽位繁忙被跳过：服务自身正常，学校状态未知，不拉低健康
        status = "ok" if not (deep and deep.get("ok") is False) else "degraded"
    else:
        status = "degraded"
    if redis_info.get("ok") is False:
        status = "degraded"
    # 分层结论（additive）：webvpn/cas/portal 来自无凭据分层探测缓存，
    # jwxt 来自最近一次 deep_check；无任何来源时省略 layers 字段。
    # 只暴露三态 ok 与错误摘要，不包含内部地址/凭据。
    school = net
    layers = {}
    if layer_cache:
        for name, info in (layer_cache.get("layers") or {}).items():
            ok = info.get("ok")
            if ok is None:
                continue
            item = {"ok": bool(ok)}
            if info.get("error"):
                item["error"] = str(info["error"])[:160]
            layers[name] = item
    if deep is not None:
        jw = ((deep.get("layers") or {}).get("jwxt") or {})
        if jw.get("ok") is not None:
            layers["jwxt"] = {"ok": bool(jw["ok"])}
    if layers:
        school = dict(net)
        school["layers"] = layers
    return {
        "status": status,
        "service": "jwxt-service",
        "version": VERSION,
        "contract_version": SCHOOL_CONTRACT_VERSION,
        "time": int(now),
        "uptime_s": int(now - state.started),
        "school": school,
        "redis": redis_info,
        "deep": deep,
        "last_real": last,
        "periodic_deep": bool(getattr(server, "health_credentials", None)),
    }


def _metrics_text(state):
    """导出 Prometheus 文本格式的进程指标（无第三方依赖）。"""
    lines = [
        "# HELP jwxt_http_requests_total HTTP 请求总数（按端点）",
        "# TYPE jwxt_http_requests_total counter",
        "# HELP jwxt_http_requests_ok HTTP 成功请求数（2xx/3xx）",
        "# TYPE jwxt_http_requests_ok counter",
        "# HELP jwxt_http_requests_err HTTP 失败请求数",
        "# TYPE jwxt_http_requests_err counter",
        "# HELP jwxt_http_request_duration_seconds_total HTTP 请求耗时累计（秒）",
        "# TYPE jwxt_http_request_duration_seconds_total counter",
        "# HELP jwxt_http_request_duration_seconds HTTP 请求耗时直方图（秒）",
        "# TYPE jwxt_http_request_duration_seconds histogram",
        "# HELP jwxt_http_cache_hits_total 缓存命中请求数",
        "# TYPE jwxt_http_cache_hits_total counter",
        "# HELP jwxt_http_last_status 最近一次 HTTP 状态码（按端点）",
        "# TYPE jwxt_http_last_status gauge",
        "# HELP jwxt_http_last_duration_seconds 最近一次 HTTP 耗时（秒）",
        "# TYPE jwxt_http_last_duration_seconds gauge",
        "# HELP jwxt_upstream_calls_total 学校上游调用总数",
        "# TYPE jwxt_upstream_calls_total counter",
        "# HELP jwxt_upstream_errors_total 学校上游调用失败数",
        "# TYPE jwxt_upstream_errors_total counter",
        "# HELP jwxt_upstream_retries_total 学校上游 GET 重试数",
        "# TYPE jwxt_upstream_retries_total counter",
    ]
    with state.metrics_lock:
        metrics = dict(state.metrics)
        last_reals = dict(state.last_reals)
    with _METRICS_LOCK:
        upstream = dict(_METRICS)
    for endpoint, m in sorted(metrics.items()):
        labels = 'endpoint="%s"' % endpoint.replace("\\", "\\\\").replace('"', '\\"')
        lines.append("jwxt_http_requests_total{%s} %d" % (labels, m["total"]))
        lines.append("jwxt_http_requests_ok{%s} %d" % (labels, m["ok"]))
        lines.append("jwxt_http_requests_err{%s} %d" % (labels, m["err"]))
        lines.append("jwxt_http_request_duration_seconds_total{%s} %.3f"
                     % (labels, m["latency_sum"] / 1000.0))
        buckets = m.get("latency_buckets") or []
        for i, b in enumerate(LATENCY_BUCKETS_MS):
            lines.append('jwxt_http_request_duration_seconds_bucket{%s,le="%.3f"} %d'
                         % (labels, b / 1000.0,
                            buckets[i] if i < len(buckets) else 0))
        lines.append('jwxt_http_request_duration_seconds_bucket{%s,le="+Inf"} %d'
                     % (labels, m["total"]))
        lines.append("jwxt_http_request_duration_seconds_sum{%s} %.3f"
                     % (labels, m["latency_sum"] / 1000.0))
        lines.append("jwxt_http_request_duration_seconds_count{%s} %d"
                     % (labels, m["total"]))
        lines.append("jwxt_http_cache_hits_total{%s} %d" % (labels, m["cache_hit"]))
        last = last_reals.get(endpoint)
        if last:
            lines.append("jwxt_http_last_status{%s} %d" % (labels, last["status"]))
            lines.append("jwxt_http_last_duration_seconds{%s} %.3f"
                         % (labels, last["duration_ms"] / 1000.0))
    lines.append("jwxt_upstream_calls_total %d" % upstream["upstream_calls"])
    lines.append("jwxt_upstream_errors_total %d" % upstream["upstream_errors"])
    lines.append("jwxt_upstream_retries_total %d" % upstream["upstream_retries"])
    return "\n".join(lines) + "\n"
