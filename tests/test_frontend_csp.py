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

    assert '"app": "单体应用"' in script
    assert '"redis": "Redis（可选）"' in script
    assert "/admin.js?v=21" in page


def test_homepage_notice_bar_and_history_are_external_scripts():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "static" / "notice.js").read_text(encoding="utf-8")
    style = (ROOT / "frontend" / "src" / "app.css").read_text(encoding="utf-8")
    assert 'id="noticeBar"' in index
    assert 'id="noticeHistoryModal"' in index
    assert 'src="/notice.js?v=5"' in index
    assert 'href="/style.css?v=76"' in index
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


def test_jump_bridge_hash_matches_rendered_page():
    """/jump/go 桥接页有独立 CSP：其内联脚本哈希必须与渲染结果一致。

    防止批量替换内联脚本哈希时误伤桥接页（历史上发生过一次）。
    """
    from app.jwxt_http import Handler

    html = Handler._jump_bridge(None, "code-for-hash-check")
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    config = (ROOT / "frontend" / "templates" / "default.conf.template").read_text(encoding="utf-8")
    bridge_block = config[config.index("location = /jump/go {"):]
    bridge_block = bridge_block[:bridge_block.index("}")]
    declared = set(re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", bridge_block))
    assert declared == {"sha256-" + digest}

    # 桥接页哈希不得出现在主站 CSP（两处用途不同）
    main_csp = config[:config.index("location = /jump/go {")]
    assert "sha256-" + digest not in main_csp


def test_locations_with_custom_headers_keep_security_headers():
    """nginx 的 add_header 不继承：自建响应头的 location 必须显式重复安全头。

    否则该 location 会静默丢失 CSP / X-Content-Type-Options / HSTS 等
    （历史上 /admin 即因此没有 CSP）。
    """
    config = (ROOT / "frontend" / "templates" / "default.conf.template").read_text(encoding="utf-8")
    # 锚定行首，避免误匹配 Permissions-Policy 里的 "geolocation=()" 等字面量
    blocks = re.findall(r"(?m)^\s*location[^\n{]*\{[^}]*\}", config)
    assert len(blocks) >= 5, "未解析到预期的 location 块"

    directive = re.compile(r"(?m)^\s*add_header\s")
    for block in blocks:
        head = block.split("{", 1)[0].strip()
        if not directive.search(block) or "/jump/go" in head:
            continue
        assert directive.search(block[:block.index("{")] ) is None  # 仅按块内指令判定
        assert "add_header X-Content-Type-Options" in block, "该 location 丢失安全头：%s" % head
        assert "add_header Strict-Transport-Security" in block, "该 location 丢失 HSTS：%s" % head

    admin_block = next(b for b in blocks if b.split("{", 1)[0].strip() == "location = /admin")
    # 必须保持继承整站安全头（含 CSP）：块内不得出现真正的 add_header 指令
    assert not re.search(r"(?m)^\s*add_header\s", admin_block)
