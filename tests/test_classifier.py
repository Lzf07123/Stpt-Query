"""异常分类器测试：验证规则表与原提示词速查表一致。"""
from __future__ import annotations

from app.classifier import classify_empty_result, classify_error


def test_token_required():
    r = classify_error("grades", 401, '{"error": "token required"}')
    assert r["meta"]["category"] == "服务访问令牌错误"
    assert not r["success"]


def test_credentials_rejected():
    r = classify_error("login", 200, '{"success": false, "error": "login verify failed"}')
    assert r["meta"]["category"] == "凭据被拒绝"
    assert "官网登录验证" in r["output"]


def test_connection_refused():
    r = classify_error("grades", error_message="Connection refused")
    assert r["meta"]["category"] == "HTTP 节点配置或执行异常"


def test_timeout():
    r = classify_error("schedule", error_message="request timeout")
    assert r["meta"]["category"] == "节点超时"


def test_empty_result_is_not_error():
    assert classify_empty_result("grades", {"success": True, "count": 0})
    assert not classify_empty_result("grades", {"success": False, "count": 0})
