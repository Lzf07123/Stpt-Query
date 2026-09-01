#!/usr/bin/env python3
"""核心业务模块：常量、专用异常、上游客户端、查询/渲染逻辑与配置。"""

import base64
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlencode, urlparse

import requests
import urllib3

#!/usr/bin/env python3
"""教务信息查询 Web 服务（FastAPI 多模块实现）。

原 http.server 实现已重构为 FastAPI/uvicorn 单文件服务：业务逻辑、请求适配、
配置解析均在本文档内；路由/鉴权/限流/会话/缓存/响应语义保持不变。
"""
import argparse
import base64
from dataclasses import dataclass
import gzip
import hashlib
import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from logging.handlers import RotatingFileHandler
from urllib.parse import quote, unquote, urlencode, urlparse

import requests
import urllib3
import uvicorn
from fastapi import Depends, FastAPI, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException
from requests.adapters import HTTPAdapter
from typing import List, Optional, Union

BASE = "https://sec.stpt.edu.cn"
SERVICE = BASE + "/rump_frontend/loginFromCas/"
RUMP_HOST = "sec.stpt.edu.cn:443"
# 教务系统管理端 SSO 的 service（与 sso() 中从跳转 Location 提取的结果一致；
# 学校侧变更时可重新从 api/cas/login 跳转的 service 参数提取）
MGMT_SSO_SERVICE = "http://10.1.200.4:9090/api/cas/login?pattern=manager-login"
VERSION = "2026-08-07.02"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
TIMEOUT = 15
MAX_BODY = 1024 * 1024  # 请求体上限 1MB，防止内存滥用
# 客户端连接空闲超时（秒）：HTTP/1.1 keep-alive 的空闲连接会一直占用工作线程，
# 缩短后可在不牺牲请求处理的前提下，降低“空闲连接占满线程池”的风险
IDLE_TIMEOUT = 10
# 服务端并发请求上限（0 = 不限制；仅由 uvicorn 启动入口生效）
LIMIT_CONCURRENCY = 0
# 获取上游全局并发槽位的等待超时（秒）：超时抛 UpstreamBusyError，
# 避免请求线程无限排队在信号量上拖垮线程池
UPSTREAM_SEM_TIMEOUT = 5.0
# 显式 semesters 逗号列表的最大学期数（防 DoS：一次请求不能创建海量上游任务）
MAX_SEMESTERS = 20
# 单学期成绩/全部成绩的最大翻页数；超过后返回 truncated=True，不再静默截断
MAX_FINALS_PAGES = 10
MAX_UNI_PAGES = 10
# 同账号登录互斥锁等待超时（秒）：避免持锁方异常时同账号请求无限排队
LOGIN_LOCK_TIMEOUT = 20.0
AJAX = {"X-Requested-With": "XMLHttpRequest", "Accept": "application/json, text/plain, */*"}
# 已知端点路径：兜底路由命中这些路径但方法不支持时返回 405（而不是 404）
KNOWN_PATHS = {
    "/", "/health", "/health/deep", "/login", "/login/status",
    "/get_grades", "/get_schedule", "/get_schedule/export",
    "/jump", "/jump/go", "/metrics",
}
DNS_FALLBACK_IPS = ["154.214.217.209", "221.5.10.101"]
UPSTREAM_PARALLEL = 4  # 查询时对学校上游的最大并发请求数（--upstream-parallel / JWXT_UPSTREAM_PARALLEL 可调）
# 上游并发上限（内存模式进程级；Redis 模式下由 jwxt_redis.RedisSemaphore 替换为
# 多副本全局共享）：所有请求共用同一信号量，避免业务线程全忙时对学校上游
# 同时建立过多连接（--upstream-global / JWXT_UPSTREAM_GLOBAL 可调）
UPSTREAM_GLOBAL = 8
UPSTREAM_SEM = threading.BoundedSemaphore(8)

# 上游 TLS 证书校验：学校 *.stpt.edu.cn 证书链可被系统信任，默认开启；
# 如学校侧临时出现证书异常，可用 JWXT_VERIFY_TLS=0 关闭（不推荐）
VERIFY_TLS = True

# 周次参数安全边界：超出范围自动收敛，防止 "1-9999999" 之类参数展开成超大列表
WEEK_MIN, WEEK_MAX = 1, 30
MAX_WEEK_SEGMENTS = 40   # weeks 参数最多解析的段数
MAX_WEEKS_TOTAL = 60     # weeks 参数最多保留的周数

# /login 同账号会话复用：内存中已存在同 用户名+密码摘要 的有效会话时直接复用
LOGIN_REUSE = True
LOGIN_REUSE_MAX_AGE = 1800   # 只复用最近 N 秒内签发的会话（降低学校侧会话已过期的概率）
# 复用前是否用 getHomeParam 轻量探测学校侧会话仍有效（默认开启）：
# 失效会话会被清除并自动重新登录，避免“缓存会话过期但 /login 仍复用”导致查询失败
LOGIN_REUSE_PROBE = True
MAX_SESSIONS = 5000          # 内存会话数量上限，超出淘汰最旧

# 对外公开地址（JWXT_PUBLIC_URL），用于生成 https 下载/跳转链接；
# 为空时使用请求 Host + X-Forwarded-Proto（经过合法性校验）
PUBLIC_URL = ""
# 允许的跨域来源（JWXT_CORS_ORIGIN）；空 = 不返回 CORS 头（默认后端调用，无需跨域）
CORS_ORIGIN = ""
CORS_ORIGINS = []
# 是否信任 X-Forwarded-For 作为客户端 IP（JWXT_TRUST_PROXY；仅在明确反代场景开启）
TRUST_PROXY = False
# GET 查询串携带 username/password 是否允许（JWXT_ALLOW_GET_CREDENTIALS；默认兼容旧行为）
ALLOW_GET_CREDENTIALS = True
# 配置了 token 时 /login/status 是否也要求 Bearer（JWXT_PROTECT_LOGIN_STATUS；默认不要求）
PROTECT_LOGIN_STATUS = False
# TRUST_PROXY 时的可信代理网段（JWXT_TRUSTED_PROXY_CIDRS；空 = 保持信任所有 XFF）
TRUSTED_PROXY_CIDRS = []

# /jump 直达页映射：别名 -> 学生门户地址（stu）后追加的路径/锚点。
# "home" 为学生端首页；其他页面按学校教务系统实际路由补充即可，例如
# {"grades": "#/score/query"}（哈希路由）或 {"grades": "/jwglxt/xx.html"}。
# 未注册的 page 值按教务系统内相对路径/锚点原样拼接，覆盖任意页面。
JUMP_PAGES = {
    "home": "",
}

# info 字段白名单：默认只返回展示/渲染所需字段；
# zjh（身份证号）、zxzpbase（证件照 base64）属敏感字段，需显式 include_sensitive_info=true 才返回
INFO_FIELDS = ("xh", "xm", "xbmc", "nj", "xymc", "zyfxmc", "bjmc")
SENSITIVE_INFO_FIELDS = ("zjh", "zxzpbase")

# 成绩查询失败结果的短缓存秒数：学校侧偶发错误时避免调用方立刻重打上游
NEG_CACHE_TTL = 30

# 课表缓存按班级共享：学号前 8 位视为班级标识，同班同学（前 8 位相同）
# 复用同一份课表缓存。默认关闭：同班同学可能因选修课不同导致课表不一致，
# 共享缓存会把别人的课表返回给本班其他同学（串数据风险）。
# 仅当确认学校同班课表完全一致时，才设 JWXT_SCHEDULE_CLASS_SHARE=1 开启。
SCHEDULE_CLASS_SHARE = False

# /jump 跳转码有效期（秒）：/jump 签发短时随机跳转码，/jump/go 只认跳转码，
# 不再把长期有效的 session 直接放进 URL（--jump-code-ttl / JWXT_JUMP_CODE_TTL 可调）
JUMP_CODE_TTL = 60

# /health 后台探测间隔（秒）：探测线程持续刷新学校可达性缓存，
# 健康请求只读缓存，不再同步阻塞探测（--health-probe-interval 可调）
HEALTH_PROBE_INTERVAL = 10

# 学校端“会话已失效”的信号：明确的状态码 + 常见提示文案。
# 命中后视为认证类失败（不进入负缓存、按 401 返回并清除内存会话），
# 避免把“强制下线/未登录”误当成瞬时上游故障反复重试。
SESSION_INVALID_CODES = {
    "53000010", "53000011", "53000012",
    "53010010", "53010011", "53010012",
}
SESSION_INVALID_HINTS = (
    "未登录", "未登陆", "已在其他地方登录", "强制下线",
    "会话已失效", "登录已过期", "请重新登录",
)


class SchoolError(RuntimeError):
    """学校侧业务/数据错误：登录失败、业务 code、响应结构异常等。"""


class SessionInvalidError(SchoolError):
    """学校端会话失效（未登录 / 已在其他地方登录 / 强制下线）。"""


class UpstreamError(RuntimeError):
    """上游网络/服务瞬时故障：按瞬时失败处理（可负缓存、保留会话）。"""


class UpstreamBusyError(UpstreamError):
    """上游全局并发槽位等待超时：按瞬时故障处理（可负缓存、保留会话）。"""


class TokenError(Exception):
    """会话相关错误（保留旧类名以减少改动面）。"""


class WarmPendingError(Exception):
    """首次查询时预热门户尚未成功；会话记录仍有效，可短退避后重试。"""


SESSION_TTL = 12 * 3600      # 内存会话有效期（秒），make_app() 启动时按配置覆盖


class LockTimeoutError(Exception):
    """同账号登录互斥锁等待超时：请求线程不应无限排队。"""


class PoolFullError(Exception):
    """后台任务队列已满：拒绝新任务，调用方决定降级策略。"""


def _cache_put(cache, lock, key, value, max_items, ttl):
    """带大小上限的 TTL 缓存写入：先清过期项，仍超限则淘汰最旧一条。
    用于上游学校级配置缓存（键很少），上限只为防御异常输入导致无限增长。"""
    now = time.time()
    with lock:
        if len(cache) >= max_items:
            dead = [k for k, v in cache.items() if now - v[0] >= ttl]
            for k in dead:
                cache.pop(k, None)
        if len(cache) >= max_items:
            oldest = min(cache, key=lambda k: cache[k][0])
            cache.pop(oldest, None)
        cache[key] = value


# CAS 登录页 RSA 公钥缓存：缓存命中时仍拉取登录页（保证会话 Cookie），仅跳过 JS 下载
_RSA_KEY_CACHE = {}
_RSA_KEY_CACHE_TTL = 600
_RSA_KEY_CACHE_MAX = 32
_RSA_KEY_CACHE_LOCK = threading.Lock()

