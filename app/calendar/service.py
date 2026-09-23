"""日历订阅业务闭环：开启 / 状态识别 / 刷新 / 轮换 / 关闭 / 订阅源。

身份权威性分级（与设计文档一致）：
- L1：刚刚成功的课表查询签发的短时 `verified_token`（零学校流量即可下结论）
- L2：本地密码比对命中（订阅创建时已用该密码验证过）
- L3：回退执行一次真实学校登录（仅在 L1/L2 都不成立时）

安全约束：密码只以密文落盘；订阅令牌只存哈希 + 加密副本；任何路径都不输出明文凭据。
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from .config import (CalendarConfig, CalendarConfigError, Term,
                     load_config_dict, read_config_payload)
from .crypto import CalendarCrypto, CalendarCryptoError
from .ics import build_events, render
from .qr import QrPayloadError, qr_matrix, webcal_url
from .rules import ScheduleDataError
from .store import CalendarStore, now_iso

FEED_PATH = "/cal/%s.ics"


class CalendarError(Exception):
    """业务错误；HTTP 层据此返回状态码与可读文案。"""

    def __init__(self, status_code: int, detail: str, code: str = "") -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail
        self.code = code


class CalendarAuthError(CalendarError):
    def __init__(self, detail: str = "账号或密码错误") -> None:
        super().__init__(401, detail, "credential_error")


class CalendarUpstreamError(CalendarError):
    def __init__(self, detail: str = "学校服务暂时不可用，请稍后重试") -> None:
        super().__init__(503, detail, "upstream_error")


@dataclass(frozen=True)
class CalendarSettings:
    enabled: bool = False
    master_key: str = ""
    db_path: str = "/var/lib/edu-query/calendar/calendar.db"
    config_path: str = "config/calendar.json"
    public_base_url: str = ""
    default_refresh: int = 43200
    min_refresh: int = 3600
    max_failures: int = 3
    pause_hours: int = 24
    max_per_owner: int = 5
    ttl_days: int = 210


class SchoolPort:
    """内嵌查询代理适配：一次 /login + 一次 /get_schedule（带 rows）。"""

    def __init__(self, client: Any) -> None:
        self.client = client

    async def schedule_rows(self, username: str, password: str, semester: str) -> List[Dict[str, Any]]:
        from ..pipeline import ServiceError

        try:
            login = await self.client.post("/login", {"username": username, "password": password})
        except ServiceError as exc:
            if exc.status_code == 401:
                raise CalendarAuthError() from exc
            raise CalendarUpstreamError() from exc
        session = str((login or {}).get("session") or "").strip()
        if not session:
            raise CalendarUpstreamError("登录未返回会话，请稍后重试")
        try:
            data = await self.client.post("/get_schedule", {
                "session": session, "semester": semester, "weeks": "all",
                "include_rows": "true",
            })
        except ServiceError as exc:
            if exc.status_code == 401:
                raise CalendarAuthError() from exc
            raise CalendarUpstreamError() from exc
        if not (data or {}).get("success"):
            message = str((data or {}).get("error") or "")
            if "密码" in message or "PASSERROR" in message or "锁定" in message:
                raise CalendarAuthError()
            raise CalendarUpstreamError(message[:120] or "学校查询失败")
        return list((data or {}).get("rows") or [])


class CalendarService:
    def __init__(self, settings: CalendarSettings, store: CalendarStore,
                 crypto: CalendarCrypto, config: CalendarConfig,
                 school: SchoolPort, *,
                 config_payload: Optional[Dict[str, Any]] = None,
                 config_source: str = "file") -> None:
        self.settings = settings
        self.store = store
        self.crypto = crypto
        self.config = config
        self._config_payload = dict(config_payload or config.to_dict())
        self._config_source = config_source if config_source in ("file", "database") else "file"
        self.school = school
        self._locks: Dict[str, asyncio.Lock] = {}
        self._attempt_lock = asyncio.Lock()

    # ---------------- 工具 ----------------
    def feed_url(self, token: str) -> str:
        base = (self.settings.public_base_url or "").rstrip("/")
        return base + FEED_PATH % token

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def normalized_refresh(self, value: Optional[int]) -> int:
        try:
            seconds = int(value) if value is not None else int(self.settings.default_refresh)
        except (TypeError, ValueError):
            seconds = int(self.settings.default_refresh)
        return max(int(self.settings.min_refresh), seconds)

    def _term(self, semester: str) -> Term:
        term = self.config.term(semester)
        if term is None:
            raise CalendarError(422, "该学期尚未配置教学周起始日期：%s" % semester,
                                "semester_not_configured")
        return term

    @staticmethod
    def _expiry(days: int) -> str:
        return (datetime.now().astimezone() + timedelta(days=max(1, int(days)))).isoformat(
            timespec="seconds")

    def _build_ics(self, rows: List[Dict[str, Any]], term: Term, sequence: int,
                   refresh_seconds: Optional[int] = None) -> Tuple[bytes, str, List[str]]:
        """生成 ICS；REFRESH-INTERVAL 必须取该订阅自己的刷新间隔而不是全局默认值。"""
        events, anomalies = build_events(rows, term, self.config, self.crypto, sequence)
        text = render(events, calendar_name="我的课表",
                      refresh_seconds=refresh_seconds or self.settings.default_refresh)
        payload = text.encode("utf-8")
        return payload, hashlib.sha256(payload).hexdigest(), anomalies

    # ---------------- 身份判定 ----------------
    def _password_matches(self, record: Dict[str, Any], password: str) -> bool:
        blob = record.get("cred_enc")
        if not blob:
            return False
        try:
            stored = self.crypto.decrypt(blob, "cred|%s" % record["id"])
        except CalendarCryptoError:
            return False
        return self.crypto.compare(stored, password)

    async def _authoritative(self, owner_hash: str, username: str, password: str,
                             verified_token: Optional[str], record: Optional[Dict[str, Any]]) -> str:
        """返回权威结论：active / reauth_needed / none。"""
        if record is not None and self._password_matches(record, password):
            return "active"
        if verified_token and self.crypto.check_verify_token(verified_token, owner_hash):
            return "reauth_needed" if record is not None else "none"
        # L3：回退真实登录（同时刷新课表行以证明凭据有效）
        await self.school.schedule_rows(username, password,
                                        (record or {}).get("semester") or "")
        return "reauth_needed" if record is not None else "none"

    def issue_verify_token(self, username: str) -> str:
        return self.crypto.issue_verify_token(self.crypto.owner_hash(username))

    # ---------------- 状态 ----------------
    async def status(self, username: str, password: str, semester: str,
                     verified_token: Optional[str] = None) -> Dict[str, Any]:
        owner_hash = self.crypto.owner_hash(username)
        record = self.store.get_by_owner_semester(owner_hash, semester)
        state = await self._authoritative(owner_hash, username, password, verified_token, record)
        others = [item["semester"] for item in self.store.list_by_owner(owner_hash)
                  if item["semester"] != semester]
        if record is None or state == "none":
            return {"state": "none", "enabled": False, "semester": semester,
                    "other_semesters": others}
        return {
            "state": state,
            "enabled": True,
            "semester": semester,
            "other_semesters": others,
            "refresh_interval": record["refresh_interval"],
            "last_refresh_at": record.get("last_fetch_at"),
            "last_status": record.get("last_fetch_status") or "pending",
            "feed_url": self._feed_url_of(record) if state in ("active", "reauth_needed") else None,
        }

    def _feed_url_of(self, record: Dict[str, Any]) -> str:
        try:
            token = self.crypto.decrypt(record["token_enc"], "token|%s" % record["id"])
        except CalendarCryptoError:
            return ""
        return self.feed_url(token)

    def qr(self, username: str, password: str, semester: str,
           verified_token: Optional[str] = None) -> Dict[str, Any]:
        """订阅二维码矩阵：只服务已通过 L1/L2 证明的调用方，且不触发学校流量。

        身份要求与状态查询一致：本地密码比对命中，或持有刚由课表查询签发的短时令牌。
        不在此走 L3 真实登录，避免用一次扫码动作额外打学校风控。
        """
        owner_hash = self.crypto.owner_hash(username)
        record = self.store.get_by_owner_semester(owner_hash, semester)
        if record is None:
            raise CalendarError(404, "未找到该学期的订阅", "not_found")
        proven = self._password_matches(record, password) or bool(
            verified_token and self.crypto.check_verify_token(verified_token, owner_hash))
        if not proven:
            raise CalendarAuthError("请先用当前密码重新授权，再生成扫码图像")
        url = self._feed_url_of(record)
        if not url:
            raise CalendarError(409, "订阅地址不可用，请先轮换地址", "token_unavailable")
        try:
            result = qr_matrix(webcal_url(url))
        except QrPayloadError as exc:
            raise CalendarError(500, "二维码生成失败，请改用复制订阅地址", "qr_failed") from exc
        result["scheme"] = "webcal"
        return result

    def local_state(self, username: str, password: str, semester: str) -> Dict[str, Any]:
        """L1 快路径：课表查询刚成功（密码已被学校接受）时的权威状态判定，零学校流量。"""
        owner_hash = self.crypto.owner_hash(username)
        record = self.store.get_by_owner_semester(owner_hash, semester)
        token = self.issue_verify_token(username)
        if record is None:
            return {"state": "none", "enabled": False, "semester": semester,
                    "verified_token": token}
        state = "active" if self._password_matches(record, password) else "reauth_needed"
        return {
            "state": state, "enabled": True, "semester": semester,
            "refresh_interval": record["refresh_interval"],
            "last_refresh_at": record.get("last_fetch_at"),
            "last_status": record.get("last_fetch_status"),
            "feed_url": self._feed_url_of(record),
            "verified_token": token,
        }

    # ---------------- 开启 / 重新授权 ----------------
    async def create(self, username: str, password: str, semester: str, *, weeks: str = "all",
                     refresh_interval: Optional[int] = None, reauthorize: bool = False) -> Dict[str, Any]:
        term = self._term(semester)
        owner_hash = self.crypto.owner_hash(username)
        existing = self.store.get_by_owner_semester(owner_hash, semester)
        if existing is not None:
            if self._password_matches(existing, password):
                return {"created": False, "state": "active", "semester": semester,
                        "feed_url": self._feed_url_of(existing),
                        "refresh_interval": existing["refresh_interval"]}
            if not reauthorize:
                raise CalendarError(409, "该学期已存在订阅且密码不同，请使用重新授权",
                                    "reauthorize_required")
        elif len(self.store.list_by_owner(owner_hash)) >= int(self.settings.max_per_owner):
            raise CalendarError(409, "订阅数量已达上限", "too_many_subscriptions")

        interval = self.normalized_refresh(refresh_interval)
        async with self._lock(owner_hash):
            rows = await self.school.schedule_rows(username, password, semester)
            sequence = int(existing["revision"]) + 1 if existing else 1
            payload, etag, anomalies = self._build_ics(rows, term, sequence, interval)
            if existing is None:
                token = self.crypto.new_token()
                calendar_id = str(uuid.uuid4())
                record, created = self.store.create(
                    owner_hash=owner_hash, semester=semester, weeks=weeks, options={},
                    refresh_interval=interval,
                    token_hash=self.crypto.token_hash(token),
                    token_enc=self.crypto.encrypt(token, "token|%s" % calendar_id),
                    username_enc=self.crypto.encrypt(username, "user|%s" % calendar_id),
                    cred_enc=self.crypto.encrypt(password, "cred|%s" % calendar_id),
                    expires_at=self._expiry(self.settings.ttl_days),
                    next_refresh_at=now_iso(interval),
                    calendar_id=calendar_id,
                )
                if not created:      # 并发创建：以既有记录为准（同 id 语义保持幂等）
                    calendar_id = record["id"]
                    token = self.crypto.decrypt(record["token_enc"], "token|%s" % calendar_id)
                else:
                    calendar_id = record["id"]
            else:
                calendar_id = existing["id"]
                token = self.crypto.decrypt(existing["token_enc"], "token|%s" % calendar_id)
                self.store.update_credentials(
                    calendar_id, self.crypto.encrypt(password, "cred|%s" % calendar_id),
                    self.crypto.encrypt(username, "user|%s" % calendar_id))
            self.store.save_snapshot(calendar_id, payload, etag, changed=True, status="ok",
                                     next_refresh_at=now_iso(interval), fail_count=0,
                                     clear_pause=True)
            self.store.audit(calendar_id, "reauthorize" if existing else "create", "user", "ok")
        result = {"created": existing is None, "state": "active", "semester": semester,
                  "feed_url": self.feed_url(token), "refresh_interval": interval,
                  "events_estimated": payload.count(b"BEGIN:VEVENT")}
        if anomalies:
            result["anomalies"] = anomalies[:5]
        return result

    # ---------------- 刷新 ----------------
    async def refresh(self, username: str, password: str, semester: str) -> Dict[str, Any]:
        owner_hash = self.crypto.owner_hash(username)
        record = self.store.get_by_owner_semester(owner_hash, semester)
        if record is None:
            raise CalendarError(404, "未找到该学期的订阅", "not_found")
        if not self._password_matches(record, password):
            await self.school.schedule_rows(username, password, semester)   # L3 校验
        term = self._term(semester)
        async with self._lock(record["id"]):
            last = record.get("last_fetch_at")
            cooldown = max(60, int(self.settings.min_refresh) // 60)
            if last:
                try:
                    delta = (datetime.now().astimezone()
                             - datetime.fromisoformat(last)).total_seconds()
                except ValueError:
                    delta = cooldown + 1
                if delta < cooldown:
                    return {"refreshed": False, "skipped": True,
                            "next_allowed_at": (datetime.fromisoformat(last)
                                                + timedelta(seconds=cooldown)).isoformat(
                                                    timespec="seconds"),
                            "state": "active"}
            rows = await self.school.schedule_rows(username, password, semester)
            payload, etag, anomalies = self._build_ics(rows, term, int(record["revision"]) + 1,
                                                       int(record["refresh_interval"]))
            changed = etag != (record.get("ics_etag") or "")
            self.store.save_snapshot(record["id"], payload, etag if changed else record.get("ics_etag") or etag,
                                     changed=changed, status="ok",
                                     next_refresh_at=now_iso(int(record["refresh_interval"])),
                                     fail_count=0, clear_pause=True)
            self.store.audit(record["id"], "refresh", "user", "ok")
        return {"refreshed": True, "changed": changed, "state": "active",
                "events_estimated": payload.count(b"BEGIN:VEVENT"),
                "anomalies": anomalies[:5]}

    async def refresh_record(self, record: Dict[str, Any], actor: str = "scheduler") -> str:
        """按记录刷新（后台调度/后台手工触发）；失败按策略退避，不抛异常给调用方。"""
        semester = record["semester"]
        term = self._term(semester)
        try:
            username = self.crypto.decrypt(record["username_enc"], "user|%s" % record["id"])
            password = self.crypto.decrypt(record["cred_enc"], "cred|%s" % record["id"])
        except CalendarCryptoError as exc:
            self.store.mark_failure(record["id"], status="config_error", error=str(exc),
                                    fail_count=int(record.get("fail_count") or 0) + 1,
                                    next_refresh_at=now_iso(24 * 3600), paused_until=None)
            return "config_error"
        if username and not self._password_matches(record, password):
            return "config_error"
        async with self._lock(record["id"]):
            try:
                rows = await self.school.schedule_rows(username, password, semester)
            except CalendarAuthError:
                failures = int(record.get("fail_count") or 0) + 1
                pause = None
                if failures >= int(self.settings.max_failures):
                    pause = now_iso(int(self.settings.pause_hours) * 3600)
                self.store.mark_failure(record["id"], status="credential_error",
                                        error="凭据失效或密码已变更", fail_count=failures,
                                        next_refresh_at=now_iso(2 * 3600), paused_until=pause)
                self.store.audit(record["id"], "refresh", actor, "credential_error")
                return "credential_error"
            except CalendarError as exc:
                failures = int(record.get("fail_count") or 0) + 1
                backoff = min(4, failures) * 1800
                self.store.mark_failure(record["id"], status="upstream_error",
                                        error=exc.detail, fail_count=failures,
                                        next_refresh_at=now_iso(backoff), paused_until=None)
                self.store.audit(record["id"], "refresh", actor, "upstream_error")
                return "upstream_error"
            payload, etag, _ = self._build_ics(rows, term, int(record["revision"]) + 1,
                                                int(record["refresh_interval"]))
            changed = etag != (record.get("ics_etag") or "")
            self.store.save_snapshot(record["id"], payload,
                                     etag if changed else record.get("ics_etag") or etag,
                                     changed=changed, status="ok",
                                     next_refresh_at=now_iso(int(record["refresh_interval"])),
                                     fail_count=0, clear_pause=True)
            self.store.audit(record["id"], "refresh", actor, "ok")
            return "ok"

    async def refresh_due(self, limit: int = 2, cooldown_seconds: int = 300) -> List[str]:
        """后台调度入口：原子抢占到期订阅并逐个刷新。"""
        claimed = self.store.claim_due(now_iso(), limit=limit, cooldown_seconds=cooldown_seconds)
        results = []
        for record in claimed:
            results.append(await self.refresh_record(record, actor="scheduler"))
        return results

    # ---------------- 轮换 / 关闭 ----------------
    async def rotate(self, username: str, password: str, semester: str, *,
                     verified_token: Optional[str] = None) -> Dict[str, Any]:
        owner_hash = self.crypto.owner_hash(username)
        record = self.store.get_by_owner_semester(owner_hash, semester)
        if record is None:
            raise CalendarError(404, "未找到该学期的订阅", "not_found")
        await self._authoritative(owner_hash, username, password, verified_token, record)
        token = self.crypto.new_token()
        self.store.rotate_token(record["id"], self.crypto.token_hash(token),
                                self.crypto.encrypt(token, "token|%s" % record["id"]))
        self.store.audit(record["id"], "rotate", "user", "ok")
        return {"rotated": True, "feed_url": self.feed_url(token), "semester": semester}

    async def close(self, username: str, password: str, semester: str, *,
                    verified_token: Optional[str] = None) -> Dict[str, Any]:
        owner_hash = self.crypto.owner_hash(username)
        record = self.store.get_by_owner_semester(owner_hash, semester)
        if record is None:
            return {"deleted": True, "already": True, "semester": semester}
        await self._authoritative(owner_hash, username, password, verified_token, record)
        deleted = self.store.delete(record["id"])
        self.store.audit(record["id"], "close", "user", "ok")
        return {"deleted": True, "already": not deleted, "semester": semester}

    # ---------------- 订阅源 ----------------
    def feed(self, token: str) -> Dict[str, Any]:
        record = self.store.get_by_token_hash(self.crypto.token_hash(token))
        if record is None:
            raise CalendarError(404, "not found", "not_found")
        if record.get("revoked_at"):
            raise CalendarError(404, "not found", "not_found")
        expires = record.get("expires_at")
        if expires:
            try:
                if datetime.fromisoformat(expires) < datetime.now().astimezone():
                    raise CalendarError(404, "not found", "not_found")
            except ValueError:
                pass
        blob = record.get("ics_blob")
        if not blob:
            raise CalendarError(404, "not found", "not_found")
        return {"body": bytes(blob), "etag": record.get("ics_etag") or hashlib.sha256(blob).hexdigest(),
                "last_modified": record.get("updated_at") or record.get("last_fetch_at")}


    # ---------------- 后台视图与配置 ----------------
    def default_semester(self) -> str:
        """最近配置的学期（用于 UI 默认值）。"""
        if not self.config.terms:
            return ""
        return max(self.config.terms.values(), key=lambda t: t.monday).semester

    def public_config(self) -> Dict[str, Any]:
        return {
            "default_semester": self.default_semester(),
            "semesters": sorted(self.config.terms),
            "inferred_periods": list(self.config.inferred_periods()),
            "default_refresh_interval": int(self.settings.default_refresh),
            "min_refresh_interval": int(self.settings.min_refresh),
        }

    # ---------------- 后台配置校准（节次时间 + 第一周定义） ----------------
    def admin_config(self) -> Dict[str, Any]:
        """返回当前生效配置（文件基线或后台校准快照）与派生信息。"""
        payload = copy.deepcopy(self._config_payload)
        payload["source"] = self._config_source
        payload["default_semester"] = self.default_semester()
        payload["inferred_periods"] = list(self.config.inferred_periods())
        return payload

    def _apply_payload(self, payload: Dict[str, Any], source: str) -> None:
        self.config = load_config_dict(payload)
        self._config_payload = copy.deepcopy(payload)
        self._config_source = source

    def _persist_config(self, payload: Dict[str, Any], action: str) -> Dict[str, Any]:
        try:
            load_config_dict(payload)
        except CalendarConfigError as exc:
            raise CalendarError(422, str(exc), "invalid_calendar_config") from exc
        self.store.set_config_json(payload)
        self._apply_payload(payload, "database")
        self.store.audit(None, "calendar_config_%s" % action, "admin", "ok")
        return self.admin_config()

    def admin_update_periods(self, periods: Dict[str, Any],
                             period_source: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """校准节次起止时间（可同时校准每节来源 official/inferred）。"""
        payload = copy.deepcopy(self._config_payload)
        payload["periods"] = periods
        if period_source is not None:
            payload["period_source"] = period_source
        return self._persist_config(payload, "periods")

    def admin_update_terms(self, terms: Dict[str, Any]) -> Dict[str, Any]:
        """校准学期基准（第一周周一、正式上课首日、教学周数、节假日例外）。"""
        payload = copy.deepcopy(self._config_payload)
        payload["terms"] = terms
        return self._persist_config(payload, "terms")

    def admin_reset_config(self) -> Dict[str, Any]:
        """删除后台校准快照，恢复仓库文件基线。"""
        self.store.clear_config_json()
        payload = read_config_payload(self.settings.config_path)
        self._apply_payload(payload, "file")
        self.store.audit(None, "calendar_config_reset", "admin", "ok")
        return self.admin_config()

    def admin_view(self, record: Dict[str, Any]) -> Dict[str, Any]:
        """后台视图：显示解密后的学号，但绝不返回密码或完整订阅 URL。"""
        from .store import _admin_state
        try:
            username = self.crypto.decrypt(record["username_enc"], "user|%s" % record["id"])
        except CalendarCryptoError:
            username = "<密钥不匹配>"
        token = ""
        try:
            token = self.crypto.decrypt(record["token_enc"], "token|%s" % record["id"])
        except CalendarCryptoError:
            token = ""
        return {
            "id": record["id"],
            "username": username,
            "semester": record["semester"],
            "state": _admin_state(record),
            "refresh_interval": record["refresh_interval"],
            "last_refresh_at": record.get("last_fetch_at"),
            "last_status": record.get("last_fetch_status"),
            "last_error": record.get("last_fetch_error") or "",
            "fail_count": int(record.get("fail_count") or 0),
            "paused_until": record.get("paused_until"),
            "created_at": record.get("created_at"),
            "expires_at": record.get("expires_at"),
            "has_credential": bool(record.get("cred_enc")),
            "feed_url_masked": ("%s/cal/%s….ics" % ((self.settings.public_base_url or "").rstrip("/"),
                                                    token[:4])) if token else "",
            "events": int(record.get("ics_blob").count(b"BEGIN:VEVENT")) if record.get("ics_blob") else 0,
        }

    def admin_find(self, calendar_id: str) -> Optional[Dict[str, Any]]:
        return self.store.get_by_id(calendar_id)

    async def admin_refresh(self, calendar_id: str) -> Dict[str, Any]:
        record = self.store.get_by_id(calendar_id)
        if record is None:
            raise CalendarError(404, "订阅不存在", "not_found")
        status = await self.refresh_record(record, actor="admin")
        return {"refreshed": status == "ok", "state": status}

    def admin_pause(self, calendar_id: str, hours: int = 24) -> Dict[str, Any]:
        record = self.store.get_by_id(calendar_id)
        if record is None:
            raise CalendarError(404, "订阅不存在", "not_found")
        until = (datetime.now().astimezone() + timedelta(hours=max(1, int(hours)))).isoformat(
            timespec="seconds")
        self.store.set_paused(calendar_id, until)
        self.store.audit(calendar_id, "pause", "admin", "ok")
        return {"paused_until": until}

    def admin_resume(self, calendar_id: str) -> Dict[str, Any]:
        record = self.store.get_by_id(calendar_id)
        if record is None:
            raise CalendarError(404, "订阅不存在", "not_found")
        self.store.set_paused(calendar_id, None)
        self.store.audit(calendar_id, "resume", "admin", "ok")
        return {"paused_until": None}

    def admin_rotate(self, calendar_id: str) -> Dict[str, Any]:
        record = self.store.get_by_id(calendar_id)
        if record is None:
            raise CalendarError(404, "订阅不存在", "not_found")
        token = self.crypto.new_token()
        self.store.rotate_token(calendar_id, self.crypto.token_hash(token),
                                self.crypto.encrypt(token, "token|%s" % calendar_id))
        self.store.audit(calendar_id, "rotate", "admin", "ok")
        return {"rotated": True, "feed_url": self.feed_url(token)}

    def admin_delete(self, calendar_id: str) -> Dict[str, Any]:
        record = self.store.get_by_id(calendar_id)
        if record is None:
            return {"deleted": True, "already": True}
        deleted = self.store.delete(calendar_id)
        self.store.audit(calendar_id, "delete", "admin", "ok")
        return {"deleted": True, "already": not deleted}

    def admin_list(self, *, username: Optional[str] = None, semester: Optional[str] = None,
                   state: Optional[str] = None, page: int = 1, size: int = 20) -> Dict[str, Any]:
        owner_hash = self.crypto.owner_hash(username) if username else None
        data = self.store.list_admin(owner_hash=owner_hash, semester=semester, state=state,
                                     page=page, size=size)
        data["items"] = [self.admin_view(item) for item in data["items"]]
        data["stats"] = self.store.stats()
        return data


def build_service(settings: CalendarSettings, client: Any) -> Optional[CalendarService]:
    """按配置构建服务；未启用或配置非法时返回 None（调用方决定是否阻止启动）。

    配置优先级：后台校准快照（SQLite calendar_config）> 仓库文件基线 config/calendar.json。
    首次启动没有快照时回退文件基线；后台保存校准后快照成为运行时权威配置。
    """
    if not settings.enabled:
        return None
    crypto = CalendarCrypto(settings.master_key)
    store = CalendarStore(settings.db_path)
    stored = store.get_config_json()
    if stored is not None:
        payload, source = stored, "database"
    else:
        payload, source = read_config_payload(settings.config_path), "file"
    config = load_config_dict(payload)
    return CalendarService(settings, store, crypto, config, SchoolPort(client),
                           config_payload=payload, config_source=source)

