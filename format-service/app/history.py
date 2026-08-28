"""Bounded query history stores for single- and multi-instance deployments."""
from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import datetime
from threading import Lock
from collections import deque
from typing import Any, Iterable


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _parse_time(value: Any, fallback: float) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = str(value or "")
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return fallback


def event_score(event: dict, fallback: float) -> float:
    return _parse_time(event.get("time"), fallback)


class MemoryEventHistory:
    """Process-local bounded history used when Redis is not configured."""

    def __init__(self, limit: int = 100) -> None:
        self.limit = max(1, int(limit))
        self._events: dict[str, deque[tuple[float, str, dict]]] = {}
        self._lock = Lock()

    def _member(self, key: str, event_id: str, event: dict) -> str:
        identity = event_id or _canonical(event)
        return hashlib.sha256(f"{key}:{identity}".encode("utf-8")).hexdigest()

    def add(self, key: str, event: dict, event_id: str = "",
            score: float | None = None) -> str:
        score = time_fallback() if score is None else float(score)
        member = self._member(key, event_id, event)
        with self._lock:
            events = self._events.setdefault(key, deque(maxlen=self.limit))
            filtered = [(item_score, item_member, item)
                        for item_score, item_member, item in events
                        if item_member != member]
            events.clear()
            events.extend(filtered)
            events.append((score, member, dict(event)))
        return member

    def recent(self, key: str, limit: int = 100) -> list[dict]:
        with self._lock:
            snapshot = list(self._events.get(key, ()))
        snapshot.sort(key=lambda value: (value[0], value[1]), reverse=True)
        return [dict(item) for _, _, item in snapshot[:max(0, limit)]]


def time_fallback() -> float:
    return time.time()


class RedisEventHistory:
    """Redis ZSET history with read compatibility for legacy List keys."""

    def __init__(self, redis: Any, version_prefix: str = "gw:v2",
                 legacy_prefix: str = "gw", limit: int = 100) -> None:
        self.redis = redis
        self.version_prefix = version_prefix.rstrip(":")
        self.legacy_prefix = legacy_prefix.rstrip(":")
        self.limit = max(1, int(limit))

    def _member(self, key: str, event_id: str, event: dict) -> str:
        identity = event_id or _canonical(event)
        return hashlib.sha256(f"{key}:{identity}".encode("utf-8")).hexdigest()

    async def add(self, key: str, event: dict, event_id: str = "",
                  score: float | None = None) -> str:
        score = time_fallback() if score is None else float(score)
        if not math.isfinite(score):
            score = time_fallback()
        record_id = self._member(key, event_id, event)
        payload = dict(event)
        payload["_record_id"] = record_id
        member = _canonical(payload)
        pipeline = self.redis.pipeline(transaction=True)
        pipeline.zadd(key, {member: score})
        pipeline.zremrangebyrank(key, 0, -(self.limit + 1))
        await pipeline.execute()
        return member

    async def seed_newest(self, key: str, events: Iterable[tuple[dict, str, float]]) -> bool:
        operations = []
        for event, event_id, score in events:
            member = self._member(key, event_id, event)
            operations.append((member, dict(event), float(score)))
        if not operations:
            return False

        pipeline = self.redis.pipeline(transaction=True)
        for member, event, score in operations:
            payload = dict(event)
            payload["_record_id"] = member
            pipeline.zadd(key, {_canonical(payload): score}, nx=True)
        pipeline.zremrangebyrank(key, 0, -(self.limit + 1))
        await pipeline.execute()
        return True

    async def _read_versioned(self, key: str, limit: int) -> list[tuple[dict, float, str]]:
        rows = await self.redis.zrange(key, 0, -1, desc=True, withscores=True) or []
        parsed: list[tuple[dict, float, str]] = []
        for raw, score in rows:
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(event, dict):
                parsed.append((event, float(score), str(raw)))
        return parsed[:limit]

    async def _read_legacy(self, key: str, limit: int) -> list[tuple[dict, float, str]]:
        rows = await self.redis.lrange(key, 0, -1) or []
        parsed: list[tuple[dict, float, str]] = []
        for index, raw in enumerate(rows):
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            identity = f"legacy:{key}:{index}"
            score = _parse_time(event.get("time"), time_fallback() - index)
            event = dict(event)
            event["_history_member"] = identity
            parsed.append((event, score, identity))
            if len(parsed) >= limit:
                break
        return parsed

    async def recent(self, versioned_key: str, legacy_key: str,
                     limit: int = 100) -> list[dict]:
        limit = max(0, int(limit))
        try:
            events = await self._read_versioned(versioned_key, limit)
        except Exception:
            raise
        try:
            legacy = await self._read_legacy(legacy_key, limit)
        except Exception:
            legacy = []

        deduped: dict[str, tuple[dict, float, str]] = {}
        for event, score, member in events + legacy:
            identity = str(event.get("run_id") or event.pop("_history_member", member))
            previous = deduped.get(identity)
            if previous is None or score > previous[1]:
                deduped[identity] = (event, score, member)
        ordered = sorted(deduped.values(),
                         key=lambda value: (value[1], value[2]), reverse=True)
        events = []
        for event, _, _ in ordered[:limit]:
            event.pop("_record_id", None)
            event.pop("_history_member", None)
            events.append(event)
        return events
