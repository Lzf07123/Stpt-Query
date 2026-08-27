#!/usr/bin/env python3
"""HTTP/FastAPI 适配层：Handler、FastHandler、应用工厂与 CLI。"""

import argparse
import base64
import gzip
import hashlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional, Union
from urllib.parse import quote, unquote, urlencode, urlparse

import uvicorn
from fastapi import Depends, FastAPI, Request, Response, Security
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

import jwxt_core as _core_mod
import jwxt_redis as _redis_mod
from jwxt_core import (
    ALLOW_GET_CREDENTIALS, BASE, CORS_ORIGIN, DNS_FALLBACK_IPS, IDLE_TIMEOUT,
    JUMP_CODE_TTL, KNOWN_PATHS, LIMIT_CONCURRENCY, LOG, LOGIN_REUSE,
    LOGIN_LOCK_TIMEOUT, LOGIN_REUSE_MAX_AGE, LOGIN_REUSE_PROBE, MAX_BODY, MAX_SEMESTERS,
    MAX_SESSIONS, MGMT_SSO_SERVICE, NEG_CACHE_TTL, PROTECT_LOGIN_STATUS,
    PUBLIC_URL, SCHEDULE_CLASS_SHARE, SESSION_TTL, SERVICE, TIMEOUT, TRUST_PROXY,
    UPSTREAM_PARALLEL, UPSTREAM_SEM_TIMEOUT, VERSION, WARM_WAIT, Config, J,
    SchoolError, SessionInvalidError, TokenError, UpstreamError,
    UpstreamBusyError, LockTimeoutError, PoolFullError, apply_config, load_config,
    main, main_schedule, public_url, to_webvpn, _background_executor, _cache_put,
    _cache_write_allowed, _clone_j, _cors_allow, _deep_executor, _env, _internal_error_body,
    _is_auth_failure, _is_transient_error, _mask_body, _md_cell, _norm_num,
    _norm_semesters_key, _odd_or_double_error, _parse_weeks, _pwd_hash,
    _run_upstream, _sanitize_url, _schedule_cache_key, _schedule_export_suffix,
    _take, _to_bool, _to_int, _trace_local, _trusted_proxy_ok, _weeks_param_error,
    dump_cookies, dump_session_cookies, dump_portal, load_cookies,
    _DaemonPool, _upstream_executor, setup_logging, set_trace_id, trace_id,
)
from jwxt_state import (
    ServerState, SessionStore, BodyTooLarge, KeyedLocks, ConcurrencyLimiter,
    RateLimiter, TTLCache, ShortCodeStore, JumpCodeStore, _restore_portal,
    _ensure_warmed, _session_j, query_with_session, with_session_j,
    _probe_session, _warm_background, _jump_target, _fetch_tgt, mint_st,
    probe_school, deep_check, _health_probe_loop, health_payload, _metrics_text,
)

