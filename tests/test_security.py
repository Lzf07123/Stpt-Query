"""跨层安全与过载保护契约。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import re
import redis
from fastapi.testclient import TestClient

from app.main import Settings, create_app


HEADERS = {"Authorization": "Bearer security-token"}
PAYLOAD = {"username": "2023000001", "password": "pw", "option": "成绩"}


def _cfg(**kwargs):
    options = {
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "security-token",
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "upstream-token",
        "llm_api_key": "",
        "rate_limit": 100,
    }
    options.update(kwargs)
    return Settings(**options, _env_file=None)


async def test_global_concurrency_rejects_when_no_slot_is_free():
    class BusyPipeline:
        def __init__(self):
            self.active = 0

        async def run(self, request):
            self.active += 1
            try:
                await asyncio.sleep(0.15)
            finally:
                self.active -= 1
            return {"success": True, "kind": "grades", "output": "ok", "meta": {}}

    pipeline = BusyPipeline()
    app = create_app(_cfg(global_concurrency=1, concurrency_wait_timeout=0))
    app.state.pipeline = pipeline
    await app.state.query_slots.acquire()
    with TestClient(app) as client:
        assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 503
        app.state.query_slots.release()
        assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 200


def test_redis_rate_limiter_degrades_to_local_memory():
    class FakeAsyncRedis:
        async def incr(self, key):
            raise redis.ConnectionError("redis down")

        async def expire(self, key, seconds):
            raise redis.ConnectionError("redis down")

    fake_redis = FakeAsyncRedis()
    app = create_app(_cfg(rate_limit=2))
    app.state.redis = fake_redis
    client = TestClient(app)

    # 上游不可达 → 查询失败 502（信封语义），但限流计数照常累加
    assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 502
    assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 502
    states = {item["key"]: item["status"]
              for item in app.state.dependency_health.snapshot()}
    assert states["redis"] == "degraded"
    assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 429


def test_redis_rate_limiter_sets_ttl_atomically():
    class AtomicRedis:
        def __init__(self):
            self.counts = {}
            self.ttls = {}
            self.eval_count = 0

        async def eval(self, script, numkeys, key, seconds):
            assert "INCR" in script
            assert "EXPIRE" in script
            self.eval_count += 1
            count = self.counts.get(key, 0) + 1
            self.counts[key] = count
            if count == 1 or self.ttls.get(key, -2) < 0:
                self.ttls[key] = seconds
            return count

        async def hincrby(self, key, field, amount):
            return 1

        async def aclose(self):
            return None

    fake_redis = AtomicRedis()
    app = create_app(_cfg(rate_limit=2))
    app.state.redis = fake_redis
    with TestClient(app) as client:
        # 上游不可达 → 502，但每次请求都应经过限流器计数
        assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 502
        assert client.post("/api/v1/run", headers=HEADERS, json=PAYLOAD).status_code == 502
        assert fake_redis.eval_count == 2
        assert list(fake_redis.ttls.values()) == [60]


def test_edge_hides_sensitive_query_strings_and_sets_csp():
    config = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    assert 'location = /jump/go {' in config
    assert 'access_log off;' in config
    assert '"$request_method $uri $server_protocol"' in config
    assert "img-src 'self' data:" in config


def test_query_log_file_uses_persistent_named_volume():
    compose = open("docker-compose.yml", encoding="utf-8").read()
    volume_section = compose.split("\nvolumes:\n", 1)[1]
    assert "app-query-logs:/var/log/edu-query" in compose
    assert "name: edu-query-app_format-query-logs-persistent" in volume_section
    assert "type: tmpfs" not in volume_section


def test_runtime_memory_guardrails_are_configured():
    compose = open("docker-compose.yml", encoding="utf-8").read()
    # 单容器总限额：覆盖 nginx + uvicorn/编排 + LibreOffice 转换余量
    assert "mem_limit: ${APP_MEM_LIMIT:-640m}" in compose
    assert "FRONTEND_MEM_LIMIT" not in compose
    assert compose.count("MALLOC_ARENA_MAX=${MALLOC_ARENA_MAX:-2}") == 1
    nginx = open("frontend/nginx.conf", encoding="utf-8").read()
    # worker_processes 固定值：auto 会按宿主核数 fork，与容器 pids 上限冲突
    assert "worker_processes 2;" in nginx


def test_single_container_topology():
    """单容器拓扑：唯一 service edu-query-app（nginx 8080 唯一宿主映射），
    uvicorn 仅容器内回环 8000，不再有跨容器网络与依赖。"""
    compose = open("docker-compose.yml", encoding="utf-8").read()
    # 恰一个 service，名为 edu-query-app（连字符规范）
    assert compose.count("\n  edu-query-app:") == 1
    assert not any(marker in compose for marker in ("\n  app:", "\n  frontend:"))
    # 唯一宿主映射：nginx 8080 入口；uvicorn 不对外（无 expose）
    assert compose.count("ports:") == 1
    assert '"${APP_PORT:-8000}:8080"' in compose
    assert "expose:" not in compose
    # 无跨容器网络与依赖
    assert "depends_on:" not in compose
    assert "\nnetworks:" not in compose
    # nginx 反代目标为容器内回环 uvicorn
    nginx_template = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    assert "proxy_pass http://127.0.0.1:8000;" in nginx_template
    assert "http://app:8000" not in nginx_template
    # 双进程入口：uvicorn 绑回环 + nginx daemon off + bash wait -n
    entrypoint = open("docker-entrypoint.sh", encoding="utf-8").read()
    assert "uvicorn app.main:app --host 127.0.0.1 --port 8000" in entrypoint
    assert 'nginx -g "daemon off;"' in entrypoint
    assert "wait -n" in entrypoint
    dockerfile = open("Dockerfile", encoding="utf-8").read()
    assert "nginx " in dockerfile and "gettext-base" in dockerfile
    assert "COPY --chown=10001:10001 frontend/static /usr/share/nginx/html" in dockerfile
    assert 'CMD ["bash", "/app/docker-entrypoint.sh"]' in dockerfile


def test_inline_script_hashes_are_covered_by_csp():
    """全站脚本已外置（查询页/后台 CSP 均走 'self'），页面不允许任何内联脚本；
    nginx 模板中残留的哈希只允许属于 /jump/go 桥接页（由 app/jwxt_http.py 维护）。"""
    config = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    declared_hashes = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", config))
    index_html = open("frontend/static/index.html", encoding="utf-8").read()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", index_html, re.S) == []
    admin_html = open("frontend/static/admin.html", encoding="utf-8").read()
    assert re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", admin_html, re.S) == []
    # 主站 server 级 CSP（/jump/go 之前）不得再声明任何内联哈希
    main_csp = config[:config.index("location = /jump/go {")]
    assert "sha256-" not in main_csp


def test_csp_does_not_allow_inline_styles():
    config = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    assert "'unsafe-inline'" not in config
    assert "style-src 'self';" in config


def test_calendar_feed_token_is_redacted_from_access_logs():
    """订阅源路径即长期密钥：uvicorn 访问日志落盘前必须替换成占位符。"""
    from app.trace import install_access_log_redaction, redact_feed_paths

    token = "K" * 43
    line = 'GET /cal/%s.ics HTTP/1.1' % token
    assert token not in redact_feed_paths(line)
    assert "[redacted]" in redact_feed_paths(line)
    # 非订阅路径不受影响
    assert redact_feed_paths("GET /admin/api/calendars HTTP/1.1") == "GET /admin/api/calendars HTTP/1.1"
    assert redact_feed_paths("GET /cal/not-a-feed-path HTTP/1.1") == "GET /cal/not-a-feed-path HTTP/1.1"

    install_access_log_redaction()
    logger = logging.getLogger("uvicorn.access")
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ("10.0.0.9", "GET", "/cal/%s.ics" % token, "1.1", 200), None)
    for item in logger.filters:
        item.filter(record)
    rendered = record.getMessage()
    assert token not in rendered and "/cal/[redacted].ics" in rendered


def test_calendar_api_reuses_orchestration_rate_limit(tmp_path):
    """日历接口能触发本地密码比对与学校登录，必须与 /run 共用限流。"""
    cfg = _cfg(rate_limit=1, calendar_enabled=True,
               calendar_master_key=base64.urlsafe_b64encode(b"c" * 32).decode(),
               calendar_db_path=str(tmp_path / "calendar.db"))
    body = {"username": "2023000001", "password": "pw", "semester": "2026-2027-1"}
    with TestClient(create_app(cfg)) as client:
        codes = [client.post("/api/v1/calendars/status", headers=HEADERS, json=body).status_code
                 for _ in range(4)]
    assert 429 in codes, codes


def test_calendar_compare_accepts_non_ascii_credentials():
    """口令可能含中文/emoji：常量时间比较必须先编码，否则 500。"""
    digest = hashlib.sha256(b"x").hexdigest()
    assert digest
    from app.calendar.crypto import CalendarCrypto
    assert CalendarCrypto.compare("密码123", "密码123") is True
    assert CalendarCrypto.compare("密码123", "密码124") is False
    assert CalendarCrypto.compare("emoji🔐", "emoji🔐") is True


def test_calendar_feed_path_is_redacted_in_log_messages():
    """非 access log 的日志行也不得带订阅源路径。"""
    from app.trace import _redact_sensitive_log_message, redact_feed_paths

    token = "M" * 43
    assert token not in redact_feed_paths("fetching /cal/%s.ics failed" % token)
    rendered = _redact_sensitive_log_message("GET /cal/%s.ics?x=1" % token)
    assert token not in rendered and "[redacted]" in rendered
