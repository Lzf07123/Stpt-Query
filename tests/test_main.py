"""HTTP 层测试：探针端点与鉴权。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import Settings, create_app


def _client() -> TestClient:
    cfg = Settings(
        environment="development",
        auto_rotate_token=False,
        api_token="test-token",
        service_base_url="http://127.0.0.1:9",
        service_api_token="x",
        llm_api_key="",
    )
    return TestClient(create_app(cfg))


def test_liveness():
    resp = _client().get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_readiness_without_redis():
    resp = _client().get("/health/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["redis"] == "not-configured"


def test_run_requires_bearer_token():
    resp = _client().post("/run", json={
        "username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 401


def test_run_with_token_reaches_pipeline():
    resp = _client().post(
        "/run",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    # 上游 127.0.0.1:9 不可达 → 应返回分类后的友好错误而非 500
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is False
    assert body["kind"] == "login_error"
    assert body["run_id"]


def test_run_records_structured_query_log_without_password(caplog):
    import json
    import logging

    with caplog.at_level(logging.INFO, logger="edu-query-app.query"):
        resp = _client().post(
            "/run",
            headers={"Authorization": "Bearer test-token"},
            json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 200
    query_records = [
        r for r in caplog.records
        if getattr(r, "name", "") == "edu-query-app.query"
        and '"event": "query"' in r.getMessage()
    ]
    assert query_records, "应输出一条结构化查询日志"
    data = json.loads(query_records[0].getMessage())
    assert data["username"] == "2023000001"
    assert data["option"] == "成绩"
    assert data["success"] is False and data["kind"] == "login_error"
    assert data["run_id"]
    assert "password" not in query_records[0].getMessage()


def test_query_logs_requires_bearer_token():
    resp = _client().get("/query-logs")
    assert resp.status_code == 401


def test_query_logs_returns_entries_after_run():
    client = _client()
    resp = client.post(
        "/run",
        headers={"Authorization": "Bearer test-token"},
        json={"username": "2023000001", "password": "pw", "option": "成绩"})
    assert resp.status_code == 200
    resp = client.get("/query-logs", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] >= 1
    assert body["logs"][0]["username"] == "2023000001"
    assert body["logs"][0]["kind"] == "login_error"
    serialized = str(body)
    for forbidden in ("password", "session", "token"):
        assert forbidden not in serialized
