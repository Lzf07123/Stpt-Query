"""后台刷新调度：按 next_refresh_at 原子抢占到期订阅，单实例内串行刷新。"""
from __future__ import annotations

import asyncio
import random
from typing import Optional

from ..trace import LOG
from .service import CalendarService


class CalendarScheduler:
    """周期性检查到期订阅；抢占由 store.claim_due 的原子 UPDATE 保证。"""

    def __init__(self, service: CalendarService, interval: int = 60, batch: int = 2,
                 jitter: float = 0.1) -> None:
        self.service = service
        self.interval = max(15, int(interval))
        self.batch = max(1, int(batch))
        self.jitter = max(0.0, min(0.5, float(jitter)))

    async def tick(self) -> None:
        """单轮工作：顺带清理过期数据（幂等键 + 超保留期审计行），再刷新到期订阅。"""
        try:
            removed = self.service.store.cleanup_expired()
            if removed["ops_removed"] or removed["audit_removed"]:
                LOG.info("日历过期数据清理 ops=%s audit=%s",
                         removed["ops_removed"], removed["audit_removed"])
        except Exception as exc:
            LOG.warning("日历过期数据清理异常：%s", exc.__class__.__name__)
        try:
            results = await self.service.refresh_due(limit=self.batch)
        except Exception as exc:                          # 调度失败不影响主服务
            LOG.warning("日历刷新调度异常：%s", exc.__class__.__name__)
            return
        if results:
            LOG.info("日历刷新完成 results=%s", results)

    async def run(self, stop: asyncio.Event) -> None:
        LOG.info("日历刷新调度已启动 interval=%ss batch=%s", self.interval, self.batch)
        while not stop.is_set():
            delay = self.interval * (1 + random.uniform(0, self.jitter))
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
                break
            except asyncio.TimeoutError:
                pass
            await self.tick()
        LOG.info("日历刷新调度已停止")
