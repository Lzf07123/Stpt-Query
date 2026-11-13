"""异步查询任务 worker：领单、阶段上报、过期清理与空闲退避。

从 main.create_app 闭包外移；共享状态经 (app, ctx) 传入，
可被测试替换的 job_store/pipeline 仍从 ``app.state`` 晚绑定读取。
"""
from __future__ import annotations

import asyncio
import time

from .context import (WORKER_IDLE_DELAY_FLOOR, AppContext,
                      _next_worker_idle_delay)
from .trace import LOG


async def _run_one_job(app, ctx: AppContext) -> None:
    cfg = ctx.cfg
    idle_delay = WORKER_IDLE_DELAY_FLOOR
    while True:
        claimed = None
        try:
            if app.state.job_store is None:
                # 无 Redis 部署下异步任务禁用（/api/v1/run/jobs 返回 503），worker 休眠等重启
                await asyncio.sleep(5)
                continue
            claimed = await app.state.job_store.claim(cfg.job_result_ttl_seconds)
        except Exception as exc:
            LOG.warning("异步任务领取失败：%s", exc.__class__.__name__)
            await asyncio.sleep(1)
            continue
        if claimed is None:
            # 空轮询指数退避（0.2s→1s 封顶），降低 Redis 空转；领到任务即重置
            await asyncio.sleep(idle_delay)
            idle_delay = _next_worker_idle_delay(idle_delay)
            continue
        idle_delay = WORKER_IDLE_DELAY_FLOOR

        job_id, body, session, client_ip, started_at = claimed

        async def report_phase(phase: str) -> None:
            try:
                await app.state.job_store.mark_phase(
                    job_id, phase, cfg.job_result_ttl_seconds)
            except Exception:
                LOG.warning("异步 job=%s 阶段更新失败", job_id)

        try:
            await report_phase("dispatching")
            dispatch_wait_started = time.perf_counter()
            async with ctx.query_slots:
                ctx.metrics.observe_concurrency_wait(
                    "query", time.perf_counter() - dispatch_wait_started)
                started_at = time.time()
                await report_phase("querying")
                if not await app.state.job_store.mark_running(
                        job_id, started_at, cfg.job_result_ttl_seconds):
                    continue
                result = await asyncio.wait_for(
                    app.state.pipeline.run(
                        body, session=session, progress_cb=report_phase),
                    timeout=cfg.request_timeout)
        except asyncio.TimeoutError:
            result = {
                "success": False, "kind": "request_error", "run_id": job_id,
                "output": "查询超时，请稍后重试",
                "meta": {"elapsed_ms": int(cfg.request_timeout * 1000)},
            }
        except Exception as exc:
            LOG.exception("异步 job=%s 执行异常", job_id)
            result = {
                "success": False, "kind": "internal_error", "run_id": job_id,
                "output": "服务内部异常，请稍后重试",
                "meta": {"elapsed_ms": int((time.time() - started_at) * 1000)},
            }

        try:
            await app.state.job_store.complete(
                job_id, result, cfg.job_result_ttl_seconds)
            await ctx.record_query_log(ctx.query_log_entry(
                body, client_ip, started_at, result, result.get("run_id", "")))
        except Exception as exc:
            LOG.exception("异步 job=%s 状态写入失败", job_id)


async def _reap_stale_jobs(app, ctx: AppContext) -> None:
    while True:
        try:
            if app.state.job_store is None:
                # 无 Redis 部署下异步任务整体禁用；避免 None.claim 每秒空转报错
                await asyncio.sleep(30)
                continue
            stale_after = max(
                ctx.cfg.job_stale_after_seconds, ctx.cfg.request_timeout + 30)
            await app.state.job_store.reap_stale(
                stale_after, ctx.cfg.job_result_ttl_seconds)
        except Exception as exc:
            LOG.warning("异步任务过期清理失败：%s", exc.__class__.__name__)
        await asyncio.sleep(30)


def start_job_workers(app, ctx: AppContext) -> None:
    """创建 worker 与 reaper 后台任务；调用顺序与拆分前 _startup 保持一致。

    无 Redis（job_store=None）时异步任务整体禁用，不创建空转任务。
    """
    if app.state.job_store is None:
        return
    app.state.job_worker_tasks = [
        asyncio.create_task(_run_one_job(app, ctx),
                            name=f"query-job-worker-{index}")
        for index in range(max(1, ctx.cfg.job_workers))
    ]
    app.state.job_reaper_task = asyncio.create_task(
        _reap_stale_jobs(app, ctx), name="query-job-reaper")