# 教务系统管理端地址缓存（学校级配置，30 分钟 TTL；带查询参数的 URL 疑似含
# 会话信息不缓存；student() 失败时会主动失效并重新探测，保证配置变更后仍可用）
_MGMT_CACHE = {}
_MGMT_CACHE_TTL = 1800
_MGMT_CACHE_MAX = 32
_MGMT_CACHE_LOCK = threading.Lock()

# CAS SSO service 参数缓存（按 管理端前缀+pattern，1 小时 TTL；失败主动失效）
_SSO_SERVICE_CACHE = {}
_SSO_SERVICE_CACHE_TTL = 3600
_SSO_SERVICE_CACHE_MAX = 64
_SSO_SERVICE_CACHE_LOCK = threading.Lock()

# 查询等待“后台预热学生门户”完成的上限（秒）：预热通常约 1.5s，
# 超时后按需同步补齐，保证 /login 快速返回后首次查询仍可用
WARM_WAIT = 10

LOG = logging.getLogger("jwxt-service")
_trace_local = threading.local()


def trace_id():
    """当前线程的请求追溯 ID；无请求上下文（如后台任务）时为 '-'。"""
    return getattr(_trace_local, "id", "-")


def set_trace_id(tid):
    _trace_local.id = tid


class _TraceFilter(logging.Filter):
    def filter(self, record):
        record.trace_id = trace_id()
        return True


def setup_logging(level=logging.INFO, log_file=""):
    """初始化服务日志：控制台必出；可选文件日志（10MB 轮转，保留 5 份）。"""
    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-7s [%(trace_id)s] [%(threadName)s] %(message)s")
    for h in LOG.handlers[:]:
        LOG.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass
    LOG.setLevel(level)
    LOG.propagate = False
    if not LOG.filters:
        LOG.addFilter(_TraceFilter())
    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    LOG.addHandler(ch)
    if log_file:
        try:
            fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024,
                                     backupCount=5, encoding="utf-8")
        except OSError as e:
            # 日志目录/卷不可写时降级为仅控制台，不能阻断服务启动
            LOG.warning("无法打开日志文件 %s，已降级为仅控制台日志: %s", log_file, e)
        else:
            fh.setLevel(level)
            fh.setFormatter(fmt)
            LOG.addHandler(fh)


SENSITIVE_PARAM = ("password", "ticket", "token", "st", "session")


def _sanitize_url(url):
    """掩码 URL 查询参数中的票据/密码，避免泄露进日志。"""
    base, _, qs = url.partition("?")
    if not qs:
        return url
    out = []
    for seg in qs.split("&"):
        k, _, v = seg.partition("=")
        out.append("%s=***" % k if k.lower() in SENSITIVE_PARAM else seg)
    return base + "?" + "&".join(out)


def _mask_body(raw):
    """请求体脱敏：password 全掩码，session/token 只保留前 6 位。"""
    try:
        obj = json.loads(raw)
    except Exception:
        return raw
    if not isinstance(obj, dict):
        return raw
    out = {}
    for k, v in obj.items():
        kk = str(k).lower()
        if kk in ("password", "passwd"):
            out[k] = "***"
        elif kk in ("session", "seesion", "token", "authorization"):
            out[k] = (str(v)[:6] + "***") if v not in (None, "") else v
        else:
            out[k] = v
    return json.dumps(out, ensure_ascii=False)

try:
    import socket

    _ORIG_GAI = socket.getaddrinfo

    def _gai(host, port, *a, **k):
        try:
            return _ORIG_GAI(host, port, *a, **k)
        except socket.gaierror:
            if host != "sec.stpt.edu.cn" or not DNS_FALLBACK_IPS:
                raise
            fam = k.get("family") or (a[0] if a else socket.AF_UNSPEC)
            out = []
            for ip in DNS_FALLBACK_IPS:
                try:
                    for f, t, p, c, sa in _ORIG_GAI(ip, port):
                        if fam in (socket.AF_UNSPEC, 0, f):
                            out.append((f, t, p, "", (sa[0], sa[1])))
                except socket.gaierror:
                    pass
            if out:
                return out
            raise

    socket.getaddrinfo = _gai
except Exception:
    pass


_SHARED_ADAPTER = None
_SHARED_ADAPTER_LOCK = threading.Lock()
_EXECUTOR_LOCK = threading.Lock()
_UPSTREAM_EXECUTOR = None
_BACKGROUND_EXECUTOR = None


class _SharedPoolAdapter(HTTPAdapter):
    """进程级共享连接池适配器。

    requests.Session.close() 会关闭自身挂载的 adapter；共享池由进程持有，
    close() 置为 no-op，避免单个会话/克隆会话关闭时清空全局连接池。
    """

    def close(self):
        pass


def _shared_adapter():
    """懒创建全局共享连接池（线程安全）。"""
    global _SHARED_ADAPTER
    if _SHARED_ADAPTER is None:
        with _SHARED_ADAPTER_LOCK:
            if _SHARED_ADAPTER is None:
                _SHARED_ADAPTER = _SharedPoolAdapter(
                    pool_connections=64, pool_maxsize=16)
    return _SHARED_ADAPTER


@contextmanager
def _upstream_slot():
    """上游并发槽位（内存/Redis 模式共用同一接口）：
    获取超时抛 UpstreamBusyError，不无限排队。"""
    if not UPSTREAM_SEM.acquire(timeout=UPSTREAM_SEM_TIMEOUT):
        raise UpstreamBusyError(
            "上游并发已达上限（等待 %.1fs 超时），请稍后重试" % UPSTREAM_SEM_TIMEOUT)
    try:
        yield
    finally:
        UPSTREAM_SEM.release()


def _upstream_executor():
    """进程级共享上游执行器：避免每个请求批次新建 ThreadPoolExecutor。"""
    global _UPSTREAM_EXECUTOR
    if _UPSTREAM_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _UPSTREAM_EXECUTOR is None:
                _UPSTREAM_EXECUTOR = _DaemonPool(
                    # 大小至少覆盖全局并发上限，避免线程数成为比信号量更早的瓶颈；
                    # daemon 线程保证进程退出不被在飞上游请求拖住
                    max_workers=max(8, UPSTREAM_PARALLEL * 2, UPSTREAM_GLOBAL),
                    thread_name_prefix="jwxt-upstream")
    return _UPSTREAM_EXECUTOR


class _DaemonPool:
    """daemon 线程的固定大小任务池：进程退出不等待正在执行的后台任务。"""

    def __init__(self, max_workers=8, maxsize=0, thread_name_prefix="jwxt-bg"):
        self._q = queue.Queue(maxsize=maxsize)
        self._threads = []
        for i in range(max(1, int(max_workers))):
            t = threading.Thread(
                target=self._worker, daemon=True,
                name="%s-%d" % (thread_name_prefix, i))
            t.start()
            self._threads.append(t)

    def _worker(self):
        while True:
            item = self._q.get()
            if item is None:
                return
            fut, fn, args, kwargs = item
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args, **kwargs))
            except BaseException as exc:
                fut.set_exception(exc)
            finally:
                self._q.task_done()

    def submit(self, fn, *args, **kwargs):
        if self._q.maxsize and self._q.full():
            raise PoolFullError("后台任务队列已满，请稍后重试")
        fut = Future()
        try:
            self._q.put_nowait((fut, fn, args, kwargs))
        except queue.Full:
            raise PoolFullError("后台任务队列已满，请稍后重试")
        return fut

    def shutdown(self, wait=False, cancel_futures=False):
        """停止 worker；cancel_futures=True 时取消仍在队列中的任务，避免 Future 永久挂起。"""
        if cancel_futures:
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    item[0].cancel()
                self._q.task_done()
        for _ in self._threads:
            self._q.put(None)
        if wait:
            for t in self._threads:
                t.join()


def _background_executor():
    """进程级共享后台执行器：登录预热、深度探测等不再每请求新建线程。"""
    global _BACKGROUND_EXECUTOR
    if _BACKGROUND_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _BACKGROUND_EXECUTOR is None:
                _BACKGROUND_EXECUTOR = _DaemonPool(
                    max_workers=8, maxsize=64, thread_name_prefix="jwxt-bg")
    return _BACKGROUND_EXECUTOR


_DEEP_EXECUTOR = None


def _deep_executor():
    """深度健康探测专用单线程池：避免与预热任务抢线程，排队时间可忽略。"""
    global _DEEP_EXECUTOR
    if _DEEP_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _DEEP_EXECUTOR is None:
                _DEEP_EXECUTOR = _DaemonPool(
                    max_workers=1, thread_name_prefix="jwxt-deep")
    return _DEEP_EXECUTOR


# 进程级上游调用指标（单实例；/metrics 导出）
_METRICS_LOCK = threading.Lock()
_METRICS = {
    "upstream_calls": 0,
    "upstream_errors": 0,
    "upstream_retries": 0,
    "upstream_latency_sum": 0.0,
}


def _record_upstream(ms, retried=False):
    with _METRICS_LOCK:
        _METRICS["upstream_calls"] += 1
        _METRICS["upstream_latency_sum"] += ms
        if retried:
            _METRICS["upstream_retries"] += 1


def _record_upstream_error(ms):
    with _METRICS_LOCK:
        _METRICS["upstream_errors"] += 1
        _METRICS["upstream_latency_sum"] += ms


def _page_total(data, got):
    """从学校分页响应读取 total；缺失/非法时回退为已取行数（不翻页）。"""
    raw = data.get("total")
    if raw in (None, ""):
        return got
    try:
        return max(int(raw), got)
    except Exception:
        return got


