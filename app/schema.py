"""请求/响应模型：查询请求与异步任务的内部数据结构。

对外响应统一由 api_semantics 信封承载（/api/v1/*）；本模块的 QueryResult
是 pipeline 与任务队列的内部载荷，出站前经 envelope_from_pipeline 转换。
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Option = str  # "成绩" | "课表"
JobPhase = Literal[
    "queued", "dispatching", "querying", "analyzing",
    "generating_pdf", "done",
]


class WorkflowRequest(BaseModel):
    """查询请求：7 个输入字段。"""

    username: str = Field(
        ...,
        min_length=10,
        max_length=10,
        pattern=r"^\d{10}$",
        description="学号（10 位数字）",
    )
    password: str = Field(..., min_length=1, max_length=256, description="密码")
    option: str = Field(..., max_length=20, description="查询项目：成绩 / 课表")
    semesters: Optional[str] = Field(
        default=None,
        pattern=r"^(|all|recent:[1-9][0-9]*|(\d{4}-\d{4}-[1-2])(,\d{4}-\d{4}-[1-2])*)$",
        description=(
            "学期，默认当前学期（如 2025-2026-2）；仅成绩支持 all（全部学期）、"
            "recent:N（最近 N 个学期）与逗号分隔多学期，上限由查询代理约束"
        ),
    )
    weeks: str = Field(
        default="all",
        pattern=r"^(|all|[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*)$",
        description="周数，默认全部；查询课表时生效",
    )
    md2pdf: bool = Field(default=False, description="仅生成 PDF（仅成绩适用）")
    check: bool = Field(default=False, description="分析成绩（默认不分析）")
    include_all: bool = Field(
        default=False,
        description="附带全部成绩与学分概况（仅成绩适用，透传查询代理 include_all）",
    )
    odd_or_double: Optional[int] = Field(
        default=None,
        description="课表单双周：1 或 2（仅课表适用；留空沿用查询代理默认 1）",
    )
    user: Optional[str] = Field(default=None, max_length=100,
                                description="终端用户标识，默认取 username")
    inputs: Optional[Dict[str, Any]] = Field(
        default=None,
        description="历史遗留的嵌套 inputs（兼容读取），字段自动合并到顶层，平铺字段优先",
    )

    @field_validator("odd_or_double")
    @classmethod
    def _validate_odd_or_double(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value not in (1, 2):
            raise ValueError("odd_or_double 只接受 1 或 2")
        return value

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return str(value)
        return value

    @model_validator(mode="before")
    @classmethod
    def _flatten_dify_inputs(cls, data: Any) -> Any:
        if isinstance(data, dict) and isinstance(data.get("inputs"), dict):
            merged = dict(data)
            for key, value in merged.pop("inputs", {}).items():
                merged.setdefault(key, value)
            return merged
        return data


class JumpRenewRequest(BaseModel):
    """重新获取免密跳转链接：重新认证签发（不续期过期码，安全边界）。

    字段约束与 WorkflowRequest 的 username/password 一致；
    密码仅用于本次登录（LOGIN_REUSE 命中时不打学校），不落日志。
    """

    username: str = Field(
        ...,
        min_length=10,
        max_length=10,
        pattern=r"^\d{10}$",
        description="学号（10 位数字）",
    )
    password: str = Field(..., min_length=1, max_length=256, description="密码")

    @field_validator("username", mode="before")
    @classmethod
    def _normalize_username(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return str(value)
        return value


class QueryResult(BaseModel):
    """pipeline 与任务队列的内部载荷（出站前经 envelope_from_pipeline 转换）。"""

    success: bool
    kind: str
    output: str = ""
    run_id: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)
    files: List[Dict[str, str]] = Field(default_factory=list)
    pdf_base64: str = ""


class SessionWorkflowRequest(WorkflowRequest):
    """任务 worker 使用的内部请求；队列中只允许短期 session，不允许密码。"""

    password: Optional[str] = Field(default=None, max_length=256, description="密码")


class JobSubmissionResponse(BaseModel):
    """异步任务受理结果；前端以 job_id 轮询状态。"""

    job_id: str
    state: Literal["queued"] = "queued"
    deduplicated: bool = False
    position: Optional[int] = None
    poll_url: str
    retry_after: int = 1


class JobStatusResponse(BaseModel):
    """异步任务状态；绝不返回 session、密码、网关令牌或查询代理票据。"""

    job_id: str
    state: str
    phase: JobPhase = "queued"
    phase_index: int = 2
    phase_label: str = "排队中"
    phase_started_at: Optional[float] = None
    position: Optional[int] = None
    enqueued_at: float
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result: Optional[QueryResult] = None
