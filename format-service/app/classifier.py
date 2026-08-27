"""确定性异常分类器：替代原 Dify 的 4 个「报错解析」LLM 节点。

规则来自 docs/异常诊断-提示词.md 的「常见信号速查」表，把 LLM 查表改为代码查表：
毫秒级返回、零 token 成本、可单元测试；未命中规则时给出通用诊断，可选 LLM 兜底。
"""
from __future__ import annotations

import re
import json
from typing import Any, Dict, List, Optional, Tuple

# (分类, 正则信号, 用户可自行处理, 需要管理员/运维处理)
_RULES: Tuple[Tuple[str, str, List[str], List[str]], ...] = (
    (
        "HTTP 节点配置或执行异常",
        r"Connection refused|ConnectionError|ConnectError|ConnectTimeout|connect failed|连接失败|服务无法连接",
        ["稍后重试"],
        ["核对服务是否启动、BaseURL 地址与端口是否正确"],
    ),
    (
        "路径或地址配置错误",
        r"\b404\b|Not Found",
        ["稍后重试"],
        ["核对服务路径/地址配置是否变更"],
    ),
    (
        # 必须先于「服务访问令牌错误」：学校端拒绝密码时上游也可能返回 401
        "凭据被拒绝",
        r"login verify failed|登录验证失败|账号或密码错误|账号不存在|账号被锁定|PASSERROR|教务系统登录失败",
        ["核对学号与密码、确认密码是否近期修改；先在官网登录验证，官网也失败则重置密码"],
        ["如官网可登录但服务失败，记录现场信息通知管理员"],
    ),
    (
        "服务访问令牌错误",
        r"token required|invalid token|token 无效",
        ["无需用户处理"],
        ["核对上游服务的 Bearer 令牌配置与请求头"],
    ),
    (
        "会话失效或已过期",
        r"session.{0,32}(invalid|expired)|会话无效|会话已过期|SessionInvalid|TokenError|重新登录",
        ["重新登录后再查询"],
        ["检查会话 TTL、Redis 配置与多副本令牌一致性"],
    ),
    (
        "请求参数错误",
        r"\b400\b|username|password|必填",
        ["确认学号、密码已完整填写，字段名正确"],
        ["检查上游节点变量映射"],
    ),
    (
        "节点超时",
        r"timeout|request timeout|timed out",
        ["稍后重试；确认是网络慢还是远端系统慢"],
        ["必要时调大超时设置，但不要依赖无限重试"],
    ),
    (
        "课表学期未开放",
        r"课表查询暂未开放|学期.{0,30}暂未开放|暂未开放.{0,30}课表",
        ["在学期开放后重试；可先查询历史已开放学期"],
        ["无需处理；学校端控制课表开放时间"],
    ),
    (
        "远端系统异常或服务内部错误",
        r"JSONDecodeError|auth master|KeyError|HTTP 500|\b500\b",
        ["稍后重试"],
        ["记录现场信息并通知管理员（学校门户/教务系统改版、限流或风控）"],
    ),
)