def rsa_encrypt(t, mod):
    b = [ord(c) for c in t]
    if len(b) % 2:
        b.append(0)
    m = sum((b[2 * j] + (b[2 * j + 1] << 8)) * 65536 ** j for j in range(len(b) // 2))
    h = format(pow(m, 0x10001, int(mod, 16)), "x")
    return h if len(h) % 2 == 0 else "0" + h


def ent(t):
    k = hashlib.md5(RUMP_HOST.encode()).hexdigest().encode()
    nums = [str((ord(c) + k[i % len(k)]) & 0xFF) for i, c in enumerate(t)]
    return base64.b64encode(("." + ".".join(nums)).encode()).decode().replace("+", "-").replace("/", "_")


def to_webvpn(url):
    scheme, rest = url.split("://", 1)
    host, _, path = rest.partition("/")
    frag = ""
    if "#" in path:
        path, frag = path.split("#", 1)
    query = ""
    if "?" in path:
        path, query = path.split("?", 1)
    u = "https://sec.stpt.edu.cn/webvpn/%s/%s/%s" % (ent(scheme), ent(host), path)
    if query:
        u += "?" + query
    if frag:
        u += "#" + frag
    return u


class J:
    def __init__(self, u, p):
        self.u, self.p = u, p
        self.s = requests.Session()
        self.s.verify = VERIFY_TLS
        self.s.headers["User-Agent"] = UA
        # 所有会话共享同一连接池：克隆会话之间可复用 TCP/TLS 连接，
        # 且单个会话关闭不会销毁共享池（见 _SharedPoolAdapter）
        ad = _shared_adapter()
        self.s.mount("https://", ad)
        self.s.mount("http://", ad)
        self.cas = self.mgmt = self.stu = None
        self.stu_frag = None      # 学生门户原始地址中的 hash 片段（如 #/jwxt/js）
        self._tgt = None  # CAS TGT（登录/换票时取得），用于免密签发 service ticket
        self.mgmt_key = None      # 管理端地址缓存键（student() 失败时用于失效）
        self.sso_key = None       # CAS service 缓存键（prefix, pattern）

    def close(self):
        """关闭底层 requests.Session（连接池），防止长生命周期下 fd 泄漏。"""
        try:
            self.s.close()
        except Exception:
            pass

    def req(self, method, url, **kw):
        t0 = time.time()
        safe = _sanitize_url(url)
        LOG.debug("上游请求 %s %s", method, safe)
        retried = False
        try:
            # 所有上游 HTTP 请求统一受进程级信号量约束，避免 /jump、探测等
            # 路径绕过全局并发上限；等待超时快速失败，避免线程池被排队请求占满
            with _upstream_slot():
                try:
                    r = self.s.request(method, url, **kw)
                except requests.exceptions.ProxyError:
                    LOG.warning("上游代理异常，改为直连重试 %s %s", method, safe)
                    self.s.trust_env = False
                    r = self.s.request(method, url, **kw)
                except requests.exceptions.RequestException:
                    # 幂等 GET 遇到瞬时网络失败时重试一次；写操作不重试，避免重复提交
                    if method.upper() == "GET" and not kw.get("stream"):
                        LOG.debug("上游 GET 瞬时失败，重试一次 %s %s", method, safe)
                        retried = True
                        r = self.s.request(method, url, **kw)
                    else:
                        raise
        except Exception:
            _record_upstream_error((time.time() - t0) * 1000)
            raise
        _record_upstream((time.time() - t0) * 1000, retried)
        LOG.debug("上游响应 %s %s status=%s duration_ms=%.0f",
                  method, safe, r.status_code, (time.time() - t0) * 1000)
        return r

    def _key(self, cas_url):
        # 登录页查询参数（service 等）不影响公钥：归一化缓存键，让
        # “带参数/不带参数”两种取数路径共享同一份公钥缓存，减少回退路径的 JS 下载
        cache_key = cas_url.split("?", 1)[0]
        with _RSA_KEY_CACHE_LOCK:
            hit = _RSA_KEY_CACHE.get(cache_key)
            cached = None
            if hit is not None and time.time() - hit[0] < _RSA_KEY_CACHE_TTL:
                cached = (hit[1], hit[2])
        html = self.req("GET", cas_url, timeout=TIMEOUT).text
        if cached:
            return cached[0], cached[1], True
        m = re.search(r'src="([^"]*app\.[^"]+\.js[^"]*)"', html)
        src = m.group(1)
        if src.startswith("/"):
            src = "https://sec.stpt.edu.cn" + src
        js = self.req("GET", src, timeout=TIMEOUT).text
        e = re.search(r'public_exponent:"([0-9a-f]+)"', js).group(1)
        mod = re.search(r'modulus:"([0-9a-f]+)"', js).group(1)
        _cache_put(_RSA_KEY_CACHE, _RSA_KEY_CACHE_LOCK, cache_key,
                   (time.time(), e, mod), _RSA_KEY_CACHE_MAX, _RSA_KEY_CACHE_TTL)
        return e, mod, False

    def _tickets(self, service, cas_url=None):
        key_url = cas_url or self.cas + "/lyuapServer/login"
        for attempt in (0, 1):
            e, mod, from_cache = self._key(key_url)
            try:
                r = self.req("POST", self.cas + "/lyuapServer/v1/tickets?vpn-12-cas.stpt.edu.cn",
                             headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                                      "X-Requested-With": "XMLHttpRequest",
                                      "Accept": "application/json, text/plain, */*",
                                      "loginUserToken": rsa_encrypt("lyasp" + str(int(time.time() * 1000)), mod)},
                             data={"username": self.u, "password": rsa_encrypt(self.p, mod),
                                   "service": service, "loginType": "", "id": "", "code": "", "otpcode": ""},
                             timeout=TIMEOUT).json()
                if not isinstance(r, dict) or "tgt" not in r or "ticket" not in r:
                    raise SchoolError("CAS 登录票据获取失败: %s" % json.dumps(r, ensure_ascii=False)[:200])
                return r["tgt"], r["ticket"]
            except Exception:
                # 仅当失败时用的是缓存的公钥，才清缓存重取一次（学校侧可能已轮换密钥）；
                # 新拉取的公钥失败（如密码错误）则保持旧行为，单次尝试直接报错
                if attempt == 0 and from_cache:
                    _invalidate_key(key_url)
                    continue
                raise

    def login(self, verify=True):
        self.req("GET", BASE + "/", allow_redirects=True, timeout=TIMEOUT)
        d = self.req("GET", BASE + "/rump_frontend/getLoginParam/", timeout=TIMEOUT).json()["data"]
        if d["auth"].get("master") != "cas":
            raise SchoolError("auth master: %s" % d["auth"])
        r = self.req("GET", BASE + "/rump_frontend/redirectToCas/", timeout=TIMEOUT)
        cas_url = re.search(r'href="(https?://[^"]+?lyuapServer/login[^"]*)"', r.text).group(1).replace("&amp;", "&")
        self.cas = cas_url.split("/lyuapServer/login")[0]
        tgt, ticket = self._tickets(SERVICE, cas_url)
        self._tgt = tgt
        # 三个相互独立的 CAS Cookie 写入并行执行（克隆会话避免共享 Session 线程安全问题）；
        # 完成后读回校验，缺失项串行补齐——即使学校侧是“覆盖”而非“合并”语义也不会丢 Cookie，
        # 最终状态与旧串行流程等价：串行 3 个往返 → 并行 1 + 校验 1 ≈ 省 1 个往返
        cas_cookies = (("CASTGC", tgt), ("session", "1"), ("locale", "zh_CN"))
        jobs = [("c%d" % i, lambda jj, n=n, v=v: jj.req(
            "POST", BASE + "/webvpn/cookie/",
            data={"domain": "cas.stpt.edu.cn", "name": n, "value": v, "path": "/", "expires": ""},
            timeout=TIMEOUT))
            for i, (n, v) in enumerate(cas_cookies)]
        try:
            cres = _run_upstream(self, jobs, max_workers=3)
            for name in ("c0", "c1", "c2"):
                _take(cres, name)
        except Exception:
            LOG.debug("并行写入 CAS Cookie 失败，回退串行")
            for n, v in cas_cookies:
                self.req("POST", BASE + "/webvpn/cookie/",
                         data={"domain": "cas.stpt.edu.cn", "name": n, "value": v,
                               "path": "/", "expires": ""}, timeout=TIMEOUT)
        try:
            rv = self.req("GET", BASE + "/webvpn/cookie/?domain=%s&path=/" % quote("cas.stpt.edu.cn"),
                          timeout=TIMEOUT)
            have = {seg.partition("=")[0].strip() for seg in rv.text.split(";") if "=" in seg}
            for n, v in cas_cookies:
                if n not in have:
                    self.req("POST", BASE + "/webvpn/cookie/",
                             data={"domain": "cas.stpt.edu.cn", "name": n, "value": v,
                                   "path": "/", "expires": ""}, timeout=TIMEOUT)
        except Exception:
            LOG.debug("CAS Cookie 读回校验失败，按未校验继续（loginFromCas 会兜底）")
        self.req("GET", BASE + "/rump_frontend/loginFromCas/?ticket=" + quote(ticket),
                 allow_redirects=True, timeout=TIMEOUT)
        if verify:
            self.verify()

    def verify(self):
        """校验当前会话确实是目标学号（登录成功的判据），可与其它独立请求并行。"""
        r = self.req("GET", BASE + "/rump_frontend/getHomeParam/", timeout=TIMEOUT)
        self._raise_if_session_invalid(r)
        home = r.json()
        if home.get("code") != 0 or home.get("data", {}).get("username") != self.u:
            raise SessionInvalidError("login verify failed")

    def get_app_list(self):
        return self.req("GET", BASE + "/rump_frontend/getAppList/", timeout=TIMEOUT).json()

    def jwxt(self, app_list=None, force=False):
        """解析教务系统管理端地址。app_list 可由调用方并行预取；默认走学校级缓存，
        force=True 强制重新探测跳转（缓存失效兜底）。"""
        d = app_list if app_list is not None else self.get_app_list()
        self.mgmt, self.mgmt_key = _resolve_mgmt(self, d, force=force)

    def vcookies(self, domain, path="/"):
        r = self.req("GET", BASE + "/webvpn/cookie/?domain=%s&path=%s" % (quote(domain), quote(path)),
                     timeout=TIMEOUT)
        for part in r.text.split(";"):
            if "=" in part:
                n, v = part.strip().split("=", 1)
                self.s.cookies.set(n, v, domain="sec.stpt.edu.cn", path="/")

    def sso(self, prefix, pattern, hint, full=False):
        """管理端 SSO：默认快速路径 = 缓存 service + 会话 TGT 免密换票（省 2~3 个往返）；
        full=True 时强制重新探测 service 并走完整密码登录（缓存失效/学校侧变更兜底）。"""
        self.sso_key = (prefix, pattern)
        if full:
            service = _sso_service(self, prefix, pattern, hint, force=True)
            _, st = self._tickets(service)
        else:
            service = _sso_service(self, prefix, pattern, hint)
            try:
                st = mint_st(self, service)
            except Exception:
                LOG.debug("TGT 免密换票失败，回退完整 CAS 登录 pattern=%s", pattern)
                _, st = self._tickets(service)
        self.req("GET", prefix + "api/cas/login?pattern=%s&ticket=%s" % (pattern, quote(st)),
                 allow_redirects=True, timeout=TIMEOUT)
        self.vcookies("10.1.200.4", "/")

    def student(self):
        """进入学生端门户并取得学生访问地址。优先走缓存/TGT 快速路径；
        失败时清缓存并按旧完整流程重试一次，学校侧配置变更后仍可用。"""
        try:
            self._student_once()
        except Exception as e:
            _invalidate_mgmt(getattr(self, "mgmt_key", None))
            _invalidate_sso_service(*(getattr(self, "sso_key", None) or (None, None)))
            LOG.debug("学生门户快速路径失败，回退完整 SSO 重试: %s", e)
            self._student_once(full=True)

    def _student_once(self, full=False):
        if full:
            self.jwxt(force=True)
            self.sso(self.mgmt, "manager-login", MGMT_SSO_SERVICE, full=True)
        else:
            self.sso(self.mgmt, "manager-login", MGMT_SSO_SERVICE)
        ts = int(time.time())
        csrf = hashlib.md5((str(ts) + "lyedu").encode()).hexdigest()
        r = self.req("GET",
                     self.mgmt + "api/system/sysSet/selectByParamCode?vpn-12-10.1.200.4:9090&paramCode=XSFWDZ&_t=%d" % ts,
                     headers=dict(AJAX, csrfToken=csrf, Referer=self.mgmt), timeout=TIMEOUT)
        self._raise_if_session_invalid(r)
        r = r.json()
        # 保存原始学生访问地址（短），令牌里存它即可，复用时经 to_webvpn 确定性还原
        self.stu_src = self._data(r, "学生访问地址")["paraValue"]
        self.stu = to_webvpn(self.stu_src).split("#")[0]
        self.req("GET", self.stu, timeout=TIMEOUT)

    def hdrs(self, ts):
        return dict(AJAX, **{"Content-Type": "application/json;charset=UTF-8",
                             "csrfToken": hashlib.md5((str(ts) + "lyedu").encode()).hexdigest(),
                             "Referer": self.stu})

    def _raise_if_session_invalid(self, r):
        """响应文本含会话失效信号时抛 SessionInvalidError。

        兼容学校返回非 JSON 错误页（HTML/纯文本）的情况：即使 .json() 会失败，
        也能在解析前识别“未登录/强制下线”，避免误判成普通上游故障。
        """
        text = getattr(r, "text", "") or ""
        if _has_session_hint(text):
            raise SessionInvalidError("学校端会话已失效: %s" % text[:200])

    def api(self, method, path, ts, body=None):
        r = self.req(method, self.stu + path % ts, headers=self.hdrs(ts), data=body, timeout=TIMEOUT)
        self._raise_if_session_invalid(r)
        return r.json()

    def _data(self, r, what):
        if not isinstance(r, dict):
            raise SchoolError("%s: 接口响应不是 JSON 对象: %s" % (what, str(r)[:200]))
        code = r.get("code")
        msg = str(r.get("message") or "")
        if (code in SESSION_INVALID_CODES
                or any(h in msg for h in SESSION_INVALID_HINTS)):
            raise SessionInvalidError("%s: 学校端会话已失效（%s）"
                                      % (what, msg or code))
        if r.get("code") not in (None, 200):
            raise SchoolError("%s: 学校端返回错误 code=%s message=%s" % (what, r.get("code"), r.get("message")))
        if "data" not in r:
            raise SchoolError("%s: 接口响应缺少 data 字段: %s" % (what, json.dumps(r, ensure_ascii=False)[:200]))
        return r["data"]

    def current(self):
        return self._data(self.api("GET", "api/baseInfo/semester/selectCurrentXnXq?vpn-12-10.1.200.4:9995&_t=%d",
                                   int(time.time())), "当前学期")["semester"]

    def finals(self, sem, size=100):
        ts = int(time.time())
        data = self._finals_page(sem, ts, 1, size)
        rows = list(data.get("rows") or [])
        total = _page_total(data, len(rows))
        truncated = False
        if len(rows) < total:
            pages = min(MAX_FINALS_PAGES, (total + size - 1) // size)
            if pages > 1:
                jobs = [("p%d" % n, lambda jj, n=n: jj._finals_page(sem, ts, n, size))
                        for n in range(2, pages + 1)]
                res = _run_upstream(self, jobs, max_workers=min(UPSTREAM_PARALLEL, len(jobs)))
                for n in range(2, pages + 1):
                    st, val = res.get("p%d", ("err", RuntimeError("未执行")))
                    if st == "ok" and isinstance(val, dict):
                        rows.extend(val.get("rows") or [])
                    else:
                        # 分页失败重试一次；仍失败则整体报错，避免静默返回残缺成绩
                        LOG.warning("成绩分页失败重试 semester=%s page=%s error=%s",
                                    sem, n, val)
                        val = self._finals_page(sem, ts, n, size)
                        rows.extend(val.get("rows") or [])
            if len(rows) < total:
                truncated = True
        data = dict(data)
        data["rows"] = rows
        data["truncated"] = truncated
        return sem, data

    def _finals_page(self, sem, ts, page_no, size=100):
        body = json.dumps({"pageNo": page_no, "pageSize": size,
                           "total": 0, "param": {"semester": sem}})
        return self._data(self.api(
            "POST", "api/score/scorequerymanage/studentFinalQuery?vpn-12-10.1.200.4:9995&_t=%d",
            ts, body), "期末成绩")

    def info(self):
        return self._data(self.api("GET", "api/student/studentInfo/selectXsSyByid/%s?vpn-12-10.1.200.4:9995&_t=%%d" % self.u,
                                   int(time.time())), "学籍信息")

    def sems(self):
        return self._data(self.api("GET", "api/baseInfo/semester/selectXnXqListTy?vpn-12-10.1.200.4:9995&_t=%d",
                                   int(time.time())), "学期列表")

    def uni(self):
        ts = int(time.time())
        try:
            return self._uni_pages(ts, "1")
        except Exception as e:
            # 仅在 type=1 业务错误时降级为不带 type 查询；
            # 网络/认证失败直接抛出，保留原始错误原因
            if _is_auth_failure(e) or isinstance(e, requests.exceptions.RequestException):
                raise
            LOG.debug("全部成绩 type=1 查询失败，降级为不带 type 查询: %s", e)
            return self._uni_pages(ts, None)

    def _uni_pages(self, ts, typ):
        data = self._uni_page(ts, 1, typ)
        if not isinstance(data, dict):
            raise SchoolError("个人首页全部成绩: 返回结构异常")
        rows = list(data.get("rows") or [])
        total = _page_total(data, len(rows))
        truncated = False
        if len(rows) < total:
            pages = min(MAX_UNI_PAGES, (total + 199) // 200)
            if pages > 1:
                jobs = [("p%d" % n, lambda jj, n=n: jj._uni_page(ts, n, typ))
                        for n in range(2, pages + 1)]
                res = _run_upstream(self, jobs, max_workers=min(UPSTREAM_PARALLEL, len(jobs)))
                for n in range(2, pages + 1):
                    st, val = res.get("p%d", ("err", RuntimeError("未执行")))
                    if st == "ok" and isinstance(val, dict):
                        rows.extend(val.get("rows") or [])
                    else:
                        LOG.warning("全部成绩分页失败重试 page=%s error=%s", n, val)
                        val = self._uni_page(ts, n, typ)
                        rows.extend(val.get("rows") or [])
            if len(rows) < total:
                truncated = True
        data = dict(data)
        data["rows"] = rows
        data["truncated"] = truncated
        return data

    def _uni_page(self, ts, page_no, typ):
        url = "api/score/universityScore/queryUniversityScore?vpn-12-10.1.200.4:9995&_t=%d"
        body = json.dumps({"pageNo": page_no, "pageSize": 200,
                           "param": {"type": typ} if typ else {}})
        return self._data(self.api("POST", url, ts, body), "个人首页全部成绩")

    def credit(self):
        return self._data(self.api("POST", "api/score/universityScore/queryCreditSituation?vpn-12-10.1.200.4:9995&_t=%d",
                                   int(time.time()), "{}"), "学分概况")

    def schedule(self, semester, weeks=None, odd_or_double=1):
        ts = int(time.time())
        if isinstance(weeks, str):
            weeks = _parse_weeks(weeks)
        if not weeks:
            weeks = list(range(1, 26))
        wl = [x for x in (_to_int(w, None) for w in weeks) if x is not None] or list(range(1, 26))
        body = json.dumps({"studentId": self.u, "oddOrDouble": _to_int(odd_or_double, 1),
                           "semester": semester, "weeks": wl})
        return self._data(self.api("POST", "api/arrange/CourseScheduleAllQuery/studentCourseSchedule?vpn-12-10.1.200.4:9995&_t=%d",
                                   ts, body), "我的课表")

    def export_schedule(self, semester, weeks=None, odd_or_double=1, course_type="studentSchedule"):
        ts = int(time.time())
        if isinstance(weeks, str):
            weeks = _parse_weeks(weeks)
        if not weeks:
            weeks = list(range(1, 26))
        wl = [x for x in (_to_int(w, None) for w in weeks) if x is not None] or list(range(1, 26))
        body = json.dumps([{"tableType": "1", "studentId": self.u,
                            "weeks": wl,
                            "oddOrDouble": "1" if _to_int(odd_or_double, 1) == 1 else "0",
                            "semester": semester}])
        url = self.stu + ("api/arrange/courseSchedulePrint/export?courseType=%s&vpn-12-10.1.200.4:9995&_t=%d"
                          % (quote(course_type), ts))
        r = self.req("POST", url, headers=self.hdrs(ts), data=body, timeout=40)
        self._raise_if_session_invalid(r)
        if r.status_code != 200:
            if r.status_code >= 500:
                raise UpstreamError("课表导出失败：HTTP %s %s" % (r.status_code, r.text[:200]))
            raise SchoolError("课表导出失败：HTTP %s %s" % (r.status_code, r.text[:200]))
        return r


def _invalidate_key(url):
    key = url.split("?", 1)[0]
    with _RSA_KEY_CACHE_LOCK:
        existed = key in _RSA_KEY_CACHE
        _RSA_KEY_CACHE.pop(key, None)
        return existed


def _mgmt_target(app_list):
    """从应用列表提取“教务系统”入口 URL（与 jwxt 原逻辑一致）。"""
    target = next(x["url"] for g in app_list["sites"].values()
                  for x in g if x.get("name") == "教务系统")
    return BASE + target if target.startswith("/") else target


def _resolve_mgmt(j, app_list, force=False):
    """解析教务系统管理端最终地址：优先学校级缓存（30 分钟 TTL），miss 时跟随跳转探测。
    带查询参数的 URL 疑似含会话信息，不缓存（保持原逐次探测行为）。
    返回 (mgmt, 缓存键)；键供 student() 失败时主动失效。"""
    target = _mgmt_target(app_list)
    if not force:
        with _MGMT_CACHE_LOCK:
            hit = _MGMT_CACHE.get(target)
            if hit is not None and time.time() - hit[0] < _MGMT_CACHE_TTL:
                return hit[1], target
    r = j.req("GET", target, allow_redirects=True, timeout=TIMEOUT)
    mgmt = r.url.split("#")[0]
    key = None
    if "?" not in mgmt:
        _cache_put(_MGMT_CACHE, _MGMT_CACHE_LOCK, target,
                   (time.time(), mgmt), _MGMT_CACHE_MAX, _MGMT_CACHE_TTL)
        key = target
    return mgmt, key


def _invalidate_mgmt(key):
    if key:
        with _MGMT_CACHE_LOCK:
            _MGMT_CACHE.pop(key, None)


def _sso_service(j, prefix, pattern, hint, force=False):
    """从 api/cas/login 跳转 Location 提取 CAS service 参数；默认走缓存（1 小时 TTL），
    force=True 强制重新探测。取不到时回退 hint；强制探测结果不写缓存（避免坏值滞留）。"""
    key = (prefix, pattern)
    if not force:
        with _SSO_SERVICE_CACHE_LOCK:
            hit = _SSO_SERVICE_CACHE.get(key)
            if hit is not None and time.time() - hit[0] < _SSO_SERVICE_CACHE_TTL:
                return hit[1]
    r = j.req("GET", prefix + "api/cas/login?pattern=%s&returnUrl=%s" % (pattern, quote(prefix, safe="")),
              allow_redirects=False, timeout=TIMEOUT)
    m = re.search(r"service=([^&]+)", r.headers.get("location") or "")
    if m:
        service = unquote(m.group(1))
        if not force:
            _cache_put(_SSO_SERVICE_CACHE, _SSO_SERVICE_CACHE_LOCK, key,
                       (time.time(), service), _SSO_SERVICE_CACHE_MAX, _SSO_SERVICE_CACHE_TTL)
    else:
        service = hint
    return service


def _invalidate_sso_service(prefix=None, pattern=None):
    if prefix and pattern:
        with _SSO_SERVICE_CACHE_LOCK:
            _SSO_SERVICE_CACHE.pop((prefix, pattern), None)


def dump_cookies(j):
    return [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in j.s.cookies]


_SESSION_HOSTS = ("sec.stpt.edu.cn", "cas.stpt.edu.cn")


def _cookie_needed(dom):
    """Cookie 是否会在查询请求中被发送：请求只走 sec/cas 两个主机，
    故保留等于这两个主机、其子域或其父域（如 .stpt.edu.cn）的 Cookie。"""
    d = (dom or "").lstrip(".")
    if not d:
        return False
    return any(d == h or h.endswith("." + d) for h in _SESSION_HOSTS)


def dump_session_cookies(j):
    """仅保留复用会话实际需要的 Cookie：查询请求只走 sec.stpt.edu.cn（WebVPN），
    cas.stpt.edu.cn 仅作兜底；父域（.stpt.edu.cn）Cookie 也会发给这两个主机，需保留。
    其余域 Cookie 无用途。默认 path=/ 省略以缩小令牌。"""
    out = []
    for c in dump_cookies(j):
        dom = c.get("domain") or ""
        if not _cookie_needed(dom):
            continue
        if not c.get("name") or c.get("value") in (None, ""):
            continue
        item = {"name": c["name"], "value": c["value"], "domain": dom}
        if c.get("path") not in (None, "", "/"):
            item["path"] = c["path"]
        out.append(item)
    return out


def dump_portal(j):
    """门户信息最小化：令牌只存查询必需的 stu 原始地址 + 免密跳转所需 TGT/CAS/mgmt。"""
    stu_src = getattr(j, "stu_src", None) or j.stu
    return {"stu": stu_src,
            "frag": stu_src.partition("#")[2] if stu_src else None,
            "mgmt": getattr(j, "mgmt", None),
            "tgt": getattr(j, "_tgt", None),
            "cas": getattr(j, "cas", None)}


def load_cookies(j, items):
    for it in items or []:
        kw = {}
        if it.get("domain"):
            kw["domain"] = it["domain"]
        kw["path"] = it.get("path") or "/"
        j.s.cookies.set(it.get("name", ""), it.get("value", ""), **kw)


def _clone_j(j):
    """复制会话上下文（Cookie/门户地址）供并发上游请求使用；每个副本持有独立 requests.Session，
    避免共享 Session 在并发下的线程安全问题。"""
    j2 = type(j)(j.u, j.p)
    load_cookies(j2, dump_cookies(j))
    j2.cas, j2.mgmt, j2.stu = j.cas, j.mgmt, j.stu
    j2.stu_frag = getattr(j, "stu_frag", None)
    j2._tgt = getattr(j, "_tgt", None)
    return j2


def _run_upstream(j, jobs, max_workers=None):
    """并发执行一组相互独立的上游调用。

    jobs 为 [(名称, fn(会话) -> 结果)]。第一个任务复用主会话，其余任务使用克隆会话并行执行，
    每请求并发上限默认 UPSTREAM_PARALLEL；进程级并发由 J.req 内的 UPSTREAM_SEM 统一约束。
    顶层批次使用进程级共享执行器；嵌套批次（如分页内部再并行）回退到独立线程池，
    避免共享池被外层任务占满后内层子任务无法调度导致死锁。
    返回 {名称: ("ok", 结果)} 或 {名称: ("err", 异常)}，单个任务失败不影响其他任务。
    """
    results = {}
    parent_trace = trace_id()

    def run(item, jref, clone):
        set_trace_id(parent_trace)
        prev_nested = getattr(_trace_local, "in_upstream", False)
        _trace_local.in_upstream = True
        try:
            try:
                name, fn = item
                return name, ("ok", fn(jref))
            except Exception as e:
                return name, ("err", e)
        finally:
            _trace_local.in_upstream = prev_nested
            if clone:
                jref.close()

    def collect(ex):
        futs = []
        for i, item in enumerate(jobs):
            clone = i != 0
            jref = _clone_j(j) if clone else j
            futs.append(ex.submit(run, item, jref, clone))
        for fut in futs:
            name, res = fut.result()
            results[name] = res

    if getattr(_trace_local, "in_upstream", False):
        with ThreadPoolExecutor(
                max_workers=max(1, min(max_workers or UPSTREAM_PARALLEL, len(jobs))),
                thread_name_prefix="jwxt-nested") as pool:
            collect(pool)
    else:
        collect(_upstream_executor())
    return results


def _take(res, name):
    """取并行结果；任务失败时抛出原异常（与串行调用时的行为一致）。"""
    st, val = res.get(name) or ("err", RuntimeError("%s 未执行" % name))
    if st != "ok":
        raise val
    return val


def _md_cell(v):
    """Markdown 表格单元格转义：竖线与换行不破坏表格。"""
    return str(v if v is not None else "").replace("|", "\\|") \
        .replace("\r", "").replace("\n", "<br>")


def render(results, uni=None, credit=None, info=None, note=""):
    shown = [(s, f) for s, f in results if f.get("rows")]
    multi = len(shown) > 1
    L = ["## 我的成绩" + ("（共 %d 个学期）" % len(shown) if multi else ("（%s）" % shown[0][0] if shown else "")), ""]
    if info:
        L.append("学号：%s ｜ 姓名：%s ｜ 学院：%s ｜ 专业：%s ｜ 班级：%s" % (
            info.get("xh", ""), info.get("xm", ""), info.get("xymc", ""),
            info.get("zyfxmc", ""), info.get("bjmc", "")))
        L.append("")
    head = ("| 学年学期 | 学号 | 姓名 | 课程 | 期末成绩 | GPA | 学分 | 课程类别 | 备注 |"
            if multi else "| 学号 | 姓名 | 课程 | 期末成绩 | GPA | 学分 | 课程类别 | 备注 |")
    L += [head, "|" + "---|" * (9 if multi else 8)]
    n = 0
    for sem, final in shown:
        for x in final.get("rows") or []:
            cells = [x.get("studentCode") or info.get("xh", ""), x.get("studentName") or info.get("xm", ""),
                     x.get("courseName", ""), _norm_num(x.get("totalScore", "")),
                     _norm_num((x.get("totalScoreGpa") or "").strip()), _norm_num(x.get("courseCredits", "")),
                     x.get("courseStyleName", ""), x.get("totalScoreMark", "")]
            if multi:
                cells.insert(0, sem)
            L.append("| " + " | ".join(_md_cell(c) for c in cells) + " |")
            n += 1
    if not n:
        L.append("| （暂无成绩数据） |")
    elif len(results) > len(shown):
        L += ["", "> 已查询 %d 个学期，其中 %d 个学期无成绩记录（已省略）。" % (len(results), len(results) - len(shown))]
    if credit:
        L += ["", "学分概况：总学分 %s | 已修 %s | 进度 %s%%" % (
            credit.get("totalCredit"), credit.get("repairedCredit"), credit.get("creditRate"))]
    if uni and uni.get("rows"):
        L += ["", "### 全部成绩（%s 门，个人首页）" % uni.get("total"), "",
              "| 课程 | 成绩 | GPA | 排名 | 学分 |", "|---|---|---|---|---|"]
        for x in uni["rows"]:
            pos = x.get("positiveExamScoreValue")
            mk = x.get("makeupExamValue")
            has_mk = mk not in (None, "")
            has_pos = pos not in (None, "")
            score = x.get("courseScore")
            if score is None:
                score = mk if has_mk else (pos if has_pos else "")
            gpa = x.get("scoreGPA")
            if gpa is None:
                gpa = x.get("makeupExamGPA") if has_mk else (x.get("positiveExamGPA") if has_pos else "")
            rank = x.get("scoreRank")
            L.append("| %s | %s | %s | %s | %s |" % tuple(_md_cell(c) for c in (
                x.get("courseName", ""), score, gpa if gpa is not None else "",
                rank if rank is not None else "—", x.get("courseCredit", ""))))
    if note:
        L += ["", note]
    return "\n".join(L)


def fmt_weeks(w):
    w = str(w or "").strip()
    if not w:
        return ""
    marker = ""
    for m in ("单", "双"):
        if m in w:
            marker = m
            w = w.replace(m, "").strip()
    parts = []
    for seg in w.replace("，", ",").split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if "-" in seg:
            a, _, b = seg.partition("-")
            if a.isdigit() and b.isdigit():
                parts.append("%s-%s" % (a, b))
                continue
        if seg.isdigit():
            parts.append(seg)
        else:
            parts.append(seg)
    return ("、".join(parts) + marker + "周") if parts else (w + marker + "周")


def render_schedule(data, semester, info=None, note="", details=True):
    blocks = data or []
    day_slots = {}
    time_slots = {}
    for b in blocks:
        wk = b.get("week") or {}
        tm = b.get("time") or {}
        day_slots.setdefault(wk.get("sort", 99), (wk.get("weekCode", ""), wk.get("weekName", "")))
        time_slots.setdefault(tm.get("sort", 99), tm.get("timeName", ""))
    day_order = sorted(day_slots)
    time_order = sorted(time_slots)
    day_names = [day_slots[k][1] for k in day_order]
    time_names = [time_slots[k] for k in time_order]
    grid = {}
    for b in blocks:
        wk = b.get("week") or {}
        tm = b.get("time") or {}
        grid[(wk.get("sort", 99), tm.get("sort", 99))] = b.get("courseList") or []

    L = ["## 我的课表（%s）" % semester, ""]
    if info:
        L.append("学号：%s ｜ 姓名：%s ｜ 学院：%s ｜ 专业：%s ｜ 班级：%s" % (
            info.get("xh", ""), info.get("xm", ""), info.get("xymc", ""),
            info.get("zyfxmc", ""), info.get("bjmc", "")))
        L.append("")

    def cell(key):
        cells = []
        for c in grid.get(key) or []:
            bits = [c.get("courseName", "")]
            if c.get("teacherName"):
                bits.append(c.get("teacherName"))
            if c.get("classroomName"):
                bits.append(c.get("classroomName"))
            w = fmt_weeks(c.get("weeks"))
            if w:
                bits.append(w)
            cells.append("<br>".join(bits))
        return "<br>".join(cells) if cells else ""

    L.append("| 节次 | " + " | ".join(day_names) + " |")
    L.append("|" + "---|" * (len(day_names) + 1))
    for ts_ in time_order:
        cells = [time_names[time_order.index(ts_)]] + [cell((d, ts_)) for d in day_order]
        L.append("| " + " | ".join(_md_cell(x) for x in cells) + " |")

    rows = []
    for b in blocks:
        wk = b.get("week") or {}
        tm = b.get("time") or {}
        for c in b.get("courseList") or []:
            rows.append(c)
    if details and rows:
        L += ["", "### 课程明细（%d 条）" % len(rows), "",
              "| 星期 | 节次 | 课程 | 周次 | 教师 | 教室 |", "|---|---|---|---|---|---|"]
        seen = set()
        for c in rows:
            key = (c.get("dayOfWeekName"), c.get("timeName"), c.get("courseName"),
                   c.get("weeks"), c.get("teacherName"), c.get("classroomName"))
            if key in seen:
                continue
            seen.add(key)
            L.append("| %s | %s | %s | %s | %s | %s |" % (
                c.get("dayOfWeekName", ""), c.get("timeName", ""), c.get("courseName", ""),
                fmt_weeks(c.get("weeks")), c.get("teacherName", ""), c.get("classroomName", "")))
    if note:
        L += ["", note]
    return "\n".join(L)


def main(username: str, password: str, semesters: str = "", include_all: bool = False,
         include_rows: bool = True, include_output: bool = True,
         include_sensitive: bool = False, j=None) -> dict:
    u = (username or "").strip()
    p = (password or "").strip()
    # 携带会话 j 时无需密码（v3 令牌不含密码）；仅当需要新登录时才要求密码
    if not u or (not p and j is None):
        return {"success": False, "error": "请提供 username 和 password", "output": "",
                "count": 0, "rows": [], "info": {}, "stats": None}
    created = None
    try:
        if j is None:
            j = J(u, p)
            created = j
            j.login()
            j.jwxt()
            j.student()
        s = (semesters or "").strip()
        # 只调用真正需要的接口：显式指定学期时无需当前学期/学期列表；
        # 相互独立的调用（学籍/当前学期/学期列表/全部成绩/学分）并行执行
        need_current = (not s) or s == "all" or s.startswith("recent:")
        need_sems = s == "all" or s.startswith("recent:")
        jobs = [("info", lambda jj: jj.info())]
        if need_current:
            jobs.append(("current", lambda jj: jj.current()))
        if need_sems:
            jobs.append(("sems", lambda jj: jj.sems()))
        if include_all:
            jobs.append(("uni", lambda jj: jj.uni()))
            jobs.append(("credit", lambda jj: jj.credit()))
        res = _run_upstream(j, jobs)
        info = _take(res, "info")
        cur = _take(res, "current") if need_current else None
        alls = _take(res, "sems") if need_sems else None
        uni = cr = None
        note = ""
        if include_all:
            for name, label in (("uni", "个人首页“全部成绩”"), ("credit", "学分概况")):
                st, val = res.get(name, ("err", RuntimeError("未执行")))
                if st == "ok":
                    if name == "uni":
                        uni = val
                        if val.get("truncated"):
                            note += "个人首页“全部成绩”超过单次返回上限（%d 页），仅返回部分记录。\n" % MAX_UNI_PAGES
                    else:
                        cr = val
                else:
                    note += "%s暂不可用（%s），已跳过。\n" % (label, val)
        if not s:
            sems = [cur]
        elif s == "all":
            sems = [x for x in alls if x <= cur]
        elif s.startswith("recent:"):
            try:
                n = int(s.split(":", 1)[1])
            except Exception:
                n = 1
            n = max(1, min(n, MAX_SEMESTERS))
            sems = [x for x in alls if x <= cur][:n]
        elif "," in s:
            sems = []
            raw_parts = [x.strip() for x in s.split(",") if x.strip()]
            for x in raw_parts[:MAX_SEMESTERS]:
                x = x.strip()
                if x and x not in sems:
                    sems.append(x)
            if len(raw_parts) > MAX_SEMESTERS:
                note += "semesters 数量超过上限（%d），仅查询前 %d 个学期。\n" % (MAX_SEMESTERS, MAX_SEMESTERS)
        else:
            sems = [s]
        if len(sems) == 1:
            results = [j.finals(sems[0])]
        else:
            fr = _run_upstream(j, [("f%d" % i, lambda jj, sem=sem: jj.finals(sem)[1])
                                   for i, sem in enumerate(sems)])
            results = [(sem, _take(fr, "f%d" % i)) for i, sem in enumerate(sems)]
        truncated = False
        rows = []
        for sem, final in results:
            if final.get("truncated"):
                truncated = True
                note += "学期 %s 成绩超过单次返回上限（%d 页），仅返回部分记录。\n" % (sem, MAX_FINALS_PAGES)
            for x in final.get("rows") or []:
                rows.append({
                    "semester": sem,
                    "studentCode": x.get("studentCode", ""),
                    "studentName": x.get("studentName", ""),
                    "courseName": x.get("courseName", ""),
                    "score": _norm_num(x.get("totalScore", "")),
                    "gpa": _norm_num((x.get("totalScoreGpa") or "").strip()),
                    "credit": _norm_num(x.get("courseCredits", "")),
                    "courseType": x.get("courseStyleName", ""),
                })
        resp = {
            "success": True,
            "count": len(rows),
            "error": "",
        }
        if truncated:
            resp["truncated"] = True
        if include_output:
            resp["output"] = render(results, uni, cr, info, note.strip())
        resp["rows"] = rows if include_rows else []
        resp["info"] = _safe_info(info, include_sensitive)
        resp["stats"] = _grade_stats(rows)
        return resp
    except SessionInvalidError as e:
        if created is not None:
            # 密码路径：学校端会话被下线/未登录不是瞬时故障，不缓存；
            # 若刚登录就被挤下线，调用方可稍后重试（下次 /login 会重新建会话）
            LOG.warning("密码登录查询遇学校端会话失效 username=%s error=%s", u, e)
            return {"success": False, "error": "学校端会话已失效（%s），请稍后重试或重新登录"
                    % e, "output": "", "count": 0, "rows": [], "info": {}, "stats": None,
                    "neg_cacheable": False, "credential_error": True}
        # 会话路径：交由 handler 转 401 并清除内存会话，防止 /login 复用死会话
        raise TokenError("session 已失效（学校端会话被下线或未登录），请重新 POST /login 获取新 session")
    except Exception as e:
        credential_error = _is_auth_failure(e)
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e), "output": "",
                "count": 0, "rows": [], "info": {}, "stats": None,
                "neg_cacheable": (False if credential_error
                                  else _is_transient_error(e)),
                "credential_error": credential_error}
    finally:
        if created is not None:
            created.close()


def _to_int(v, default=None, what=""):
    """宽松整数解析：容忍 JSON/命令行转义残留的反斜杠与引号，如 "1\\"、"\\"1\\""。"""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    s = str(v).strip().strip("'\"\\ ")
    try:
        return int(s)
    except Exception:
        return default


def _clamp_week(n):
    """周次收敛到合法区间；非法/空值返回 None。"""
    if n is None:
        return None
    return max(WEEK_MIN, min(WEEK_MAX, int(n)))


def _parse_weeks(v):
    """解析周次参数（列表/逗号/区间），全部收敛到 [WEEK_MIN, WEEK_MAX] 且总数有上限，
    防止 "1-9999999" 之类输入展开成超大列表导致内存/上游请求体膨胀。"""
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        out = []
        for x in v:
            n = _clamp_week(_to_int(x, None, "weeks"))
            if n is not None:
                out.append(n)
            if len(out) >= MAX_WEEKS_TOTAL:
                break
        return sorted(set(out))[:MAX_WEEKS_TOTAL] or None
    s = str(v).strip().lower()
    if not s or s in ("all", "全部", "全学期"):
        return None
    out = []
    for seg in s.replace("，", ",").split(",")[:MAX_WEEK_SEGMENTS]:
        seg = seg.strip().strip("'\"\\ ")
        if not seg:
            continue
        if "-" in seg:
            a, _, b = seg.partition("-")
            na = _clamp_week(_to_int(a, None, "weeks"))
            nb = _clamp_week(_to_int(b, None, "weeks"))
            if na is not None and nb is not None:
                lo, hi = min(na, nb), max(na, nb)
                for w in range(lo, hi + 1):
                    out.append(w)
                    if len(out) >= MAX_WEEKS_TOTAL:
                        break
            if len(out) >= MAX_WEEKS_TOTAL:
                break
            continue
        n = _to_int(seg, None, "weeks")
        if n is not None:
            out.append(_clamp_week(n))
        if len(out) >= MAX_WEEKS_TOTAL:
            break
    return sorted(set(out))[:MAX_WEEKS_TOTAL] or None


def _weeks_param_error(v):
    """显式传入了 weeks 但无法解析出任何有效周次时返回错误文案（None=合法）。

    区分“未传/全部语义”（None、空串、all、全部、全学期、空列表）与“坏值”
    （如 "abc"、"[1, 2]"）：坏值此前会静默回退成全部周次，导致课表/导出
    给出与请求不符的整学期数据，这里改为交由调用方返回 400。
    部分坏值（如 "1-3,abc"）仍容忍，只保留可解析部分。
    """
    if v is None:
        return None
    if isinstance(v, (list, tuple)):
        if not v:
            return None
        if _parse_weeks(v) is None:
            return "weeks 参数无法解析：%r" % (v,)
        return None
    s = str(v).strip()
    if not s or s.lower() in ("all", "全部", "全学期"):
        return None
    if _parse_weeks(s) is None:
        return "weeks 参数无法解析：%r" % (v,)
    return None


def _odd_or_double_error(v):
    """odd_or_double 只接受 1/2；其他值返回错误文案，避免把非法值透传上游。"""
    n = _to_int(v, None)
    if n is None or n not in (1, 2):
        return "odd_or_double 必须为 1 或 2"
    return None


def main_schedule(username: str, password: str, semester: str = "", weeks=None,
                  odd_or_double: int = 1, include_info: bool = True, include_rows: bool = True,
                  include_details: bool = True, j=None) -> dict:
    u = (username or "").strip()
    p = (password or "").strip()
    if not u or (not p and j is None):
        return {"success": False, "error": "请提供 username 和 password", "output": "", "count": 0, "rows": [], "semester": ""}
    created = None
    try:
        if j is None:
            j = J(u, p)
            created = j
            j.login()
            j.jwxt()
            j.student()
        need_current = not (semester or "").strip()
        jobs = []
        if include_info:
            jobs.append(("info", lambda jj: jj.info()))
        if need_current:
            jobs.append(("current", lambda jj: jj.current()))
        if jobs:
            res = _run_upstream(j, jobs)
            info = _take(res, "info") if include_info else None
            cur = _take(res, "current") if need_current else None
        else:
            info = cur = None
        sem = (semester or "").strip() or cur
        data = j.schedule(sem, _parse_weeks(weeks), _to_int(odd_or_double, 1))
        rows = []
        for b in data or []:
            wk = b.get("week") or {}
            tm = b.get("time") or {}
            for c in b.get("courseList") or []:
                rows.append({
                    "semester": sem,
                    "dayOfWeekCode": wk.get("weekCode", ""),
                    "dayOfWeekName": c.get("dayOfWeekName") or wk.get("weekName", ""),
                    "timeCode": tm.get("timeCode", ""),
                    "timeName": c.get("timeName") or tm.get("timeName", ""),
                    "courseCode": c.get("courseCode", ""),
                    "courseName": c.get("courseName", ""),
                    "weeks": c.get("weeks", ""),
                    "teacherName": c.get("teacherName", ""),
                    "classroomName": c.get("classroomName", ""),
                    "className": c.get("className", ""),
                })
        resp = {
            "success": True,
            "output": render_schedule(data, sem, info, details=include_details),
            "count": len(rows),
            "semester": sem,
            "error": "",
        }
        resp["rows"] = rows if include_rows else []
        return resp
    except SessionInvalidError as e:
        if created is not None:
            LOG.warning("密码登录查询遇学校端会话失效 username=%s error=%s", u, e)
            return {"success": False, "error": "学校端会话已失效（%s），请稍后重试或重新登录"
                    % e, "output": "", "count": 0, "rows": [], "semester": semester or "",
                    "neg_cacheable": False, "credential_error": True}
        raise TokenError("session 已失效（学校端会话被下线或未登录），请重新 POST /login 获取新 session")
    except Exception as e:
        credential_error = _is_auth_failure(e)
        return {"success": False, "error": "%s: %s" % (type(e).__name__, e),
                "output": "", "count": 0, "rows": [], "semester": semester or "",
                "neg_cacheable": (False if credential_error
                                  else _is_transient_error(e)),
                "credential_error": credential_error}
    finally:
        if created is not None:
            created.close()


def _to_bool(v):
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _norm_num(v):
    """显示层规范化：学校偶发返回 '.4' 这类小数点开头的字符串，统一为 '0.4'（数值不变）；
    None/空值统一为空串，避免把 None 显示成 "None"。"""
    if v is None:
        return ""
    s = str(v).strip()
    if s in ("", "None", "null"):
        return ""
    return "0" + s if re.match(r"^\.[0-9]+$", s) else s


def _safe_info(info, include_sensitive=False):
    """info 字段白名单：默认仅返回渲染所需字段；身份证号/证件照需显式开启。"""
    if not isinstance(info, dict):
        return {}
    out = {k: info.get(k, "") for k in INFO_FIELDS}
    if include_sensitive:
        for k in SENSITIVE_INFO_FIELDS:
            if k in info and info[k] is not None:
                out[k] = info[k]
    return out


# 进程级随机盐：会话为内存态，重启即失效，盐无需持久化
_PWD_SALT = secrets.token_bytes(16)


def _pwd_hash(username, password):
    """密码单向摘要（仅内存使用）：用于 /login 同账号会话复用的口令校验，
    不保存明文密码、不落盘。"""
    return hashlib.sha256(_PWD_SALT + ("%s:%s" % (username, password)).encode("utf-8")).hexdigest()


def _norm_semesters_key(raw):
    """规范化成绩缓存的学期参数：去空白、去重、排序，避免 'A,B' 与 'B,A' 漏命中。"""
    parts = [x.strip() for x in str(raw or "").split(",") if x.strip()]
    return ",".join(sorted(set(parts)))


def _norm_weeks_key(v):
    """规范化课表缓存的 weeks 参数：列表/字符串统一解析成去重排序的逗号串，
    语义相同（如 '1-3' 与 '1,2,3'）共享缓存命中。"""
    w = _parse_weeks(v)
    return ",".join(str(x) for x in w) if w else ""


def _schedule_cache_owner(u):
    """课表缓存的所有者键：开启班级共享时，学号前 8 位视为班级标识（同班共享）；
    默认关闭（选修课差异风险），此时始终返回完整学号；非纯数字或不足 8 位同样回退。"""
    if not SCHEDULE_CLASS_SHARE:
        return u or ""
    s = (u or "").strip()
    if s.isdigit() and len(s) >= 8:
        return s[:8]
    return s


def _schedule_cache_key(cache_u, sem_raw, weeks_raw, odd_raw,
                        include_rows, include_details):
    """构造课表缓存键：学号前 8 位相同视为同班，共享同一份缓存。"""
    return "%s|%s|%s|%s|%d|%d" % (
        _schedule_cache_owner(cache_u), sem_raw,
        _norm_weeks_key(weeks_raw), odd_raw,
        include_rows, include_details)


def _schedule_pdf_cache_key(cache_u, sem_raw, weeks_raw, odd_raw):
    """构造课表 PDF 缓存键；参数语义与课表查询一致，但不含渲染裁剪开关。"""
    sem_key = (sem_raw or "").strip() or "__current__"
    return "pdf|%s|%s|%s|%s" % (
        _schedule_cache_owner(cache_u), sem_key,
        _norm_weeks_key(weeks_raw), _to_int(odd_raw, 1))


def _schedule_export_suffix(body, code, semester):
    """构造课表导出下载地址的查询串，与请求参数保持一致（code 为短时下载码，
    不包含长期有效的 session，避免令牌出现在 URL/访问日志/浏览器历史中）。
    weeks 先做归一化：列表/区间字符串统一转成去重排序的逗号串（如 [1,3] -> "1,3"），
    否则 str([1, 3]) 生成的 "weeks=[1, 3]" 在 GET 端无法解析，会回退导出全部周次。"""
    query = {"code": code, "semester": semester or ""}
    weeks = _parse_weeks(body.get("weeks"))
    if weeks:
        query["weeks"] = ",".join(str(x) for x in weeks)
    query["odd_or_double"] = str(body.get("odd_or_double", 1))
    return "/get_schedule/export?%s" % urlencode(query)


def _cors_allow(origin):
    """按请求 Origin 决定 CORS 允许来源；单 origin 配置保持旧行为（始终返回）。"""
    origins = CORS_ORIGINS or ([x.strip() for x in CORS_ORIGIN.split(",") if x.strip()]
                               if CORS_ORIGIN else [])
    if not origins:
        return None
    if len(origins) == 1 and origins[0] != "*":
        return origins[0]
    if origin and origin in origins:
        return origin
    if "*" in origins:
        return "*"
    return None


def _trusted_proxy_ok(peer):
    """TRUST_PROXY 且配置了可信网段时，仅接受来自这些网段的 XFF。"""
    if not TRUSTED_PROXY_CIDRS or not peer:
        return True
    try:
        addr = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(addr in net for net in TRUSTED_PROXY_CIDRS)


def public_url(headers):
    """生成对外链接基准地址：优先 JWXT_PUBLIC_URL；否则用 Host + X-Forwarded-Proto，
    Host 做基本合法性校验，防止 Host 头注入。X-Forwarded-Proto 仅在显式配置
    TRUST_PROXY 时才采信，避免客户端直接伪造 scheme。"""
    if PUBLIC_URL:
        return PUBLIC_URL.rstrip("/")
    proto = "http"
    if TRUST_PROXY:
        proto = (headers.get("X-Forwarded-Proto") or "http").strip().lower()
        if proto not in ("http", "https"):
            proto = "http"
    host = (headers.get("Host") or "").strip()
    if TRUST_PROXY:
        xfh = (headers.get("X-Forwarded-Host") or "").strip()
        if xfh:
            host = xfh
    if not host or any(ch in host for ch in ("/", "\\", " ", "@")) or ".." in host:
        host = "localhost"
    return "%s://%s" % (proto, host)


def _grade_stats(rows):
    """基于成绩行预计算统计（数值型成绩；'优秀' 等非数值成绩不参与分布/均值计算）。"""
    nums = []
    for r in rows or []:
        s = str(r.get("score") or "").strip()
        try:
            nums.append(float(s))
        except ValueError:
            continue
    n = len(nums)
    if not n:
        return None
    bins = {"ge90": 0, "80_89": 0, "70_79": 0, "60_69": 0, "lt60": 0}
    for v in nums:
        if v >= 90:
            bins["ge90"] += 1
        elif v >= 80:
            bins["80_89"] += 1
        elif v >= 70:
            bins["70_79"] += 1
        elif v >= 60:
            bins["60_69"] += 1
        else:
            bins["lt60"] += 1
    return {
        "count": n,
        "avg": round(sum(nums) / n, 2),
        "max": max(nums),
        "min": min(nums),
        "bins": bins,
        "percent": {k: round(v * 100.0 / n, 1) for k, v in bins.items()},
    }


def _is_transient_error(e):
    """判断失败是否适合负缓存：requests 网络异常与明确的上游 HTTP 5xx 视为瞬时。

    学校业务错误、登录/认证失败和编程错误都不进入负缓存，避免掩盖真实问题。
    """
    if isinstance(e, (requests.exceptions.RequestException, UpstreamError)):
        return True
    msg = str(e) or ""
    # 明确的上游 HTTP 5xx（如课表导出、CAS 换票）也视为瞬时，避免一次
    # 学校侧抖动把整个请求变成不可重试的失败
    return "HTTP 5" in msg


def _is_auth_failure(e):
    """判断异常是否为“会话/认证失效”类（应清会话），而非瞬时网络故障。"""
    if isinstance(e, (SessionInvalidError, TokenError)):
        return True
    msg = str(e) or ""
    return (any(h in msg for h in SESSION_INVALID_HINTS)
            or "login verify failed" in msg
            or "HTTP 401" in msg or "HTTP 403" in msg)


def _has_session_hint(text):
    return bool(text) and any(h in text for h in SESSION_INVALID_HINTS)


def _cache_write_allowed(include_sensitive, r):
    """成绩响应是否允许写入缓存：敏感字段（身份证/证件照）响应不缓存；
    失败结果仅当标记 neg_cacheable（上游瞬时故障）时才允许负缓存。"""
    if include_sensitive:
        return False
    return bool(r.get("success") or r.get("neg_cacheable"))


def _internal_error_body():
    """对外 500 响应统一使用通用文案，异常详情只进服务端日志。"""
    return {
        "success": False,
        "error": "服务内部错误，请携带响应头 X-Trace-Id 联系管理员",
        "output": "",
        "count": 0,
        "rows": [],
        "info": {},
        "stats": None,
        "semester": "",
        "url": "",
        "page": "",
    }


LATENCY_BUCKETS_MS = (5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000)


@dataclass
class Config:
    """服务配置：字段与原 http.server 版本的命令行参数一一对应，未传时使用默认值。"""

    host: str = "0.0.0.0"
    port: int = 8766
    token: str = ""
    redis_url: str = ""
    session_ttl_hours: int = 1
    rate_limit: int = 600
    login_rate_limit: int = 60
    max_workers: int = 32
    thread_pool_size: int = 40
    warm_wait: int = 10
    idle_timeout: int = 10
    limit_concurrency: int = 0
    upstream_sem_timeout: float = 5.0
    max_semesters: int = 20
    upstream_parallel: int = 4
    upstream_global: int = 8
    health_username: str = ""
    health_password: str = ""
    health_interval: int = 300
    grades_cache_ttl: int = 300
    schedule_cache_ttl: int = 300
    schedule_pdf_cache_ttl: int = 300
    schedule_pdf_cache_max_items: int = 8
    schedule_pdf_prewarm: str = "1"
    schedule_class_share: str = "0"
    jump_cache_ttl: int = 30
    jump_code_ttl: int = 60
    health_probe_interval: int = 10
    verify_tls: str = "1"
    login_reuse: str = "1"
    login_reuse_max_age: int = 1800
    login_reuse_probe: str = "1"
    max_sessions: int = 1000
    grades_cache_max_items: int = 1000
    schedule_cache_max_items: int = 1000
    jump_cache_max_items: int = 1000
    public_url: str = ""
    cors_origin: str = ""
    trust_proxy: str = "0"
    dns_fallback_ips: str = "154.214.217.209,221.5.10.101"
    log_file: str = ""
    log_level: str = "INFO"
    # 安全可配置项（默认保持原有宽松行为，运维可按需收紧）
    allow_get_credentials: str = "1"
    protect_login_status: str = "0"
    trusted_proxy_cidrs: str = ""


def _env(name, default=""):
    return os.environ.get(name, default)


def load_config():
    """从 JWXT_* 环境变量读取配置，默认值与原来的 http.server 实现完全一致。"""
    return Config(
        host=_env("JWXT_HOST", "0.0.0.0"),
        port=int(_env("JWXT_PORT", "8766")),
        token=_env("JWXT_API_TOKEN", ""),
        redis_url=_env("JWXT_REDIS_URL", ""),
        session_ttl_hours=int(_env("JWXT_SESSION_TTL_HOURS", "1")),
        rate_limit=int(_env("JWXT_RATE_LIMIT", "600")),
        login_rate_limit=int(_env("JWXT_LOGIN_RATE_LIMIT", "60")),
        max_workers=int(_env("JWXT_MAX_WORKERS", "32")),
        thread_pool_size=int(_env("JWXT_THREAD_POOL_SIZE", "40")),
        warm_wait=int(_env("JWXT_WARM_WAIT", str(WARM_WAIT))),
        idle_timeout=int(_env("JWXT_IDLE_TIMEOUT", str(IDLE_TIMEOUT))),
        limit_concurrency=int(_env("JWXT_LIMIT_CONCURRENCY", str(LIMIT_CONCURRENCY))),
        upstream_sem_timeout=float(_env("JWXT_UPSTREAM_SEM_TIMEOUT", str(UPSTREAM_SEM_TIMEOUT))),
        max_semesters=int(_env("JWXT_MAX_SEMESTERS", str(MAX_SEMESTERS))),
        upstream_parallel=int(_env("JWXT_UPSTREAM_PARALLEL", "4")),
        upstream_global=int(_env("JWXT_UPSTREAM_GLOBAL", "8")),
        health_username=_env("JWXT_HEALTH_USERNAME", ""),
        health_password=_env("JWXT_HEALTH_PASSWORD", ""),
        health_interval=int(_env("JWXT_HEALTH_INTERVAL", "300")),
        grades_cache_ttl=int(_env("JWXT_GRADES_CACHE_TTL", "300")),
        schedule_cache_ttl=int(_env("JWXT_SCHEDULE_CACHE_TTL", "300")),
        schedule_pdf_cache_ttl=int(_env("JWXT_SCHEDULE_PDF_CACHE_TTL", "300")),
        schedule_pdf_cache_max_items=int(
            _env("JWXT_SCHEDULE_PDF_CACHE_MAX_ITEMS", "8")),
        schedule_pdf_prewarm=_env("JWXT_SCHEDULE_PDF_PREWARM", "1"),
        schedule_class_share=_env("JWXT_SCHEDULE_CLASS_SHARE", "0"),
        jump_cache_ttl=int(_env("JWXT_JUMP_CACHE_TTL", "30")),
        jump_code_ttl=int(_env("JWXT_JUMP_CODE_TTL", "60")),
        health_probe_interval=int(_env("JWXT_HEALTH_PROBE_INTERVAL", "10")),
        verify_tls=_env("JWXT_VERIFY_TLS", "1"),
        login_reuse=_env("JWXT_LOGIN_REUSE", "1"),
        login_reuse_max_age=int(_env("JWXT_LOGIN_REUSE_MAX_AGE", "1800")),
        login_reuse_probe=_env("JWXT_LOGIN_REUSE_PROBE", "1"),
        max_sessions=int(_env("JWXT_MAX_SESSIONS", "1000")),
        grades_cache_max_items=int(_env("JWXT_GRADES_CACHE_MAX_ITEMS", "1000")),
        schedule_cache_max_items=int(_env("JWXT_SCHEDULE_CACHE_MAX_ITEMS", "1000")),
        jump_cache_max_items=int(_env("JWXT_JUMP_CACHE_MAX_ITEMS", "1000")),
        public_url=_env("JWXT_PUBLIC_URL", ""),
        cors_origin=_env("JWXT_CORS_ORIGIN", ""),
        trust_proxy=_env("JWXT_TRUST_PROXY", "0"),
        allow_get_credentials=_env("JWXT_ALLOW_GET_CREDENTIALS", "1"),
        protect_login_status=_env("JWXT_PROTECT_LOGIN_STATUS", "0"),
        trusted_proxy_cidrs=_env("JWXT_TRUSTED_PROXY_CIDRS", ""),
        dns_fallback_ips=_env("JWXT_DNS_FALLBACK_IPS", ",".join(DNS_FALLBACK_IPS)),
        log_file=_env("JWXT_LOG_FILE", ""),
        log_level=_env("JWXT_LOG_LEVEL", "INFO"),
    )


def apply_config(cfg):
    """把配置写入进程级全局量（与原来的 http.server 实现保持一致）。"""
    global SESSION_TTL, UPSTREAM_PARALLEL, UPSTREAM_SEM, JUMP_CODE_TTL, VERIFY_TLS
    global LOGIN_REUSE, LOGIN_REUSE_MAX_AGE, LOGIN_REUSE_PROBE
    global SCHEDULE_CLASS_SHARE, MAX_SESSIONS, PUBLIC_URL, CORS_ORIGIN
    global TRUST_PROXY, DNS_FALLBACK_IPS, UPSTREAM_SEM_TIMEOUT, MAX_SEMESTERS
    global WARM_WAIT
    global UPSTREAM_GLOBAL
    global CORS_ORIGINS, ALLOW_GET_CREDENTIALS, PROTECT_LOGIN_STATUS, TRUSTED_PROXY_CIDRS
    SESSION_TTL = max(300, cfg.session_ttl_hours * 3600)
    UPSTREAM_PARALLEL = max(1, cfg.upstream_parallel)
    UPSTREAM_GLOBAL = max(1, cfg.upstream_global)
    UPSTREAM_SEM = threading.BoundedSemaphore(UPSTREAM_GLOBAL)
    if _UPSTREAM_EXECUTOR is not None:
        LOG.warning("上游并发配置已在运行后变更：共享线程池大小不会动态调整，请重启生效")
    UPSTREAM_SEM_TIMEOUT = max(0.5, float(getattr(cfg, "upstream_sem_timeout", 5.0)))
    MAX_SEMESTERS = max(1, int(getattr(cfg, "max_semesters", 20)))
    WARM_WAIT = max(1, int(getattr(cfg, "warm_wait", 10)))
    JUMP_CODE_TTL = max(5, cfg.jump_code_ttl)
    VERIFY_TLS = _to_bool(cfg.verify_tls)
    LOGIN_REUSE = _to_bool(cfg.login_reuse)
    LOGIN_REUSE_MAX_AGE = max(60, cfg.login_reuse_max_age)
    LOGIN_REUSE_PROBE = _to_bool(cfg.login_reuse_probe)
    SCHEDULE_CLASS_SHARE = _to_bool(cfg.schedule_class_share)
    MAX_SESSIONS = max(10, cfg.max_sessions)
    PUBLIC_URL = (cfg.public_url or "").strip().rstrip("/")
    CORS_ORIGIN = (cfg.cors_origin or "").strip()
    CORS_ORIGINS = [x.strip() for x in CORS_ORIGIN.split(",") if x.strip()]
    TRUST_PROXY = _to_bool(cfg.trust_proxy)
    ALLOW_GET_CREDENTIALS = _to_bool(getattr(cfg, "allow_get_credentials", "1"))
    PROTECT_LOGIN_STATUS = _to_bool(getattr(cfg, "protect_login_status", "0"))
    TRUSTED_PROXY_CIDRS = []
    raw_proxies = (getattr(cfg, "trusted_proxy_cidrs", "") or "").strip()
    for part in raw_proxies.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            TRUSTED_PROXY_CIDRS.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            LOG.warning("忽略无效的受信代理网段: %s", part)
    ips = [x.strip() for x in (cfg.dns_fallback_ips or "").split(",") if x.strip()]
    if ips:
        DNS_FALLBACK_IPS = ips
    if not VERIFY_TLS:
        urllib3.disable_warnings()
    # 同步按值导入到 jwxt_http / jwxt_state 的可变配置副本，
    # 避免 make_app(自定义配置) 后 HTTP 层仍读到模块导入时的旧值
    try:
        import jwxt_http as _http_mod
        _http_mod.ALLOW_GET_CREDENTIALS = ALLOW_GET_CREDENTIALS
        _http_mod.PROTECT_LOGIN_STATUS = PROTECT_LOGIN_STATUS
        _http_mod.TRUST_PROXY = TRUST_PROXY
        _http_mod.SESSION_TTL = SESSION_TTL
        _http_mod.MAX_SESSIONS = MAX_SESSIONS
        _http_mod.JUMP_CODE_TTL = JUMP_CODE_TTL
        _http_mod.LOGIN_REUSE = LOGIN_REUSE
        _http_mod.LOGIN_REUSE_MAX_AGE = LOGIN_REUSE_MAX_AGE
        _http_mod.LOGIN_REUSE_PROBE = LOGIN_REUSE_PROBE
    except Exception:
        pass
    try:
        import jwxt_state as _state_mod
        _state_mod.WARM_WAIT = WARM_WAIT
        _state_mod.LOGIN_REUSE = LOGIN_REUSE
    except Exception:
        pass
