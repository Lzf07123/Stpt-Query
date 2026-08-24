"""查询日志模块测试：结构化 JSON、敏感字段剔除、失败不影响主流程。"""
from __future__ import annotations

import json
import logging

from app.querylog import _sanitize, log_query


def test_sanitize_keeps_allowed_fields_only():
    entry = {
        "event": "query", "run_id": "r1", "username": "2023000001",
        "password": "secret", "session": "s1", "token": "t1",
        "success": True, "kind": "grades", "elapsed_ms": 12,
        "not_allowed": "x",
    }
    clean = _sanitize(entry)
    assert clean == {
        "event": "query", "run_id": "r1", "username": "2023000001",
        "success": True, "kind": "grades", "elapsed_ms": 12,
    }


def test_log_query_emits_single_json_line_without_sensitive_fields(caplog):
    with caplog.at_level(logging.INFO, logger="edu-query-app.query"):
        log_query({
            "event": "query", "time": "2026-08-25T12:00:00+08:00", "run_id": "r1",
            "client_ip": "127.0.0.1", "username": "2023000001", "option": "成绩",
            "semesters": "", "weeks": "all", "md2pdf": False, "check": False,
            "success": True, "kind": "grades", "elapsed_ms": 42,
            "password": "pw", "session": "s1", "authorization": "Bearer x",
        })
    assert len(caplog.records) == 1
    data = json.loads(caplog.records[0].message)
    assert data["event"] == "query"
    assert data["username"] == "2023000001"
    assert data["kind"] == "grades"
    for forbidden in ("password", "session", "authorization", "token"):
        assert forbidden not in data