class Handler:  # 请求逻辑基类（不再依赖 http.server，FastHandler 提供 FastAPI 适配）

    def _cors_headers(self):
        """按配置返回 CORS 响应头；未配置时不返回任何 CORS 头（默认后端调用场景）。"""
        origin = (self.headers.get("Origin") or "").strip()
        allow = _cors_allow(origin)
        if allow is None:
            return []
        return [
            ("Access-Control-Allow-Origin", allow),
            ("Access-Control-Allow-Headers", "Content-Type, Authorization"),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Expose-Headers", "X-Trace-Id"),
        ]

    def _rate_ok(self):
        ip = self._client_ip()
        if not self.server.rate_limiter.allow(ip):
            LOG.warning("触发限流 client=%s path=%s", ip, urlparse(self.path).path)
            self._reply(429, {"success": False, "error": "请求过于频繁，请稍后再试",
                              "output": "", "count": 0, "rows": []})
            return False
        return True

    def _login_rate_ok(self):
        ip = self._client_ip()
        if not self.server.login_limiter.allow(ip):
            LOG.warning("登录接口触发限流 client=%s", ip)
            self._reply(429, {"success": False, "error": "登录尝试过于频繁，请稍后再试",
                              "output": "", "count": 0, "rows": []})
            return False
        return True

    def _login_session(self, u, pw):
        """返回可复用的会话 (sid, meta)。调用方必须已持有 login_locks。

        优先复用 同用户名+密码摘要 的近期有效会话；否则完整登录并创建会话、
        后台预热门户。新登录/复用探测失败时抛异常，由调用方决定错误语义。
        """
        reuse_start = time.time()
        if LOGIN_REUSE:
            reuse = self.server.sessions.find_reusable(
                u, _pwd_hash(u, pw), LOGIN_REUSE_MAX_AGE)
            if reuse:
                _c = self.server.sessions.get(reuse)
                if _c is not None:
                    if LOGIN_REUSE_PROBE:
                        if not _probe_session(self.server.sessions, reuse):
                            LOG.warning("复用会话探测失效，重新登录 username=%s", u)
                            reuse = None
                        else:
                            _c = self.server.sessions.get(reuse)
                            if _c is None:
                                reuse = None
                    if reuse:
                        _c = self.server.sessions.get(reuse)
                        if _c is not None:
                            return reuse, {
                                "reused": True,
                                "probed": bool(LOGIN_REUSE_PROBE),
                                "latency_ms": int((time.time() - reuse_start) * 1000),
                                "expires_in": max(0, int(_c["exp"] - time.time())),
                                "expires_at": int(_c["exp"]),
                            }
        j = J(u, pw)
        try:
            # 完成 WebVPN/CAS 登录；登录校验（getHomeParam）推迟到下一步，
            # 与 getAppList 并行执行（省 1 个往返）
            j.login(verify=False)
            res = _run_upstream(j, [
                ("verify", lambda jj: jj.verify()),
                ("apps", lambda jj: jj.get_app_list()),
            ], max_workers=2)
            _take(res, "verify")
            apps = _take(res, "apps")
        except Exception:
            j.close()
            raise
        evt = threading.Event()
        sid = self.server.sessions.create(
            u, dump_session_cookies(j), dump_portal(j),
            warm_evt=evt, pwd_hash=_pwd_hash(u, pw))
        try:
            _background_executor().submit(
                _warm_background, self.server.sessions, sid, j, evt, apps,
                getattr(self, "trace_id", "-"))
        except PoolFullError:
            # 队列满时跳过预热：会话仍可用，首次查询会按需同步补齐
            LOG.warning("后台任务队列已满，跳过会话预热 session=%s***", str(sid)[:6])
            j.close()
        return sid, {
            "reused": False,
            "probed": False,
            "latency_ms": int((time.time() - reuse_start) * 1000),
            "expires_in": SESSION_TTL,
            "expires_at": int(time.time()) + SESSION_TTL,
        }

    def _reply_login_error(self, e):
        """登录阶段错误的统一响应：瞬时 502、学校业务/认证失败 401、内部异常 500。"""
        if _is_transient_error(e):
            LOG.warning("教务登录瞬时失败 error=%s", e)
            self._reply(502, {"success": False,
                              "error": "教务系统登录暂时不可用，请稍后重试",
                              "output": "", "count": 0, "rows": []})
            return
        if _is_auth_failure(e) or isinstance(e, SchoolError):
            LOG.warning("教务登录失败 error=%s", e)
            self._reply(401, {"success": False,
                              "error": "教务系统登录失败：%s" % e,
                              "output": "", "count": 0, "rows": []})
            return
        LOG.exception("教务登录内部异常 error=%s", e)
        self._reply(500, _internal_error_body())

    def _finish(self, extra=""):
        ms = (time.time() - self._t0) * 1000
        st = getattr(self, "_status", "?")
        LOG.info("请求完成 status=%s duration_ms=%.0f %s", st, ms, extra)
        try:
            state = getattr(getattr(self, "server", None), "state", None)
            if state is not None:
                endpoint = urlparse(self.path).path
                code = getattr(self, "_status", 0) or 0
                state.record(endpoint, 200 <= code < 400, ms,
                             getattr(self, "_cache_hit", False), status_code=code)
        except Exception:
            pass

    def _reply(self, code, obj, body=None):
        data = body if body is not None else json.dumps(obj, ensure_ascii=False).encode("utf-8")
        enc = (self.headers.get("Accept-Encoding") or "").lower()
        compressed = False
        if "gzip" in enc and len(data) >= 512:
            data = gzip.compress(data, 5)
            compressed = True
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        for k, v in self._cors_headers():
            self.send_header(k, v)
        self.send_header("X-Trace-Id", getattr(self, "trace_id", "-"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in getattr(self, "_extra_headers", []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        self._finish("bytes=%d" % len(data))

    def _reply_text(self, code, text):
        """纯文本响应：/jump 默认直接返回可跳转的 URL 本体。"""
        data = text.encode("utf-8")
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self._cors_headers():
            self.send_header(k, v)
        self.send_header("X-Trace-Id", getattr(self, "trace_id", "-"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)
        self._finish("bytes=%d" % len(data))

    def _reply_html(self, code, html):
        """HTML 响应：/jump/go 免密桥接页。"""
        data = html.encode("utf-8")
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in self._cors_headers():
            self.send_header(k, v)
        self.send_header("X-Trace-Id", getattr(self, "trace_id", "-"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)
        self._finish("bytes=%d" % len(data))

    def _reply_file(self, data, filename, ctype="application/msword"):
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        # 中文文件名用 RFC 5987 编码，避免非 ASCII 响应头导致下载端解析失败
        self.send_header("Content-Disposition",
                         "attachment; filename=\"schedule.doc\"; filename*=UTF-8''%s" % quote(filename))
        for k, v in self._cors_headers():
            self.send_header(k, v)
        self.send_header("X-Trace-Id", getattr(self, "trace_id", "-"))
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in getattr(self, "_extra_headers", []):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)
        self._finish("bytes=%d" % len(data))

    def _ok(self):
        token = self.server.token
        ok = not token or self.headers.get("Authorization", "") == "Bearer " + token
        if not ok:
            LOG.warning("未授权访问 path=%s client=%s",
                        urlparse(self.path).path,
                        self.client_address[0] if self.client_address else "-")
        return ok

    def do_OPTIONS(self):
        self._begin()
        self._reply(200, {})

    def _health(self, state):
        return health_payload(state, self.server)

    def _deep_throttled(self, state, min_gap=30):
        now = time.time()
        with state.lock:
            last_at = state.last_deep_start or 0
            deep = dict(state.deep) if state.deep else None
        if now - last_at < min_gap and deep is not None:
            deep["cached"] = True
            return deep
        with state.lock:
            state.last_deep_start = now
        return None

    def _deep(self, u, pw):
        if not self._ok():
            self._reply(401, {"success": False, "error": "token required",
                              "output": "", "count": 0, "rows": []})
            return
        if not self._login_rate_ok():
            return
        state = self.server.state
        if not u or not pw:
            self._reply(400, {"success": False,
                              "error": "username/password 必填：GET /health/deep?username=..&password=.. 或 POST JSON"})
            return
        cached = self._deep_throttled(state)
        if cached is not None:
            self._reply(200, cached)
            return
        holder = {}
        def runner():
            set_trace_id(getattr(self, "trace_id", "-"))
            holder["r"] = deep_check(u, pw, state)
        fut = _deep_executor().submit(runner)
        try:
            fut.result(timeout=40)
        except TimeoutError:
            self._reply(200, {"checked_at": int(time.time()), "ok": False,
                              "latency_ms": 40000, "stages": [], "semester": "",
                              "error": "真实探测超过 40s 未完成，结果稍后自动刷新", "username": u})
            return
        except Exception as e:
            LOG.exception("深度探测异常: %s", e)
            self._reply(500, _internal_error_body())
            return
        self._reply(200, holder["r"])

    def _export(self, u, pw, session, semester, weeks, odd, code=""):
        state = self.server.state
        err = _weeks_param_error(weeks) or _odd_or_double_error(odd)
        if err:
            self._reply(400, {"success": False, "error": err,
                              "output": "", "count": 0, "rows": []})
            return
        weeks = _parse_weeks(weeks)
        if code:
            # 下载码先校验不消费：导出成功后再删除，失败可重试
            sid = self.server.export_codes.resolve(code, consume=False)
            if not sid:
                self._reply(401, {"success": False,
                                  "error": "下载链接已失效或过期，请重新查询课表获取新链接",
                                  "output": "", "count": 0, "rows": []})
                return
            session = sid

        def run(j):
            sem2 = (semester or "").strip() or j.current()
            r = j.export_schedule(sem2, weeks, odd)
            return sem2, r.content

        try:
            if session:
                sem2, data = with_session_j(self.server.sessions, session, run)
            else:
                if not u or not pw:
                    self._reply(400, {"success": False,
                                      "error": "需携带认证信息：GET 用 ?code=.. 或 ?session=..，"
                                               "POST body 用 username/password 或 session",
                                      "output": "", "count": 0, "rows": []})
                    return
                if not self._login_rate_ok():
                    return
                try:
                    with self.server.login_locks.lock(u, timeout=LOGIN_LOCK_TIMEOUT):
                        session, _ = self._login_session(u, pw)
                except LockTimeoutError:
                    LOG.warning("登录锁等待超时 endpoint=/get_schedule/export username=%s", u)
                    self._reply(503, {"success": False,
                                      "error": "登录处理繁忙，请稍后重试",
                                      "output": "", "count": 0, "rows": []})
                    return
                except Exception as e:
                    self._reply_login_error(e)
                    return
                sem2, data = with_session_j(self.server.sessions, session, run)
            with self.server.state.lock:
                self.server.state.last_real = {"at": int(time.time()),
                                               "endpoint": "/get_schedule/export",
                                               "ok": True, "count": len(data)}
            self._reply_file(data, "学生课表_%s.doc" % sem2)
            if code:
                # 导出成功后再一次性消费，避免瞬时失败烧掉可重试的链接
                self.server.export_codes.resolve(code, consume=True)
        except Exception as e:
            if isinstance(e, TokenError):
                LOG.warning("课表导出令牌无效: %s", e)
                if session:
                    self.server.sessions.invalidate(session)
                self._reply(401, {"success": False,
                                  "error": str(e),
                                  "output": "", "count": 0, "rows": []})
                return
            if _is_auth_failure(e):
                LOG.warning("课表导出认证失败 endpoint=/get_schedule/export error=%s", e)
                if session:
                    self.server.sessions.invalidate(session)
                if code:
                    # 会话已失效，code 继续留存的唯一结果是反复 401：一并消费删除
                    self.server.export_codes.resolve(code, consume=True)
                self._reply(401, {"success": False,
                                  "error": "教务系统登录已失效，请重新获取下载链接或登录",
                                  "output": "", "count": 0, "rows": []})
                return
            if _is_transient_error(e):
                LOG.warning("课表导出上游失败 endpoint=/get_schedule/export error=%s", e)
                self._reply(502, {"success": False,
                                  "error": "学校课表导出暂不可用，请稍后重试",
                                  "output": "", "count": 0, "rows": []})
                return
            if isinstance(e, SchoolError):
                # 学校业务错误（如返回业务 code）按上游失败处理，不再误报 500
                LOG.warning("课表导出业务失败 endpoint=/get_schedule/export error=%s", e)
                self._reply(502, {"success": False,
                                  "error": "学校课表导出失败，请稍后重试",
                                  "output": "", "count": 0, "rows": []})
                return
            LOG.exception("课表导出失败 endpoint=/get_schedule/export")
            self._reply(500, _internal_error_body())

    def _jump_run(self, j, page, verify):
        url = _jump_target(j.stu, page, getattr(j, "stu_frag", None))
        out = {"url": url, "warm_url": BASE + "/"}
        try:
            st = mint_st(j, SERVICE)
            out["login_url"] = BASE + "/rump_frontend/loginFromCas/?ticket=" + quote(st)
            out["ticket_ok"] = True
        except Exception as e:
            LOG.warning("免密跳转票据签发失败: %s", e)
            out["ticket_ok"] = False
            out["ticket_error"] = "%s: %s" % (type(e).__name__, e)
            out["auth_failed"] = _is_auth_failure(e)
        if out.get("ticket_ok") and getattr(j, "mgmt", None):
            try:
                st2 = mint_st(j, MGMT_SSO_SERVICE)
                out["app_url"] = (j.mgmt + "api/cas/login?pattern=manager-login&ticket=" + quote(st2))
            except Exception as e:
                LOG.warning("教务系统免密票据签发失败: %s", e)
                out["app_error"] = "%s: %s" % (type(e).__name__, e)
        elif out.get("ticket_ok"):
            out["app_error"] = "会话缺少教务系统地址（请重新 POST /login）"
        if verify:
            try:
                r = j.req("GET", url, allow_redirects=True, timeout=TIMEOUT)
                out["verify"] = {"ok": r.status_code < 400,
                                 "status": r.status_code,
                                 "final_url": _sanitize_url(r.url)}
            except Exception as e:
                out["verify"] = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
        return out

    def _jump(self, session, page, redirect, json_mode, verify, warm=False):
        """复用 session 免认证直达教务系统：用会话 TGT 签发两张一次性 CAS 票据——
        login_url 完成 WebVPN 门户免密登录，app_url 完成教务系统 SSO 免密登录，
        全程不弹密码框。默认直接返回 login_url（纯文本）；json=1 返回结构化 JSON
        （url 目标页 / login_url 门户免密入口 / app_url 教务系统免密入口 / warm_url
        预热地址 / note 使用说明）；redirect=1 直接 302 到 login_url（warm=1 时 302
        到 warm_url，供全新设备先取 WebVPN 会话 Cookie）；verify=1 用会话 Cookie
        实请求目标页，不可达或无法换票时返回 502，避免给出需要重新认证的链接。

        安全说明：返回的中转链接（/jump/go?code=..）不含 session，只含随机短时跳转码
        （默认 60 秒有效，JWXT_JUMP_CODE_TTL 可调），过期后需重新调用 /jump 获取新码；
        跳转码仅能用于 /jump/go 免密跳转，不能调用任何数据接口。每次调用为校验 TGT
        而签的两张票据签完即弃。默认（verify=0）对「该 session 可免密签票」的结论做短
        TTL 缓存（JWXT_JUMP_CACHE_TTL，默认 30 秒，0 关闭），命中时跳过两次上游签票、
        直接返回链接；票据本身不缓存，点击 /jump/go 时仍实时签发，安全语义不变。
        verify=1 绕过缓存做真实可达性检查。"""
        page_name = (page or "home").strip() or "home"
        if not session:
            msg = "需携带认证信息：GET 用 ?session=..，POST body 用 session"
            if json_mode:
                self._reply(400, {"success": False, "error": msg,
                                  "url": "", "page": page_name, "count": 0, "rows": []})
            else:
                self._reply_text(400, msg)
            return
        try:
            if verify:
                res = with_session_j(self.server.sessions, session,
                                     lambda j: self._jump_run(j, page, True))
            else:
                cache = getattr(self.server, "jump_cache", None)
                key = "jump|" + session
                hit = cache.get(key) if cache is not None else None
                if hit is not None and hit.get("ok"):
                    self._cache_hit = True
                    # 缓存命中：跳过两次上游签票校验，只重建页面 URL（本地操作）。
                    # 缓存只存“可签票”结论，不存任何票据/中转 URL。
                    def build(j):
                        return {"url": _jump_target(j.stu, page, getattr(j, "stu_frag", None)),
                                "warm_url": BASE + "/",
                                "ticket_ok": True,
                                "app_ok": bool(hit.get("app"))}
                    res = with_session_j(self.server.sessions, session, build)
                else:
                    res = with_session_j(self.server.sessions, session,
                                         lambda j: self._jump_run(j, page, False))
                    if res.get("ticket_ok") and cache is not None:
                        cache.set(key, {"ok": True, "app": bool(res.get("app_url"))})
        except ValueError as e:
            if json_mode:
                self._reply(400, {"success": False, "error": str(e),
                                  "url": "", "page": page_name, "count": 0, "rows": []})
            else:
                self._reply_text(400, str(e))
            return
        except TokenError as e:
            LOG.warning("跳转会话无效: %s", e)
            # 会话已不可用：清除会话与跳转缓存，使下次 /login 重新签发新会话
            self.server.sessions.invalidate(session)
            cache = getattr(self.server, "jump_cache", None)
            if cache is not None:
                cache.delete("jump|" + session)
            msg = str(e)
            if json_mode:
                self._reply(401, {"success": False, "error": msg,
                                  "url": "", "page": page_name, "count": 0, "rows": []})
            else:
                self._reply_text(401, msg)
            return
        if not res.get("ticket_ok"):
            # TGT 无法签发票据：认证类失败清除死会话，避免 LOGIN_REUSE 反复返回
            # 死会话；瞬时网络故障保留会话，仅废弃跳转缓存。
            if res.get("auth_failed"):
                self.server.sessions.invalidate(session)
            cache = getattr(self.server, "jump_cache", None)
            if cache is not None:
                cache.delete("jump|" + session)
            msg = "免密跳转票据签发失败，无法给出免认证链接：%s" % res.get("ticket_error")
            with self.server.state.lock:
                self.server.state.last_real = {"at": int(time.time()),
                                               "endpoint": "/jump",
                                               "ok": False, "count": 1,
                                               "page": page_name, "error": msg}
            if json_mode:
                self._reply(502, {"success": False, "error": msg,
                                  "url": res["url"], "page": page_name,
                                  "ticket_error": res.get("ticket_error"),
                                  "count": 0, "rows": []})
            else:
                self._reply_text(502, msg)
            return
        if verify and not res.get("verify", {}).get("ok", True):
            v = res["verify"]
            msg = "目标页不可达（会话可能已失效）：%s" % (v.get("error") or "HTTP %s" % v.get("status"))
            with self.server.state.lock:
                self.server.state.last_real = {"at": int(time.time()),
                                               "endpoint": "/jump",
                                               "ok": False, "count": 1,
                                               "page": page_name, "error": msg}
            if json_mode:
                self._reply(502, {"success": False, "error": msg,
                                  "url": res["url"], "page": page_name,
                                  "verify": v, "count": 0, "rows": []})
            else:
                self._reply_text(502, msg)
            return
        c = self.server.sessions.get(session)
        if c is None:
            self._reply(401, {"success": False,
                              "error": "session 无效或已过期，请先 POST /login 获取新 session",
                              "url": "", "page": page_name, "count": 0, "rows": []})
            return
        # 签发一次性短时跳转码：URL 中不再携带长期有效的 session
        code = self.server.jump_codes.mint(session)
        res.update({"success": True, "page": page_name,
                    "username": c["username"],
                    "expires_in": max(0, int(c["exp"] - time.time())),
                    "expires_at": int(c["exp"]), "error": "",
                    "note": "进门户：打开 login_url，点按钮后自动免密进入（全新设备弹窗预热，"
                            "无需输密码）；进教务系统：登录门户后打开 app_url 免密进入——"
                            "请勿使用门户里的“教务系统”图标（会触发学校统一认证要求登录）。"
                            "链接点击时实时签发票据（学校票据有效期仅 60 秒），跳转码 "
                            "%d 秒内有效，过期请重新调用 /jump 获取新链接，请勿外传。" % JUMP_CODE_TTL})
        # 返回点击时实时签票的中转地址，避免预签票据（60 秒有效）在用户点击前过期；
        # 基准地址优先 JWXT_PUBLIC_URL，否则用经过校验的 Host + X-Forwarded-Proto
        go_base = public_url(self.headers) + "/jump/go?code=" + quote(code)
        res["login_url"] = go_base
        if res.get("app_url") or res.get("app_ok"):
            res["app_url"] = go_base + "&app=1"
        with self.server.state.lock:
            self.server.state.last_real = {"at": int(time.time()),
                                           "endpoint": "/jump",
                                           "ok": True, "count": 1, "page": page_name}
        if redirect:
            target = res["warm_url"] if warm else res["login_url"]
            self._status = 302
            self.send_response(302)
            self.send_header("Location", target)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            for k, v in self._cors_headers():
                self.send_header(k, v)
            self.send_header("X-Trace-Id", getattr(self, "trace_id", "-"))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self._finish("redirect=%s" % target)
            return
        if json_mode:
            self._reply(200, res)
        else:
            self._reply_text(200, res["login_url"])

    def _jump_go(self, code, app, plain=False, json_mode=False):
        """浏览器点击免密链接的实时签票中转：此刻签发 60 秒有效的票据。

        只接受 /jump 签发的短时跳转码（code），不再接受裸 session；跳转码过期或
        不存在时返回 401，提示重新调用 /jump 获取新链接。
        plain/app 直接跳转入口为一次性消费；桥接页/json 取票不消费，便于弹窗
        被拦截后重试（TTL 内有效）。

        json=1：返回 {login_url, app_url, warm_url}（两张实时票据），供桥接页
        串联「门户登录 + 教务系统登录」实现无感登陆；此路径只签一次双票，不再重复签发。
        非 json 模式：默认返回桥接页，按钮点击后弹窗完成预热+门户登录，主窗口直达教务系统；
        plain=1 直接 302 到门户回调（curl/API 场景）；app=1 直接 302 到教务 SSO。
        """
        consume = bool(app or plain)
        sid = self.server.jump_codes.resolve(code, consume=consume) if code else None
        if not sid:
            msg = "跳转链接已失效或过期，请重新调用 /jump 获取新链接"
            LOG.warning("跳转码无效或过期 code=%s***", (code or "")[:6])
            if json_mode:
                self._reply(401, {"success": False, "error": msg})
            else:
                self._reply_text(401, msg)
            return
        try:
            if json_mode:
                def mint_pair(j):
                    # 两张票据并行签发（克隆会话避免共享 Session 的线程安全问题），
                    # 减少学校侧往返对跳转耗时的影响
                    out = {}
                    tid = trace_id()
                    def m_portal():
                        set_trace_id(tid)
                        try:
                            out["login"] = mint_st(j, SERVICE)
                        except Exception as e:
                            out["login_err"] = e
                    def m_app():
                        set_trace_id(tid)
                        cj = _clone_j(j)
                        try:
                            if not getattr(j, "mgmt", None):
                                raise RuntimeError("会话缺少教务系统地址（请重新 POST /login）")
                            out["app"] = mint_st(cj, MGMT_SSO_SERVICE)
                        except Exception as e:
                            out["app_err"] = e
                        finally:
                            cj.close()
                    f1 = None
                    try:
                        ex = _background_executor()
                        f1 = ex.submit(m_portal)
                        f2 = ex.submit(m_app)
                    except PoolFullError:
                        if f1 is not None:
                            f1.cancel()
                        raise RuntimeError("后台任务繁忙，请稍后重试")
                    f1.result()
                    f2.result()
                    if "login_err" in out:
                        raise out["login_err"]
                    if "app_err" in out:
                        raise out["app_err"]
                    st1 = out["login"]
                    st2 = out["app"]
                    login = BASE + "/rump_frontend/loginFromCas/?ticket=" + quote(st1)
                    app_url = j.mgmt + "api/cas/login?pattern=manager-login&ticket=" + quote(st2)
                    stu = _jump_target(j.stu, "home", getattr(j, "stu_frag", None))
                    return {"login_url": login, "app_url": app_url, "stu_url": stu}
                pair = with_session_j(self.server.sessions, sid, mint_pair)
                pair.update({"success": True, "warm_url": BASE + "/"})
                self._reply(200, pair)
                return

            def run(j):
                if app:
                    if not getattr(j, "mgmt", None):
                        raise RuntimeError("会话缺少教务系统地址（请重新 POST /login）")
                    st = mint_st(j, MGMT_SSO_SERVICE)
                    return j.mgmt + "api/cas/login?pattern=manager-login&ticket=" + quote(st)
                st = mint_st(j, SERVICE)
                return BASE + "/rump_frontend/loginFromCas/?ticket=" + quote(st)
            res = with_session_j(self.server.sessions, sid, run)
        except TokenError as e:
            LOG.warning("跳转会话无效: %s", e)
            self.server.sessions.invalidate(sid)
            cache = getattr(self.server, "jump_cache", None)
            if cache is not None:
                cache.delete("jump|" + sid)
            msg = str(e)
            if json_mode:
                self._reply(401, {"success": False, "error": msg})
            else:
                self._reply_text(401, msg)
            return
        except Exception as e:
            LOG.warning("免密票据签发失败: %s", e)
            # 认证类失败清除死会话与跳转缓存；瞬时网络故障保留会话，
            # 避免上游抖动导致有效会话被误杀
            if _is_auth_failure(e):
                self.server.sessions.invalidate(sid)
            cache = getattr(self.server, "jump_cache", None)
            if cache is not None:
                cache.delete("jump|" + sid)
            msg = "免密跳转票据签发失败：%s" % e
            if json_mode:
                self._reply(502, {"success": False, "error": msg})
            else:
                self._reply_text(502, msg)
            return
        if app:
            target = res
        elif plain:
            target = res
        else:
            self._extra_headers.append((
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'sha256-OH8gUQ7Q9SYCDbphqBc7Cs4B7CWHxFl43qPdr39Enns='; "
                "style-src 'unsafe-inline'; img-src 'self' data:; "
                "connect-src 'self'; object-src 'none'; base-uri 'self'",
            ))
            self._reply_html(200, self._jump_bridge(code))
            return
        self._status = 302
        self.send_response(302)
        self.send_header("Location", target)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for k, v in self._cors_headers():
            self.send_header(k, v)
        self.send_header("X-Trace-Id", getattr(self, "trace_id", "-"))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._finish("redirect=%s" % target)

    def _jump_bridge(self, code):
        """免密桥接页（移动端适配）：静默获取 WebVPN 会话并直达教务系统。

        桌面端加载即自动打开不可见小弹窗完成「预热 → 门户登录」，主窗口直达
        教务系统；移动端（iOS/安卓/微信等弹窗受限）跳过自动弹窗、显示大按钮，
        按钮同样触发弹窗链；弹窗不可用时提供三步手动链路（纯顶层跳转，移动端
        可靠）。说明：my_client_ticket 为 SameSite=Lax，门户登录必须在顶层
        上下文完成，纯后台 iframe 无法携带该 Cookie，故弹窗/顶层跳转是必要载体。
        """
        warm = BASE + "/"
        retry = "/jump/go?code=" + quote(code)
        login_plain = "/jump/go?plain=1&code=" + quote(code)
        app_plain = "/jump/go?app=1&code=" + quote(code)
        e = lambda s: (s or "").replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>正在免密进入教务系统…</title>
<style>
  *{-webkit-tap-highlight-color:transparent}
  body{font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
       background:#f5f7fa;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;
       padding:16px;box-sizing:border-box}
  .card{background:#fff;border-radius:14px;box-shadow:0 2px 12px rgba(0,0,0,.08);
        padding:28px 24px;max-width:440px;width:100%;text-align:center}
  h2{color:#1f2937;font-size:20px;margin:0 0 12px}
  p{color:#4b5563;font-size:14px;line-height:1.7;margin:8px 0}
  .muted{color:#9ca3af;font-size:12px}
  .tip{background:#fff7e6;border:1px solid #ffe1a8;color:#8a5a00;border-radius:8px;
       font-size:13px;line-height:1.6;padding:10px 12px;margin:12px 0 0;text-align:left}
  a{color:#2563eb;text-decoration:none}
  .btn{display:none;width:100%;box-sizing:border-box;margin:18px 0 8px;padding:16px 0;
       background:#2563eb;color:#fff;border:0;border-radius:12px;font-size:17px;
       cursor:pointer}
  .btn:disabled{opacity:.6;cursor:default}
  .err{color:#dc2626;font-size:13px;display:none;margin-top:10px}
  .spin{display:inline-block;width:18px;height:18px;border:2px solid #e5e7eb;border-top-color:#2563eb;
        border-radius:50%;animation:rot .8s linear infinite;vertical-align:-3px;margin-right:8px}
  @keyframes rot{to{transform:rotate(360deg)}}
  .steps{display:none;margin-top:16px;border-top:1px solid #eef1f5;padding-top:14px;text-align:left}
  .steps ol{margin:6px 0 0;padding-left:20px;color:#4b5563;font-size:13px;line-height:2}
  .steps a{font-weight:600}
  @media (max-width:480px){
    .card{padding:24px 18px}
    h2{font-size:18px}
    .btn{font-size:17px;padding:15px 0}
  }
</style>
</head>
<body>
<div class="card">
  <h2>正在免密进入教务系统</h2>
  <p id="status"><span class="spin"></span>正在建立 WebVPN 会话并登录…</p>
  <p class="tip">提示：本功能需要浏览器<strong>允许本站弹窗</strong>。
     如未自动跳转，请在浏览器地址栏点击弹窗图标选择「允许」（微信等内置浏览器不支持弹窗，
     请使用下方三步方式）。</p>
  <button class="btn" id="go">免密进入教务系统</button>
  <p class="err" id="err"></p>
  <div class="steps" id="steps">
    <p class="muted" style="text-align:center">当前浏览器不支持自动跳转，请按顺序点击（每步均为免密，无需输密码）：</p>
    <ol>
      <li><a href="@@WARM@@" target="_blank" rel="noopener">打开 WebVPN 首页</a>（首次使用需建立会话）</li>
      <li><a href="@@LOGIN_PLAIN@@">免密进门户</a></li>
      <li><a href="@@APP_PLAIN@@">进入教务系统</a></li>
    </ol>
  </div>
  <p class="muted" style="margin-top:14px">如提示“访问出现异常”，请先打开
     <a href="@@WARM@@" target="_blank" rel="noopener">WebVPN 首页</a> 一次，再
     <a href="@@RETRY@@">重新进入</a>。</p>
</div>
<script>
  var CODE = new URLSearchParams(window.location.search).get("code") || "";
  var WARM = "@@WARM@@";
  var WARM_DIRECT = "@@WARM_DIRECT@@";
  var statusEl = document.getElementById('status');
  var btn = document.getElementById('go');
  var errEl = document.getElementById('err');
  var stepsEl = document.getElementById('steps');
  var jumped = false;
  var isMobile = /Android|iPhone|iPad|iPod|Mobile|Windows Phone|MicroMessenger/i.test(navigator.userAgent);
  var showErr = function(m){ errEl.textContent = m; errEl.style.display = 'block'; };
  var showBtn = function(){ if (!jumped) btn.style.display = 'block'; };
  var showSteps = function(){ if (!jumped) stepsEl.style.display = 'block'; };

  // 弹窗依次完成：预热 → 门户登录 → 教务系统 SSO（同一 WebVPN 会话），
  // 每一步以“加载完成或超时”确认后再推进（避免会话未就绪即跳转导致“会话已过期”）；
  // 完成后主窗口直达学生端个人界面（绕过管理端“切换个人首页”链路）。
  function waitNext(w, ms){
    return new Promise(function(resolve){
      var done = false;
      var finish = function(){ if (!done) { done = true; try { w.removeEventListener('load', onload); } catch (e) {} resolve(); } };
      var onload = function(){ finish(); };
      try { w.addEventListener('load', onload); } catch (e) {}
      setTimeout(finish, ms);
    });
  }
  function chain(){
    var w = null;
    try {
      w = window.open(WARM_DIRECT, '_blank', isMobile ? '' : 'width=2,height=2,left=-2000,top=-2000');
    } catch (e) {}
    if (!w) return Promise.reject(new Error('POPUP_BLOCKED'));
    statusEl.innerHTML = '<span class="spin"></span>正在建立 WebVPN 会话…';
    var warmReady = waitNext(w, 2500);
    return fetch('/jump/go?json=1&code=' + encodeURIComponent(CODE))
      .then(function(r){ return r.json(); })
      .then(function(d){
        if (!d.success || !d.login_url || !d.app_url || !d.stu_url)
          throw new Error(d.error || '票据签发失败，请稍后重试');
        return warmReady.then(function(){
          statusEl.innerHTML = '<span class="spin"></span>正在登录门户…';
          try { w.location.href = d.login_url; } catch (e) {}
          return waitNext(w, 3000);
        }).then(function(){
          statusEl.innerHTML = '<span class="spin"></span>正在建立教务系统会话…';
          try { w.location.href = d.app_url; } catch (e) {}
          return waitNext(w, 3000);
        }).then(function(){
          jumped = true;
          try { if (w && !w.closed) w.close(); } catch (e) {}
          location.href = d.stu_url;
        });
      })
      .catch(function(e){
        try { if (w && !w.closed) w.close(); } catch (e2) {}
        if (e && e.message === 'POPUP_BLOCKED') {
          statusEl.textContent = '浏览器拦截了弹窗：请在地址栏允许本站弹窗后点击按钮重试'
                               + '（Chrome：地址栏右侧弹窗图标 → 允许；Safari：设置中关闭「阻止弹窗」）；'
                               + '微信内置浏览器不支持弹窗，请直接使用下方三步方式（全程免密）。';
          showBtn();
          showSteps();
        } else {
          showErr(e && e.message ? e.message : '免密登录失败，请稍后重试。');
          showBtn();
          showSteps();
        }
      });
  }

  // 桌面端：加载即自动尝试（静默）；移动端：跳过自动弹窗，显示按钮
  if (isMobile) {
    statusEl.textContent = '点击下方按钮，免密进入教务系统。';
    showBtn();
  } else {
    chain();
    setTimeout(showBtn, 15000);
  }

  btn.addEventListener('click', function(){
    btn.disabled = true;
    btn.textContent = '正在免密进入…';
    statusEl.innerHTML = '<span class="spin"></span>正在建立会话并登录…';
    chain().catch(function(){
      btn.disabled = false;
      btn.textContent = '免密进入教务系统';
      showSteps();
    });
  });
</script>
</body>
</html>"""
        return (html.replace("@@WARM@@", e(warm))
                    .replace("@@RETRY@@", e(retry))
                    .replace("@@LOGIN_PLAIN@@", e(login_plain))
                    .replace("@@APP_PLAIN@@", e(app_plain))
                    .replace("@@WARM_DIRECT@@", e(BASE + "/rump_frontend/login/")))

    def do_GET(self):
        self._begin()
        if not self._rate_ok():
            return
        try:
            self._handle_get()
        except Exception as e:
            LOG.exception("GET 请求处理异常 path=%s", self.path)
            try:
                with self.server.state.lock:
                    self.server.state.last_real = {"at": int(time.time()),
                                                   "endpoint": urlparse(self.path).path,
                                                   "ok": False, "count": 0,
                                                   "error": "%s: %s" % (type(e).__name__, e)}
            except Exception:
                pass
            self._reply(500, _internal_error_body())

    def _handle_get(self):
        p = urlparse(self.path).path
        q = urlparse(self.path).query
        params = dict(x.split("=", 1) for x in q.split("&") if "=" in x)
        if p == "/health":
            self._reply(200, self._health(self.server.state))
        elif p == "/health/deep":
            if (params.get("username") or params.get("password")) and not ALLOW_GET_CREDENTIALS:
                self._reply(400, {"success": False,
                                  "error": "GET 不允许携带明文凭据，请改用 POST /health/deep",
                                  "output": "", "count": 0, "rows": []})
                return
            if params.get("username") or params.get("password"):
                LOG.warning("GET /health/deep 携带明文凭据（建议改用 POST，避免进入代理/浏览器历史）")
            self._deep(unquote(params.get("username", "")).strip(),
                       unquote(params.get("password", "")).strip())
        elif p == "/login/status":
            tok = unquote(params.get("session", "")).strip()
            st = self.server.sessions.status(tok) if tok else {"valid": False}
            self._reply(200, st)
        elif p == "/get_schedule/export":
            if (params.get("username") or params.get("password")) and not ALLOW_GET_CREDENTIALS:
                self._reply(400, {"success": False,
                                  "error": "GET 不允许携带明文凭据，请改用短时 code 或 POST",
                                  "output": "", "count": 0, "rows": []})
                return
            if params.get("username") or params.get("password"):
                LOG.warning("GET /get_schedule/export 携带明文凭据（建议改用短时 code 或 POST）")
            self._export(unquote(params.get("username", "")).strip(),
                         unquote(params.get("password", "")).strip(),
                         unquote(params.get("session", "")).strip(),
                         unquote(params.get("semester", "")).strip(),
                         unquote(params.get("weeks", "")).strip(),
                         params.get("odd_or_double", "1"),
                         unquote(params.get("code", "")).strip())
        elif p == "/metrics":
            self._reply_text(200, _metrics_text(self.server.state))
        elif p == "/jump/go":
            # 浏览器点击的中转：无需 Bearer，但只接受 /jump 签发的短时跳转码
            # （code），不接受裸 session；json=1 供桥接页取双票
            self._jump_go(unquote(params.get("code", "")).strip(),
                          _to_bool(params.get("app", False)),
                          _to_bool(params.get("plain", False)),
                          _to_bool(params.get("json", False)))
        elif p == "/jump":
            if not self._ok():
                self._reply(401, {"success": False, "error": "token required",
                                  "url": "", "count": 0, "rows": []})
                return
            self._jump(unquote(params.get("session", "")).strip(),
                       unquote(params.get("page", "")).strip(),
                       _to_bool(params.get("redirect", False)),
                       _to_bool(params.get("json", False)),
                       _to_bool(params.get("verify", False)),
                       _to_bool(params.get("warm", False)))
        elif p == "/":
            self._reply(200, {
                "service": "jwxt-service",
                "version": VERSION,
                "endpoints": ["GET /health", "GET|POST /health/deep", "POST /login",
                              "POST /get_grades", "POST /get_schedule",
                              "GET|POST /get_schedule/export", "GET|POST /login/status",
                              "GET|POST /jump", "GET /jump/go", "GET /metrics"],
                "post_body": {"username": "学号", "password": "密码",
                              "semesters": "可选: 空/单学期/逗号多学期/recent:N/all",
                              "include_all": "可选布尔, 默认 false（默认仅返回所选学期成绩）；true 时额外附带个人首页全部成绩与学分概况",
                              "include_rows": "可选布尔, 默认 true; 仅需展示可设 false 省略 rows（省 LLM 输入 token）",
                              "include_output": "可选布尔, 默认 true; 仅需 info+rows 时设 false 省略已渲染 output（省 LLM 输入 token）",
                              "stats": "get_grades 响应自动附带预计算统计（count/avg/max/min/bins/percent），供分析节点直接引用",
                              "info": "get_grades 响应自动附带学生基础信息（xm/xh/xbmc/nj/xymc/zyfxmc/bjmc），供格式化节点引用",
                              "schedule": "POST /get_schedule 见 /get_schedule 说明",
                              "include_details": "可选布尔, 默认 true; 仅需展示可设 false 省略课程明细表（省 LLM 输出 token）",
                              "session": "可选: POST /login 获取的短随机 session ID（默认内存保存，重启失效；配置 JWXT_REDIS_URL 后由 Redis TTL 管理），携带 session 即可查询，无需重复传密码",
                              "schedule_export": "GET /get_schedule/export?code=..&semester=..&weeks=.. 下载课表 Word 文件（code 为 /get_schedule 返回的短时下载码，不携带 session）",
                              "jump": "GET|POST /jump?session=..&page=..&redirect=..&json=..&verify=..&warm=.. 复用登录会话免认证直达教务系统：默认直接返回 login_url（纯文本）；json=1 返回结构化 JSON（url/login_url/app_url/warm_url/note）；redirect=1 直接 302 到 login_url（warm=1 时 302 到 warm_url），verify=1 附带目标页可达性检查；GET /jump/go?code=..&app=..&plain=..&json=.. 为浏览器点击中转（无需 Bearer，只接受 /jump 签发的短时跳转码）：默认返回桥接页，加载即自动打开不可见弹窗静默完成「预热+门户登录 → 主窗口进教务系统」，弹窗被拦时回退按钮，plain=1 直接 302 门户回调，app=1 直接 302 教务 SSO，json=1 返回 {login_url, app_url, warm_url} 两张实时票据",
                              "trace": "每次响应头 X-Trace-Id 携带本次请求追溯 ID；服务端日志按该 ID 检索完整调用链",
                              "health_isolation": "单端口：/health 请求绕过业务线程池，业务线程被慢上游占满时健康探针仍即时响应",
                              "grades_cache_ttl": "成绩查询缓存秒数（默认 300，按 学号+查询参数；0 关闭；内存/Redis 模式均生效）",
                              "schedule_cache_ttl": "课表查询缓存秒数（默认 300，按 学号+学期+周次；0 关闭；内存/Redis 模式均生效）",
                              "schedule_class_share": "课表缓存按班级共享（默认 0：同班可能因选修课不同而课表不一致，共享会串数据；仅确认同班课表一致时开启）",
                              "jump_cache_ttl": "免密跳转签票能力缓存秒数（默认 30，按 session；0 关闭。只缓存“可签票”结论，票据不缓存）",
                              "jump_code_ttl": "/jump 跳转码有效期秒数（默认 60；/jump/go 只接受跳转码，过期需重新调用 /jump）",
                              "upstream_global": "上游并发上限（默认 8；内存模式为进程级，Redis 模式为多副本全局共享）",
                              "health_probe_interval": "/health 后台探测间隔秒数（默认 10；健康请求只读缓存，不阻塞探测）",
                              "login_reuse_probe": "/login 复用前轻量探测学校会话有效性（默认 1；失效自动清除并重新登录）"},
                "sessions": "会话：/login 返回 32 字符随机 session ID，默认服务端内存保存（不加密、不落盘、重启失效）；"
                            "闲置默认 1 小时过期，成功使用自动滑动续期；配置 JWXT_REDIS_URL 后会话切换为 Redis 共享，支持多副本部署",
                "metrics": "GET /metrics 返回 Prometheus 文本格式进程指标（请求量/耗时/缓存命中/上游调用；配置了 JWXT_API_TOKEN 时需 Bearer）",
            })
        else:
            if p in KNOWN_PATHS:
                self._reply(405, {"success": False, "error": "method not allowed",
                                  "output": "", "count": 0, "rows": []})
            else:
                self._reply(404, {"success": False, "error": "not found: " + p,
                                  "output": "", "count": 0, "rows": []})

    def do_POST(self):
        self._begin()
        if not self._rate_ok():
            return
        try:
            self._handle_post()
        except BodyTooLarge as e:
            self.close_connection = True
            LOG.warning("请求体超限 path=%s %s", urlparse(self.path).path, e)
            self._reply(413, {"success": False, "error": "请求体过大（上限 %d 字节）" % MAX_BODY,
                              "output": "", "count": 0, "rows": []})
        except Exception as e:
            LOG.exception("请求处理异常 path=%s", self.path)
            try:
                with self.server.state.lock:
                    self.server.state.last_real = {"at": int(time.time()),
                                                   "endpoint": urlparse(self.path).path,
                                                   "ok": False, "count": 0,
                                                   "error": "%s: %s" % (type(e).__name__, e)}
            except Exception:
                pass
            self._reply(500, _internal_error_body())

    def _handle_post(self):
        p = urlparse(self.path).path
        if p == "/health/deep":
            b = self._body()
            self._deep(str(b.get("username") or "").strip(),
                       str(b.get("password") or "").strip())
            return
        if p == "/login/status":
            b = self._body()
            tok = str(b.get("session") or "").strip()
            st = self.server.sessions.status(tok) if tok else {"valid": False}
            self._reply(200, st)
            return
        if p == "/login":
            if not self._login_rate_ok():
                return
            if not self._ok():
                self._reply(401, {"success": False, "error": "token required", "output": "", "count": 0, "rows": []})
                return
            b = self._body()
            u = (b.get("username") or "").strip()
            pw = (b.get("password") or "").strip()
            if not u or not pw:
                if b.get("session") or b.get("seesion"):
                    self._reply(400, {"success": False,
                                      "error": "调用有误：/login 用于用 username/password 换取令牌；"
                                               "携带令牌查询请调用 /get_grades 或 /get_schedule，字段名是 session",
                                      "output": "", "count": 0, "rows": []})
                else:
                    self._reply(400, {"success": False, "error": "username/password 必填（/login 用于换取令牌）",
                                      "output": "", "count": 0, "rows": []})
                return
            # 同账号并发登录互斥：避免两个请求同时向学校创建会话，
            # 触发“已在其他地方登录”互相顶下线；持锁后统一走复用/新登录助手
            try:
                with self.server.login_locks.lock(u, timeout=LOGIN_LOCK_TIMEOUT):
                    sid, meta = self._login_session(u, pw)
            except LockTimeoutError:
                LOG.warning("登录锁等待超时 username=%s", u)
                self._reply(503, {"success": False,
                                  "error": "登录处理繁忙，请稍后重试",
                                  "output": "", "count": 0, "rows": []})
                return
            except Exception as e:
                self._reply_login_error(e)
                return
            self._reply(200, {"success": True, "session": sid, "token": sid,
                              "username": u, **meta})
            return
        if p == "/get_schedule/export":
            if not self._ok():
                self._reply(401, {"success": False, "error": "token required", "output": "", "count": 0, "rows": []})
                return
            b = self._body()
            self._export(str(b.get("username") or "").strip(),
                         str(b.get("password") or "").strip(),
                         str(b.get("session") or "").strip(),
                         str(b.get("semester") or "").strip(),
                         b.get("weeks"),
                         b.get("odd_or_double", 1),
                         str(b.get("code") or "").strip())
            return
        if p == "/jump":
            if not self._ok():
                self._reply(401, {"success": False, "error": "token required",
                                  "url": "", "count": 0, "rows": []})
                return
            b = self._body()
            self._jump(str(b.get("session") or "").strip(),
                       str(b.get("page") or "").strip(),
                       _to_bool(b.get("redirect", False)),
                       _to_bool(b.get("json", False)),
                       _to_bool(b.get("verify", False)),
                       _to_bool(b.get("warm", False)))
            return
        if p not in ("/get_grades", "/get_schedule"):
            if p in KNOWN_PATHS:
                self._reply(405, {"success": False, "error": "method not allowed",
                                  "output": "", "count": 0, "rows": []})
            else:
                self._reply(404, {"success": False, "error": "not found: " + p,
                                  "output": "", "count": 0, "rows": []})
            return
        if not self._ok():
            self._reply(401, {"success": False, "error": "token required", "output": "", "count": 0, "rows": []})
            return
        b = self._body()
        session = str(b.get("session") or "").strip()
        u = (b.get("username") or "").strip()
        pw = (b.get("password") or "").strip()
        if not session and (not u or not pw):
            self._reply(400, {"success": False, "error": "需携带认证信息：请求体提供 username/password 或 session（POST /login 获取）",
                              "output": "", "count": 0, "rows": []})
            return
        try:
            if p == "/get_schedule":
                include_rows = _to_bool(b.get("include_rows", True))
                include_details = _to_bool(b.get("include_details", True))
                # 课表结果短 TTL 缓存：键 = 学号 + 学期 + 规范化周次 + 单双周 + 裁剪参数
                # （不含密码）。课表一周内基本不变，重复查询直接命中缓存，减少学校上游压力；
                # 学校侧偶发失败以短时负缓存防抖。缓存不落盘、重启即失效，与成绩缓存同级。
                cache = getattr(self.server, "schedule_cache", None)
                sem_raw = str(b.get("semester") or "").strip()
                weeks_raw = b.get("weeks")
                weeks_err = _weeks_param_error(weeks_raw)
                if weeks_err:
                    self._reply(400, {"success": False, "error": weeks_err,
                                      "output": "", "count": 0, "rows": [],
                                      "semester": sem_raw})
                    return
                odd_err = _odd_or_double_error(b.get("odd_or_double", 1))
                if odd_err:
                    self._reply(400, {"success": False, "error": odd_err,
                                      "output": "", "count": 0, "rows": [],
                                      "semester": sem_raw})
                    return
                odd_raw = _to_int(b.get("odd_or_double", 1), 1)
                cache_u = u
                if not cache_u and session:
                    _c = self.server.sessions.get(session)
                    if _c:
                        cache_u = _c.get("username", "")
                ck = None
                if cache is not None and cache_u:
                    ck = _schedule_cache_key(cache_u, sem_raw, weeks_raw, odd_raw,
                                             include_rows, include_details)
                    hit = cache.get(ck)
                    if hit is not None:
                        self._cache_hit = True
                        hit_sid = session
                        if hit_sid:
                            self.server.sessions.touch(hit_sid)
                        elif u and pw:
                            # 密码路径命中缓存：尝试找回同账号会话，用于签发下载码
                            hit_sid = self.server.sessions.find_reusable(
                                u, _pwd_hash(u, pw), LOGIN_REUSE_MAX_AGE)
                            if hit_sid:
                                self.server.sessions.touch(hit_sid)
                        with self.server.state.lock:
                            self.server.state.last_real = {"at": int(time.time()),
                                                           "endpoint": p,
                                                           "ok": bool(hit.get("success")),
                                                           "count": hit.get("count", 0),
                                                           "cached": True}
                        if not hit.get("success"):
                            LOG.warning("业务失败（负缓存命中） endpoint=%s error=%s", p, hit.get("error"))
                        if hit_sid and hit.get("success"):
                            resp = dict(hit)
                            code = self.server.export_codes.mint(hit_sid)
                            suffix = _schedule_export_suffix(b, code, resp.get("semester", ""))
                            base = public_url(self.headers)
                            resp["download_url"] = base + suffix if base else suffix
                            self._reply(200, resp)
                            return
                        self._reply(200, hit, body=cache.get_payload(ck))
                        return
                if session:
                    try:
                        r = query_with_session(self.server.sessions, session, lambda uu, pp, jj: main_schedule(
                            uu, pp, sem_raw,
                            weeks_raw, odd_raw, j=jj,
                            include_rows=include_rows, include_details=include_details))
                    except TokenError as e:
                        LOG.warning("会话无效 endpoint=%s error=%s", p, e)
                        self.server.sessions.invalidate(session)
                        self._reply(401, {"success": False, "error": str(e),
                                          "output": "", "count": 0, "rows": []})
                        return
                else:
                    if not self._login_rate_ok():
                        return
                    try:
                        with self.server.login_locks.lock(u, timeout=LOGIN_LOCK_TIMEOUT):
                            session, _ = self._login_session(u, pw)
                    except LockTimeoutError:
                        LOG.warning("登录锁等待超时 endpoint=%s username=%s", p, u)
                        self._reply(503, {"success": False,
                                          "error": "登录处理繁忙，请稍后重试",
                                          "output": "", "count": 0, "rows": []})
                        return
                    except Exception as e:
                        self._reply_login_error(e)
                        return
                    try:
                        r = query_with_session(self.server.sessions, session, lambda uu, pp, jj: main_schedule(
                            uu, pp, sem_raw,
                            weeks_raw, odd_raw, j=jj,
                            include_rows=include_rows, include_details=include_details))
                    except TokenError as e:
                        LOG.warning("会话无效 endpoint=%s error=%s", p, e)
                        self.server.sessions.invalidate(session)
                        self._reply(401, {"success": False, "error": str(e),
                                          "output": "", "count": 0, "rows": []})
                        return
            else:
                grades_include_rows = _to_bool(b.get("include_rows", True))
                grades_include_output = _to_bool(b.get("include_output", True))
                grades_include_all = _to_bool(b.get("include_all", False))
                grades_include_sensitive = _to_bool(b.get("include_sensitive_info", False))
                # 成绩查询结果短 TTL 缓存：键 = 学号 + 查询参数（不含密码）。
                # session 查询时先从会话存储取学号，未登录成功过则不缓存。
                cache = getattr(self.server, "grades_cache", None)
                cache_u = u
                if not cache_u and session:
                    _c = self.server.sessions.get(session)
                    if _c:
                        cache_u = _c.get("username", "")
                ck = None
                if cache is not None and cache_u:
                    ck = "%s|%s|%d|%d|%d|%d" % (cache_u, _norm_semesters_key(b.get("semesters")),
                                                 grades_include_all, grades_include_rows,
                                                 grades_include_output, grades_include_sensitive)
                    hit = cache.get(ck)
                    if hit is not None:
                        self._cache_hit = True
                        if session:
                            self.server.sessions.touch(session)
                        with self.server.state.lock:
                            self.server.state.last_real = {"at": int(time.time()),
                                                           "endpoint": p,
                                                           "ok": bool(hit.get("success")),
                                                           "count": hit.get("count", 0),
                                                           "cached": True}
                        if not hit.get("success"):
                            LOG.warning("业务失败（负缓存命中） endpoint=%s error=%s", p, hit.get("error"))
                        self._reply(200, hit, body=cache.get_payload(ck))
                        return
                if not session and not self._login_rate_ok():
                    # 缓存未命中且未携带 session：即将触发完整登录，与 /login 共用登录限流
                    return
                if session:
                    try:
                        r = query_with_session(self.server.sessions, session, lambda uu, pp, jj: main(
                            uu, pp, str(b.get("semesters") or ""),
                            grades_include_all,
                            include_rows=grades_include_rows,
                            include_output=grades_include_output,
                            include_sensitive=grades_include_sensitive, j=jj))
                    except TokenError as e:
                        LOG.warning("会话无效 endpoint=%s error=%s", p, e)
                        self.server.sessions.invalidate(session)
                        self._reply(401, {"success": False, "error": str(e),
                                          "output": "", "count": 0, "rows": [],
                                          "info": {}, "stats": None})
                        return
                else:
                    try:
                        with self.server.login_locks.lock(u, timeout=LOGIN_LOCK_TIMEOUT):
                            session, _ = self._login_session(u, pw)
                    except LockTimeoutError:
                        LOG.warning("登录锁等待超时 endpoint=%s username=%s", p, u)
                        self._reply(503, {"success": False,
                                          "error": "登录处理繁忙，请稍后重试",
                                          "output": "", "count": 0, "rows": []})
                        return
                    except Exception as e:
                        self._reply_login_error(e)
                        return
                    try:
                        r = query_with_session(self.server.sessions, session, lambda uu, pp, jj: main(
                            uu, pp, str(b.get("semesters") or ""),
                            grades_include_all,
                            include_rows=grades_include_rows,
                            include_output=grades_include_output,
                            include_sensitive=grades_include_sensitive, j=jj))
                    except TokenError as e:
                        LOG.warning("会话无效 endpoint=%s error=%s", p, e)
                        self.server.sessions.invalidate(session)
                        self._reply(401, {"success": False, "error": str(e),
                                          "output": "", "count": 0, "rows": [],
                                          "info": {}, "stats": None})
                        return
                # 敏感字段（身份证/证件照）响应不缓存；失败结果仅当标记
                # neg_cacheable（上游瞬时故障）时负缓存，凭据错误不缓存
                if ck is not None and _cache_write_allowed(grades_include_sensitive, r):
                    if r.get("success"):
                        cache.set(ck, r, payload=json.dumps(r, ensure_ascii=False).encode("utf-8"))
                    else:
                        nf = dict(r)
                        nf.setdefault("info", {})
                        nf.setdefault("stats", None)
                        nf.setdefault("rows", [])
                        nf.pop("neg_cacheable", None)
                        nf.pop("credential_error", None)
                        cache.set(ck, nf, ttl=NEG_CACHE_TTL,
                                  payload=json.dumps(nf, ensure_ascii=False).encode("utf-8"))
            if not r.get("success"):
                LOG.warning("业务失败 endpoint=%s error=%s", p, r.get("error"))
            if p == "/get_schedule" and session and r.get("success"):
                code = self.server.export_codes.mint(session)
                suffix = _schedule_export_suffix(b, code, r.get("semester", ""))
                base = public_url(self.headers)
                r["download_url"] = base + suffix if base else suffix
            if p == "/get_schedule" and ck is not None:
                if _cache_write_allowed(False, r):
                    if r.get("success"):
                        cached_r = dict(r)
                        cached_r.pop("download_url", None)  # 下载链接含短时 code，不入缓存
                        cache.set(ck, cached_r,
                                  payload=json.dumps(cached_r, ensure_ascii=False).encode("utf-8"))
                    else:
                        # 负缓存：仅上游瞬时故障才缓存，凭据错误不缓存
                        nf = dict(r)
                        nf.setdefault("rows", [])
                        nf.pop("download_url", None)
                        nf.pop("neg_cacheable", None)
                        nf.pop("credential_error", None)
                        cache.set(ck, nf, ttl=NEG_CACHE_TTL,
                                  payload=json.dumps(nf, ensure_ascii=False).encode("utf-8"))
            # 认证失败返回 401；其余失败（上游瞬时/学校业务错误）返回 200 + success:false，
            # 避免把“学校查询失败”误判成“凭据无效”
            auth = bool(r.pop("credential_error", None))
            r.pop("neg_cacheable", None)
            status = 401 if auth else 200
            with self.server.state.lock:
                rec = {"at": int(time.time()), "endpoint": p, "ok": bool(r.get("success")),
                       "count": r.get("count", 0)}
                if not r.get("success") and r.get("error"):
                    rec["error"] = str(r["error"])[:200]
                self.server.state.last_real = rec
            self._reply(status, r)
        except Exception as e:
            LOG.exception("请求处理异常 endpoint=%s", p)
            try:
                with self.server.state.lock:
                    self.server.state.last_real = {"at": int(time.time()), "endpoint": p,
                                                   "ok": False, "count": 0,
                                                   "error": "%s: %s" % (type(e).__name__, e)}
            except Exception:
                pass
            self._reply(500, _internal_error_body())



#------------------------------------------------------------------------
# FastAPI 单文件服务层（原 fastapi_app.py 合并而来）
#------------------------------------------------------------------------

class Runtime:
    """Handler 通过 self.server.* 访问的共享运行时，对应原 http.server 实例上挂载的属性。"""

    def __init__(self, cfg):
        self.token = (cfg.token or "").strip()
        self.state = ServerState()
        self._redis_backend = None
        redis_url = (getattr(cfg, "redis_url", "") or "").strip()
        if redis_url:
            self._redis_backend = _redis_mod.RedisBackend(redis_url)
            LOG.info("Redis 共享状态已启用：会话/缓存/限流/短码/锁/上游信号量使用 Redis")
            self.sessions = _redis_mod.RedisSessionStore(
                self._redis_backend, SESSION_TTL, max_sessions=MAX_SESSIONS)
            self.rate_limiter = _redis_mod.RedisRateLimiter(
                self._redis_backend, cfg.rate_limit)
            self.login_limiter = _redis_mod.RedisRateLimiter(
                self._redis_backend, cfg.login_rate_limit)
            self.login_locks = _redis_mod.RedisKeyedLocks(self._redis_backend)
            self.grades_cache = _redis_mod.RedisTTLCache(
                self._redis_backend, cfg.grades_cache_ttl)
            self.schedule_cache = _redis_mod.RedisTTLCache(
                self._redis_backend, cfg.schedule_cache_ttl)
            self.jump_cache = _redis_mod.RedisTTLCache(
                self._redis_backend, cfg.jump_cache_ttl)
            self.jump_codes = _redis_mod.RedisJumpCodeStore(
                self._redis_backend, JUMP_CODE_TTL)
            self.export_codes = _redis_mod.RedisShortCodeStore(
                self._redis_backend, ttl=max(30, JUMP_CODE_TTL))
            _core_mod.UPSTREAM_SEM = _redis_mod.RedisSemaphore(
                self._redis_backend, _core_mod.UPSTREAM_GLOBAL,
                timeout=cfg.upstream_sem_timeout)
        else:
            self.sessions = SessionStore(SESSION_TTL, max_sessions=MAX_SESSIONS)
            self.rate_limiter = RateLimiter(cfg.rate_limit)
            self.login_limiter = RateLimiter(cfg.login_rate_limit)
            self.login_locks = KeyedLocks()
            self.grades_cache = TTLCache(cfg.grades_cache_ttl)
            self.schedule_cache = TTLCache(cfg.schedule_cache_ttl)
            self.jump_cache = TTLCache(cfg.jump_cache_ttl)
            self.jump_codes = JumpCodeStore(JUMP_CODE_TTL)
            self.export_codes = ShortCodeStore(ttl=max(30, JUMP_CODE_TTL))
        # 服务端并发上限保持进程内：它是单实例自我保护；
        # 多副本全局并发由 Redis 信号量（上游）与限流统一约束
        self.concurrency_limiter = ConcurrencyLimiter(
            getattr(cfg, "limit_concurrency", LIMIT_CONCURRENCY))
        self.health_credentials = (
            ((cfg.health_username or "").strip(), cfg.health_password)
            if (cfg.health_username or "").strip() and cfg.health_password
            else None
        )


def _start_background(runtime, cfg, stop_event=None):
    """启动后台线程并返回线程列表；stop_event 置位后线程自行退出（供 lifespan 优雅停机）。"""
    stop = stop_event or threading.Event()
    threads = []
    if runtime.health_credentials:
        def periodic_deep():
            while not stop.wait(max(30, cfg.health_interval)):
                set_trace_id("periodic")
                with runtime.state.lock:
                    runtime.state.last_deep_start = time.time()
                deep_check(runtime.health_credentials[0],
                           runtime.health_credentials[1], runtime.state)
        t = threading.Thread(target=periodic_deep, daemon=True)
        t.start()
        threads.append(t)

    def probe_loop():
        _health_probe_loop(runtime.state, max(2, cfg.health_probe_interval), stop,
                           runtime._redis_backend)
    t = threading.Thread(target=probe_loop, daemon=True)
    t.start()
    threads.append(t)

    def sweeper():
        # 清理周期与健康探测解耦：上限 120s，让限流桶/缓存清理更及时
        while not stop.wait(max(60, min(cfg.health_interval, 120))):
            runtime.sessions.sweep()
            runtime.grades_cache.sweep()
            runtime.schedule_cache.sweep()
            runtime.jump_cache.sweep()
            runtime.jump_codes.sweep()
            runtime.export_codes.sweep()
            runtime.rate_limiter.sweep()
            runtime.login_limiter.sweep()
    t = threading.Thread(target=sweeper, daemon=True)
    t.start()
    threads.append(t)
    return threads


class _BodySink:
    """把 BaseHTTPRequestHandler 的 wfile 写操作重定向到内存响应体。"""

    def __init__(self, handler):
        self._handler = handler

    def write(self, data):
        self._handler._body_chunks.append(data)

    def flush(self):
        pass


class FastHandler(Handler):
    """把原 Handler 的低层 socket 响应适配到 Starlette Response。

    通过重写 send_response/send_header/end_headers/wfile/_body 等接口，
    原 do_GET/do_POST/_handle_get/_handle_post/_jump/_jump_go 等全部逻辑无需改动即可复用。
    """

    def __init__(self, server, command, path, headers, raw_body, client_ip, trace_id=None):
        self.server = server
        self.command = command
        self.path = path
        self.headers = headers
        self._raw_body = raw_body
        self.client_address = (client_ip, 0)
        self.requestline = "%s %s HTTP/1.1" % (command, path)
        self.close_connection = False
        self.trace_id = trace_id or "-"
        self._t0 = time.time()
        self._status = None
        self._extra_headers = []
        self._pending_status = 200
        self._pending_headers = []
        self._body_chunks = []
        self._cache_hit = False

    def handle(self):
        """FastAPI 直接调用 do_GET/do_POST，不再需要 BaseHTTPRequestHandler 的 socket 循环。"""
        pass

    def send_response(self, code, message=None):
        self._pending_status = code

    def send_header(self, keyword, value):
        self._pending_headers.append((keyword, str(value)))

    def end_headers(self):
        pass

    @property
    def wfile(self):
        return _BodySink(self)

    def _begin(self):
        # 优先复用中间件签发的 trace_id，保证响应头与日志（含线程池内）一致
        if not self.trace_id or self.trace_id == "-":
            self.trace_id = secrets.token_hex(6)
        _trace_local.id = self.trace_id
        self._t0 = time.time()
        self._status = None
        self._extra_headers = []
        client = self.client_address[0] if self.client_address else "-"
        LOG.info("请求开始 method=%s path=%s client=%s",
                 self.command, _sanitize_url(self.path), client)

    def _client_ip(self):
        if TRUST_PROXY:
            peer = self.client_address[0] if self.client_address else ""
            if _trusted_proxy_ok(peer):
                xff = self.headers.get("X-Forwarded-For", "")
                if xff:
                    return xff.split(",")[0].strip()
        return self.client_address[0] if self.client_address else "-"

    def _body(self):
        """与原实现语义一致：读已缓冲请求体，校验上限、宽容解析 JSON。"""
        n = len(self._raw_body)
        if n > MAX_BODY:
            raise BodyTooLarge("body=%d 超过上限 %d" % (n, MAX_BODY))
        raw = self._raw_body.decode("utf-8", "replace") if n > 0 else ""
        if not raw:
            LOG.info("请求体 size=0")
            return {}
        try:
            obj = json.loads(raw)
        except Exception as e:
            LOG.warning("请求体不是合法 JSON size=%d: %s", n, e)
            return {}
        if not isinstance(obj, dict):
            LOG.info("请求体 size=%d type=%s", n, type(obj).__name__)
            return {}
        if LOG.isEnabledFor(logging.DEBUG):
            LOG.debug("请求体 size=%d body=%s", n, _mask_body(raw))
        else:
            LOG.info("请求体 size=%d fields=%s",
                     n, ",".join(sorted(str(k) for k in obj)))
        return obj

    def build_response(self):
        body = b"".join(self._body_chunks)
        headers = {}
        for key, value in self._pending_headers:
            headers.setdefault(key, value)
        return Response(content=body, status_code=self._pending_status or 200,
                        headers=headers)


def _raw_target(request):
    """还原 http.server 的 self.path（原始请求目标，路径+查询串均未解码）。"""
    scope = request.scope
    raw_path = scope.get("raw_path")
    if raw_path:
        path = raw_path.decode("latin-1")
    else:
        path = scope.get("path", "/")
    qs = scope.get("query_string") or b""
    return path + ("?" + qs.decode("latin-1") if qs else "")


def _client_ip(request):
    if TRUST_PROXY:
        peer = request.client.host if request.client else ""
        if _trusted_proxy_ok(peer):
            xff = request.headers.get("X-Forwarded-For", "")
            if xff:
                return xff.split(",")[0].strip()
    return request.client.host if request.client else "-"


class ApiError(Exception):
    """依赖/中间件主动返回的业务错误，统一由异常处理器转成原 JSON 响应形状。"""

    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload


def _api_error_headers(request):
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
        "X-Trace-Id": getattr(request.state, "trace_id", "-"),
        "X-Content-Type-Options": "nosniff",
    }
    allow = _cors_allow((request.headers.get("Origin") or "").strip())
    if allow is not None:
        headers.update({
            "Access-Control-Allow-Origin": allow,
            "Access-Control-Allow-Headers": "Content-Type, Authorization",
            "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
            "Access-Control-Expose-Headers": "X-Trace-Id",
        })
    return headers


def _sync_trace(request):
    """把中间件签发的 trace_id 同步到当前线程（依赖可能运行在不同线程）。"""
    trace = getattr(request.state, "trace_id", None)
    if trace:
        _trace_local.id = trace
    return trace or "-"


def _get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def _general_rate_limit(request: Request,
                        runtime: Runtime = Depends(_get_runtime)) -> None:
    """全局限流依赖：替代原 Handler._rate_ok，按 IP 每分钟计数。"""
    ip = _client_ip(request)
    if not runtime.rate_limiter.allow(ip):
        _sync_trace(request)
        LOG.warning("触发限流 client=%s path=%s", ip, _sanitize_url(_raw_target(request)))
        raise ApiError(429, {"success": False, "error": "请求过于频繁，请稍后再试",
                             "output": "", "count": 0, "rows": []})


def _token_required_for(payload):
    """按端点生成 Bearer 鉴权依赖；不同端点保留原有 401 响应形状。"""
    def dependency(request: Request,
                   runtime: Runtime = Depends(_get_runtime),
                   credentials: HTTPAuthorizationCredentials = Security(
                       HTTPBearer(auto_error=False))):
        token = runtime.token
        if token and (credentials is None or credentials.credentials != token):
            _sync_trace(request)
            LOG.warning("未授权访问 path=%s client=%s",
                        _sanitize_url(_raw_target(request)), _client_ip(request))
            raise ApiError(401, payload)
    return dependency


def _export_code_or_token_required(request: Request,
                                   runtime: Runtime = Depends(_get_runtime),
                                   credentials: HTTPAuthorizationCredentials = Security(
                                       HTTPBearer(auto_error=False))):
    """课表导出鉴权：未配置 token 或 Bearer 正确时放行；
    GET 携带有效短时下载码（code）时也放行，供浏览器直接点下载链接。"""
    token = runtime.token
    if not token:
        return
    if credentials is not None and credentials.credentials == token:
        return
    if request.method == "GET" and request.query_params.get("code"):
        # 只要带 code 就放行到 handler，由 _export 返回“链接已失效”等友好错误
        return
    _sync_trace(request)
    LOG.warning("未授权访问 path=%s client=%s",
                _sanitize_url(_raw_target(request)), _client_ip(request))
    raise ApiError(401, {"success": False, "error": "token required",
                         "output": "", "count": 0, "rows": []})


def _make_lifespan(cfg):
    """FastAPI lifespan：启动时拉起后台线程，退出时置停止事件并等待线程结束。"""
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        _apply_thread_pool_size(getattr(cfg, "thread_pool_size", 40))
        stop = threading.Event()
        threads = _start_background(app.state.runtime, cfg, stop)
        try:
            yield
        finally:
            stop.set()
            for t in threads:
                t.join(timeout=3)
    return lifespan


class _LoginBody(BaseModel):
    username: str = Field(..., description="学号")
    password: str = Field(..., description="密码")


class _LoginStatusBody(BaseModel):
    session: str = Field("", description="会话 ID（/login 返回）")


class _DeepBody(BaseModel):
    username: str = Field("", description="学号")
    password: str = Field("", description="密码")


class _GradesBody(BaseModel):
    username: str = Field("", description="学号（与 session 二选一）")
    password: str = Field("", description="密码（与 session 二选一）")
    session: str = Field("", description="会话 ID（/login 返回）")
    semesters: str = Field("", description="空=当前学期；单学期/逗号多学期/recent:N/all",
                           json_schema_extra={"example": "2025-2026-2"})
    include_all: bool = Field(False, description="true 时附带全部成绩与学分概况")
    include_rows: bool = Field(True, description="false 时 rows 置空")
    include_output: bool = Field(True, description="false 时省略 output（省 LLM token）")
    include_sensitive_info: bool = Field(False, description="true 时返回身份证号与证件照")


class _ScheduleBody(BaseModel):
    username: str = Field("", description="学号（与 session 二选一）")
    password: str = Field("", description="密码（与 session 二选一）")
    session: str = Field("", description="会话 ID（/login 返回）")
    semester: str = Field("", description="学期，如 2025-2026-2",
                          json_schema_extra={"example": "2025-2026-2"})
    weeks: Optional[Union[str, List[int]]] = Field(
        None, description="周次：逗号/区间字符串（如 1-8,10,13-18）或整数列表；空=全部周")
    odd_or_double: int = Field(1, description="单双周：1 或 2")
    include_rows: bool = Field(True, description="false 时 rows 置空")
    include_details: bool = Field(True, description="false 时 output 省略课程明细表")


class _ExportBody(BaseModel):
    code: str = Field("", description="短时下载码（/get_schedule 响应 download_url 中返回，推荐）")
    session: str = Field("", description="会话 ID（与 username/password 二选一）")
    username: str = Field("", description="学号（与 session 二选一）")
    password: str = Field("", description="密码（与 session 二选一）")
    semester: str = Field("", description="学期，如 2025-2026-2",
                          json_schema_extra={"example": "2025-2026-2"})
    weeks: Optional[Union[str, List[int]]] = Field(
        None, description="周次：逗号/区间字符串或整数列表；空=全部周")
    odd_or_double: int = Field(1, description="单双周：1 或 2")


class _JumpBody(BaseModel):
    session: str = Field("", description="会话 ID（/login 返回）")
    page: str = Field("home", description="页面：默认 home；或教务系统内相对路径/锚点")
    json_mode: bool = Field(False, alias="json", description="true 返回结构化 JSON")
    redirect: bool = Field(False, description="true 直接 302 到 login_url")
    verify: bool = Field(False, description="true 实请求目标页做可达性检查")
    warm: bool = Field(False, description="配合 redirect=1 时 302 到 warm_url")


def _body_spec_from_model(model, description="", example=None):
    """由 Pydantic 模型生成 OpenAPI requestBody（只影响 /docs，不改服务端解析）。"""
    schema = model.model_json_schema()
    spec = {"required": bool(schema.get("required")),
            "description": description,
            "content": {"application/json": {"schema": schema}}}
    if example is not None:
        spec["content"]["application/json"]["example"] = example
    return spec


def _query(name, schema, description="", required=False):
    return {"name": name, "in": "query", "required": required,
            "schema": schema, "description": description}


# /docs 展示用：POST 请求体来自 Pydantic 模型，GET 查询参数保持显式声明
# （不参与服务端解析，行为与原实现一致）
OPENAPI_DOCS = {
    "/login": {
        "post": {
            "requestBody": _body_spec_from_model(
                _LoginBody,
                example={"username": "学号", "password": "密码"},
                description="登录学校教务系统，换取 session。",
            ),
        },
    },
    "/login/status": {
        "get": {
            "parameters": [_query("session", {"type": "string"},
                                  "会话 ID（/login 返回）", required=True)],
        },
        "post": {
            "requestBody": _body_spec_from_model(
                _LoginStatusBody,
                example={"session": "<SESSION>"},
                description="查询会话有效性；缺失时返回 {valid:false}。",
            ),
        },
    },
    "/health/deep": {
        "get": {
            "parameters": [
                _query("username", {"type": "string"}, "学号", required=True),
                _query("password", {"type": "string"}, "密码", required=True),
            ],
        },
        "post": {
            "requestBody": _body_spec_from_model(
                _DeepBody,
                example={"username": "学号", "password": "密码"},
                description="真实登录 + 接口探测（30 秒节流）。",
            ),
        },
    },
    "/get_grades": {
        "post": {
            "requestBody": _body_spec_from_model(
                _GradesBody,
                example={"session": "<SESSION>", "semesters": "2025-2026-2",
                         "include_all": True, "include_rows": True,
                         "include_output": False, "include_sensitive_info": False},
                description="成绩查询；session 与 username/password 至少提供一项。",
            ),
        },
    },
    "/get_schedule": {
        "post": {
            "requestBody": _body_spec_from_model(
                _ScheduleBody,
                example={"session": "<SESSION>", "semester": "2025-2026-2",
                         "weeks": "1-8,10,13-18", "odd_or_double": 1,
                         "include_rows": True, "include_details": True},
                description="课表查询；会话查询成功时响应附 download_url。",
            ),
        },
    },
    "/get_schedule/export": {
        "get": {
            "parameters": [
                _query("code", {"type": "string"}, "短时下载码（/get_schedule 成功响应 download_url 中返回，推荐）"),
                _query("session", {"type": "string"}, "会话 ID（与 username/password 二选一）"),
                _query("username", {"type": "string"}, "学号（与 session 二选一）"),
                _query("password", {"type": "string"}, "密码（与 session 二选一）"),
                _query("semester", {"type": "string"}, "学期，如 2025-2026-2"),
                _query("weeks", {"type": "string"}, "周次，如 1-8,10,13-18；空=全部周"),
                _query("odd_or_double", {"type": "integer"}, "单双周：1 或 2"),
            ],
        },
        "post": {
            "requestBody": _body_spec_from_model(
                _ExportBody,
                example={"session": "<SESSION>", "semester": "2025-2026-2",
                         "weeks": "1-8,10,13-18", "odd_or_double": 1},
                description="下载课表 Word 文件（attachment）。",
            ),
        },
    },
    "/jump": {
        "get": {
            "parameters": [
                _query("session", {"type": "string"}, "会话 ID（/login 返回）", required=True),
                _query("page", {"type": "string"}, "页面：默认 home；或教务系统内相对路径/锚点"),
                _query("json", {"type": "boolean"}, "true 返回结构化 JSON"),
                _query("redirect", {"type": "boolean"}, "true 直接 302 到 login_url"),
                _query("verify", {"type": "boolean"}, "true 实请求目标页做可达性检查"),
                _query("warm", {"type": "boolean"}, "配合 redirect=1 时 302 到 warm_url"),
            ],
        },
        "post": {
            "requestBody": _body_spec_from_model(
                _JumpBody,
                example={"session": "<SESSION>", "page": "home",
                         "json": True, "redirect": False},
                description="复用登录会话生成免密直达教务系统的中转链接。",
            ),
        },
    },
    "/jump/go": {
        "get": {
            "parameters": [
                _query("code", {"type": "string"}, "/jump 签发的短时跳转码", required=True),
                _query("app", {"type": "boolean"}, "true 直接 302 到教务 SSO"),
                _query("plain", {"type": "boolean"}, "true 直接 302 到门户回调"),
                _query("json", {"type": "boolean"}, "true 返回双票据 JSON"),
            ],
        },
    },
}


def _docs_patch(app):
    """为 /docs 与 /openapi.json 补充可填写的请求体/查询参数（不改服务端行为）。"""
    original_openapi = app.openapi

    def openapi():
        schema = original_openapi()
        for path, methods in OPENAPI_DOCS.items():
            for method, spec in methods.items():
                op = schema.get("paths", {}).get(path, {}).get(method)
                if op is None:
                    continue
                if "requestBody" in spec:
                    op["requestBody"] = spec["requestBody"]
                if "parameters" in spec:
                    existing = {p.get("name") for p in op.get("parameters", [])}
                    op.setdefault("parameters", []).extend(
                        p for p in spec["parameters"] if p.get("name") not in existing)
        return schema

    app.openapi = openapi


async def _read_limited_body(request: Request):
    """流式读取请求体并实时限制大小，避免超大请求先整块读入内存。"""
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY:
            raise BodyTooLarge("body=%d 超过上限 %d" % (total, MAX_BODY))
        chunks.append(chunk)
    return b"".join(chunks)


def _apply_thread_pool_size(size):
    """调整 anyio 默认线程池大小（run_in_threadpool 使用），默认 40。"""
    try:
        import anyio
        anyio.to_thread.current_default_thread_limiter().total_tokens = max(4, int(size))
    except Exception as e:
        LOG.warning("设置线程池大小失败 size=%s error=%s", size, e)


def make_app(cfg=None, start_background=True):
    """构建 FastAPI 应用。

    cfg=None 时读取 JWXT_* 环境变量；start_background=False 用于测试/内嵌场景，
    不注册 lifespan（后台探测与清理线程只在服务真正启动时拉起）。
    """
    cfg = cfg or load_config()
    apply_config(cfg)
    setup_logging(getattr(logging, str(cfg.log_level).strip().upper(), logging.INFO),
                     (cfg.log_file or "").strip())

    legacy = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".jwxt_sessions.json")
    if os.path.exists(legacy):
        LOG.warning("检测到旧版会话文件 %s（内含明文密码）。服务已改为内存会话，不再读写该文件，请尽快删除。",
                    legacy)

    runtime = Runtime(cfg)
    if not runtime.token:
        LOG.warning("JWXT_API_TOKEN 未配置：接口无 Bearer 鉴权，请勿直接暴露公网")
    if cfg.max_workers != 32:
        LOG.warning("JWXT_MAX_WORKERS/--max-workers 已废弃且不再参与并发控制，请移除该配置")

    app = FastAPI(
        title="jwxt-service (FastAPI)",
        version=VERSION,
        description="教务信息查询 Web 服务（成绩/课表/学籍/免密跳转，Dify HTTP 节点调用）。"
                    "原 http.server 实现已封装为 FastAPI，路由与响应语义保持不变。",
        lifespan=_make_lifespan(cfg) if start_background else None,
    )
    app.state.runtime = runtime

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        trace = secrets.token_hex(6)
        request.state.trace_id = trace
        _trace_local.id = trace
        response = await call_next(request)
        response.headers.setdefault("X-Trace-Id", trace)
        return response

    @app.middleware("http")
    async def concurrency_middleware(request: Request, call_next):
        limiter = runtime.concurrency_limiter
        if limiter is not None and not limiter.try_acquire():
            if not getattr(request.state, "trace_id", None):
                request.state.trace_id = secrets.token_hex(6)
                _trace_local.id = request.state.trace_id
            LOG.warning("触发并发上限 path=%s client=%s",
                        _sanitize_url(_raw_target(request)), _client_ip(request))
            return JSONResponse(
                status_code=503,
                content={"success": False,
                         "error": "服务繁忙，请稍后再试",
                         "output": "", "count": 0, "rows": []},
                headers=_api_error_headers(request))
        try:
            return await call_next(request)
        finally:
            if limiter is not None:
                limiter.release()

    @app.exception_handler(ApiError)
    async def api_error_handler(request: Request, exc: ApiError):
        return JSONResponse(status_code=exc.status_code, content=exc.payload,
                            headers=_api_error_headers(request))

    @app.exception_handler(StarletteHTTPException)
    async def http_error_handler(request: Request, exc: StarletteHTTPException):
        if exc.status_code == 405:
            # 已知路径但方法不支持：返回服务统一 JSON 契约
            return JSONResponse(
                status_code=405,
                content={"success": False, "error": "method not allowed",
                         "output": "", "count": 0, "rows": []},
                headers=_api_error_headers(request))
        return JSONResponse(status_code=exc.status_code,
                            content={"detail": exc.detail},
                            headers=_api_error_headers(request))

    async def dispatch(request: Request) -> Response:
        try:
            cl = request.headers.get("content-length")
            if cl:
                try:
                    if int(cl) > MAX_BODY:
                        raise BodyTooLarge("content-length=%s 超过上限 %d"
                                           % (cl, MAX_BODY))
                except ValueError:
                    pass
            body = await _read_limited_body(request)
        except BodyTooLarge as e:
            LOG.warning("请求体超限 path=%s %s", _sanitize_url(_raw_target(request)), e)
            headers = _api_error_headers(request)
            # 请求体未消费完，必须关闭连接，避免 keep-alive 下残留 body 污染下一请求
            headers["Connection"] = "close"
            return JSONResponse(
                status_code=413,
                content={"success": False,
                         "error": "请求体过大（上限 %d 字节）" % MAX_BODY,
                         "output": "", "count": 0, "rows": []},
                headers=headers)
        handler = FastHandler(runtime, request.method, _raw_target(request),
                              request.headers, body, _client_ip(request),
                              trace_id=getattr(request.state, "trace_id", None))

        def run():
            try:
                if request.method == "OPTIONS":
                    handler.do_OPTIONS()
                    return
                handler._begin()
                if request.method == "GET":
                    handler._handle_get()
                else:
                    handler._handle_post()
            except BodyTooLarge as e:
                LOG.warning("请求体超限 path=%s %s", urlparse(handler.path).path, e)
                handler._extra_headers.append(("Connection", "close"))
                handler._reply(413, {"success": False,
                                     "error": "请求体过大（上限 %d 字节）" % MAX_BODY,
                                     "output": "", "count": 0, "rows": []})
            except Exception as e:
                LOG.exception("请求处理异常 method=%s path=%s", request.method,
                              _sanitize_url(handler.path))
                try:
                    with runtime.state.lock:
                        runtime.state.last_real = {"at": int(time.time()),
                                                   "endpoint": urlparse(handler.path).path,
                                                   "ok": False, "count": 0,
                                                   "error": "%s: %s" % (type(e).__name__, e)}
                except Exception:
                    pass
                handler._reply(500, _internal_error_body())

        await run_in_threadpool(run)
        return handler.build_response()

    # 与原 Handler 的路径集合一致；额外注册全路径兜底以复现原 404 JSON。
    # 每个方法单独注册（OpenAPI operationId 唯一），请求仍统一进入 dispatch，
    # 由原 Handler 按方法/路径分发，行为与原 http.server 一致。
    # 只注册端点实际支持的方法，避免 OpenAPI/路由声明出不存在的 GET/POST；
    # OPTIONS 仅保留在已知路径上供 CORS 预检，兜底路径只处理 GET/POST。
    routes = [
        ("/", "root", ("GET",)),
        ("/health", "health", ("GET",)),
        ("/health/deep", "health_deep", ("GET", "POST")),
        ("/login", "login", ("POST",)),
        ("/login/status", "login_status", ("GET", "POST")),
        ("/get_grades", "get_grades", ("POST",)),
        ("/get_schedule", "get_schedule", ("POST",)),
        ("/get_schedule/export", "get_schedule_export", ("GET", "POST")),
        ("/jump", "jump", ("GET", "POST")),
        ("/jump/go", "jump_go", ("GET",)),
        ("/metrics", "metrics", ("GET",)),
        ("/{full_path:path}", "fallback", ("GET", "POST")),
    ]
    protected_paths = {"/health/deep", "/login", "/get_grades", "/get_schedule",
                       "/get_schedule/export", "/jump", "/metrics"}
    if PROTECT_LOGIN_STATUS and runtime.token:
        protected_paths.add("/login/status")
    token_payloads = {
        # /jump 的 401 响应形状与原实现一致（含 url 字段）
        "/jump": {"success": False, "error": "token required",
                  "url": "", "count": 0, "rows": []},
    }
    default_token_payload = {"success": False, "error": "token required",
                             "output": "", "count": 0, "rows": []}
    for path, name, methods in routes:
        route_methods = methods + (("OPTIONS",) if path != "/{full_path:path}" else ())
        for method in route_methods:
            deps = []
            if method != "OPTIONS":
                deps.append(Depends(_general_rate_limit))
                if path in protected_paths:
                    if path == "/get_schedule/export":
                        deps.append(Depends(_export_code_or_token_required))
                    else:
                        payload = token_payloads.get(path, default_token_payload)
                        deps.append(Depends(_token_required_for(payload)))
            app.add_api_route(path, dispatch, methods=[method], name=name,
                              operation_id="jwxt_%s_%s" % (name, method.lower()),
                              include_in_schema=(method != "OPTIONS"),
                              dependencies=deps)
    _docs_patch(app)
    return app


def _parser():
    ap = argparse.ArgumentParser(
        description="教务信息查询 Web 服务（FastAPI/uvicorn 封装）；"
                    "所有参数均有同名 JWXT_* 环境变量，生产推荐用环境变量配置")
    ap.add_argument("--version", action="version", version="jwxt-service %s" % VERSION)
    ap.add_argument("--host", default=_env("JWXT_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(_env("JWXT_PORT", "8766")))
    ap.add_argument("--token", default=_env("JWXT_API_TOKEN", ""),
                    help="可选 Bearer token（API 网关鉴权）")
    ap.add_argument("--redis-url", default=_env("JWXT_REDIS_URL", ""),
                    help="Redis 连接串（可选；配置后启用多实例共享状态，"
                         "如 redis://:pass@host:6379/0）")
    ap.add_argument("--session-ttl-hours", type=int,
                    default=int(_env("JWXT_SESSION_TTL_HOURS", "1")),
                    help="会话闲置有效期（小时，默认 1；成功使用自动滑动续期，Redis 模式同样生效）")
    ap.add_argument("--rate-limit", type=int,
                    default=int(_env("JWXT_RATE_LIMIT", "600")),
                    help="每 IP 每分钟最大请求数（0 关闭）")
    ap.add_argument("--login-rate-limit", type=int,
                    default=int(_env("JWXT_LOGIN_RATE_LIMIT", "60")),
                    help="每 IP 每分钟最大登录尝试数（0 关闭）")
    ap.add_argument("--max-workers", type=int,
                    default=int(_env("JWXT_MAX_WORKERS", "32")),
                    help="最大并发工作线程数（uvicorn 默认线程池，供兼容保留）")
    ap.add_argument("--thread-pool-size", type=int,
                    default=int(_env("JWXT_THREAD_POOL_SIZE", "40")),
                    help="anyio 同步任务线程池大小（run_in_threadpool 使用，默认 40）")
    ap.add_argument("--warm-wait", type=int,
                    default=int(_env("JWXT_WARM_WAIT", str(WARM_WAIT))),
                    help="/login 后台预热学生门户的等待上限秒数（默认 10）")
    ap.add_argument("--idle-timeout", type=int,
                    default=int(_env("JWXT_IDLE_TIMEOUT", str(IDLE_TIMEOUT))),
                    help="客户端 keep-alive 空闲超时秒数（默认 10）")
    ap.add_argument("--limit-concurrency", type=int,
                    default=int(_env("JWXT_LIMIT_CONCURRENCY", str(LIMIT_CONCURRENCY))),
                    help="服务端并发请求上限（0 不限制；超出返回 503）")
    ap.add_argument("--upstream-sem-timeout", type=float,
                    default=float(_env("JWXT_UPSTREAM_SEM_TIMEOUT", str(UPSTREAM_SEM_TIMEOUT))),
                    help="上游全局并发槽位等待超时秒数（默认 5）")
    ap.add_argument("--max-semesters", type=int,
                    default=int(_env("JWXT_MAX_SEMESTERS", str(MAX_SEMESTERS))),
                    help="显式 semesters 列表的最大学期数（默认 20）")
    ap.add_argument("--upstream-parallel", type=int,
                    default=int(_env("JWXT_UPSTREAM_PARALLEL", "4")),
                    help="查询时对学校上游的最大并发请求数（默认 4）")
    ap.add_argument("--upstream-global", type=int,
                    default=int(_env("JWXT_UPSTREAM_GLOBAL", "8")),
                    help="上游并发上限（默认 8；内存模式为进程级，Redis 模式为多副本全局共享）")
    ap.add_argument("--health-username", default=_env("JWXT_HEALTH_USERNAME", ""),
                    help="周期真实探测账号（需配合 --health-password）")
    ap.add_argument("--health-password", default=_env("JWXT_HEALTH_PASSWORD", ""))
    ap.add_argument("--health-interval", type=int,
                    default=int(_env("JWXT_HEALTH_INTERVAL", "300")))
    ap.add_argument("--grades-cache-ttl", type=int,
                    default=int(_env("JWXT_GRADES_CACHE_TTL", "300")),
                    help="成绩查询结果缓存秒数（按 学号+查询参数；0 关闭；内存/Redis 模式均生效）")
    ap.add_argument("--schedule-cache-ttl", type=int,
                    default=int(_env("JWXT_SCHEDULE_CACHE_TTL", "300")),
                    help="课表查询结果缓存秒数（按 学号+学期+周次；0 关闭；内存/Redis 模式均生效）")
    ap.add_argument("--schedule-class-share",
                    default=_env("JWXT_SCHEDULE_CLASS_SHARE", "0"),
                    help="课表缓存按班级共享 1/0（默认关闭）")
    ap.add_argument("--jump-cache-ttl", type=int,
                    default=int(_env("JWXT_JUMP_CACHE_TTL", "30")),
                    help="免密跳转签票能力缓存秒数（按 session；0 关闭）")
    ap.add_argument("--jump-code-ttl", type=int,
                    default=int(_env("JWXT_JUMP_CODE_TTL", "60")),
                    help="/jump 跳转码有效期秒数（默认 60）")
    ap.add_argument("--health-probe-interval", type=int,
                    default=int(_env("JWXT_HEALTH_PROBE_INTERVAL", "10")),
                    help="/health 后台探测间隔秒数（默认 10）")
    ap.add_argument("--verify-tls", default=_env("JWXT_VERIFY_TLS", "1"),
                    help="上游 TLS 证书校验 1/0（默认开启）")
    ap.add_argument("--login-reuse", default=_env("JWXT_LOGIN_REUSE", "1"),
                    help="同账号会话复用 1/0（默认开启）")
    ap.add_argument("--login-reuse-max-age", type=int,
                    default=int(_env("JWXT_LOGIN_REUSE_MAX_AGE", "1800")),
                    help="同账号会话复用的最大签发时长（秒），默认 1800")
    ap.add_argument("--login-reuse-probe", default=_env("JWXT_LOGIN_REUSE_PROBE", "1"),
                    help="复用前轻量探测学校会话有效性 1/0（默认开启）")
    ap.add_argument("--max-sessions", type=int,
                    default=int(_env("JWXT_MAX_SESSIONS", "5000")),
                    help="会话数量上限，超出淘汰最旧（Redis 模式同样生效）")
    ap.add_argument("--public-url", default=_env("JWXT_PUBLIC_URL", ""),
                    help="对外公开地址（如 https://school.dev.lizf.cn）")
    ap.add_argument("--cors-origin", default=_env("JWXT_CORS_ORIGIN", ""),
                    help="允许的跨域来源；空 = 不返回 CORS 头")
    ap.add_argument("--trust-proxy", default=_env("JWXT_TRUST_PROXY", "0"),
                    help="信任 X-Forwarded-For 作为客户端 IP 1/0（仅明确反代场景开启）")
    ap.add_argument("--allow-get-credentials", default=_env("JWXT_ALLOW_GET_CREDENTIALS", "1"),
                    help="允许 GET 查询串携带 username/password 1/0（默认 1 兼容旧行为；0 时返回 400）")
    ap.add_argument("--protect-login-status", default=_env("JWXT_PROTECT_LOGIN_STATUS", "0"),
                    help="配置 token 后 /login/status 是否也要求 Bearer 1/0（默认 0）")
    ap.add_argument("--trusted-proxy-cidrs", default=_env("JWXT_TRUSTED_PROXY_CIDRS", ""),
                    help="TRUST_PROXY 时的可信代理网段（逗号分隔 CIDR；空=信任所有 XFF）")
    ap.add_argument("--dns-fallback-ips",
                    default=_env("JWXT_DNS_FALLBACK_IPS", ",".join(DNS_FALLBACK_IPS)),
                    help="DNS 解析失败时的兜底 IP 列表（逗号分隔）")
    ap.add_argument("--log-file", default=_env("JWXT_LOG_FILE", ""),
                    help="日志文件路径（可选；自动按 10MB 轮转，保留 5 份）")
    ap.add_argument("--log-level", default=_env("JWXT_LOG_LEVEL", "INFO"),
                    help="日志级别 DEBUG/INFO/WARNING/ERROR（默认 INFO）")
    return ap


def run(argv=None):
    a = _parser().parse_args(argv)
    cfg = Config(**vars(a))
    app = make_app(cfg)
    LOG.info("jwxt-service %s (FastAPI) listening on http://%s:%d",
             VERSION, cfg.host, cfg.port)
    LOG.info("FastAPI 文档: http://127.0.0.1:%d/docs", cfg.port)
    uvicorn.run(app, host=cfg.host, port=cfg.port,
                log_level=str(cfg.log_level).strip().lower() or "info",
                timeout_keep_alive=getattr(cfg, "idle_timeout", IDLE_TIMEOUT),
                # 关闭 uvicorn 原始 access log：其 request line 会原样记录查询串
                # （含 password/session 等敏感参数）；服务自身的日志已脱敏且完整
                access_log=False)
