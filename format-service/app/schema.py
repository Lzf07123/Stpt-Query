"""请求/响应模型：对齐原 Dify 开始节点与 dify-workflow-api 网关契约。"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Option = str  # "成绩" | "课表"


class WorkflowRequest(BaseModel):
    """原 Dify 开始节点的 7 个输入 + Dify 风格 inputs 兼容。"""

    username: str = Field(
        ...,
        min_length=10,
        max_length=10,
        pattern=r"^\d{10}$",
        description="学号（10 位数字）",
    )
    password: str = Field(..., min_length=1, description="密码")
    option: str = Field(..., description="查询项目：成绩 / 课表")
    semesters: Optional[str] = Field(
        default=None,
        pattern=r"^$|^\d{4}-\d{4}-[1-2]$",
        description="学期，默认当前学期（如 2025-2026-2）",
    )
    weeks: str = Field(
        default="all",
        pattern=r"^(|all|[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*)$",
        description="周数，默认全部；查询课表时生效",
    )
    md2pdf: bool = Field(default=False, description="仅生成 PDF（仅成绩适用）")
    check: bool = Field(default=False, description="分析成绩（默认不分析）")
    user: Optional[str] = Field(default=None, description="终端用户标识，默认取 username")
    inputs: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Dify 风格 inputs（兼容），字段自动合并到顶层，平铺字段优先",
    )

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


class QueryResult(BaseModel):
    """统一响应：三个原 END 节点合并为单一确定性结构。"""

    success: bool
    kind: str
    output: str = ""
    run_id: str = ""
    meta: Dict[str, Any] = Field(default_factory=dict)
    files: List[Dict[str, str]] = Field(default_factory=list)
    pdf_base64: str = ""
