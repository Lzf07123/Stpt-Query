"""全站通知：共享 JSONL 权威存储与可选 Redis 恢复副本。"""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Literal

from pydantic import BaseModel, Field, field_validator


NOTICE_REDIS_KEY = "gw:v3:notices"


class NoticeError(Exception):
    """通知业务错误；HTTP 层负责转换为用户可读响应。"""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class NoticeValidationError(NoticeError):
    def __init__(self, detail: str) -> None:
        super().__init__(422, detail)


class NoticeConflictError(NoticeError):
    def __init__(self, detail: str) -> None:
        super().__init__(409, detail)


class NoticeStorageError(NoticeError):
    def __init__(self, detail: str = "通知存储不可用，请稍后重试") -> None:
        super().__init__(503, detail)


class NoticeContent(BaseModel):
    """通知正文与级别校验；正文始终保持单行纯文本。"""

    content: str = Field(..., min_length=1, max_length=120)
    level: Literal["info", "warning"] = "info"

    @field_validator("content", mode="before")
    @classmethod
    def _single_line_text(cls, value: Any) -> str:
        text = " ".join(str(value or "").split())
        if not text:
            raise ValueError("通知内容不能为空")
        if len(text) > 120:
            raise ValueError("通知内容不能超过 120 个字符")
        if any(ord(char) < 32 for char in text):
            raise ValueError("通知内容只能使用可显示的单行文本")
        return text


class NoticeRecord(NoticeContent):
    id: str
    status: Literal["draft", "active", "archived", "deleted"] = "draft"
    revision: int = Field(default=1, ge=1)
    created_at: str
    updated_at: str
    published_at: str | None = None
    archived_at: str | None = None


class NoticeCreate(NoticeContent):
    status: Literal["draft", "active"] = "draft"


class NoticeUpdate(BaseModel):
    content: str | None = Field(default=None, min_length=1, max_length=120)
    level: Literal["info", "warning"] | None = None
    status: Literal["draft", "active", "archived"] | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _single_line_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        return NoticeContent(content=value).content


class _FileState:
    def __init__(self, records: dict[str, NoticeRecord], raw_lines: int,
                 parse_errors: int, stat_signature: tuple[int, int] | None) -> None:
        self.records = records
        self.raw_lines = raw_lines
        self.parse_errors = parse_errors
        self.stat_signature = stat_signature


def _timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="microseconds")


