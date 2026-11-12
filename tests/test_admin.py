"""管理端契约：独立鉴权、文件历史检索与资源监控。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import Settings, create_app
from app import metrics
from app.querylog import JSONLFileWriter


ADMIN = {"Authorization": "Bearer " + ("b" * 24)}


@pytest.fixture(autouse=True)
def _reset_orchestration_scan_cache():
    """编排进程扫描结果带 30 秒限频缓存；每个用例前后重置，避免跨用例串味。"""
    metrics.reset_orchestration_scan_cache()
    yield
    metrics.reset_orchestration_scan_cache()


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
        assert stack["expected_services"] == 2
        assert stack["services"]["app"]["source"] == "cgroup"
        assert set(stack["services"]) == {"app", "frontend"}
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
    assert metrics._classify_orchestration_process("python main.py") is None  # 旧查询代理入口已移除
    result = metrics._orchestration_memory(expected=("app", "frontend", "redis"))

    assert result["expected_services"] == 3
    assert result["discovered_services"] == 1
    assert result["services"]["redis"] == {
        "memory_bytes": redis_memory,
        "limit_bytes": redis_memory,
        "process_count": 1,
        "source": "cgroup",
    }
    assert result["memory_bytes"] == redis_memory
    # 单体应用样本缺失时，聚合上限不可知；Redis 自身上限必须仍可见
    assert result["limit_bytes"] is None


def test_orchestration_memory_default_excludes_redis(tmp_path, monkeypatch):
    """默认单体拓扑（无 Redis）不应把 redis 计入期望服务。"""
    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)
    result = metrics._orchestration_memory()
    assert result["expected_services"] == 2
    assert set(result["services"]) == {"app", "frontend"}
    assert result["services"]["frontend"]["process_count"] == 0


def test_process_cgroup_id_accepts_docker_scope(tmp_path):
    """systemd cgroup 驱动下容器挂在 docker-<id>.scope，必须能识别出容器 ID。"""
    container_id = "b" * 64
    for line in (
        f"0::/system.slice/docker-{container_id}.scope\n",
        f"11:memory:/system.slice/docker-{container_id}.scope\n",
    ):
        proc = tmp_path / "100"
        proc.mkdir(exist_ok=True)
        (proc / "cgroup").write_text(line)
        assert metrics._process_cgroup_id(proc) == container_id


def test_host_cgroup_values_systemd_scope(tmp_path, monkeypatch):
    """v2 + systemd 驱动：/system.slice/docker-<id>.scope 下读取 memory.current/max。"""
    container_id = "c" * 64
    root = tmp_path / "cgroup"
    scope = root / "system.slice" / f"docker-{container_id}.scope"
    scope.mkdir(parents=True)
    (scope / "memory.current").write_text("33554432\n")
    (scope / "memory.max").write_text("67108864\n")
    monkeypatch.setattr(metrics, "_HOST_CGROUP_ROOT", root)
    assert metrics._host_cgroup_values(container_id) == (33554432, 67108864)


def test_host_cgroup_values_v1(tmp_path, monkeypatch):
    """v1：/memory/docker/<id> 下读 usage/limit，无限大上限归一为 None。"""
    container_id = "d" * 64
    root = tmp_path / "cgroup"
    cg = root / "memory" / "docker" / container_id
    cg.mkdir(parents=True)
    (cg / "memory.usage_in_bytes").write_text("16777216\n")
    (cg / "memory.limit_in_bytes").write_text("9223372036854771712\n")
    monkeypatch.setattr(metrics, "_HOST_CGROUP_ROOT", root)
    assert metrics._host_cgroup_values(container_id) == (16777216, None)


def test_orchestration_memory_frontend_uses_systemd_cgroup(tmp_path, monkeypatch):
    """systemd cgroup 驱动下 frontend 必须走 cgroup 实测，不得静默降级 RSS。"""
    frontend_id = "e" * 64
    app_dir = tmp_path / "100"
    app_dir.mkdir()
    (app_dir / "cmdline").write_bytes(b"uvicorn\x00app.main:app\x00--host\x000.0.0.0\x00")
    (app_dir / "cgroup").write_text(f"0::/docker/{'f' * 64}\n")
    (app_dir / "status").write_text("VmRSS:\t102400 kB\n")
    for pid, cmd in (
        ("101", b"nginx:\x00master process\x00nginx\x00"),
        ("102", b"nginx:\x00worker process\x00"),
    ):
        d = tmp_path / pid
        d.mkdir()
        (d / "cmdline").write_bytes(cmd)
        (d / "cgroup").write_text(f"0::/system.slice/docker-{frontend_id}.scope\n")
        (d / "status").write_text("VmRSS:\t20480 kB\n")

    cgroup_root = tmp_path / "cgroup"
    scope = cgroup_root / "system.slice" / f"docker-{frontend_id}.scope"
    scope.mkdir(parents=True)
    (scope / "memory.current").write_text("33554432\n")
    (scope / "memory.max").write_text("67108864\n")

    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)
    monkeypatch.setattr(metrics, "_HOST_CGROUP_ROOT", cgroup_root)
    monkeypatch.setattr(metrics, "_cgroup_memory", lambda: (None, None))

    result = metrics._orchestration_memory()
    frontend = result["services"]["frontend"]
    assert frontend["source"] == "cgroup"
    assert frontend["memory_bytes"] == 33554432
    assert frontend["limit_bytes"] == 67108864
    assert frontend["process_count"] == 2
    assert result["discovered_services"] == 2
    assert result["source"] == "cgroup"


def test_orchestration_memory_nginx_rss_includes_workers(tmp_path, monkeypatch):
    """cgroup 不可读时，frontend 的 RSS 估算必须计入 nginx worker 而不只是 master。"""
    for pid, cmd, rss_kb in (
        ("100", b"nginx:\x00master process\x00nginx\x00", 2048),
        ("101", b"nginx:\x00worker process\x00", 8192),
        ("102", b"nginx:\x00worker process\x00", 8192),
    ):
        d = tmp_path / pid
        d.mkdir()
        (d / "cmdline").write_bytes(cmd)
        (d / "cgroup").write_text("0::/\n")
        (d / "status").write_text(f"VmRSS:\t{rss_kb} kB\n")

    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)
    monkeypatch.setattr(metrics, "_cgroup_memory", lambda: (None, None))
    monkeypatch.setattr(metrics, "_HOST_CGROUP_ROOT", tmp_path / "no-cgroup")

    frontend = metrics._orchestration_memory()["services"]["frontend"]
    assert frontend["source"] == "process_rss"
    assert frontend["process_count"] == 3
    assert frontend["memory_bytes"] == (2048 + 8192 + 8192) * 1024


def test_orchestration_expected_services_follow_redis_config(tmp_path):
    """资源监控的期望分桶必须跟随部署配置：无 Redis=2，配置外部 Redis=3。"""
    def stack_for(**overrides):
        with TestClient(create_app(_settings(tmp_path, **overrides))) as client:
            payload = client.get("/admin/api/metrics", headers=ADMIN).json()
        assert payload.get("latest"), payload
        return payload["latest"]["orchestration"]["memory"]

    single = stack_for()
    assert single["expected_services"] == 2
    assert set(single["services"]) == {"app", "frontend"}

    with_redis = stack_for(redis_url="redis://127.0.0.1:6399/0")
    assert with_redis["expected_services"] == 3
    assert set(with_redis["services"]) == {"app", "frontend", "redis"}


def test_orchestration_scan_is_throttled_and_resettable(tmp_path, monkeypatch):
    """/proc 全 PID 扫描按 30 秒限频缓存；重置钩子强制下一轮重扫。"""
    process_dir = tmp_path / "100"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"redis-server\x00*:6379\x00")
    (process_dir / "cgroup").write_text("0::/docker/%s\n" % ("a" * 64))
    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)

    first = metrics._scan_orchestration_processes()
    assert first["redis"][0]["pid"] == 100

    # 限频窗口内不再触碰 /proc：让 _proc_root 直接抛错也能拿到缓存结果
    def forbidden():
        raise AssertionError("限频窗口内不应重新扫描 /proc")

    monkeypatch.setattr(metrics, "_proc_root", forbidden)
    assert metrics._scan_orchestration_processes() == first

    metrics.reset_orchestration_scan_cache()
    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)
    (process_dir / "cmdline").write_bytes(b"uvicorn\x00app.main:app\x00")
    third = metrics._scan_orchestration_processes()
    assert set(third) == {"app"}


def test_orchestration_memory_rereads_rss_within_scan_cache(tmp_path, monkeypatch):
    """进程分类限频缓存，但 RSS 内存值必须每次采样实时读取。"""
    process_dir = tmp_path / "100"
    process_dir.mkdir()
    (process_dir / "cmdline").write_bytes(b"nginx:\x00master process\x00nginx\x00")
    (process_dir / "cgroup").write_text("0::/\n")
    (process_dir / "status").write_text("VmRSS:\t1024 kB\n")
    monkeypatch.setattr(metrics, "_proc_root", lambda: tmp_path)
    monkeypatch.setattr(metrics, "_cgroup_memory", lambda: (None, None))
    monkeypatch.setattr(metrics, "_HOST_CGROUP_ROOT", tmp_path / "no-cgroup")

    first = metrics._orchestration_memory()["services"]["frontend"]
    assert first["memory_bytes"] == 1024 * 1024

    (process_dir / "status").write_text("VmRSS:\t4096 kB\n")
    second = metrics._orchestration_memory()["services"]["frontend"]
    assert second["memory_bytes"] == 4096 * 1024
    assert second["process_count"] == 1
