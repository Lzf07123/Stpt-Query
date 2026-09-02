from __future__ import annotations

from app.main import Settings, _parse_trusted_proxy_cidrs, create_app
from app.runtime_metrics import RuntimeMetrics


def _cfg(**kwargs):
    options = {
        "_env_file": None,
        "environment": "development",
        "auto_rotate_token": False,
        "api_token": "metrics-token",
        "service_base_url": "http://127.0.0.1:9",
        "service_api_token": "upstream-token",
        "llm_api_key": "",
    }
    options.update(kwargs)
    return Settings(**options)


def test_runtime_metrics_renders_counts_summaries_and_gauges():
    metrics = RuntimeMetrics()
    metrics.observe_request("/run", 200, 0.25)
    metrics.observe_request("/run", 503, 0.75)
    metrics.observe_llm(False)
    metrics.observe_pdf_cache(True)
    metrics.observe_concurrency_wait("query", 0.4)
    metrics.observe_redis(True)

    text = metrics.render()
    assert 'edu_query_requests_total{route="/run",status="200"} 1' in text
    assert 'edu_query_llm_requests_total{result="failure"} 1' in text
    assert 'edu_query_pdf_cache_total{result="hit"} 1' in text
    assert 'edu_query_concurrency_wait_seconds_sum{slot="query"} 0.400000' in text
    assert 'edu_query_concurrency_wait_seconds_count{slot="query"} 1' in text
    assert 'edu_query_request_duration_seconds_sum{route="/run"} 1.000000' in text
    assert 'edu_query_request_duration_seconds_count{route="/run"} 2' in text
    assert "edu_query_redis_degraded 1" in text


def test_metrics_endpoint_is_registered_without_auth():
    app = create_app(_cfg())
    paths = {route.path for route in app.routes}
    assert "/metrics" in paths
    nginx = open("frontend/templates/default.conf.template", encoding="utf-8").read()
    assert "location = /metrics {" in nginx
    assert "return 404;" in nginx.split("location = /metrics {", 1)[1]


def test_trusted_proxy_cidr_parser_accepts_multiple_networks():
    networks = _parse_trusted_proxy_cidrs("172.16.0.0/12, not-a-cidr ,10.0.0.0/8")
    assert len(networks) == 2
