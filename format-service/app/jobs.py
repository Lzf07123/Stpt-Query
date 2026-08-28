"""Redis-backed async query jobs.

The submit path exchanges credentials for a short-lived query proxy session
before queuing. Passwords never enter Redis, status documents, logs, or HTTP
polling responses.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Optional

from redis.asyncio import Redis

from .schema import JobPhase, JobStatusResponse, SessionWorkflowRequest


def _fingerprint(req: SessionWorkflowRequest) -> str:
    value = "|".join([
        req.username,
        req.option,
        req.semesters or "",
        req.weeks,
        str(req.md2pdf),
        str(req.check),
    ])
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _job_payload(req: SessionWorkflowRequest, session: str,
                 fingerprint: str, client_ip: str) -> dict:
    payload = req.model_dump(mode="json")
    payload.pop("password", None)
    payload["session"] = session
    payload["fingerprint"] = fingerprint
    payload["client_ip"] = client_ip
    return payload


PHASE_META: dict[str, tuple[int, str]] = {
    "queued": (2, "排队中"),
    "dispatching": (3, "等待查询槽位"),
    "querying": (4, "查询成绩/课表"),
    "analyzing": (5, "成绩分析"),
    "generating_pdf": (6, "生成 PDF"),
    "done": (7, "完成"),
}


def _phase_meta(phase: str) -> tuple[int, str]:
    return PHASE_META.get(phase, PHASE_META["queued"])


def _public_status(job_id: str, raw: Optional[dict],
                   position: Optional[int] = None) -> JobStatusResponse | None:
    if not raw:
        return None
    result = raw.get("result")
    phase = str(raw.get("phase") or "queued")
    if phase not in PHASE_META:
        phase = "queued"
    phase_index, phase_label = _phase_meta(phase)
    return JobStatusResponse(
        job_id=job_id,
        state=str(raw.get("state") or "queued"),
        phase=phase,  # type: ignore[arg-type]
        phase_index=phase_index,
        phase_label=phase_label,
        phase_started_at=raw.get("phase_started_at"),
        position=position,
        enqueued_at=float(raw.get("enqueued_at") or 0),
        started_at=raw.get("started_at"),
        finished_at=raw.get("finished_at"),
        result=result,
    )


class JobStore:
    """Atomic queue, deduplication, status, and payload storage."""

    def __init__(self, redis: Redis, prefix: str = "gw:jobs") -> None:
        self.redis = redis
        self.ready_key = f"{prefix}:ready"
        self.active_key = f"{prefix}:active"
        self.pending_key = f"{prefix}:pending-index"
        self.dedupe_prefix = f"{prefix}:dedupe"
        self.status_prefix = f"{prefix}:status"
        self.payload_prefix = f"{prefix}:payload"

    def _dedupe_key(self, fingerprint: str) -> str:
        return f"{self.dedupe_prefix}:{fingerprint}"

    def _status_key(self, job_id: str) -> str:
        return f"{self.status_prefix}:{job_id}"

    def _payload_key(self, job_id: str) -> str:
        return f"{self.payload_prefix}:{job_id}"

    async def enqueue(self, req: SessionWorkflowRequest, session: str,
                      client_ip: str, pending_limit: int,
                      ttl_seconds: int) -> tuple[str, bool, int]:
        """Queue one session-only job and return (job_id, deduplicated, position)."""
        fingerprint = _fingerprint(req)
        dedupe_key = self._dedupe_key(fingerprint)

        job_id = uuid.uuid4().hex
        now = time.time()
        status = {
            "state": "queued",
            "phase": "queued",
            "phase_started_at": now,
            "enqueued_at": now,
            "started_at": None,
            "finished_at": None,
            "result": None,
        }
        payload = _job_payload(req, session, fingerprint, client_ip)

        status_json = json.dumps(status, ensure_ascii=False, separators=(",", ":"))
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        async with self.redis.lock(f"{self.ready_key}:enqueue-lock", timeout=5,
                                   blocking_timeout=5):
            existing_id = await self.redis.get(dedupe_key)
            if existing_id:
                raw = await self._raw_status(existing_id)
                if raw and raw.get("state") in ("queued", "running"):
                    position = await self.position(existing_id)
                    return existing_id, True, position or 0
                await self.redis.delete(dedupe_key)
            if int(await self.redis.zcard(self.pending_key)) >= pending_limit:
                raise QueueFullError(pending_limit)
            pipeline = self.redis.pipeline(transaction=True)
            pipeline.set(self._status_key(job_id), status_json, ex=ttl_seconds)
            pipeline.set(dedupe_key, job_id, ex=ttl_seconds)
            pipeline.set(self._payload_key(job_id), payload_json, ex=ttl_seconds)
            pipeline.zadd(self.pending_key, {job_id: now})
            pipeline.zadd(self.ready_key, {job_id: now})
            await pipeline.execute()
        position = await self.position(job_id)
        return job_id, False, (position or 0) + 1

    async def claim(
            self, ttl_seconds: int,
    ) -> Optional[tuple[str, SessionWorkflowRequest, str, str, float]]:
        now = time.time()
        async with self.redis.lock(f"{self.ready_key}:claim-lock", timeout=5,
                                   blocking_timeout=5):
            job_ids = await self.redis.zrange(self.ready_key, 0, 0)
            if not job_ids:
                return None
            job_id = job_ids[0]
            await self.redis.zrem(self.ready_key, job_id)
            await self.redis.zadd(self.active_key, {job_id: now})
        raw_payload = await self.redis.get(self._payload_key(job_id))
        if not raw_payload:
            await self.fail(str(job_id), "internal_error", "任务数据已过期", ttl_seconds=300)
            return None
        payload = json.loads(raw_payload)
        payload.pop("fingerprint", None)
        request = SessionWorkflowRequest.model_validate(payload)
        raw = await self._raw_status(str(job_id))
        return str(job_id), request, str(payload["session"]), \
            str(payload.get("client_ip") or "unknown"), now

    async def mark_running(self, job_id: str, started_at: float,
                           ttl_seconds: int) -> bool:
        raw = await self._raw_status(job_id)
        if not raw or raw.get("state") != "queued":
            return False
        raw.update({"state": "running", "started_at": started_at})
        await self.redis.set(
            self._status_key(job_id), json.dumps(raw, separators=(",", ":")),
            ex=ttl_seconds)
        return True

    async def mark_phase(self, job_id: str, phase: JobPhase,
                         ttl_seconds: int) -> bool:
        raw = await self._raw_status(job_id)
        if not raw or raw.get("state") not in ("queued", "running"):
            return False
        previous_phase = str(raw.get("phase") or "queued")
        if previous_phase == phase:
            return True
        completed = list(raw.get("completed_phases") or [])
        if previous_phase not in completed:
            completed.append(previous_phase)
        phase_index, phase_label = _phase_meta(phase)
        raw.update({
            "phase": phase,
            "phase_index": phase_index,
            "phase_label": phase_label,
            "phase_started_at": time.time(),
            "completed_phases": completed,
        })
        await self.redis.set(
            self._status_key(job_id),
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")),
            ex=ttl_seconds)
        return True

    async def complete(self, job_id: str, result: dict, ttl_seconds: int) -> None:
        now = time.time()
        raw = await self._raw_status(job_id) or {}
        success = bool(result.get("success"))
        phase = "done" if success else str(raw.get("phase") or "queued")
        phase_index, phase_label = _phase_meta(phase)
        previous_phase = str(raw.get("phase") or "queued")
        completed = list(raw.get("completed_phases") or [])
        if success and previous_phase not in completed:
            completed.append(previous_phase)
        raw.update({
            "state": "success" if success else "failed",
            "finished_at": now,
            "phase": phase,
            "phase_index": phase_index,
            "phase_label": phase_label,
            "result": result,
            "completed_phases": completed,
        })
        status_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
        await self.redis.set(self._status_key(job_id), status_json, ex=ttl_seconds)
        fingerprint = await self._payload_fingerprint(job_id)
        if fingerprint:
            await self.redis.delete(self._dedupe_key(fingerprint))
        await self.redis.zrem(self.active_key, job_id)
        await self.redis.zrem(self.pending_key, job_id)
        await self.redis.delete(self._payload_key(job_id))

    async def fail(self, job_id: str, kind: str, message: str,
                   ttl_seconds: int) -> None:
        await self.complete(job_id, {
            "success": False,
            "kind": kind,
            "run_id": uuid.uuid4().hex,
            "output": message,
            "meta": {},
            "files": [],
            "pdf_base64": "",
        }, ttl_seconds)

    async def status(self, job_id: str) -> Optional[JobStatusResponse]:
        raw = await self._raw_status(job_id)
        if not raw:
            return None
        position = await self.position(job_id) if raw.get("state") == "queued" else None
        return _public_status(job_id, raw, position + 1 if position is not None else None)

    async def position(self, job_id: str) -> Optional[int]:
        rank = await self.redis.zrank(self.ready_key, job_id)
        return int(rank) if rank is not None else None

    async def pending_count(self) -> int:
        return int(await self.redis.zcard(self.pending_key))

    async def reap_stale(self, stale_after_seconds: int, ttl_seconds: int) -> int:
        deadline = time.time() - stale_after_seconds
        job_ids = await self.redis.zrangebyscore(self.active_key, 0, deadline)
        for job_id in job_ids:
            raw = await self._raw_status(job_id)
            state = raw.get("state") if raw else None
            if state == "running":
                await self.fail(job_id, "request_error", "任务执行超时，请稍后重试", ttl_seconds)
            elif state == "queued":
                await self.fail(job_id, "busy_error", "任务等待执行超时，请稍后重试", ttl_seconds)
            else:
                await self.redis.zrem(self.active_key, job_id)
                await self.redis.zrem(self.pending_key, job_id)
        return len(job_ids)

    async def _raw_status(self, job_id: str) -> Optional[dict]:
        raw = await self.redis.get(self._status_key(job_id))
        return json.loads(raw) if raw else None

    async def _payload_fingerprint(self, job_id: str) -> Optional[str]:
        raw = await self.redis.get(self._payload_key(job_id))
        if not raw:
            return None
        payload = json.loads(raw)
        return payload.get("fingerprint")


class QueueFullError(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__("async job queue is full")
        self.limit = limit
