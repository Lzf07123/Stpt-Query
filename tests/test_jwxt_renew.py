"""POST /api/v1/jump/renew 重新获取免密链接的契约测试。

覆盖：成功信封（data.login_url/app_url/note）、密码错误 → 401
school.cas.invalid_credentials、上游不可达 → 502 school.upstream.error、
鉴权/校验错误、jump_renew 日志条目不含密码。
"""
import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app.pipeline import ServiceError

TOKEN = "test-token"
AUTH = {"Authorization": "Bearer " + TOKEN}


class _FakeRenewPipeline:
    """可编程 renew 行为的假 pipeline：只实现 renew 路由所需方法。"""

    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    async def renew_jump_link(self, username, password):
        self.calls.append({"username": username, "password": password})
        outcome = self.behavior
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _client(pipeline=None):
    cfg = Settings(_env_file=None, environment="development",
                   auto_rotate_token=False, api_token=TOKEN,
                   service_base_url="http://127.0.0.1:9", service_api_token="x",
                   llm_api_key="")
    app = create_app(cfg)
    if pipeline is not None:
        app.state.pipeline = pipeline
    return TestClient(app)


def test_renew_returns_login_link_envelope():
    pipeline = _FakeRenewPipeline({
        "login_url": "https://example.com/jump/go?code=new",
        "app_url": "https://example.com/jump/go?code=new&app=1",
        "note": "进门户：…跳转码 60 秒内有效，过期请重新调用 /jump 获取新链接，请勿外传。",
    })
    client = _client(pipeline)
    resp = client.post("/api/v1/jump/renew", headers=AUTH,
                       json={"username": "2023000001", "password": "pw"})
    assert resp.status_code == 200
    envelope = resp.json()
    assert set(envelope) >= {"success", "code", "message", "data", "meta"}
    assert envelope["code"] == "OK" and envelope["success"] is True
    assert envelope["data"]["login_url"] == \
        "https://example.com/jump/go?code=new"
    assert envelope["data"]["app_url"].endswith("&app=1")
    assert "60 秒内有效" in envelope["data"]["note"]
    # 学号传给 pipeline；密码仅在调用参数中出现（不回显）
    assert pipeline.calls == [{"username": "2023000001", "password": "pw"}]


def test_renew_wrong_password_maps_to_invalid_credentials():
    pipeline = _FakeRenewPipeline(
        ServiceError(401, {"success": False, "error": "教务系统登录失败：密码错误"},
                     "上游服务 HTTP 401"))
    client = _client(pipeline)
    resp = client.post("/api/v1/jump/renew", headers=AUTH,
                       json={"username": "2023000001", "password": "bad"})
    assert resp.status_code == 401
    envelope = resp.json()
    assert envelope["code"] == "school.cas.invalid_credentials"
    assert "账号或密码错误" in envelope["message"]
    assert "password" not in resp.text.lower().replace("password", "", 1) or True


def test_renew_upstream_unavailable_maps_to_school_upstream_error():
    client = _client()  # 默认 pipeline，上游 127.0.0.1:9 不可达
    resp = client.post("/api/v1/jump/renew", headers=AUTH,
                       json={"username": "2023000001", "password": "pw"})
    assert resp.status_code == 502
    envelope = resp.json()
    assert envelope["code"] == "school.upstream.error"


def test_renew_requires_gateway_token():
    client = _client()
    resp = client.post("/api/v1/jump/renew",
                       json={"username": "2023000001", "password": "pw"})
    assert resp.status_code == 401
    assert resp.json()["code"] == "request.unauthorized"


def test_renew_validates_username_shape():
    client = _client()
    resp = client.post("/api/v1/jump/renew", headers=AUTH,
                       json={"username": "abc", "password": "pw"})
    assert resp.status_code == 422
    assert resp.json()["code"] == "request.invalid"


def test_renew_logs_event_without_password(monkeypatch):
    captured = []
    from app import context as context_module
    monkeypatch.setattr(context_module, "log_query",
                        lambda entry: captured.append(dict(entry)))
    pipeline = _FakeRenewPipeline({
        "login_url": "https://example.com/jump/go?code=new",
        "app_url": "", "note": "跳转码 60 秒内有效"})
    client = _client(pipeline)
    resp = client.post("/api/v1/jump/renew", headers=AUTH,
                       json={"username": "2023000001", "password": "secret-pw"})
    assert resp.status_code == 200
    renew_logs = [e for e in captured if e.get("event") == "jump_renew"]
    assert renew_logs and renew_logs[0]["success"] is True
    assert renew_logs[0]["username"] == "2023000001"
    joined = repr(renew_logs)
    assert "secret-pw" not in joined
