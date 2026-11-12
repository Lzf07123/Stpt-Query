"""首查预热的错误语义：瞬时失败必须与会话失效区分。"""
from __future__ import annotations

import json
from types import SimpleNamespace

import requests
import pytest

from app import jwxt_http, jwxt_state
from app.jwxt_core import (
    SessionInvalidError,
    TokenError,
    WarmPendingError,
    _mask_body,
    _sanitize_url,
)
from app.jwxt_http import Handler, PoolFullError
from app.jwxt_state import SessionStore, health_payload


class FakeSessions:
    def __init__(self, record):
        self.record = record

    def get(self, sid):
        return self.record


class FakeUpstream:
    def __init__(self, error):
        self.error = error

    def jwxt(self, *_args):
        return None

    def student(self):
        raise self.error

    def close(self):
        return None


def test_transient_warm_failure_preserves_session(monkeypatch):
    record = {"portal": {}, "cookies": {}, "username": "2023000001"}
    sessions = FakeSessions(record)
    monkeypatch.setattr(
        jwxt_state, "_session_j",
        lambda *args, **kwargs: FakeUpstream(requests.ConnectionError("upstream")))

    with pytest.raises(WarmPendingError) as exc_info:
        jwxt_state._ensure_warmed(sessions, "sid", record)

    assert sessions.get("sid") is record
    assert str(exc_info.value) == "会话预热门户暂未完成，请稍后重试"


def test_auth_warm_failure_remains_session_invalid(monkeypatch):
    record = {"portal": {}, "cookies": {}, "username": "2023000001"}
    sessions = FakeSessions(record)
    monkeypatch.setattr(
        jwxt_state, "_session_j",
        lambda *args, **kwargs: FakeUpstream(SessionInvalidError("学校端会话已失效")))

    with pytest.raises(TokenError):
        jwxt_state._ensure_warmed(sessions, "sid", record)


def test_short_codes_are_redacted_from_logs():
    sanitized = _sanitize_url("/get_schedule/export?code=abc123&semester=2025-2026-1")
    assert sanitized == "/get_schedule/export?code=***&semester=2025-2026-1"
    assert json.loads(_mask_body('{"code":"abc123"}')) == {"code": "***"}


def test_tgt_in_url_path_is_masked():
    """mint_st 把 CAS TGT 放进 URL 路径段，DEBUG 日志必须整段掩码。"""
    url = ("https://cas.stpt.edu.cn/lyuapServer/v1/tickets/"
           "TGT-abc123-secret?vpn-12-cas.stpt.edu.cn")
    sanitized = _sanitize_url(url)
    assert "TGT-abc123-secret" not in sanitized
    assert sanitized == ("https://cas.stpt.edu.cn/lyuapServer/v1/tickets/"
                         "***?vpn-12-cas.stpt.edu.cn")
    # 无 TGT 的票据端点与普通路径不受影响
    assert _sanitize_url("https://cas.stpt.edu.cn/lyuapServer/v1/tickets?a=1") == \
        "https://cas.stpt.edu.cn/lyuapServer/v1/tickets?a=1"
    assert _sanitize_url("/x/v1/tickets/TGT-1/sub?k=v") == "/x/v1/tickets/***/sub?k=v"


def test_session_and_token_in_body_are_fully_masked():
    masked = json.loads(_mask_body(
        '{"session":"abcdef0123456789","token":"tok-secret","password":"pw"}'))
    assert masked == {"session": "***", "token": "***", "password": "***"}


def test_non_ascii_bearer_is_unauthorized_not_500():
    """Bearer 值含非 ASCII 字符时按未认证处理，不再抛 TypeError/500。"""
    handler = Handler()
    handler.server = SimpleNamespace(token="real-token")
    handler.headers = {"Authorization": "Bearer 密码-token-é"}
    handler.path = "/login"
    handler.client_address = ("127.0.0.1", 0)
    assert handler._ok() is False
    # 正确令牌仍放行；UTF-8 编码比较恒时且对任意输入安全
    handler.headers = {"Authorization": "Bearer real-token"}
    assert handler._ok() is True


