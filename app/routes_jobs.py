"""/run/jobs 异步任务路由：受理（同步换票）与轮询。"""
from __future__ import annotations

import time

from fastapi import Depends, HTTPException, Request, Response, status

from .classifier import classify_error
from .context import AppContext, _JOB_ID_RE
from .jobs import QueueFullError
from .pipeline import ServiceError
from .render import extract_session
from .schema import (JobStatusResponse, JobSubmissionResponse,
                     SessionWorkflowRequest, WorkflowRequest)
from .trace import LOG, new_run_id


def register_job_routes(app, ctx: AppContext) -> None:
    @app.post("/run/jobs", response_model=JobSubmissionResponse)
    async def submit_run_job(body: WorkflowRequest, request: Request,
                             _: None = Depends(ctx.require_auth)) -> JobSubmissionResponse:
        client_ip = ctx.client_ip(request)
        await ctx.check_rate_limit(client_ip)
        if app.state.redis is None or app.state.job_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="异步查询未启用：必须配置 REDIS_URL")

        pending_count = await app.state.job_store.pending_count()
        if pending_count >= ctx.cfg.job_pending_limit:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前排队人数已达上限，请稍后再试")

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
            if exc.status_code in (401, 403):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=failure) from exc
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=failure) from exc

        session = extract_session(login_body).get("session", "")
        if not session:
            failure = classify_error("login", body=login_body)
            failure["run_id"] = login_run_id
            await ctx.record_query_log(ctx.query_log_entry(
                body, client_ip, started, failure, failure.get("run_id", "")))
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=failure)

        internal_request = SessionWorkflowRequest(**body.model_dump())
        try:
            job_id, deduplicated, position = await app.state.job_store.enqueue(
                internal_request, session, client_ip,
                max(1, ctx.cfg.job_pending_limit), ctx.cfg.job_result_ttl_seconds)
        except QueueFullError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="当前排队人数已达上限，请稍后再试") from exc
        except Exception as exc:
            LOG.warning("异步任务入队失败：%s", exc.__class__.__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="排队服务暂时不可用，请稍后再试") from exc

        return JobSubmissionResponse(
            job_id=job_id,
            deduplicated=deduplicated,
            position=position,
            poll_url=f"/run/jobs/{job_id}",
        )

    @app.get("/run/jobs/{job_id}", response_model=JobStatusResponse)
    async def get_run_job(job_id: str, response: Response,
                          _: None = Depends(ctx.require_auth)) -> JobStatusResponse:
        if not _JOB_ID_RE.fullmatch(job_id):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="任务不存在或已过期")
        if app.state.redis is None or app.state.job_store is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="异步查询未启用：必须配置 REDIS_URL")
        try:
            job_status = await app.state.job_store.status(job_id)
        except Exception as exc:
            LOG.warning("异步任务状态查询失败：%s", exc.__class__.__name__)
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="任务状态暂时不可用，请稍后再试") from exc
        if job_status is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="任务不存在或已过期")
        response.headers["Cache-Control"] = "no-store"
        return job_status
