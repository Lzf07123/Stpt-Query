"""统一接口语义测试：信封形状、错误码注册表、映射完整性与端点验收。

- 所有 /api/v1/* JSON 响应必须携带完整信封五键；
- 错误码 → HTTP 状态映射与 classifier category 映射必须覆盖全部注册类别；
- 业务失败使用真实 HTTP 状态码；豁免端点（探针/透传）保持原语义。
"""

import pytest
from fastapi.testclient import TestClient

from app.api_semantics import (CODES, envelope_from_pipeline, envelope_http_status,
                               code_for_calendar, code_for_category, error_envelope,
                               ok_envelope, pagination)
from app.main import Settings, create_app

HEADERS = {"Authorization": "Bearer test-token"}
PAYLOAD = {"username": "2023000001", "password": "pw", "option": "成绩"}

ENVELOPE_KEYS = {"success", "code", "message", "data", "meta"}


def _client() -> TestClient:
    cfg = Settings(
        _env_file=None,
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
    )
    return TestClient(create_app(cfg))


# ---------------------------------------------------------------------------
# 语义核心（纯函数）
# ---------------------------------------------------------------------------

def test_code_registry_covers_categories_and_calendar_codes():
    """classifier 全部注册类别与日历自有码都必须有映射，且映射目标已登记。"""
    from app.classifier import _RULES
    from app.notices import NoticeError  # noqa: F401  确认模块可导入

    categories = {rule[0] for rule in _RULES} | {"未分类异常"}
    for category in categories:
        code = code_for_category(category)
        assert code in CODES, "类别 %s 映射到未注册错误码 %s" % (category, code)
    for calendar_code in ("credential_error", "upstream_error", "not_found",
                          "token_unavailable", "invalid_calendar_config"):
        assert code_for_calendar(calendar_code) in CODES


def test_error_envelope_uses_registered_status_and_message():
    payload, status_code = error_envelope("school.cas.invalid_credentials")
    assert payload["success"] is False
    assert payload["code"] == "school.cas.invalid_credentials"
    assert status_code == 401
    assert payload["message"]
    assert set(payload) == ENVELOPE_KEYS


def test_error_envelope_rejects_unregistered_code():
    with pytest.raises(ValueError):
        error_envelope("not.a.registered.code")


def test_ok_envelope_shape():
    payload = ok_envelope({"kind": "grades"}, trace_id="run-1", elapsed_ms=12,
                          extra_meta={"count": 3})
    assert payload["success"] is True
    assert payload["code"] == "OK"
    assert payload["data"] == {"kind": "grades"}
    assert payload["meta"] == {"trace_id": "run-1", "elapsed_ms": 12, "count": 3}
    assert envelope_http_status(payload) == 200


def test_pagination_semantics():
    page = pagination(10, 0, 20)
    assert page == {"total": 10, "offset": 0, "limit": 20, "has_more": False}
    assert pagination(10, 0, 5)["has_more"] is True


def test_envelope_from_pipeline_success_and_meta_merge():
    payload, status_code = envelope_from_pipeline({
        "success": True, "kind": "grades", "run_id": "run-9",
        "output": "## 我的成绩", "pdf_base64": "AAAA",
        "meta": {"count": 5, "elapsed_ms": 88},
    })
    assert status_code == 200
    assert payload["success"] is True and payload["code"] == "OK"
    assert payload["data"]["kind"] == "grades"
    assert payload["data"]["output"] == "## 我的成绩"
    assert payload["data"]["pdf_base64"] == "AAAA"
    assert payload["meta"]["trace_id"] == "run-9"
    assert payload["meta"]["elapsed_ms"] == 88
    assert payload["meta"]["count"] == 5


def test_envelope_from_pipeline_failure_maps_category():
    payload, status_code = envelope_from_pipeline({
        "success": False, "kind": "login_error", "run_id": "run-2",
        "output": "**问题分类：账号或密码错误**",
        "meta": {"category": "账号或密码错误", "layer": "cas",
                 "status_code": 401,
                 "user_actions": ["核对学号与密码"],
                 "admin_actions": [], "response_summary": "HTTP 401；响应：PASSERROR"},
    })
    assert status_code == 401
    assert payload["code"] == "school.cas.invalid_credentials"
    assert payload["data"]["kind"] == "login"
    assert payload["data"]["diagnosis"]["report"].startswith("**问题分类")
    assert payload["meta"]["layer"] == "cas"
    assert payload["meta"]["category"] == "账号或密码错误"


def test_envelope_from_pipeline_lock_until_surfaces_in_meta():
    payload, status_code = envelope_from_pipeline({
        "success": False, "kind": "login_error", "run_id": "run-3",
        "output": "风控",
        "meta": {"category": "学校风控或账号临时锁定", "layer": "cas",
                 "status_code": 401, "lock_until": "2026-08-29 13:22:35",
                 "user_actions": [], "admin_actions": [], "response_summary": ""},
    })
    assert status_code == 403
    assert payload["code"] == "school.cas.locked"
    assert payload["meta"]["lock_until"] == "2026-08-29 13:22:35"


