"""轻量 Prometheus 指标：进程内存计数，避免引入低基数标签风险。"""
from __future__ import annotations

import time
from threading import Lock
from typing import Mapping


def _escape_label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(values: Mapping[str, str]) -> str:
    if not values:
        return ""
    joined = ",".join(
        f'{key}="{_escape_label(str(value))}"' for key, value in values.items()
    )
    return "{" + joined + "}"


class RuntimeMetrics:
    """进程内计数器/计量器；写入加锁，渲染由 Prometheus 定期抓取。"""

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._sums: dict[str, dict[tuple[tuple[str, str], ...], float]] = {}
        self._gauges: dict[str, float] = {}

    def inc(self, name: str, labels: Mapping[str, str] | None = None,
            value: float = 1.0) -> None:
        key = tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))
        with self._lock:
            family = self._counters.setdefault(name, {})
            family[key] = family.get(key, 0.0) + max(0.0, float(value))

    def set_gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = float(value)

    def observe_duration(self, name: str, seconds: float,
                         labels: Mapping[str, str] | None = None) -> None:
        key = tuple(sorted((str(k), str(v)) for k, v in (labels or {}).items()))
        value = max(0.0, float(seconds))
        with self._lock:
            family = self._sums.setdefault(name, {})
            family[key] = family.get(key, 0.0) + value

    def observe_concurrency_wait(self, slot: str, seconds: float) -> None:
        self.inc("edu_query_concurrency_wait_total", {"slot": slot})
        self.observe_duration(
            "edu_query_concurrency_wait_seconds", seconds, {"slot": slot})

    def observe_request(self, route: str, status_code: int,
                        seconds: float) -> None:
        labels = {"route": route or "unmatched", "status": str(status_code)}
        self.inc("edu_query_requests_total", labels)
        self.observe_duration(
            "edu_query_request_duration_seconds", seconds, {"route": route or "unmatched"})

    def observe_llm(self, success: bool) -> None:
        self.inc("edu_query_llm_requests_total", {
            "result": "success" if success else "failure"})

    def observe_pdf_cache(self, hit: bool) -> None:
        self.inc("edu_query_pdf_cache_total", {
            "result": "hit" if hit else "miss"})

    def observe_redis(self, degraded: bool) -> None:
        self.set_gauge("edu_query_redis_degraded", 1.0 if degraded else 0.0)

    @staticmethod
    def _counter_meta(name: str) -> tuple[str, str]:
        descriptions = {
            "edu_query_requests_total": "HTTP requests processed by format-service",
            "edu_query_llm_requests_total": "LLM analysis calls by result",
            "edu_query_pdf_cache_total": "Schedule PDF cache results seen by format-service",
            "edu_query_concurrency_wait_seconds": "Time spent waiting for concurrency slots",
            "edu_query_request_duration_seconds": "HTTP request duration in seconds",
        }
        types = {
            "edu_query_concurrency_wait_seconds": "summary",
            "edu_query_request_duration_seconds": "summary",
        }
        return types.get(name, "counter"), descriptions.get(name, name)

    def render(self) -> str:
        """渲染 Prometheus 文本格式；标签排序保证输出稳定。"""
        lines: list[str] = []
        with self._lock:
            counters = {name: dict(values) for name, values in self._counters.items()}
            sums = {name: dict(values) for name, values in self._sums.items()}
            gauges = dict(self._gauges)

        for name in sorted(counters):
            metric_type, help_text = self._counter_meta(name)
            lines.extend((f"# HELP {name} {help_text}",
                          f"# TYPE {name} {metric_type}"))
            for labels, value in sorted(counters[name].items()):
                lines.append(f"{name}{_labels(dict(labels))} {value:g}")

        for name in sorted(sums):
            metric_type, help_text = self._counter_meta(name)
            lines.extend((f"# HELP {name} {help_text}",
                          f"# TYPE {name} {metric_type}"))
            for labels, total in sorted(sums[name].items()):
                rendered = _labels(dict(labels))
                count = self._summary_count(name, labels, counters)
                lines.append(f"{name}_sum{rendered} {total:.6f}")
                lines.append(f"{name}_count{rendered} {count:g}")

        for name, value in sorted(gauges.items()):
            lines.append(f"# HELP {name} {name}")
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name} {value:g}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _summary_count(name: str, labels: tuple[tuple[str, str], ...],
                       counters: dict[str, dict[tuple[tuple[str, str], ...], float]],
                       ) -> float:
        if name == "edu_query_concurrency_wait_seconds":
            return counters.get("edu_query_concurrency_wait_total", {}).get(
                labels, 0.0)
        if name == "edu_query_request_duration_seconds":
            route = dict(labels).get("route")
            return sum(
                value for key, value in counters.get(
                    "edu_query_requests_total", {}).items()
                if dict(key).get("route") == route
            )
        return 0.0
