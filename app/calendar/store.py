"""SQLite 权威存储：订阅、幂等键与不含身份的审计。

设计约束：
- 学号/密码/订阅令牌只存密文；索引使用 HMAC（owner_hash）与 token 哈希
- 关闭订阅 = 立即删除密文与快照（幂等：再次调用返回 already=true）
- 刷新调度使用 next_refresh_at 的单条原子 UPDATE 抢占，天然避免重复执行
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Tuple

SCHEMA_VERSION = "1"

SCHEMA = """
CREATE TABLE IF NOT EXISTS calendars (
    id                TEXT PRIMARY KEY,
    token_hash        TEXT NOT NULL UNIQUE,
    owner_hash        TEXT NOT NULL,
    semester          TEXT NOT NULL,
    weeks             TEXT NOT NULL DEFAULT 'all',
    options_json      TEXT NOT NULL DEFAULT '{}',
    refresh_interval  INTEGER NOT NULL,
    next_refresh_at   TEXT,
    paused_until      TEXT,
    fail_count        INTEGER NOT NULL DEFAULT 0,
    ics_blob          BLOB,
    ics_etag          TEXT,
    revision          INTEGER NOT NULL DEFAULT 1,
    token_enc         BLOB NOT NULL,
    username_enc      BLOB NOT NULL,
    cred_enc          BLOB,
    cred_updated_at   TEXT,
    last_fetch_at     TEXT,
    last_fetch_status TEXT,
    last_fetch_error  TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    expires_at        TEXT,
    revoked_at        TEXT,
    UNIQUE (owner_hash, semester)
);
CREATE INDEX IF NOT EXISTS idx_calendars_due ON calendars (next_refresh_at);
CREATE TABLE IF NOT EXISTS calendar_ops (
    idem_key     TEXT NOT NULL,
    owner_hash   TEXT NOT NULL,
    calendar_id  TEXT,
    op           TEXT NOT NULL,
    response_enc BLOB,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    PRIMARY KEY (idem_key, owner_hash)
);
CREATE TABLE IF NOT EXISTS calendar_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    calendar_id TEXT,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    result      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calendar_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def now_iso(offset_seconds: int = 0) -> str:
    return (datetime.now().astimezone() + timedelta(seconds=offset_seconds)).isoformat(
        timespec="seconds")


