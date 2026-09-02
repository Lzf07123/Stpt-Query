"""前端内联脚本 CSP 哈希与重试交互回归测试。"""
from __future__ import annotations

import base64
import hashlib
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _inline_scripts(filename: str) -> list[str]:
    html = (ROOT / "frontend" / "static" / filename).read_text(encoding="utf-8")
    return [match.group(1) for match in re.finditer(r"<script>(.*?)</script>", html, re.DOTALL)]


def test_frontend_inline_scripts_are_allowed_by_csp():
    policy = (ROOT / "frontend" / "templates" / "default.conf.template").read_text(
        encoding="utf-8"
    )
    for filename in ("index.html", "admin.html"):
        scripts = _inline_scripts(filename)
        assert scripts, filename
        for script in scripts:
            digest = hashlib.sha256(script.encode("utf-8")).digest()
            expected = "sha256-" + base64.b64encode(digest).decode("ascii")
            assert f"'{expected}'" in policy


def test_retry_buttons_use_delegated_listener_not_inline_handler():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    assert 'class="empty-state-action btn btn-primary js-retry-query"' in index
    assert 'class="btn btn-ghost js-retry-query"' in index
    assert "onclick=" not in index
    assert '.closest(".js-retry-query")' in index


def test_history_scrubs_current_and_legacy_schedule_download_links():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(
        encoding="utf-8"
    )
    assert r"/^\[点击下载课表（(?:PDF|Word) 文件）\]\(/.test(line)" in index
    assert 'line.indexOf("> ⚠️ 此链接含个人令牌，请勿外传。") === 0' in index
    assert "storeResultText(id, stripLoginNote(String(record.resultText || \"\")))" in index
    assert "function sanitizeStoredHistory()" in index
    assert "sanitizeStoredHistory();" in index


def test_admin_limit_selector_lives_in_log_pagination():
    admin = (ROOT / "frontend" / "static" / "admin.html").read_text(encoding="utf-8")
    form_start = admin.index('<form class="card filter-card logs-primary" id="logFilter">')
    form_end = admin.index("</form>", form_start)
    pagination_start = admin.index('<div class="pagination">')
    pagination_end = admin.index('<section class="card log-detail-panel"', pagination_start)

    form = admin[form_start:form_end]
    pagination = admin[pagination_start:pagination_end]
    assert 'name="limit"' not in form
    assert '<div class="pagination-nav">' in pagination
    assert 'class="filter-control pagination-limit"' in pagination
    assert '<select class="custom-select-native" id="limitSelect" name="limit"' in pagination
    assert '<button class="custom-select-trigger" id="limitTrigger"' in pagination

    script = (ROOT / "frontend" / "static" / "admin.js").read_text(encoding="utf-8")
    assert 'limitSelect: $("limitSelect")' in script
    assert "elements.filter.elements.limit" not in script
    assert 'new FormData(elements.filter).get("limit")' not in script


def test_admin_orchestration_distribution_includes_redis():
    script = (ROOT / "frontend" / "static" / "admin.js").read_text(encoding="utf-8")
    page = (ROOT / "frontend" / "static" / "admin.html").read_text(encoding="utf-8")

    assert '"redis": "编排 Redis"' in script
    assert "/admin.js?v=17" in page


def test_homepage_notice_bar_and_history_are_external_scripts():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "static" / "notice.js").read_text(encoding="utf-8")
    style = (ROOT / "frontend" / "src" / "app.css").read_text(encoding="utf-8")
    assert 'id="noticeBar"' in index
    assert 'id="noticeHistoryModal"' in index
    assert 'src="/notice.js?v=5"' in index
    assert 'href="/style.css?v=67"' in index
    assert 'id="noticePause"' not in index
    assert 'id="noticePause"' not in script
    assert "text.scrollWidth > track.clientWidth + 2" in script
    assert "track.clientWidth - firstCharWidth - 1" in script
    assert 'bar.classList.toggle("notice-bar-warning", item.level === "warning")' in script
    assert "min-height: 44px;" in style
    assert "padding: 22px 20px 0;" in style
    assert ".notice-history-panel .modal-footer" in style
    assert 'translateX(var(--notice-start, 100%))' in style
    assert "transform: translateX(-100%);" in style


def test_admin_notice_management_ui_is_wired():
    admin = (ROOT / "frontend" / "static" / "admin.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "static" / "admin.js").read_text(encoding="utf-8")
    assert 'id="tabNotices"' in admin
    assert 'id="noticesPanel"' in admin
    assert 'noticeForm: $("noticeForm")' in script
    assert 'async function loadNotices()' in script
    assert 'request("notices?" + noticeQuery())' in script


def test_public_notice_routes_are_proxied_by_frontend():
    policy = (ROOT / "frontend" / "templates" / "default.conf.template").read_text(
        encoding="utf-8"
    )
    assert "notices(?:/active|/history)" in policy