def test_pool_full_skip_warm_still_sets_event(monkeypatch):
    """后台队列满跳过预热时也必须置位 warm_evt，否则首查固定空等 WARM_WAIT。"""
    class FakeJ:
        def __init__(self, u, p):
            self.u, self.p = u, p
            self.s = SimpleNamespace(cookies=[])  # dump_session_cookies 读取
            self.stu = None                       # dump_portal 读取

        def login(self, verify=True):
            return None

        def close(self):
            return None

    handler = Handler()
    handler.server = SimpleNamespace(sessions=SessionStore(3600))
    monkeypatch.setattr(jwxt_http, "LOGIN_REUSE", False)
    monkeypatch.setattr(jwxt_http, "J", FakeJ)
    monkeypatch.setattr(
        jwxt_http, "_run_upstream",
        lambda j, jobs, max_workers=None: {"verify": ("ok", None),
                                           "apps": ("ok", [])})

    class FullPool:
        def submit(self, *args, **kwargs):
            raise PoolFullError("后台任务队列已满")

    monkeypatch.setattr(jwxt_http, "_background_executor", lambda: FullPool())

    sid, meta = handler._login_session("2023000001", "pw")
    assert meta["reused"] is False
    rec = handler.server.sessions.get(sid)
    assert rec is not None
    assert rec["warm_evt"].is_set()


def test_health_payload_cold_redis_cache_returns_placeholder():
    """Redis 冷缓存时 /health 不做同步探测，返回未知占位交给后台线程。"""
    class FakeBackend:
        def __init__(self):
            self.probed = 0

        def probe(self):
            self.probed += 1
            return False

        def status_info(self, now=None):
            return {"ok": False, "at": 0, "mode": "redis", "degraded": True}

    state = jwxt_state.ServerState()
    backend = FakeBackend()
    server = SimpleNamespace(_redis_backend=backend, health_credentials=None)
    payload = health_payload(state, server)

    assert payload["redis"] == {"ok": None, "mode": "redis", "starting": True}
    assert backend.probed == 0  # 冷缓存绝不同步探测
    assert payload["status"] == "ok"  # 状态未知不拉低健康
    assert state.redis is None  # 仍交给后台探测线程首轮填充


class _FakeTCPSocket:
    def __init__(self, *args, **kwargs):
        pass

    def settimeout(self, timeout):
        pass

    def setsockopt(self, *args, **kwargs):
        pass

    def connect(self, sa):
        pass

    def close(self):
        pass


def test_dns_fallback_connects_via_ip_for_school_host(monkeypatch):
    """学校域名常规 DNS 失败时改用兜底 IP 建连；原域名保持不变（SNI/Host）。"""
    import socket as _socket

    from app import jwxt_core

    real_gai = _socket.getaddrinfo

    def gai(host, port, *args, **kwargs):
        if host == "sec.stpt.edu.cn":
            raise _socket.gaierror(8, "nodename nor servname provided")
        if host == "203.0.113.10":
            return [(_socket.AF_INET, _socket.SOCK_STREAM, 6, "",
                     ("203.0.113.10", port))]
        return real_gai(host, port, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", gai)
    monkeypatch.setattr(_socket, "socket", _FakeTCPSocket)
    monkeypatch.setattr(jwxt_core, "DNS_FALLBACK_IPS", ["203.0.113.10"])

    conn = jwxt_core._DnsFallbackHTTPSConnection("sec.stpt.edu.cn", port=443)
    sock = conn._new_conn()

    assert isinstance(sock, _FakeTCPSocket)
    assert conn._dns_host == "sec.stpt.edu.cn"  # 兜底后恢复原域名：SNI/证书校验/Host 不变


def test_dns_fallback_not_applied_to_other_hosts(monkeypatch):
    """非学校主机 DNS 失败不触发兜底（保持 fail-fast，不影响其它域名）。"""
    import socket as _socket

    from app import jwxt_core
    from urllib3.exceptions import NameResolutionError

    real_gai = _socket.getaddrinfo

    def gai(host, port, *args, **kwargs):
        if host == "api.example.com":
            raise _socket.gaierror(8, "nodename nor servname provided")
        return real_gai(host, port, *args, **kwargs)

    monkeypatch.setattr(_socket, "getaddrinfo", gai)
    monkeypatch.setattr(_socket, "socket", _FakeTCPSocket)
    monkeypatch.setattr(jwxt_core, "DNS_FALLBACK_IPS", ["203.0.113.10"])

    conn = jwxt_core._DnsFallbackHTTPSConnection("api.example.com", port=443)
    with pytest.raises((_socket.gaierror, NameResolutionError)):
        conn._new_conn()


def test_shared_adapter_installs_dns_fallback_https_pool():
    """共享池适配器把 HTTPS 池替换为支持 DNS 兜底的池类；HTTP 池保持默认。"""
    from app import jwxt_core

    adapter = jwxt_core._SharedPoolAdapter(
        pool_connections=2, pool_maxsize=2)
    mapping = adapter.poolmanager.pool_classes_by_scheme
    assert mapping["https"] is jwxt_core._DnsFallbackHTTPSConnectionPool
    assert mapping["http"] is jwxt_core.HTTPConnectionPool
    assert mapping["https"].ConnectionCls is jwxt_core._DnsFallbackHTTPSConnection
    adapter.close()  # no-op，不应报错
