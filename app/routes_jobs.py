"""/api/v1/run/jobs 异步任务路由：受理（同步换票）与轮询。

响应统一走 api_semantics 信封：受理成功 data={job_id, state, deduplicated,
position, poll_url}；轮询成功 data={job_id, state, phase, ..., result(信封)}。
登录失败等业务失败由 envelope_from_pipeline 映射为真实 HTTP 状态码。
"""
from __future__ import annotations

import time

from fastapi import Depends, Request, Response
from fastapi.responses import JSONResponse

from .api_semantics import (ApiError, envelope_from_pipeline, ok_envelope)
from .classifier import classify_error
from .context import AppContext, _JOB_ID_RE
from .jobs import QueueFullError
from .pipeline import ServiceError
from .render import extract_session
from .schema import SessionWorkflowRequest, WorkflowRequest
from .trace import LOG, new_run_id


def register_job_routes(app, ctx: AppContext) -> None:
    @app.post("/api/v1/run/jobs")
    async def submit_run_job(body: WorkflowRequest, request: Request,
                             _: None = Depends(ctx.require_auth)) -> JSONResponse:
        client_ip = ctx.client_ip(request)
        await ctx.check_rate_limit(client_ip)
        if app.state.redis is None or app.state.job_store is None:
            raise ApiError("capacity.jobs_disabled")

        pending_count = await app.state.job_store.pending_count()
        if pending_count >= ctx.cfg.job_pending_limit:
            raise ApiError("capacity.queue_full")

        started = time.time()
        login_run_id = new_run_id()
        try:
            async with ctx.login_slots:
                login_body = await app.state.pipeline.service.post("/login", {
                    "username": body.username, "password": body.password})
        except ServiceError as exc:
            failure = ctx.login_failure_result(exc, login_run_id)
            await ctx.record_query_log(ctx.query_log_entry(
                body, client_ip, started,
                failure, failure.get("run_id", "")))
            payload, status_code = envelope_from_pipeline(failure)
            return JSONResponse(status_code=status_code, content=payload)

        session = extract_session(login_body).get("session", "")
        if not session:
            failure = classify_error("login", body=login_body)
            failure["run_id"] = login_run_id
            await ctx.record_query_log(ctx.query_log_entry(
                body, client_ip, started, failure, failure.get("run_id", "")))
            payload, status_code = envelope_from_pipeline(failure)
            return JSONResponse(status_code=status_code, content=payload)

        internal_request = SessionWorkflowRequest(**body.model_dump())
        try:
            job_id, deduplicated, position = await app.state.job_store.enqueue(
                internal_request, session, client_ip,
                max(1, ctx.cfg.job_pending_limit), ctx.cfg.job_result_ttl_seconds)
        except QueueFullError as exc:
            raise ApiError("capacity.queue_full") from exc
        except Exception as exc:
            LOG.warning("异步任务入队失败：%s", exc.__class__.__name__)
            raise ApiError("capacity.queue_unavailable") from exc

        return JSONResponse(content=ok_envelope(
            {
                "job_id": job_id,
                "state": "queued",
                "deduplicated": deduplicated,
                "position": position,
                "poll_url": f"/api/v1/run/jobs/{job_id}",
            },
            trace_id=login_run_id,
            extra_meta={"retry_after": 1},
        ))

    @app.get("/api/v1/run/jobs/{job_id}")
    async def get_run_job(job_id: str, response: Response,
                          _: None = Depends(ctx.require_auth)) -> JSONResponse:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise ApiError("request.not_found", message="任务不存在或已过期")
        if app.state.redis is None or app.state.job_store is None:
            raise ApiError("capacity.jobs_disabled")
        try:
            job_status = await app.state.job_store.status(job_id)
        except Exception as exc:
            LOG.warning("异步任务状态查询失败：%s", exc.__class__.__name__)
            raise ApiError("service.unavailable",
                           message="任务状态暂时不可用，请稍后再试") from exc
        if job_status is None:
            raise ApiError("request.not_found", message="任务不存在或已过期")
        response.headers["Cache-Control"] = "no-store"
        data = job_status.model_dump(exclude={"result"})
        if job_status.result is not None:
            # 终态结果同样以信封语义呈现（嵌套信封），前端无需准备第二套解析
            result_payload, _ = envelope_from_pipeline(job_status.result.model_dump())
            data["result"] = result_payload
        return JSONResponse(content=ok_envelope(data))