def test_envelope_from_pipeline_portal_drift_detected():
    payload, _ = envelope_from_pipeline({
        "success": False, "kind": "login_error", "run_id": "run-4",
        "output": "", "meta": {"category": "远端系统异常或服务内部错误",
                               "response_summary": "HTTP 200；响应：auth master: oauth2"},
    })
    assert payload["code"] == "school.portal.changed"


def test_envelope_from_pipeline_unclassified_kinds():
    for kind, code in (("busy_error", "capacity.concurrency_full"),
                       ("request_error", "request.timeout"),
                       ("internal_error", "internal.error")):
        payload, status_code = envelope_from_pipeline({
            "success": False, "kind": kind, "run_id": "run-5", "meta": {}})
        assert payload["code"] == code, kind
        assert status_code == CODES[code][0]


def test_envelope_http_status_failure_uses_registry():
    payload, _ = error_envelope("school.jwxt.semester_closed")
    assert envelope_http_status(payload) == 409


# ---------------------------------------------------------------------------
# 端点验收：全部 /api/v1/* JSON 响应携带完整信封；业务失败用真实状态码
# ---------------------------------------------------------------------------

def test_v1_run_success_envelope():
    app = create_app(Settings(
        _env_file=None, environment="development", auto_rotate_token=False,
        api_token="test-token", service_base_url="http://127.0.0.1:9",
        service_api_token="x", llm_api_key=""))

    class OkPipeline:
        async def run(self, body):
            return {"success": True, "kind": "grades", "run_id": "run-1",
                    "output": "## 我的成绩", "meta": {"count": 2}}

    app.state.pipeline = OkPipeline()
    with TestClient(app) as client:
        resp = client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD)
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is True and body["code"] == "OK"
    assert body["data"]["kind"] == "grades"
    assert body["meta"]["trace_id"] == "run-1"
    assert body["meta"]["count"] == 2


def test_v1_run_failure_uses_real_http_status_and_diagnosis():
    client = _client()
    resp = client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD)
    # 上游不可达 → 502 + school.upstream.unavailable + diagnosis
    assert resp.status_code == 502
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS
    assert body["success"] is False
    assert body["code"] == "school.upstream.unavailable"
    assert body["data"]["diagnosis"]["user_actions"]
    assert body["meta"]["trace_id"]


def test_v1_infra_errors_are_enveloped():
    client = _client()
    # 鉴权失败
    resp = client.post("/api/v1/run", json=PAYLOAD)
    assert resp.status_code == 401
    assert resp.json()["code"] == "request.unauthorized"
    # 参数校验失败（422 框架约定）
    resp = client.post("/api/v1/run", headers=HEADERS,
                       json={"username": "abc", "password": "pw", "option": "成绩"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "request.invalid"
    assert body["data"]["errors"]
    # 异步任务未启用（503 + 稳定码，前端不再依赖文案正则）
    resp = client.post("/api/v1/run/jobs", headers=HEADERS, json=PAYLOAD)
    assert resp.status_code == 503
    assert resp.json()["code"] == "capacity.jobs_disabled"
    # 未注册路径 → 统一 404 信封
    resp = client.get("/api/v1/definitely-not-a-route", headers=HEADERS)
    assert resp.status_code == 404
    assert resp.json()["code"] == "request.not_found"
    # 旧路径已删除（不再兼容 Dify）
    assert client.post("/run", headers=HEADERS, json=PAYLOAD).status_code == 404
    assert client.post("/v1/workflows/run", headers=HEADERS,
                       json=PAYLOAD).status_code == 404


def test_v1_read_only_endpoints_are_enveloped():
    client = _client()
    for path in ("/api/v1/notices/active", "/api/v1/service-status"):
        resp = client.get(path, headers=HEADERS)
        assert resp.status_code == 200, path
        body = resp.json()
        assert set(body) == ENVELOPE_KEYS, path
        assert body["success"] is True and body["code"] == "OK", path
    body = client.get("/api/v1/service-status", headers=HEADERS).json()["data"]
    # 歧义消除：success 计数只出现在 stats.success_count
    assert "success" not in body or isinstance(body.get("success"), dict) is False
    assert "success_count" in body["stats"]
    assert "accepted_total" in body["stats"]


def test_exempt_endpoints_keep_legacy_semantics():
    client = _client()
    # K8s/容器探针不套信封
    resp = client.get("/health/live")
    assert resp.json() == {"status": "ok"}
    resp = client.get("/health")
    assert resp.json()["status"] in ("ok", "degraded")
    # 透传端点保持原语义
    resp = client.get("/jump")
    assert resp.status_code == 404
    assert resp.json()["code"] == "request.not_found"
