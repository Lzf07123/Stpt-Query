"""运行 ID 与日志：替代 Dify 系统变量 sys.workflow_run_id。"""
from __future__ import annotations

import logging
import uuid

LOG = logging.getLogger("edu-query-app")


def new_run_id() -> str:
    """每次查询生成 32 位十六进制运行 ID，贯穿日志与响应，便于排查定位。"""
    return uuid.uuid4().hex


def setup_logging(level: int = logging.INFO) -> None:
    """初始化日志；密码/session/token 等敏感字段由上游与各模块脱敏处理。"""
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
