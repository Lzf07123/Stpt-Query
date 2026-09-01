import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import jwxt_http
from jwxt_http import Handler
from jwxt_state import KeyedLocks, SessionStore, TTLCache
from rtf_pdf import PdfConversionError


PDF_METRICS = {"pdf_wait_ms": 1, "pdf_convert_ms": 2}


class _Server:
    def __init__(self):
        self.sessions = SessionStore(3600)
        self.schedule_pdf_cache = TTLCache(300, max_items=8)
        self.schedule_pdf_locks = KeyedLocks()


def _handler_with_pdf(monkeypatch, contents=b"%PDF-1.7\n", error=None):
    handler = Handler()
    server = _Server()
    handler.server = server
    calls = []

    def fake_with_session_j(_sessions, session, run):
        assert session in server.sessions.sessions
        upstream = SimpleNamespace(
            export_schedule=lambda *_args, **_kwargs: SimpleNamespace(
                content=b"{\\rtf1 schedule"))
        return run(upstream)

    def fake_rtf_to_pdf_detailed(_data):
        calls.append(_data)
        if error is not None:
            raise error
        return contents, dict(PDF_METRICS)

    monkeypatch.setattr(jwxt_http, "with_session_j", fake_with_session_j)
    monkeypatch.setattr(
        jwxt_http, "rtf_to_pdf_detailed", fake_rtf_to_pdf_detailed)
    return handler, server, calls


def test_schedule_pdf_cache_hits_for_same_owner(monkeypatch):
    handler, server, calls = _handler_with_pdf(monkeypatch)
    sid = server.sessions.create("owner-1", {}, {})

    first_pdf, first_sem, first_metrics = handler._schedule_pdf_cached(
        sid, "2025-2026-1", "1-3", 1)
    second_pdf, second_sem, second_metrics = handler._schedule_pdf_cached(
        sid, "2025-2026-1", "1,2,3", 1)

    assert first_pdf == second_pdf == b"%PDF-1.7\n"
    assert first_sem == second_sem == "2025-2026-1"
    assert len(calls) == 1
    assert first_metrics["pdf_cache_hit"] is False
    assert second_metrics["pdf_cache_hit"] is True


def test_schedule_pdf_cache_is_isolated_by_owner(monkeypatch):
    handler, server, calls = _handler_with_pdf(monkeypatch)
    sid_a = server.sessions.create("owner-a", {}, {})
    sid_b = server.sessions.create("owner-b", {}, {})

    pdf_a, _, _ = handler._schedule_pdf_cached(sid_a, "sem", [1], 1)
    pdf_b, _, _ = handler._schedule_pdf_cached(sid_b, "sem", [1], 1)

    assert pdf_a == pdf_b == b"%PDF-1.7\n"
    assert len(calls) == 2


def test_failed_schedule_pdf_is_not_cached(monkeypatch):
    handler, server, calls = _handler_with_pdf(
        monkeypatch, error=PdfConversionError("转换失败"))
    sid = server.sessions.create("owner-1", {}, {})

    with pytest.raises(PdfConversionError, match="转换失败"):
        handler._schedule_pdf_cached(sid, "sem", [1], 1)

    assert calls
    assert server.schedule_pdf_cache.get(
        jwxt_http._schedule_pdf_cache_key("owner-1", "sem", [1], 1)) is None


def test_concurrent_schedule_pdf_requests_share_one_conversion(monkeypatch):
    handler, server, calls = _handler_with_pdf(monkeypatch)
    sid = server.sessions.create("owner-1", {}, {})
    first_started = threading.Event()

    def slow_detailed(_data):
        calls.append(_data)
        first_started.set()
        threading.Event().wait(1)
        return b"%PDF-1.7\n", dict(PDF_METRICS)

    monkeypatch.setattr(
        jwxt_http, "rtf_to_pdf_detailed", slow_detailed)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            handler._schedule_pdf_cached, sid, "sem", [1], 1)
        assert first_started.wait(1)
        second = pool.submit(
            handler._schedule_pdf_cached, sid, "sem", [1], 1)
        first_result = first.result(2)
        second_result = second.result(2)

    assert first_result[0] == second_result[0] == b"%PDF-1.7\n"
    assert len(calls) == 1


def test_schedule_pdf_prewarm_populates_cache(monkeypatch):
    handler, server, calls = _handler_with_pdf(monkeypatch)
    sid = server.sessions.create("owner-1", {}, {})

    handler._prewarm_schedule_pdf(sid, "sem", [1], 1)
    hit = server.schedule_pdf_cache.get(
        jwxt_http._schedule_pdf_cache_key("owner-1", "sem", [1], 1))
    payload = server.schedule_pdf_cache.get_payload(
        jwxt_http._schedule_pdf_cache_key("owner-1", "sem", [1], 1))

    assert hit == {"semester": "sem"}
    assert payload == b"%PDF-1.7\n"
    assert len(calls) == 1
