"""单体模式契约：查询代理以内嵌 ASGI 应用运行，不经网络、不暴露内部路由。"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app import jwxt_state
from app.main import Settings, create_app


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "test-token",
        "llm_api_key": "",
        "file_log_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)


@pytest.fixture()
def mono_app(monkeypatch):
    """单体模式应用：内嵌探测不访问真实学校网络。"""
    monkeypatch.setattr(
        jwxt_state, "probe_school",
        lambda: {"ok": True, "latency_ms": 1, "busy": False})
    return create_app(_settings())


def test_monolith_mode_embeds_query_proxy(mono_app):
    assert mono_app.state.embedded_jwxt_app is not None


def test_http_compat_mode_has_no_embedded_app():
    app = create_app(_settings(service_base_url="http://127.0.0.1:9",
                               service_api_token="x"))
    assert app.state.embedded_jwxt_app is None


def test_readiness_probes_embedded_proxy_in_process(mono_app):
    with TestClient(mono_app) as client:
        body = client.get("/health/ready").json()
    assert body["query_proxy"] == "ok"


def test_service_client_reaches_embedded_app_in_process(mono_app):
    """pipeline 的服务客户端经 ASGI 传输直达内嵌应用（同一 token）。"""
    payload = asyncio.run(mono_app.state.pipeline.service.get("/health"))
    assert payload["service"] == "jwxt-service"
    assert payload["status"] in ("ok", "degraded")


def test_internal_query_proxy_routes_are_not_public(mono_app):
    with TestClient(mono_app) as client:
        for path in ("/login", "/get_schedule", "/get_grades"):
            assert client.get(path).status_code == 404
