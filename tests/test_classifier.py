"""异常分类器测试：验证规则表与原提示词速查表一致。"""
from __future__ import annotations

from app.classifier import classify_empty_result, classify_error


def test_school_risk_codes_are_not_retried_as_normal_password_error():
    pass_error_body = (
        '{"meta":{"success":true,"statusCode":200,"message":"ok"},'
        '"data":{"code":"PASSERROR","data":"5,4"}}'
    )
    r = classify_error("login", 401, pass_error_body)
    assert r["meta"]["category"] == "学校风控或账号临时锁定"
    assert "不要连续重试" in r["output"]


def test_user_lock_reports_wait_guidance_and_unlock_time():
    body = (
        '{"meta":{"success":true,"statusCode":200,"message":"ok"},'
        '"data":{"code":"USERLOCK","data":"2026-08-29 11:44:20"}}'
    )
    r = classify_error("login", 401, body)
    assert r["meta"]["category"] == "学校风控或账号临时锁定"
    assert "等待解除时间" in r["output"]
    assert "2026-08-29 11:44:20" in r["output"]


def test_token_required():
    r = classify_error("grades", 401, '{"error": "token required"}')
    assert r["meta"]["category"] == "服务访问令牌错误"
    assert not r["success"]


def test_credentials_rejected():
    r = classify_error("login", 200, '{"success": false, "error": "login verify failed"}')
    assert r["meta"]["category"] == "凭据被拒绝"
    assert "官网登录验证" in r["output"]


def test_connection_error():
    r = classify_error("login", error_message="上游服务连接失败：ConnectError")
    assert r["meta"]["category"] == "HTTP 节点配置或执行异常"


def test_connection_refused():
    r = classify_error("grades", error_message="Connection refused")
    assert r["meta"]["category"] == "HTTP 节点配置或执行异常"


def test_timeout():
    r = classify_error("schedule", error_message="request timeout")
    assert r["meta"]["category"] == "节点超时"


def test_schedule_semester_not_open_is_not_remote_error():
    body = (
        '{"success": false, "error": "SchoolError: 我的课表: 学校端返回错误 '
        'code=50060002 message=2026-2027-1学期的课表查询暂未开放，请稍后再试!"}'
    )
    result = classify_error("schedule", body=body)
    assert result["meta"]["category"] == "课表学期未开放"
    assert "历史已开放学期" in result["output"]
    assert "学校端控制课表开放时间" in result["output"]


def test_session_expired_is_classified():
    r = classify_error("grades", 401,
                       '{"success": false, "error": "session 无效或已过期，请重新登录"}')
    assert r["meta"]["category"] == "会话失效或已过期"
    assert "重新登录" in r["output"]


def test_empty_result_is_not_error():
    assert classify_empty_result("grades", {"success": True, "count": 0})
    assert not classify_empty_result("grades", {"success": False, "count": 0})


def test_error_evidence_redacts_nested_and_url_secrets():
    import json
    body = {
        "message": "login failed",
        "authorization": "Bearer super-secret-value",
        "jump_code": "personal-code",
        "detail": {"api_key": "llm-secret", "url": "/jump/go?code=link-code"},
    }
    result = classify_error("grades", 401, body)
    serialized = json.dumps(result, ensure_ascii=False)
    assert "super-secret-value" not in serialized
    assert "personal-code" not in serialized
    assert "llm-secret" not in serialized
    assert "link-code" not in serialized


def test_error_response_summary_is_redacted_and_bounded():
    body = {
        "error": "token required",
        "authorization": "Bearer super-secret-value",
        "detail": "x" * 500,
    }
    result = classify_error("grades", 401, body)
    summary = result["meta"]["response_summary"]
    assert summary.startswith("HTTP 401；响应：")
    assert "super-secret-value" not in summary
    assert len(summary) <= 320
    assert "token" not in summary.lower()
