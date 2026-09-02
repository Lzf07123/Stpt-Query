"""管理端契约：独立鉴权、文件历史检索与资源监控。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app import metrics
from app.querylog import JSONLFileWriter


ADMIN = {"Authorization": "Bearer " + ("b" * 24)}


def _settings(tmp_path, **kwargs):
    options = {
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "gateway-token",
        "admin_token": "b" * 24,
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "upstream-token",
        "llm_api_key": "",
        "file_log_enabled": True,
        "file_log_path": str(tmp_path / "queries.jsonl"),
    }
    options.update(kwargs)
    return Settings(**options, _env_file=None)


def test_admin_endpoints_require_independent_admin_token(tmp_path):
    app = create_app(_settings(tmp_path))
    with TestClient(app) as client:
        assert client.get("/admin/api/query-logs").status_code == 404
        wrong = client.get("/admin/api/metrics", headers={"Authorization": "Bearer gateway-token"})
        assert wrong.status_code == 404
        assert client.get("/admin/api/metrics", headers=ADMIN).status_code == 200
        # 管理员令牌不能反向访问网关 API，避免身份复用。
        assert client.get("/query-logs", headers=ADMIN).status_code == 401


def test_admin_query_logs_filters_and_paginates_file_history(tmp_path):
    path = tmp_path / "nested" / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    records = [
        {"event": "query", "time": "2026-08-26T10:00:00+08:00", "run_id": "run-a",
         "client_ip": "192.168.1.2", "username": "2023000001", "option": "成绩",
         "success": True, "kind": "grades", "elapsed_ms": 100},
        {"event": "query", "time": "2026-08-27T11:00:00+08:00", "run_id": "run-b",
         "client_ip": "192.168.1.3", "username": "2023000002", "option": "课表",
         "success": False, "kind": "login_error", "elapsed_ms": 200},
        {"event": "query", "time": "2026-08-28T12:00:00+08:00", "run_id": "run-c",
         "client_ip": "192.168.1.4", "username": "2023000003", "option": "成绩",
         "success": False, "kind": "render_error", "elapsed_ms": 300},
    ]
    for record in reversed(records):
        writer.write_raw(record)

    with TestClient(create_app(_settings(tmp_path, file_log_path=str(path)))) as client:
        payload = client.get("/admin/api/query-logs?limit=1", headers=ADMIN).json()
        assert [item["run_id"] for item in payload["logs"]] == ["run-c"]
        assert payload["total"] == 3
        assert payload["source"] == "file"
        assert payload["pagination"]["has_more"] is True
        assert payload["stats"] == {
            "success": 1, "failure": 2,
            "kinds": {"grades": 1, "login_error": 1, "render_error": 1},
        }

        failed = client.get("/admin/api/query-logs?success=false&keyword=3000002",
                            headers=ADMIN).json()
        assert failed["total"] == 1
        assert failed["logs"][0]["kind"] == "login_error"
        assert all(key not in failed["logs"][0] for key in ("password", "token", "session"))


def test_admin_query_logs_uses_configured_scan_limit_by_default(tmp_path):
    path = tmp_path / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    for index in range(6):
        writer.write_raw({
            "event": "query", "time": f"2026-08-28T10:{index:02d}:00+08:00",
            "run_id": f"run-{index}", "username": "2023000001",
            "option": "成绩", "success": True, "kind": "grades",
        })

    with TestClient(create_app(_settings(tmp_path, file_log_path=str(path)))) as client:
        payload = client.get("/admin/api/query-logs?limit=2", headers=ADMIN).json()
        assert payload["scanned"] == 6
        assert payload["total"] == 6
        assert payload["scan_limit"] == 10000
        assert payload["scan_truncated"] is False

        bounded = client.get("/admin/api/query-logs?limit=2&scan_limit=4",
                             headers=ADMIN).json()
        assert bounded["scanned"] == 4
        assert bounded["total"] == 4
        assert bounded["scan_limit"] == 4
        assert bounded["scan_truncated"] is True
        assert [item["run_id"] for item in bounded["logs"]] == ["run-5", "run-4"]


def test_admin_query_logs_reads_all_instance_directories_beyond_100(tmp_path):
    path = tmp_path / "instance-a" / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    for index in range(101):
        writer.write_raw({
            "event": "query", "time": f"2026-08-28T10:{index // 60:02d}:{index % 60:02d}+08:00",
            "run_id": f"run-{index:03d}", "username": f"2023000{index:03d}",
            "success": True, "kind": "grades",
        })

    with TestClient(create_app(_settings(tmp_path, file_log_path=str(path)))) as client:
        first = client.get("/admin/api/query-logs", headers=ADMIN).json()
        last = client.get(
            "/admin/api/query-logs?offset=100", headers=ADMIN).json()

    assert first["source"] == "file"
    assert first["total"] == 101
    assert [item["run_id"] for item in first["logs"]][0] == "run-100"
    assert first["stats"]["success"] == 101
    assert first["pagination"]["has_more"] is True
    assert last["total"] == 101
    assert [item["run_id"] for item in last["logs"]] == ["run-000"]
    assert last["pagination"]["has_more"] is False


def test_admin_query_logs_prefers_file_history_over_redis(tmp_path):
    path = tmp_path / "queries.jsonl"
    writer = JSONLFileWriter(str(path))
    writer.write_raw({
        "event": "query", "time": "2026-08-28T10:00:00+08:00",
        "run_id": "run-file", "username": "2023000001",
        "success": True, "kind": "grades",
    })

    class RedisOnlyHistory:
        async def recent(self, versioned_key, legacy_key, limit=100):
            return [{
                "event": "query", "time": "2026-08-28T10:01:00+08:00",
                "run_id": "run-redis", "username": "2023000002",
                "success": False, "kind": "login_error",
            }]

    app = create_app(_settings(
        tmp_path, file_log_path=str(path), redis_url="redis://redis:6379/0"))
    app.state.redis_history = RedisOnlyHistory()
    client = TestClient(app)

    payload = client.get("/admin/api/query-logs", headers=ADMIN).json()

    assert payload["source"] == "file"
    assert [item["run_id"] for item in payload["logs"]] == ["run-file"]


def test_admin_metrics_reports_snapshot_and_service_status(tmp_path):
    with TestClient(create_app(_settings(tmp_path))) as client:
        body = client.get("/admin/api/metrics", headers=ADMIN).json()
        assert body["services"]["redis"] == "not-configured"
        assert body["services"]["file_log"] == "ok"
        assert isinstance(body["history"], list)
        snapshot = body["latest"]
        assert snapshot is not None
        assert snapshot["cpu_percent"] is None
        assert snapshot["memory"]["limit_source"] == "cgroup"
        assert set(snapshot) >= {"collected_at", "process", "disk", "network", "uptime_seconds"}
        assert set(snapshot) >= {"host", "storage", "orchestration"}
        assert snapshot["host"]["source"] in {"host_proc", "container_kernel"}
        assert snapshot["host"]["scope"] in {"docker_vm", "linux_kernel"}
        assert set(snapshot["host"]) >= {
            "cpu_percent", "cpu_count", "memory", "load", "uptime_seconds", "disk", "network",
        }
        assert set(snapshot["storage"]) >= {
            "available", "file_bytes", "file_growth_bytes_per_second", "backup_count", "disk",
        }
        stack = snapshot["orchestration"]["memory"]
        assert set(stack) >= {
            "memory_bytes", "limit_bytes", "source", "discovered_services", "expected_services", "services",
        }
        assert stack["expected_services"] == 4
        assert stack["services"]["format-service"]["source"] == "cgroup"
        assert set(stack["services"]) == {
            "format-service", "get-infomation-service", "frontend", "redis",
        }
        assert set(body) >= {"generated_at", "application", "services"}
        assert body["application"]["window_seconds"] == 300
        assert body["application"]["requests"] == 0
        assert body["application"]["elapsed_p95_ms"] is None


def test_orchestration_memory_includes_redis(tmp_path, monkeypatch):
    container_id = "a" * 64
    process_dir = tmp_path / "100"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"redis-server\x00*:6379\x00")
    (process_dir / "cgroup").write_text(f"0::/docker/{container_id}\n")
    (process_dir / "status").write_text("VmRSS:\t2048 kB\n")
    redis_memory = 128 * 1024 * 1024

    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)
    monkeypatch.setattr(metrics, "_cgroup_memory", lambda: (None, None))
    monkeypatch.setattr(
        metrics, "_host_cgroup_values", lambda cgroup_id: (redis_memory, redis_memory))

    assert metrics._classify_orchestration_process("redis-server *:6379") == "redis"
    result = metrics._orchestration_memory()

    assert result["expected_services"] == 4
    assert result["discovered_services"] == 1
    assert result["services"]["redis"] == {
        "memory_bytes": redis_memory,
        "limit_bytes": redis_memory,
        "process_count": 1,
        "source": "cgroup",
    }
    assert result["memory_bytes"] == redis_memory
    # format-service 样本缺失时，聚合上限不可知；Redis 自身上限必须仍可见
    assert result["limit_bytes"] is None