class CalendarStore:
    """线程安全的 SQLite 封装（app 进程内共享单连接 + 互斥锁）。"""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._conn.execute("INSERT OR REPLACE INTO calendar_meta(key, value) VALUES('schema', ?)",
                               (SCHEMA_VERSION,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass

    # ---------- 内部工具 ----------
    def _exec(self, sql: str, args: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._conn.execute(sql, args)
            self._conn.commit()
            return cursor

    @staticmethod
    def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
        if row is None:
            return None
        data = dict(row)
        raw = data.get("options_json")
        if raw:
            try:
                data["options"] = json.loads(raw)
            except json.JSONDecodeError:
                data["options"] = {}
        return data

    # ---------- 订阅 ----------
    def get_by_id(self, calendar_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calendars WHERE id = ?",
                                     (calendar_id,)).fetchone()
        return self._row(row)

    def get_by_token_hash(self, token_hash: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute("SELECT * FROM calendars WHERE token_hash = ?",
                                     (token_hash,)).fetchone()
        return self._row(row)

    def get_by_owner_semester(self, owner_hash: str, semester: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calendars WHERE owner_hash = ? AND semester = ?",
                (owner_hash, semester)).fetchone()
        return self._row(row)

    def list_by_owner(self, owner_hash: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calendars WHERE owner_hash = ? AND revoked_at IS NULL "
                "ORDER BY semester DESC", (owner_hash,)).fetchall()
        return [self._row(r) for r in rows if r]

    def create(self, *, owner_hash: str, semester: str, weeks: str, options: Dict[str, Any],
               refresh_interval: int, token_hash: str, token_enc: bytes, username_enc: bytes,
               cred_enc: bytes, expires_at: str, next_refresh_at: Optional[str],
               calendar_id: Optional[str] = None) -> Tuple[Dict[str, Any], bool]:
        """创建订阅；同 (学号, 学期) 已存在时返回既有记录（幂等）。

        调用方可预先提供 calendar_id，以便密文的 AAD 与最终主键一致。
        """
        existing = self.get_by_owner_semester(owner_hash, semester)
        if existing:
            return existing, False
        calendar_id = calendar_id or str(uuid.uuid4())
        stamp = now_iso()
        try:
            self._exec(
                "INSERT INTO calendars (id, token_hash, owner_hash, semester, weeks, options_json,"
                " refresh_interval, next_refresh_at, revision, token_enc, username_enc, cred_enc,"
                " cred_updated_at, created_at, updated_at, expires_at)"
                " VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)",
                (calendar_id, token_hash, owner_hash, semester, weeks,
                 json.dumps(options, ensure_ascii=False), int(refresh_interval),
                 next_refresh_at, token_enc, username_enc, cred_enc, stamp, stamp, stamp, expires_at))
        except sqlite3.IntegrityError:
            current = self.get_by_owner_semester(owner_hash, semester)
            if current:
                return current, False
            raise
        return self.get_by_id(calendar_id), True

    def save_snapshot(self, calendar_id: str, ics_blob: bytes, etag: str, *,
                      changed: bool, status: str, error: str = "",
                      next_refresh_at: Optional[str] = None, fail_count: Optional[int] = None,
                      paused_until: Optional[str] = None) -> None:
        stamp = now_iso()
        sets = ["ics_blob = ?", "ics_etag = ?", "last_fetch_at = ?", "last_fetch_status = ?",
                "last_fetch_error = ?", "updated_at = ?", "revision = revision + ?"]
        args: List[Any] = [ics_blob, etag, stamp, status, error[:300], stamp, 1 if changed else 0]
        if next_refresh_at is not None:
            sets.append("next_refresh_at = ?")
            args.append(next_refresh_at)
        if fail_count is not None:
            sets.append("fail_count = ?")
            args.append(int(fail_count))
        if paused_until is not None:
            sets.append("paused_until = ?")
            args.append(paused_until)
        args.append(calendar_id)
        self._exec("UPDATE calendars SET %s WHERE id = ?" % ", ".join(sets), args)

    def mark_failure(self, calendar_id: str, *, status: str, error: str, fail_count: int,
                     next_refresh_at: str, paused_until: Optional[str]) -> None:
        self._exec(
            "UPDATE calendars SET last_fetch_at = ?, last_fetch_status = ?, last_fetch_error = ?,"
            " fail_count = ?, next_refresh_at = ?, paused_until = ?, updated_at = ? WHERE id = ?",
            (now_iso(), status, error[:300], int(fail_count), next_refresh_at, paused_until,
             now_iso(), calendar_id))

    def rotate_token(self, calendar_id: str, token_hash: str, token_enc: bytes) -> None:
        self._exec("UPDATE calendars SET token_hash = ?, token_enc = ?, updated_at = ? WHERE id = ?",
                   (token_hash, token_enc, now_iso(), calendar_id))

    def set_paused(self, calendar_id: str, paused_until: Optional[str]) -> None:
        self._exec("UPDATE calendars SET paused_until = ?, updated_at = ? WHERE id = ?",
                   (paused_until, now_iso(), calendar_id))

    def update_credentials(self, calendar_id: str, cred_enc: bytes, username_enc: bytes,
                           token_enc: Optional[bytes] = None) -> None:
        stamp = now_iso()
        if token_enc is None:
            self._exec("UPDATE calendars SET cred_enc = ?, username_enc = ?, cred_updated_at = ?,"
                       " updated_at = ? WHERE id = ?",
                       (cred_enc, username_enc, stamp, stamp, calendar_id))
        else:
            self._exec("UPDATE calendars SET cred_enc = ?, username_enc = ?, token_enc = ?,"
                       " cred_updated_at = ?, updated_at = ? WHERE id = ?",
                       (cred_enc, username_enc, token_enc, stamp, stamp, calendar_id))

    def delete(self, calendar_id: str) -> bool:
        """彻底删除（幂等）：返回是否真的删除了记录。"""
        with self._lock:
            cursor = self._conn.execute("DELETE FROM calendars WHERE id = ?", (calendar_id,))
            self._conn.commit()
            return cursor.rowcount > 0

    def claim_due(self, now: str, limit: int, cooldown_seconds: int) -> List[Dict[str, Any]]:
        """原子抢占到期订阅：抢占成功者才执行刷新（重启/重复触发安全）。"""
        claimed: List[Dict[str, Any]] = []
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calendars WHERE revoked_at IS NULL AND ics_blob IS NOT NULL"
                " AND next_refresh_at IS NOT NULL AND next_refresh_at <= ?"
                " AND (paused_until IS NULL OR paused_until <= ?)"
                " ORDER BY next_refresh_at LIMIT ?", (now, now, int(limit))).fetchall()
            for row in rows:
                data = self._row(row)
                if not data:
                    continue
                target = now_iso(int(cooldown_seconds))
                cursor = self._conn.execute(
                    "UPDATE calendars SET next_refresh_at = ? WHERE id = ? AND next_refresh_at <= ?",
                    (target, data["id"], now))
                if cursor.rowcount == 1:
                    claimed.append(data)
            self._conn.commit()
        return claimed

    # ---------- 幂等键 ----------
    def ops_get(self, idem_key: str, owner_hash: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM calendar_ops WHERE idem_key = ? AND owner_hash = ?",
                (idem_key, owner_hash)).fetchone()
            if row is None:
                return None
            if row["expires_at"] < now_iso():
                self._conn.execute("DELETE FROM calendar_ops WHERE idem_key = ? AND owner_hash = ?",
                                   (idem_key, owner_hash))
                self._conn.commit()
                return None
        return dict(row)

    def ops_put(self, idem_key: str, owner_hash: str, calendar_id: Optional[str], op: str,
                response_enc: Optional[bytes], ttl_seconds: int = 86400) -> None:
        self._exec(
            "INSERT OR REPLACE INTO calendar_ops"
            " (idem_key, owner_hash, calendar_id, op, response_enc, created_at, expires_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (idem_key, owner_hash, calendar_id, op, response_enc, now_iso(),
             now_iso(ttl_seconds)))

    # ---------- 审计（不含身份）与统计 ----------
    def audit(self, calendar_id: Optional[str], action: str, actor: str, result: str) -> None:
        self._exec("INSERT INTO calendar_audit (at, calendar_id, action, actor, result)"
                   " VALUES (?,?,?,?,?)", (now_iso(), calendar_id, action, actor, result[:120]))

    def list_admin(self, *, owner_hash: Optional[str] = None, semester: Optional[str] = None,
                   state: Optional[str] = None, page: int = 1, size: int = 20) -> Dict[str, Any]:
        where, args = ["revoked_at IS NULL"], []
        if owner_hash:
            where.append("owner_hash = ?")
            args.append(owner_hash)
        if semester:
            where.append("semester = ?")
            args.append(semester)
        clause = " AND ".join(where)
        with self._lock:
            total = self._conn.execute(
                "SELECT COUNT(*) AS c FROM calendars WHERE " + clause, args).fetchone()["c"]
            rows = self._conn.execute(
                "SELECT * FROM calendars WHERE %s ORDER BY created_at DESC LIMIT ? OFFSET ?" % clause,
                args + [int(size), max(0, (int(page) - 1) * int(size))]).fetchall()
        items = [self._row(r) for r in rows if r]
        if state:
            items = [item for item in items if _admin_state(item) == state]
        return {"items": items, "total": total, "page": int(page), "size": int(size)}

    def stats(self) -> Dict[str, Any]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM calendars WHERE revoked_at IS NULL").fetchall()
        items = [self._row(r) for r in rows if r]
        counter: Dict[str, int] = {}
        for item in items:
            key = _admin_state(item)
            counter[key] = counter.get(key, 0) + 1
        return {"total": len(items), "states": counter,
                "paused": sum(1 for i in items if i.get("paused_until") and i["paused_until"] > now_iso())}


def _admin_state(item: Dict[str, Any]) -> str:
    paused_until = item.get("paused_until") or ""
    if paused_until and paused_until > now_iso():
        return "locked"
    status = (item.get("last_fetch_status") or "").strip()
    if status == "credential_error":
        return "reauth_needed"
    if status == "upstream_error":
        return "degraded"
    return "active"
