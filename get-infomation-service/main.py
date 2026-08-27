#!/usr/bin/env python3
"""兼容入口：聚合 jwxt_core / jwxt_state / jwxt_http，保持原有导入路径与懒加载 app。

业务实现已按职责拆分：
- jwxt_core：常量、专用异常、上游客户端、查询/渲染、配置
- jwxt_state：会话/缓存/限流/短码、健康与指标
- jwxt_http：Handler/FastHandler、FastAPI 应用工厂、CLI
"""
import jwxt_core as _core
import jwxt_state as _state
import jwxt_http as _http

from jwxt_core import (
    ALLOW_GET_CREDENTIALS, BASE, CORS_ORIGIN, CORS_ORIGINS, DNS_FALLBACK_IPS,
    IDLE_TIMEOUT, JUMP_CODE_TTL, KNOWN_PATHS, LIMIT_CONCURRENCY, LOG,
    LOGIN_LOCK_TIMEOUT, LOGIN_REUSE, LOGIN_REUSE_MAX_AGE, LOGIN_REUSE_PROBE,
    MAX_BODY, MAX_FINALS_PAGES, MAX_SEMESTERS, MAX_SESSIONS, MAX_UNI_PAGES,
    MGMT_SSO_SERVICE, NEG_CACHE_TTL, PROTECT_LOGIN_STATUS, PUBLIC_URL,
    SCHEDULE_CLASS_SHARE, SESSION_TTL, SERVICE, TIMEOUT, TRUST_PROXY,
    TRUSTED_PROXY_CIDRS, UA, UPSTREAM_GLOBAL, UPSTREAM_PARALLEL, UPSTREAM_SEM,
    UPSTREAM_SEM_TIMEOUT, VERIFY_TLS, VERSION, WARM_WAIT, Config, J,
    SchoolError, SessionInvalidError, TokenError, UpstreamBusyError,
    UpstreamError, LockTimeoutError, PoolFullError, dump_cookies,
    dump_portal, dump_session_cookies, load_cookies, main, main_schedule,
    public_url, setup_logging, set_trace_id, to_webvpn, trace_id,
    _DaemonPool, _background_executor, _cache_put, _cache_write_allowed,
    _clone_j, _cors_allow, _deep_executor, _env, _has_session_hint,
    _internal_error_body, _is_auth_failure, _is_transient_error, _mask_body,
    _md_cell, _norm_num, _norm_semesters_key, _odd_or_double_error, _page_total,
    _parse_weeks, _pwd_hash, _run_upstream, _sanitize_url, _schedule_cache_key,
    _schedule_export_suffix, _take, _to_bool, _to_int, _trace_local,
    _trusted_proxy_ok, _upstream_executor, _upstream_slot, _weeks_param_error,
)
from jwxt_state import (
    BodyTooLarge, ConcurrencyLimiter, JumpCodeStore, KeyedLocks, RateLimiter,
    ServerState, SessionStore, ShortCodeStore, TTLCache, _ensure_warmed,
    _fetch_tgt, _health_probe_loop, _jump_target, _metrics_text, _probe_session,
    _restore_portal, _session_j, _warm_background, deep_check, health_payload,
    mint_st, probe_school, query_with_session, with_session_j,
)
from jwxt_http import (
    ApiError, FastHandler, Handler, Runtime, _api_error_headers,
    _apply_thread_pool_size, _client_ip, _docs_patch, _export_code_or_token_required,
    _general_rate_limit, _get_runtime, _make_lifespan, _parser,
    _read_limited_body, _raw_target, _start_background, _sync_trace,
    _token_required_for, make_app, run,
)

_APP = None


def __getattr__(name):
    """懒加载全局 app（与旧实现一致，测试/工具导入时不启动后台线程）。"""
    if name == "app":
        global _APP
        if _APP is None:
            _APP = make_app()
        return _APP
    raise AttributeError(name)


if __name__ == "__main__":
    run()
