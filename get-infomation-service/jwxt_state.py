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

from jwxt_core import (
    J, BASE, UA, TIMEOUT, VERIFY_TLS, LOG, UPSTREAM_SEM, JUMP_PAGES,
    LOGIN_REUSE, WARM_WAIT, VERSION, MAX_SESSIONS, JUMP_CODE_TTL, SessionInvalidError,
    TokenError, UpstreamBusyError, LockTimeoutError, LATENCY_BUCKETS_MS, to_webvpn,
    dump_session_cookies, dump_portal, load_cookies,
    set_trace_id, _shared_adapter, _upstream_slot, _sanitize_url, _has_session_hint,
    _is_auth_failure, _METRICS, _METRICS_LOCK,
)

class ServerState:
    def __init__(self):
        self.started = time.time()
        self.lock = threading.Lock()
        self.net = None            # 学校可达性探测缓存 {at, ok, latency_ms, ...}
        self.redis = None          # Redis 连通性探测缓存 {at, ok}（Redis 模式）
        self.deep = None           # 最近一次真实登录+接口探测结果
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
                "exp": now + self.ttl,
                "pwd_hash": pwd_hash,
            }
            if warm_evt is not None:
                rec["warm_evt"] = warm_evt
            self.sessions[sid] = rec
            self.by_user.setdefault(username, set()).add(sid)
            if len(self.sessions) > self.max_sessions:
                oldest = min(self.sessions, key=lambda k: self.sessions[k]["at"])
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
        """滑动续期：成功使用后把过期时间顺延为 now+ttl。

        与学校会话生命周期对齐：活跃会话不会被服务端 TTL 提前掐断，
        闲置会话按 SESSION_TTL 自然过期，过期后由调用方重新 /login。
        """
        with self.lock:
            s = self.sessions.get(sid)
            if s is None:
                return False
            s["exp"] = time.time() + self.ttl
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

    用于缓存成绩查询结果：成绩数据在一天内基本不变，短 TTL 可显著减少
    对学校上游的重复查询压力。ttl<=0 表示关闭缓存。
    """

    def __init__(self, ttl=0, max_items=2000):
        self.ttl = max(0, int(ttl))
        self.max_items = max(1, int(max_items))
        self.lock = threading.Lock()
        self.data = {}

    def get(self, key):
        if not self.ttl or not key:
            return None
        now = time.time()
        with self.lock:
            hit = self.data.get(key)
            if hit is None:
                return None
            if hit[0] <= now:
                self.data.pop(key, None)
                return None
            return hit[1]

    def get_payload(self, key):
        """取缓存项对应的预序列化响应体（可能为 None，表示未预序列化）。"""
        if not self.ttl or not key:
            return None
        now = time.time()
        with self.lock:
            hit = self.data.get(key)
            if hit is None or hit[0] <= now:
                return None
            return hit[2]

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
                # 先清过期项；仍超限则按插入序淘汰最旧的一条
                self.data = {k: v for k, v in self.data.items() if v[0] > now}
                if len(self.data) >= self.max_items:
                    self.data.pop(next(iter(self.data)), None)
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
        if not stu.startswith("https://sec.stpt.edu.cn/webvpn/"):
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
    LOG.info("会话预热未在 %ss 内完成，按需同步补齐 session=%s***",
             WARM_WAIT, str(sid)[:6])
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
    """/login 复用前轻量探测：用会话 Cookie 调 getHomeParam，确认学校侧会话仍有效。

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
        LOG.warning("后台预热学生门户失败 session=%s*** error=%s", str(sid)[:6], e)
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
    """从 WebVPN 虚拟 Cookie 中取回 CASTGC（TGT）：会话里没存 TGT 时的兜底。"""
    r = j.req("GET", BASE + "/webvpn/cookie/?domain=%s&path=/" % quote("cas.stpt.edu.cn"),
              timeout=TIMEOUT)
    for part in r.text.split(";"):
        name, _, val = part.strip().partition("=")
        if name == "CASTGC" and val:
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
    url = j.cas + "/lyuapServer/v1/tickets/" + quote(tgt, safe="") + "?vpn-12-cas.stpt.edu.cn"
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


def deep_check(username, password, state=None, timeout=40):
    t0 = time.time()
    stages = [{"stage": "start", "at_ms": 0}]
    def rec(name):
        stages.append({"stage": name, "at_ms": int((time.time() - t0) * 1000)})
    result = {"checked_at": int(time.time()), "ok": False, "latency_ms": 0,
              "stages": stages, "semester": "", "error": ""}
    j = None
    try:
        j = J(username, password)
        j.login(); rec("login")
        j.jwxt(); rec("jwxt")
        j.student(); rec("sso_student")
        sem = j.current(); rec("api_current_semester")
        result.update({"ok": True, "semester": sem,
                       "latency_ms": int((time.time() - t0) * 1000)})
    except Exception as e:
        rec("failed")
        result.update({"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                       "error": "%s: %s" % (type(e).__name__, e)})
    finally:
        if j is not None:
            j.close()
    result["stages"] = stages
    result["username"] = username
    if state is not None:
        with state.lock:
            state.deep = dict(result)
    return result


def _health_probe_loop(state, interval, stop_event=None, redis_backend=None):
    """后台周期探测学校可达性并刷新 state.net，/health 只读缓存立即返回。

    stop_event 用于 FastAPI lifespan 优雅停机：置位后线程在下一轮退出。
    Redis 模式下同时探测 Redis 连通性，避免 /health 在 Redis 故障时误报 ok。
    """
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        net = probe_school()
        net["at"] = int(time.time())
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

    学校可达性由后台探测线程持续刷新；这里仅首次（缓存缺失）做一次同步探测，
    之后直接返回缓存，健康检查不再因上游变慢而阻塞。
    """
    now = now or time.time()
    with state.lock:
        cached = state.net
    if cached is None:
        net = probe_school()
        net["at"] = int(now)
        with state.lock:
            state.net = net
    else:
        net = dict(cached)
    net.pop("at", None)
    with state.lock:
        deep = dict(state.deep) if state.deep else None
        last = dict(state.last_real) if state.last_real else None
        cached_redis = dict(state.redis) if getattr(state, "redis", None) else None
    if deep is not None:
        deep.pop("username", None)
    backend = getattr(server, "_redis_backend", None)
    if backend is not None:
        if cached_redis is None:
            redis_ok = backend.probe()
            cached_redis = backend.status_info(now)
            with state.lock:
                state.redis = cached_redis
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
    return {
        "status": status,
        "service": "jwxt-service",
        "version": VERSION,
        "time": int(now),
        "uptime_s": int(now - state.started),
        "school": net,
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