def _time_value(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _sorted_records(records: Iterable[NoticeRecord], field: str) -> list[NoticeRecord]:
    return sorted(
        records,
        key=lambda item: (_time_value(getattr(item, field)), item.revision, item.id),
        reverse=True,
    )


class NoticeStore:
    """JSONL 是唯一权威来源；Redis 仅在文件成功写入后尽力镜像。"""

    def __init__(self, path: str, redis: Any = None, max_active: int = 10,
                 max_history: int = 500, compact_after: int = 2000,
                 cache_seconds: float = 5.0) -> None:
        self.path = Path(path)
        self.lock_path = Path(str(self.path) + ".lock")
        self.redis = redis
        self.max_active = max(1, int(max_active))
        self.max_history = max(1, int(max_history))
        self.compact_after = max(2, int(compact_after))
        self.records: dict[str, NoticeRecord] = {}
        self.parse_errors = 0
        self.raw_lines = 0
        self.last_error = ""
        self.redis_status = "not-configured" if redis is None else "unknown"
        self._state_signature: tuple[int, int] | None = None
        self.redis_dirty = self.redis is not None
        self._cache_loaded_at = 0.0
        self._cache_seconds = max(0.0, float(cache_seconds))

    async def startup(self) -> None:
        await self.initialize()

    @property
    def status(self) -> str:
        return "ok" if not self.last_error else "error"

    def _ensure_directory(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)

    def _stat_signature(self) -> tuple[int, int] | None:
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return stat.st_mtime_ns, stat.st_size

    def _read_file(self) -> _FileState:
        signature = self._stat_signature()
        records: dict[str, NoticeRecord] = {}
        raw_lines = 0
        parse_errors = 0
        if signature is not None:
            with self.path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    raw_lines += 1
                    try:
                        item = NoticeRecord.model_validate_json(line)
                    except Exception:
                        parse_errors += 1
                        continue
                    previous = records.get(item.id)
                    if previous is None or item.revision > previous.revision:
                        records[item.id] = item
        return _FileState(records, raw_lines, parse_errors, signature)

    def _refresh(self) -> None:
        state = self._read_file()
        self.records = state.records
        self.raw_lines = state.raw_lines
        self.parse_errors = state.parse_errors
        self._state_signature = state.stat_signature
        self._prune_history()

    def _read_shared(self) -> None:
        signature = self._stat_signature()
        if (
            self._cache_seconds > 0
            and self._cache_loaded_at > 0
            and time.monotonic() - self._cache_loaded_at < self._cache_seconds
            and signature == self._state_signature
        ):
            return
        self._ensure_directory()
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH)
            try:
                self._refresh()
                self._cache_loaded_at = time.monotonic()
                self.last_error = ""
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _prune_history(self) -> None:
        archived = _sorted_records(
            (item for item in self.records.values() if item.status == "archived"),
            "archived_at",
        )
        for item in archived[self.max_history:]:
            self.records.pop(item.id, None)

    def _write_records(self, records: dict[str, NoticeRecord], append_record: NoticeRecord | None = None) -> None:
        if append_record is not None:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(append_record.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _compact_locked(self, records: dict[str, NoticeRecord]) -> None:
        temporary = self.path.with_name(self.path.name + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for item in records.values():
                handle.write(item.model_dump_json() + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def _mutate(self, operation) -> NoticeRecord:
        self._ensure_directory()
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                self._refresh()
                record, records, compact = operation(self.records)
                raw_after = self.raw_lines + (0 if compact else 1)
                if (compact or len(records) >= self.compact_after
                        or raw_after >= self.compact_after):
                    self._compact_locked(records)
                else:
                    self._write_records(records, record)
                self.records = records
                self._state_signature = None
                self._cache_loaded_at = 0.0
                return record
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _initialize(self) -> None:
        try:
            self._read_shared()
        except Exception as exc:
            self.last_error = exc.__class__.__name__
            raise

    async def initialize(self) -> None:
        try:
            await asyncio.to_thread(self._initialize)
            if self._stat_signature() is None and self.redis is not None:
                restored = await self._read_redis()
                if restored:
                    self.records = restored
                    await asyncio.to_thread(self._compact_locked, self.records)
            await self._sync_redis()
        except Exception as exc:
            self.last_error = exc.__class__.__name__

    async def _read_redis(self) -> dict[str, NoticeRecord]:
        assert self.redis is not None
        raw = await asyncio.wait_for(self.redis.hgetall(NOTICE_REDIS_KEY), timeout=1.5)
        records: dict[str, NoticeRecord] = {}
        for value in raw.values():
            try:
                item = NoticeRecord.model_validate_json(value)
            except Exception:
                continue
            if item.status != "deleted":
                records[item.id] = item
        return records

    async def _sync_redis(self, records: dict[str, NoticeRecord] | None = None) -> None:
        if self.redis is None:
            self.redis_status = "not-configured"
            return
        try:
            values = records if records is not None else self.records
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.delete(NOTICE_REDIS_KEY)
            for item in values.values():
                pipeline.hset(NOTICE_REDIS_KEY, item.id, item.model_dump_json())
            await asyncio.wait_for(pipeline.execute(), timeout=1.5)
            self.redis_status = "ok"
            self.redis_dirty = False
        except Exception as exc:
            self.redis_status = "degraded"
            self.redis_dirty = True

    async def sync_redis(self) -> None:
        if self.redis is None or not self.redis_dirty:
            return
        await self._sync_redis()

    async def _after_write(self, record: NoticeRecord) -> NoticeRecord:
        await self._sync_redis()
        return record

    async def create(self, payload: NoticeCreate) -> NoticeRecord:
        now = _timestamp()

        def operation(records: dict[str, NoticeRecord]):
            if payload.status == "active" and self._active_count(records) >= self.max_active:
                raise NoticeConflictError("同时上线通知已达上限，请先下线一条")
            record = NoticeRecord(
                id=uuid.uuid4().hex,
                content=payload.content,
                level=payload.level,
                status=payload.status,
                revision=1,
                created_at=now,
                updated_at=now,
                published_at=now if payload.status == "active" else None,
            )
            records[record.id] = record
            self._prune_history()
            return record, records, False

        try:
            record = await asyncio.to_thread(self._mutate, operation)
        except OSError as exc:
            self.last_error = exc.__class__.__name__
            raise NoticeStorageError() from exc
        return await self._after_write(record)

    async def update(self, notice_id: str, payload: NoticeUpdate) -> NoticeRecord:
        now = _timestamp()

        def operation(records: dict[str, NoticeRecord]):
            current = records.get(notice_id)
            if current is None or current.status == "deleted":
                raise NoticeError(404, "通知不存在")
            changes = payload.model_dump(exclude_none=True)
            if current.status in ("active", "archived") and (
                    "content" in changes or "level" in changes):
                raise NoticeConflictError("已发布通知内容不可修改，请新建修订版")
            next_status = changes.get("status", current.status)
            allowed = (
                current.status == "draft" and next_status in ("draft", "active")
            ) or (
                current.status == "active" and next_status in ("active", "archived")
            ) or (
                current.status == "archived" and next_status in ("archived", "active")
            )
            if not allowed:
                raise NoticeConflictError("通知状态不允许该变更")
            if next_status == "active" and current.status != "active":
                if self._active_count(records) >= self.max_active:
                    raise NoticeConflictError("同时上线通知已达上限，请先下线一条")
            data = current.model_dump()
            data.update(changes)
            data["revision"] = current.revision + 1
            data["updated_at"] = now
            if next_status == "active" and current.status != "active":
                data["published_at"] = now
                data["archived_at"] = None
            elif next_status == "archived":
                data["archived_at"] = now
            record = NoticeRecord.model_validate(data)
            records[notice_id] = record
            self._prune_history()
            return record, records, False

        try:
            record = await asyncio.to_thread(self._mutate, operation)
        except OSError as exc:
            self.last_error = exc.__class__.__name__
            raise NoticeStorageError() from exc
        return await self._after_write(record)

    async def delete(self, notice_id: str) -> None:
        def operation(records: dict[str, NoticeRecord]):
            current = records.get(notice_id)
            if current is None:
                raise NoticeError(404, "通知不存在")
            if current.status != "draft":
                raise NoticeConflictError("只有草稿可以删除")
            records.pop(notice_id, None)
            return current, records, True

        try:
            await asyncio.to_thread(self._mutate, operation)
        except OSError as exc:
            self.last_error = exc.__class__.__name__
            raise NoticeStorageError() from exc
        await self._sync_redis()

    @staticmethod
    def _active_count(records: dict[str, NoticeRecord]) -> int:
        return sum(1 for item in records.values() if item.status == "active")

    async def active(self) -> list[NoticeRecord]:
        try:
            await asyncio.to_thread(self._read_shared)
        except OSError as exc:
            self.last_error = exc.__class__.__name__
            raise NoticeStorageError() from exc
        return _sorted_records(
            (item for item in self.records.values() if item.status == "active"),
            "published_at",
        )

    async def history(self, limit: int = 50) -> list[NoticeRecord]:
        try:
            await asyncio.to_thread(self._read_shared)
        except OSError as exc:
            self.last_error = exc.__class__.__name__
            raise NoticeStorageError() from exc
        return _sorted_records(
            (item for item in self.records.values() if item.status == "archived"),
            "archived_at",
        )[:max(0, min(limit, 100))]

    async def admin_list(self, status_filter: str = "", keyword: str = "",
                         offset: int = 0, limit: int = 20) -> tuple[list[NoticeRecord], int]:
        try:
            await asyncio.to_thread(self._read_shared)
        except OSError as exc:
            self.last_error = exc.__class__.__name__
            raise NoticeStorageError() from exc
        keyword = keyword.strip().casefold()
        items = [
            item for item in self.records.values()
            if item.status != "deleted"
            and (not status_filter or item.status == status_filter)
            and (not keyword or keyword in item.content.casefold())
        ]
        ordered = _sorted_records(items, "updated_at")
        return ordered[offset:offset + limit], len(ordered)
