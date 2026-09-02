"""查询日志模块测试：结构化 JSON、敏感字段剔除、失败不影响主流程。"""
from __future__ import annotations

import json
import logging
import os

from app.querylog import JSONLFileWriter, _sanitize, log_query


def test_sanitize_keeps_allowed_fields_only():
    entry = {
        "event": "query", "run_id": "r1", "username": "2023000001",
        "password": "secret", "session": "s1", "token": "t1",
        "success": True, "kind": "grades", "elapsed_ms": 12,
        "response_summary": "响应：{\"success\":true,\"count\":2}",
        "not_allowed": "x",
    }
    clean = _sanitize(entry)
    assert clean == {
        "event": "query", "run_id": "r1", "username": "2023000001",
        "success": True, "kind": "grades", "elapsed_ms": 12,
        "response_summary": "响应：{\"success\":true,\"count\":2}",
    }


def test_log_query_emits_single_json_line_without_sensitive_fields(caplog):
    with caplog.at_level(logging.INFO, logger="edu-query-app.query"):
        log_query({
            "event": "query", "time": "2026-08-25T12:00:00+08:00", "run_id": "r1",
            "client_ip": "127.0.0.1", "username": "2023000001", "option": "成绩",
            "semesters": "", "weeks": "all", "md2pdf": False, "check": False,
            "success": True, "kind": "grades", "elapsed_ms": 42,
            "analysis": True, "analysis_usage": 128,
            "response_summary": "响应：{\"success\":true,\"count\":2}",
            "password": "pw", "session": "s1", "authorization": "Bearer x",
        })
    assert len(caplog.records) == 1
    data = json.loads(caplog.records[0].message)
    assert data["event"] == "query"
    assert data["username"] == "2023000001"
    assert data["kind"] == "grades"
    assert data["analysis"] is True
    assert data["analysis_usage"] == 128
    assert data["response_summary"].startswith("响应：")
    for forbidden in ("password", "session", "authorization", "token"):
        assert forbidden not in data


def test_jsonl_file_writer_sanitizes_and_appends(tmp_path):
    path = tmp_path / "logs" / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    entry = {
        "event": "query", "run_id": "r1", "username": "2023000001",
        "password": "secret", "token": "secret-token",
        "success": True, "kind": "grades", "elapsed_ms": 8,
        "not_allowed": "ignored",
    }
    writer.write_raw(entry)
    writer.write_raw(entry)

    assert os.stat(path).st_mode & 0o777 == 0o600
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    data = json.loads(lines[-1])
    assert data["username"] == "2023000001"
    assert data["kind"] == "grades"
    for forbidden in ("password", "token", "not_allowed"):
        assert forbidden not in data


def test_jsonl_file_writer_keeps_one_handle_until_closed(tmp_path):
    path = tmp_path / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    entry = {"event": "query", "run_id": "r1", "success": True}

    writer.write_raw(entry)
    descriptor = writer._descriptor
    writer.write_raw(entry)

    assert descriptor is not None
    assert writer._descriptor == descriptor
    writer.close()
    assert writer._descriptor is None


def test_jsonl_file_writer_rotates_by_size(tmp_path):
    path = tmp_path / "queries.jsonl"
    writer = JSONLFileWriter(str(path), max_bytes=32, backup_count=2)
    for index in range(4):
        writer.write_raw({"event": "query", "run_id": f"r{index}", "success": True})

    active_lines = path.read_text(encoding="utf-8").splitlines()
    first = (tmp_path / "queries.jsonl.1").read_text(encoding="utf-8").splitlines()
    second = (tmp_path / "queries.jsonl.2").read_text(encoding="utf-8").splitlines()
    record = {"event": "query", "run_id": "ignored", "success": True}
    assert second == [
        json.dumps(record | {"run_id": "r1"}, separators=(",", ":"))
    ]
    assert first[0] == json.dumps(
        record | {"run_id": "r2"}, separators=(",", ":")
    )
    assert len(first) == 1
    assert json.loads(active_lines[0]) == record | {"run_id": "r3"}
    assert len(active_lines) >= 1
