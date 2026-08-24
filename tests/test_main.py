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
