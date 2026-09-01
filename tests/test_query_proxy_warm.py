"""首查预热的错误语义：瞬时失败必须与会话失效区分。"""
from __future__ import annotations

import requests
import pytest

import jwxt_state
from jwxt_core import SessionInvalidError, TokenError, WarmPendingError


class FakeSessions:
    def __init__(self, record):
        self.record = record

    def get(self, sid):
        return self.record


class FakeUpstream:
    def __init__(self, error):
        self.error = error

    def jwxt(self, *_args):
        return None

    def student(self):
        raise self.error

    def close(self):
        return None


def test_transient_warm_failure_preserves_session(monkeypatch):
    record = {"portal": {}, "cookies": {}, "username": "2023000001"}
    sessions = FakeSessions(record)
    monkeypatch.setattr(
        jwxt_state, "_session_j",
        lambda *args, **kwargs: FakeUpstream(requests.ConnectionError("upstream")))

    with pytest.raises(WarmPendingError) as exc_info:
        jwxt_state._ensure_warmed(sessions, "sid", record)

    assert sessions.get("sid") is record
    assert str(exc_info.value) == "会话预热门户暂未完成，请稍后重试"


def test_auth_warm_failure_remains_session_invalid(monkeypatch):
    record = {"portal": {}, "cookies": {}, "username": "2023000001"}
    sessions = FakeSessions(record)
    monkeypatch.setattr(
        jwxt_state, "_session_j",
        lambda *args, **kwargs: FakeUpstream(SessionInvalidError("学校端会话已失效")))

    with pytest.raises(TokenError):
        jwxt_state._ensure_warmed(sessions, "sid", record)
