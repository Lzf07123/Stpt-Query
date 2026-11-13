"""统一接口语义（/api/v1/*）：响应信封、稳定错误码、meta 与分页的单一事实来源。

所有 /api/v1/* JSON 接口的响应都遵循同一信封：

    {
      "success": true|false,
      "code": "OK" | "<稳定错误码>",
      "message": "人类可读中文摘要",
      "data": { ... } | null,
      "meta": { "trace_id", "elapsed_ms", "layer", "category", "lock_until", "retry_after", ... }
    }

- code 是前端可编程的稳定机器码（替代旧的错误文案正则匹配）；
- 业务失败使用真实 HTTP 状态码（不再 200+success:false）；
- classifier 仍是异常分类的单一事实来源（meta.category），本模块只做
  category → code → HTTP 状态的映射，不重复实现分类规则；
- 豁免端点（K8s 探针 /health/live|ready、容器 /health、/metrics、
  /cal/{token}.ics、/jump/go、/get_schedule/export、页面）不套信封。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

CODE_OK = "OK"
API_VERSION = "v1"


class ApiError(Exception):
    """统一语义异常：路由抛出，由全局异常处理器转换为错误信封。

    - code 必须在 CODES 注册表中（保证错误码可枚举、可文档化）；
    - message 缺省时使用注册表内的中文摘要；
    - data 携带错误上下文（如 diagnosis 诊断报告）；
    - status_code 缺省时使用注册表默认状态码。
    """

    def __init__(self, code: str, message: str = "", data: Any = None,
                 meta: Optional[dict] = None, status_code: Optional[int] = None):
        if code not in CODES:
            raise ValueError("未注册的错误码：%s（请在 api_semantics.CODES 登记）" % code)
        self.code = code
        self.message = message or CODES[code][1]
        self.data = data
        self.meta = dict(meta or {})
        self.status_code = status_code or CODES[code][0]
        super().__init__(self.message)


# ---------------------------------------------------------------------------
# 错误码注册表：code -> (默认 HTTP 状态码, 中文摘要)
# 命名约定：<域>.<原因>；域：request/capacity/school/service/internal
# ---------------------------------------------------------------------------
CODES: Dict[str, tuple] = {
    # ---- 客户端/网关侧 ----
    "request.invalid": (400, "请求参数错误"),
    "request.unauthorized": (401, "缺少或错误的访问令牌"),
    "request.forbidden": (403, "无权访问该资源"),
    "request.not_found": (404, "接口不存在"),
    "request.method_not_allowed": (405, "请求方法不支持"),
    "request.rate_limited": (429, "请求过于频繁，请稍后再试"),
    "request.timeout": (504, "查询超时，请稍后重试"),
    # ---- 容量/排队 ----
    "capacity.concurrency_full": (503, "当前查询并发已达上限，请稍后再试"),
    "capacity.queue_full": (503, "当前排队人数已达上限，请稍后再试"),
    "capacity.queue_unavailable": (503, "排队服务暂时不可用，请稍后再试"),
    "capacity.jobs_disabled": (503, "异步查询未启用：必须配置 REDIS_URL"),
    # ---- 学校侧（layer 与 school_contract 对齐）----
    "school.cas.invalid_credentials": (401, "账号或密码错误"),
    "school.cas.locked": (403, "学校风控或账号临时锁定"),
    "school.session.expired": (401, "学校端会话已失效，请重新查询"),
    "school.upstream.busy": (503, "学校上游繁忙，请稍后再试"),
    "school.upstream.unavailable": (502, "学校上游暂时不可用，请稍后再试"),
    "school.upstream.error": (502, "学校上游返回错误"),
    "school.portal.changed": (502, "学校门户结构变更（疑似学校侧升级）"),
    "school.jwxt.semester_closed": (409, "课表查询暂未开放"),
    # ---- 服务自身 ----
    "service.unavailable": (503, "服务暂时不可用，请稍后再试"),
    "internal.error": (500, "服务内部异常，请稍后重试"),
}

# 兜底码：未归类的 HTTP 异常按状态码映射（鉴权/限流等横切依赖仍抛 HTTPException）
_HTTP_STATUS_CODES = {
    401: "request.unauthorized",
    403: "request.forbidden",
    404: "request.not_found",
    405: "request.method_not_allowed",
    422: "request.invalid",
    429: "request.rate_limited",
    502: "school.upstream.unavailable",
    503: "service.unavailable",
    504: "request.timeout",
}


# ---------------------------------------------------------------------------
# classifier category → 稳定错误码（classifier 保持类别唯一来源，这里只映射）
# ---------------------------------------------------------------------------
_CATEGORY_CODES = {
    "HTTP 节点配置或执行异常": "school.upstream.unavailable",
    "路径或地址配置错误": "school.upstream.error",
    "学校风控或账号临时锁定": "school.cas.locked",
    "账号或密码错误": "school.cas.invalid_credentials",
    "服务访问令牌错误": "request.unauthorized",
    "会话失效或已过期": "school.session.expired",
    "请求参数错误": "request.invalid",
    "节点超时": "request.timeout",
    "课表学期未开放": "school.jwxt.semester_closed",
    "远端系统异常或服务内部错误": "school.upstream.error",
}


def code_for_category(category: str, response_text: str = "") -> str:
    """classifier 类别 → 稳定错误码；远端系统异常中的门户改版信号细分为
    school.portal.changed（升级定位信号，见 docs/upstream-contract.md）。"""
    code = _CATEGORY_CODES.get(category or "", "internal.error")
    if code == "school.upstream.error" and "auth master" in str(response_text or ""):
        code = "school.portal.changed"
    return code


# 日历模块自有错误码 → 统一注册表（app/calendar/routes.py 的 CalendarError.code）
_CALENDAR_CODES = {
    "credential_error": "school.cas.invalid_credentials",
    "upstream_error": "school.upstream.unavailable",
    "not_found": "request.not_found",
    "token_unavailable": "school.upstream.unavailable",
    "invalid_calendar_config": "request.invalid",
}


def code_for_calendar(code: str) -> str:
    """日历业务码 → 统一错误码（未登记的按内部错误兜底）。"""
    return _CALENDAR_CODES.get(str(code or ""), "internal.error")


# ---------------------------------------------------------------------------
# 信封构造
# ---------------------------------------------------------------------------
def ok_envelope(data: Any, *, message: str = "", trace_id: str = "",
                elapsed_ms: Optional[int] = None,
                extra_meta: Optional[dict] = None) -> dict:
    """成功信封：data 为业务载荷，meta 承载 trace/耗时/业务元信息。"""
    meta: Dict[str, Any] = {}
    if trace_id:
        meta["trace_id"] = trace_id
    if elapsed_ms is not None:
        meta["elapsed_ms"] = elapsed_ms
    if extra_meta:
        meta.update(extra_meta)
    return {"success": True, "code": CODE_OK, "message": message,
            "data": data, "meta": meta}


def error_envelope(code: str, *, message: str = "", data: Any = None,
                   meta: Optional[dict] = None,
                   status_code: Optional[int] = None) -> tuple:
    """错误信封：返回 (payload, http_status)。code 必须已注册。"""
    if code not in CODES:
        raise ValueError("未注册的错误码：%s（请在 api_semantics.CODES 登记）" % code)
    payload = {"success": False, "code": code,
               "message": message or CODES[code][1],
               "data": data, "meta": dict(meta or {})}
    return payload, status_code or CODES[code][0]


def envelope_http_status(payload: dict) -> int:
    """信封 → HTTP 状态码（成功 200；失败取注册表默认值）。"""
    if payload.get("success"):
        return 200
    return CODES.get(payload.get("code"), CODES["internal.error"])[0]


def code_from_http_status(status_code: int) -> str:
    """横切依赖抛出的裸 HTTPException → 最近似的已注册错误码。"""
    return _HTTP_STATUS_CODES.get(status_code, "internal.error")


def pagination(total: int, offset: int, limit: int) -> dict:
    """统一分页语义（admin/日志/通知列表共用）。"""
    return {"total": total, "offset": offset, "limit": limit,
            "has_more": offset + limit < total}


# ---------------------------------------------------------------------------
# pipeline 结果 → 信封（/api/v1/run 与任务轮询共用同一转换）
# ---------------------------------------------------------------------------
def envelope_from_pipeline(result: dict) -> tuple:
    """把 pipeline.run 的结果 dict（QueryResult 形状）转换为 (信封, HTTP 状态)。

    - 成功：data = {kind, output, pdf_base64?}；result.meta 的业务元信息
      （count/response_summary/analysis_*/calendar/relogin_retry）并入信封 meta；
    - 失败：classifier 诊断进 data.diagnosis，code 由 category 映射；
      非分类失败（request_error/internal_error/busy_error）按 kind 直接映射。
    """
    result = dict(result or {})
    trace_id = str(result.get("run_id") or "")
    meta_in = dict(result.get("meta") or {})
    elapsed_ms = meta_in.pop("elapsed_ms", None)

    if result.get("success"):
        data: Dict[str, Any] = {"kind": result.get("kind", ""),
                                "output": result.get("output", "")}
        pdf = result.get("pdf_base64")
        if pdf:
            data["pdf_base64"] = pdf
        meta_in["trace_id"] = trace_id
        if elapsed_ms is not None:
            meta_in["elapsed_ms"] = elapsed_ms
        return ok_envelope(data, extra_meta=meta_in), 200

    # ---- 失败路径 ----
    kind = str(result.get("kind") or "")
    classified_meta = dict(result.get("meta") or {})
    category = str(classified_meta.get("category") or "")
    if category:
        code = code_for_category(category, classified_meta.get("response_summary", ""))
        status_code = CODES[code][0]
        diagnosis = {
            "report": result.get("output", ""),
            "user_actions": classified_meta.get("user_actions", []),
            "admin_actions": classified_meta.get("admin_actions", []),
            "response_summary": classified_meta.get("response_summary", ""),
            "status_code": classified_meta.get("status_code"),
        }
        meta_out = {"trace_id": trace_id, "layer": classified_meta.get("layer"),
                    "category": category}
        if elapsed_ms is not None:
            meta_out["elapsed_ms"] = elapsed_ms
        if classified_meta.get("lock_until"):
            meta_out["lock_until"] = classified_meta["lock_until"]
        return error_envelope(code, data={"kind": _kind_of(kind), "diagnosis": diagnosis},
                              meta=meta_out, status_code=status_code)

    # 非分类失败：编排层自产的确定性失败
    fallback = {
        "busy_error": "capacity.concurrency_full",
        "request_error": "request.timeout",
        "internal_error": "internal.error",
    }
    code = fallback.get(kind, "internal.error")
    meta_out = {"trace_id": trace_id}
    if elapsed_ms is not None:
        meta_out["elapsed_ms"] = elapsed_ms
    return error_envelope(code, data={"kind": _kind_of(kind)}, meta=meta_out)


def _kind_of(kind: str) -> str:
    """kind 归一：去掉错误后缀（grades_error → grades），保留资源语义。"""
    for suffix in ("_error", "_pdf"):
        if str(kind or "").endswith(suffix):
            return str(kind)[: -len(suffix)]
    return str(kind or "")