_SENSITIVE_NAME = (
    r"(?:password|passwd|pwd|authorization|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|secret|session|token|jump[_-]?code)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"\b%s\b[\"']?(\s*[:=]\s*)(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;&}\]]+)" % _SENSITIVE_NAME,
    re.IGNORECASE,
)
_SENSITIVE_QUERY = re.compile(
    r"([?&](?:%s|code)=)[^&\s]+" % _SENSITIVE_NAME,
    re.IGNORECASE,
)
_BEARER_VALUE = re.compile(r"(?i)(bearer\s+)[a-z0-9._~+/=-]{8,}")
_MAX_RESPONSE_SUMMARY = 300


def _redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "***" if _SENSITIVE_ASSIGNMENT.match(str(key) + ":x") else _redact_value(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact_value(item) for item in value]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if not isinstance(value, str):
        value = str(value)
    value = _SENSITIVE_QUERY.sub(r"\1***", value)
    value = _SENSITIVE_ASSIGNMENT.sub(r"\1***", value)
    return _BEARER_VALUE.sub(r"\1***", value)


def _response_source(status_code: Optional[int], body: Any,
                     error_message: str = "", error_type: str = "") -> str:
    parts = [item for item in (error_message, error_type, body) if item]
    compact = "；".join(
        _redact_value(item) if isinstance(item, str) else
        json.dumps(_redact_value(item), ensure_ascii=False, separators=(",", ":"))
        for item in parts
    )
    if isinstance(compact, (dict, list, tuple)):
        compact = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    compact = " ".join(str(compact).split())[:_MAX_RESPONSE_SUMMARY]
    prefix = "HTTP %s；" % status_code if status_code is not None else ""
    return "%s响应：%s" % (prefix, compact or "（无）")


def summarize_response(body: Any) -> str:
    """生成可入日志的上游成功响应摘要。"""
    compact = _redact_value(body)
    if isinstance(compact, (dict, list, tuple)):
        compact = json.dumps(compact, ensure_ascii=False, separators=(",", ":"))
    compact = " ".join(str(compact).split())[:_MAX_RESPONSE_SUMMARY]
    compact = re.sub(r"(?i)token", "令牌", compact)
    return "响应：%s" % (compact or "（无）")


def classify_error(
    kind: str,
    status_code: Optional[int] = None,
    body: Any = "",
    error_message: str = "",
    error_type: str = "",
) -> Dict[str, Any]:
    """返回与原「报错解析」LLM 输出对齐的结构化诊断报告。

    kind: login | grades | schedule
    """
    text = "\n".join(str(x) for x in (
        _redact_value(error_message),
        _redact_value(error_type),
        _redact_value(body),
    ) if x)

    category, user_actions, admin_actions = "", [], []
    for cat, pattern, ua, aa in _RULES:
        if re.search(pattern, text, re.IGNORECASE):
            category, user_actions, admin_actions = cat, list(ua), list(aa)
            break
    if not category:
        category = "未分类异常"
        user_actions = ["稍后重试"]
        admin_actions = ["记录现场信息（状态码、响应体、时间）并通知管理员"]

    evidence = _evidence(text, status_code)
    response_summary = _response_source(status_code, body, error_message, error_type)
    # 后台日志是管理界面；统一使用中文术语，避免敏感关键字原样进入存储与响应。
    response_summary = re.sub(r"(?i)token", "令牌", response_summary)
    return {
        "success": False,
        "kind": "%s_error" % kind,
        "output": _format_report(kind, category, evidence, user_actions, admin_actions),
        "meta": {
            "category": category,
            "status_code": status_code,
            "user_actions": user_actions,
            "admin_actions": admin_actions,
            "response_summary": response_summary,
        },
    }


def classify_empty_result(kind: str, body: Any) -> bool:
    """success:true 且 count:0 → 查询成功但无数据，不是故障。"""
    data = body if isinstance(body, dict) else {}
    if not data and isinstance(body, str):
        import json
        try:
            parsed = json.loads(body)
            data = parsed if isinstance(parsed, dict) else {}
        except Exception:
            return False
    return bool(data.get("success")) and int(data.get("count") or 0) == 0


def _evidence(text: str, status_code: Optional[int]) -> str:
    snippet = str(_redact_value(text))
    snippet = " ".join(snippet.split())[:200]
    return "HTTP %s，响应片段：%s" % (status_code if status_code is not None else "?", snippet or "（无）")


def _format_report(kind: str, category: str, evidence: str,
                   user_actions: List[str], admin_actions: List[str]) -> str:
    labels = {"login": "登录", "grades": "成绩查询", "schedule": "课表查询"}
    lines = ["**问题分类：%s**" % category,
             "- 环节：%s" % labels.get(kind, kind),
             "- 判断依据：%s" % evidence,
             "- 用户可自行处理：",
             "  - " + "\n  - ".join(user_actions) if user_actions else "  - 无",
             "- 需要管理员/运维处理：",
             "  - " + "\n  - ".join(admin_actions) if admin_actions else "  - 无"]
    return "\n".join(lines)
