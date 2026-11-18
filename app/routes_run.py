"""/api/v1/run 同步查询路由：并发槽位、限流、日志与日历附加的编排入口。

响应统一走 api_semantics 信封（success/code/message/data/meta）：
- 成功 200，data = {kind, output, pdf_base64?}，meta 携带 trace_id/耗时/日历状态；
- 业务失败使用真实 HTTP 状态码（如凭据错误 401），诊断报告在 data.diagnosis。
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime

from fastapi import Depends, Request
from fastapi.responses import JSONResponse

from .api_semantics import ApiError, envelope_from_pipeline, ok_envelope
from .context import AppContext
from .pipeline import ServiceError
from .schema import JumpRenewRequest, WorkflowRequest
from .trace import LOG, new_run_id


def register_run_routes(app, ctx: AppContext) -> None:
    @app.post("/api/v1/run")
    async def run_workflow(body: WorkflowRequest, request: Request,
                           _: None = Depends(ctx.require_auth)) -> JSONResponse:
        client_ip = ctx.client_ip(request)
        await ctx.check_rate_limit(client_ip)
        started = time.time()
        try:
            try:
                wait_started = time.perf_counter()
                await asyncio.wait_for(ctx.query_slots.acquire(),
                                       timeout=max(0.001, ctx.cfg.concurrency_wait_timeout))
            except asyncio.TimeoutError as exc:
                busy_run_id = new_run_id()
                await ctx.record_query_log({
                    "event": "query",
                    "time": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "client_ip": client_ip,
                    "username": body.user or body.username,
                    "option": body.option,
                    "run_id": busy_run_id,
                    "success": False,
                    "kind": "busy_error",
                })
                raise ApiError("capacity.concurrency_full",
                               meta={"trace_id": busy_run_id}) from exc
            try:
                ctx.metrics.observe_concurrency_wait(
                    "query", time.perf_counter() - wait_started)
                try:
                    result = await asyncio.wait_for(
                        app.state.pipeline.run(body), timeout=ctx.cfg.request_timeout)
                except asyncio.TimeoutError as exc:
                    run_id = new_run_id()
                    LOG.warning("run=%s 请求超时", run_id)
                    log_entry = ctx.query_log_entry(
                        body, client_ip, started,
                        {"success": False, "kind": "request_error", "meta": {}},
                        run_id)
                    result = {
                        "success": False,
                        "kind": "request_error",
                        "run_id": run_id,
                        "output": "查询超时，请稍后重试",
                        "meta": {"elapsed_ms": int(ctx.cfg.request_timeout * 1000)},
                    }
                else:
                    log_entry = ctx.query_log_entry(body, client_ip, started, result,
                                                    result.get("run_id", ""))
            finally:
                # pipeline 结果（含超时分支）落定即释放全局查询槽位；
                # 日志写入（含 Redis/文件 I/O）与日历附加移到槽位之外完成，
                # 不再让收尾 I/O 占用全局并发
                ctx.query_slots.release()

            # 槽位外收尾；仍在同一异常保护域内，未知异常同样返回统一 JSON
            await ctx.record_query_log(log_entry)
            calendar_service = ctx.calendar_service
            if (calendar_service is not None and result.get("success")
                    and str(body.option or "").strip() == "课表"):
                try:
                    semester = (body.semesters or "").strip() or \
                        calendar_service.default_semester()
                    if semester:
                        # 同步 SQLite + AES 解密放线程池，避免阻塞事件循环
                        result.setdefault("meta", {})["calendar"] = \
                            await asyncio.to_thread(
                                calendar_service.local_state,
                                body.username, body.password, semester)
                except Exception as exc:      # 日历状态失败不影响查询结果
                    LOG.warning("日历状态附带失败：%s", exc.__class__.__name__)
            payload, status_code = envelope_from_pipeline(result)
            return JSONResponse(status_code=status_code, content=payload)
        except ApiError:
            # 统一语义异常交由全局异常处理器转换为错误信封
            raise
        except Exception as exc:  # 兜底：未知异常也返回统一信封
            LOG.exception("run 执行异常")
            run_id = new_run_id()
            result = {"success": False, "kind": "internal_error", "run_id": run_id}
            await ctx.record_query_log(
                ctx.query_log_entry(body, client_ip, started, result, run_id))
            payload, status_code = envelope_from_pipeline({
                "success": False, "kind": "internal_error", "run_id": run_id,
                "output": "服务内部异常，请稍后重试",
                "meta": {"elapsed_ms": int((time.time() - started) * 1000)},
            })
            return JSONResponse(status_code=status_code, content=payload)

    @app.post("/api/v1/jump/renew")
    async def renew_jump_link(body: JumpRenewRequest, request: Request,
                              _: None = Depends(ctx.require_auth)) -> JSONResponse:
        """重新获取免密跳转链接（重新认证签发，不续期过期码——安全边界）。

        与 /run 同级保护：per-IP 限流（防撞库）+ 登录类槽位；
        LOGIN_REUSE 命中时同账号重复调用不打学校。密码不落任何日志。
        """
        client_ip = ctx.client_ip(request)
        await ctx.check_rate_limit(client_ip)
        started = time.time()
        username = body.username
        log_entry = {
            "event": "jump_renew",
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "client_ip": client_ip,
            "username": username,
            "success": False,
            "kind": "jump_renew",
        }
        try:
            async with ctx.login_slots:
                link = await app.state.pipeline.renew_jump_link(
                    body.username, body.password)
            log_entry.update({"success": True,
                              "elapsed_ms": int((time.time() - started) * 1000)})
            await ctx.record_query_log(log_entry)
            return JSONResponse(status_code=200, content=ok_envelope(
                data={"login_url": link["login_url"],
                      "app_url": link["app_url"],
                      "note": link["note"]},
                trace_id=new_run_id()))
        except ApiError:
            raise
        except ServiceError as exc:
            log_entry.update({"elapsed_ms": int((time.time() - started) * 1000),
                              "kind": "jump_renew_error",
                              "message": "status=%s" % exc.status_code})
            await ctx.record_query_log(log_entry)
            if exc.status_code == 401:
                raise ApiError("school.cas.invalid_credentials",
                               message="账号或密码错误，请核对后重试") from exc
            if exc.status_code == 429:
                raise ApiError("request.rate_limited",
                               message="操作过于频繁，请稍后再试") from exc
            raise ApiError("school.upstream.error",
                           message="教务系统登录暂不可用，请稍后重试") from exc
        except Exception as exc:  # 兜底：未知异常也返回统一信封
            LOG.exception("jump/renew 执行异常")
            log_entry.update({"elapsed_ms": int((time.time() - started) * 1000),
                              "kind": "internal_error"})
            await ctx.record_query_log(log_entry)
            raise ApiError("internal.error",
                           message="服务内部异常，请稍后重试") from exc
