"""前端内联脚本 CSP 哈希与重试交互回归测试。"""
from __future__ import annotations

import base64
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

import pytest


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
    assert "/admin.js?v=23" in page


def test_homepage_notice_bar_and_history_are_external_scripts():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    script = (ROOT / "frontend" / "static" / "notice.js").read_text(encoding="utf-8")
    style = (ROOT / "frontend" / "src" / "app.css").read_text(encoding="utf-8")
    assert 'id="noticeBar"' in index
    assert 'id="noticeHistoryModal"' in index
    assert 'src="/notice.js?v=6"' in index
    assert 'href="/style.css?v=82"' in index
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


def test_query_first_use_controls_are_accessible_and_isolated():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")

    assert '<form id="queryForm"' in index and "novalidate" in index
    assert 'class="query-kind-grid" role="group" aria-labelledby="queryKindLabel"' in index
    assert index.count('class="query-choice"') == 2
    assert '<a class="skip-link" href="#main">' in index
    assert '<main class="page-shell" id="main" tabindex="-1">' in index
    assert 'id="username" name="username"' in index
    assert 'id="password" name="password"' in index
    assert 'aria-describedby="usernameHelp usernameError"' in index
    assert 'aria-describedby="passwordHelp passwordError"' in index
    assert 'id="historySection"' in index
    assert 'class="full history-row hidden"' in index
    assert 'id="resultTitle" tabindex="-1"' in index
    assert 'resultTitle.focus({ preventScroll: true })' in index
    assert 'progressSteps.style.setProperty("--step-font-size", "12px")' in index

    # 查询类型与日历刷新间隔不能共用事件选择器，否则切换日历时会误改查询内容。
    assert 'document.querySelectorAll(".query-choice").forEach' in index
    assert 'document.querySelectorAll(".seg-btn").forEach' not in index


def test_result_page_has_overview_and_humanized_diagnostics():
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    style = (ROOT / "frontend" / "src" / "app.css").read_text(encoding="utf-8")

    assert 'id="resultOverview" class="result-overview hidden"' in index
    assert "function renderResultOverview(" in index
    assert "function parseDiagnosticReport(" in index
    assert "function renderDiagnosticReport(" in index
    assert 'table-shell table-shell--grades' in index
    assert 'table-shell table-shell--schedule' in index
    assert 'aria-label="查询结果表格"' in index
    for selector in (".result-overview {", ".diagnostic-report {", ".table-shell--grades"):
        assert selector in style


def _extract_function(source: str, signature: str) -> str:
    start = source.index("function " + signature)
    brace = source.index("{", start)
    depth = 0
    for index in range(brace, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError("函数未闭合: " + signature)


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能执行前端片段")
def test_result_overview_helpers_are_deterministic(tmp_path):
    index = (ROOT / "frontend" / "static" / "index.html").read_text(encoding="utf-8")
    source = "\n".join(
        _extract_function(index, signature)
        for signature in (
            "isTableSeparator(line)",
            "parseResultTable(text)",
            "gradeOverviewFacts(text)",
            "parseDiagnosticReport(text)",
            "humanizeDiagnosticEvidence(evidence)",
        )
    )
    harness = tmp_path / "result-overview.mjs"
    harness.write_text(
        source + "\n"
        "const grade = gradeOverviewFacts('| 课程 | 成绩 | 学分 |\\n|---|---|---|\\n"
        "| 高数 | 90 | 4 |\\n| 英语 | 80 | 3 |');\n"
        "if (grade[0].value !== '2 门' || grade[1].value !== '85.0' || grade[2].value !== '90') process.exit(1);\n"
        "const report = parseDiagnosticReport('**问题分类：未分类异常**\\n- 环节：登录\\n"
        "- 判断依据：HTTP 502，响应片段：教务系统登录暂时不可用\\n"
        "- 用户可自行处理：\\n  - 稍后重试\\n"
        "- 需要管理员/运维处理：\\n  - 查看日志');\n"
        "if (report.userActions[0] !== '稍后重试' || report.adminActions[0] !== '查看日志') process.exit(2);\n"
        "if (humanizeDiagnosticEvidence(report.evidence) !== '学校服务暂时不可用（HTTP 502）') process.exit(3);\n"
        "console.log('ok');\n",
        encoding="utf-8",
    )
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_first_visit_theme_follows_system_preference():
    head_script = _inline_scripts("index.html")[0]
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in head_script
    assert 't = "dark";' not in head_script


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
