"""前端脚本外置与 CSP 契约回归测试。

index.html 与 admin.html 的内联脚本均已外置：主脚本为 static/app.js（defer），
首屏主题预置为 static/theme-init.js（阻塞执行，防深色首绘闪烁，两页共用同一单源）；
全站走 CSP `script-src 'self'`，无需任何内联哈希（/jump/go 桥接页哈希独立，不在此列）。
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _inline_scripts(filename: str) -> list[str]:
    html = _read("frontend/static/" + filename)
    return [match.group(1) for match in re.finditer(r"<script>(.*?)</script>", html, re.DOTALL)]


def _location_body(config: str, head_prefix: str) -> str:
    """按 location 头部前缀取块体（到第一个收括号为止，模板内无嵌套大括号）。"""
    start = config.index(head_prefix)
    open_brace = config.index("{", start)
    return config[open_brace:config.index("}", open_brace)]


def test_index_html_has_no_inline_scripts_and_references_external_assets():
    """查询页与后台均不允许内联脚本：主脚本与主题预置全部外置，CSP 走 'self'。"""
    index = _read("frontend/static/index.html")
    assert _inline_scripts("index.html") == []
    assert '<script src="/app.js?v=' in index
    assert '/theme-init.js?v=' in index
    assert (ROOT / "frontend" / "static" / "app.js").is_file()
    assert (ROOT / "frontend" / "static" / "theme-init.js").is_file()
    # admin.html 主题预置同样外置（与查询页共用 theme-init.js 单源）
    admin = _read("frontend/static/admin.html")
    assert _inline_scripts("admin.html") == []
    assert '/theme-init.js?v=' in admin


def test_frontend_script_src_is_self_without_inline_hashes():
    """主站 CSP 的 script-src 只含 'self'：全站脚本外置后不再声明任何内联哈希。"""
    policy = _read("frontend/templates/default.conf.template")
    main_csp = policy[:policy.index("location = /jump/go {")]
    script_src = re.search(r"script-src ('[^;\"]+)", main_csp).group(1)
    hashes = re.findall(r"'(sha256-[A-Za-z0-9+/=]+)'", script_src)
    assert hashes == []
    assert script_src == "'self'"


def test_theme_init_runs_before_stylesheet_and_follows_system_preference():
    """主题预置必须阻塞执行且位于样式表之前，防止深色用户首绘闪白。"""
    index = _read("frontend/static/index.html")
    theme = _read("frontend/static/theme-init.js")
    theme_pos = index.index('/theme-init.js?v=')
    style_pos = index.index('<link rel="stylesheet"')
    assert theme_pos < style_pos
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in theme
    assert 't = "dark";' not in theme


def test_retry_buttons_use_delegated_listener_not_inline_handler():
    index = _read("frontend/static/index.html")
    app = _read("frontend/static/app.js")
    assert 'class="empty-state-action btn btn-primary js-retry-query"' in app
    assert 'class="btn btn-ghost js-retry-query"' in app
    assert "onclick=" not in index
    assert "onclick=" not in app
    assert '.closest(".js-retry-query")' in app


def test_history_scrubs_current_and_legacy_schedule_download_links():
    app = _read("frontend/static/app.js")
    assert r"/^\[点击下载课表（(?:PDF|Word) 文件）\]\(/.test(line)" in app
    assert 'line.indexOf("> ⚠️ 此链接含个人令牌，请勿外传。") === 0' in app
    assert "storeResultText(id, stripLoginNote(String(record.resultText || \"\")))" in app
    assert "function sanitizeStoredHistory()" in app
    assert "sanitizeStoredHistory();" in app


def test_admin_limit_selector_lives_in_log_pagination():
    admin = _read("frontend/static/admin.html")
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

    script = _read("frontend/static/admin.js")
    assert 'limitSelect: $("limitSelect")' in script
    assert "elements.filter.elements.limit" not in script
    assert 'new FormData(elements.filter).get("limit")' not in script


def test_admin_orchestration_distribution_includes_redis():
    script = _read("frontend/static/admin.js")
    page = _read("frontend/static/admin.html")

    assert '"app": "单体应用"' in script
    assert '"redis": "Redis（可选）"' in script
    assert "/admin.js?v=" in page


def test_admin_console_has_consistent_navigation_and_responsive_tooling():
    page = _read("frontend/static/admin.html")
    script = _read("frontend/static/admin.js")
    style = _read("frontend/src/admin.css")

    assert '<a class="skip-link" href="#main">' in page
    assert '<main class="wrap" id="main" tabindex="-1">' in page
    assert 'id="adminLoginBtn"' in page and 'aria-describedby="adminTokenHelp"' in page
    assert 'id="adminHealth"' in page and 'id="dependencySection"' in page
    assert '<button class="admin-health"' in page
    assert page.count('aria-controls="') >= 5
    assert page.count('class="admin-panel-head"') == 4
    assert page.count('role="region" aria-label="') >= 6
    # 主题预置已外置为 theme-init.js（与查询页共用同一单源），页面不再内联
    assert '/theme-init.js?v=' in page
    assert 'window.matchMedia("(prefers-color-scheme: dark)")' in _read("frontend/static/theme-init.js")

    assert 'elements.adminLoginBtn.disabled = true;' in script
    assert 'initialButton.focus({ preventScroll: true });' in script
    assert "var ADMIN_TABS = {" in script
    assert 'window.history[historyMode + "State"]' in script
    assert 'setActiveTab(pair[1], true, "push");' in script
    assert 'elements.adminHealth.addEventListener("click"' in script
    assert 'if (elements.adminHealthText.textContent !== text)' in script
    assert 'class="table-empty-state"' in script
    assert 'elements.detailValueModal.addEventListener("keydown"' in script

    for selector in (
        ".admin-login-card {",
        ".admin-panel-head {",
        ".admin-nav {",
        ".table-empty-state {",
    ):
        assert selector in style
    assert "grid-template-columns: repeat(6, minmax(0, 1fr));" in style
    assert "#calendarFilter .filter-grid {" in style


def test_homepage_notice_bar_and_history_are_external_scripts():
    index = _read("frontend/static/index.html")
    script = _read("frontend/static/notice.js")
    style = _read("frontend/src/app.css")
    assert 'id="noticeBar"' in index
    assert 'id="noticeHistoryModal"' in index
    assert 'src="/notice.js?v=' in index
    assert 'href="/style.css?v=' in index
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
    index = _read("frontend/static/index.html")
    app = _read("frontend/static/app.js")

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
    assert 'resultTitle.focus({ preventScroll: true })' in app
    assert 'progressSteps.style.setProperty("--step-font-size", "12px")' in app

    # 查询类型与日历刷新间隔不能共用事件选择器，否则切换日历时会误改查询内容。
    assert 'document.querySelectorAll(".query-choice").forEach' in app
    assert 'document.querySelectorAll(".seg-btn").forEach' not in app


def test_result_page_has_overview_and_humanized_diagnostics():
    index = _read("frontend/static/index.html")
    app = _read("frontend/static/app.js")
    style = _read("frontend/src/app.css")

    assert 'id="resultOverview" class="result-overview hidden"' in index
    assert "function renderResultOverview(" in app
    assert "function parseDiagnosticReport(" in app
    assert "function renderDiagnosticReport(" in app
    assert 'table-shell table-shell--grades' in app
    assert 'table-shell table-shell--schedule' in app
    assert 'aria-label="查询结果表格"' in app
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
    app = _read("frontend/static/app.js")
    source = "\n".join(
        _extract_function(app, signature)
        for signature in (
            "isTableSeparator(line)",
            "parseResultTable(text)",
            "numericScoreValue(raw)",
            "gradeStatsFromTable(table)",
            "gradeOverviewFacts(text)",
            "parseDiagnosticReport(text)",
            "humanizeDiagnosticEvidence(evidence)",
        )
    )
    harness = tmp_path / "result-overview.mjs"
    harness.write_text(
        source + "\n"
        "// 有学分列：加权平均（90*4+80*3)/7=85.7 + 修读学分；无 GPA 列不出 GPA 项\n"
        "const grade = gradeOverviewFacts('| 课程 | 成绩 | 学分 |\\n|---|---|---|\\n"
        "| 高数 | 90 | 4 |\\n| 英语 | 80 | 3 |');\n"
        "if (grade[0].label !== '已发布' || grade[0].value !== '2 门') process.exit(1);\n"
        "if (grade[1].label !== '加权平均' || grade[1].value !== '85.7') process.exit(4);\n"
        "if (grade[2].label !== '最高分' || grade[2].value !== '90') process.exit(5);\n"
        "if (grade[3].label !== '修读学分' || grade[3].value !== '7') process.exit(6);\n"
        "// 无学分列：诚实降级回算术平均（(90+80)/2=85.0）\n"
        "const plain = gradeOverviewFacts('| 课程 | 成绩 |\\n|---|---|\\n| 高数 | 90 |\\n| 英语 | 80 |');\n"
        "if (plain[1].label !== '平均分' || plain[1].value !== '85.0') process.exit(7);\n"
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


def test_admin_notice_management_ui_is_wired():
    admin = _read("frontend/static/admin.html")
    script = _read("frontend/static/admin.js")
    style = _read("frontend/src/admin.css")
    assert 'id="tabNotices"' in admin
    assert 'id="noticesPanel"' in admin
    assert 'id="noticeForm" novalidate' in admin
    assert 'noticeForm: $("noticeForm")' in script
    assert 'async function loadNotices()' in script
    assert 'request("notices?" + noticeQuery())' in script
    assert 'id="noticePreviewBar"' in admin and 'id="noticeCounter"' in admin
    assert 'aria-describedby="noticeContentHelp noticeCounter"' in admin
    assert admin.count('data-notice-template="') == 3
    assert "function updateNoticeComposer()" in script
    assert "function applyNoticeTemplate(name)" in script
    assert 'noticeActionButton("复制草稿", "duplicate", item.id)' in script
    assert "elements.noticePublish.setAttribute(\"aria-busy\", \"true\")" in script
    for selector in (
        ".notice-compose-grid {",
        ".notice-preview {",
        ".notice-preview-bar {",
        ".notice-template-btn {",
    ):
        assert selector in style


def test_design_v16_back_to_top_and_skeleton_contract():
    index = _read("frontend/static/index.html")
    admin = _read("frontend/static/admin.html")
    admin_script = _read("frontend/static/admin.js")
    style = _read("frontend/src/index.css")
    app_style = _read("frontend/src/app.css")

    assert 'class="back-to-top" href="#main" aria-label="返回顶部"' in index
    assert 'class="back-to-top" id="backToTop" href="#main" aria-label="返回顶部"' in admin
    assert 'href="/style.css?v=' in index
    assert 'href="/style-admin.css?v=' in admin
    assert 'admin.js?v=' in admin
    assert 'backToTop: $("backToTop")' in admin_script
    assert 'elements.backToTop.addEventListener("click"' in admin_script
    assert 'window.scrollTo({ top: 0, behavior: reduced ? "auto" : "smooth" });' in admin_script

    for expected in (
        ".back-to-top {",
        "width: 44px;",
        "height: 44px;",
        "z-index: 55;",
        "blur(var(--eduquery-glass-blur)) saturate(var(--eduquery-glass-saturate))",
        ".site-footer > .back-to-top {",
        "from { background-position: 120% 0; }",
        "to { background-position: -20% 0; }",
    ):
        assert expected in style

    # 骨架屏消费方在 app.css（#skeleton .skeleton-line/.skeleton-row）；
    # index.css 仅保留 @keyframes skeleton-shimmer 定义（模板遗留 .search-skeleton* 已删）
    assert "background-size: 220% 100%;" in app_style
    assert "animation: skeleton-shimmer 1.35s var(--eduquery-ease-out) infinite;" in app_style
    assert "@keyframes skeleton-shimmer" in style
    assert ".search-skeleton" not in style


def test_admin_mobile_header_and_dashboard_keep_first_use_context():
    page = _read("frontend/static/admin.html")
    style = _read("frontend/src/admin.css")

    nav_end = page.index("</nav>")
    actions_start = page.index('<div class="admin-head-actions">')
    assert page.index('id="adminHealth"') < nav_end < actions_start
    assert '<span id="adminHealthText" aria-live="polite">检测中</span>' in page
    assert '<span class="admin-title-wide">教务查询控制台</span>' in page
    assert '<span class="admin-title-compact" aria-hidden="true">控制台</span>' in page

    for expected in (
        ".admin-app .card {",
        "inset 0 1px 0 var(--eduquery-glass-highlight)",
        "@media (min-width: 1024px) {",
        "position: sticky;",
        "@media (max-width: 1023px) {",
        "grid-template-columns: repeat(6, minmax(0, 1fr));",
        "@media (max-width: 480px) {",
        "@media (max-width: 360px) {",
        "@media (min-width: 320px) and (max-width: 619px) {",
        "@media (max-width: 359px) {",
        ".notice-compose-grid {\n  align-items: start;",
        '.dark .admin-health[data-state="danger"] {',
        "background: var(--eduquery-destructive-soft-solid);",
        ".dark .admin-app .admin-nav-btn:not(.active),",
    ):
        assert expected in style


def test_public_notice_routes_are_proxied_by_frontend():
    policy = _read("frontend/templates/default.conf.template")
    # /api/v1/* 统一前缀 location 覆盖通知轮询等服务端点（注入网关令牌）
    assert "location ^~ /api/v1/ {" in policy
    assert 'proxy_set_header Authorization "Bearer ${API_TOKEN}"' in policy


def test_edge_rate_limit_configuration():
    """边缘限流挡板：业务入口限流，公开只读路径不限流（日历客户端/健康检查高频轮询）。"""
    zone = _read("frontend/nginx.conf")
    assert "limit_req_zone $remote_addr zone=perip:10m rate=10r/s;" in zone

    config = _read("frontend/templates/default.conf.template")

    # 业务入口必须挂边缘限流挡板（宽松值；业务限流由 app 层 RATE_LIMIT 负责）
    for head in (
        "location = /jump/go",
        "location = /get_schedule/export",
        "location ^~ /api/v1/calendars",
    ):
        assert "limit_req zone=perip burst=20 nodelay;" in _location_body(config, head), head

    # 查询/异步任务入口（/api/v1/run 前缀 location）必须挂边缘限流挡板
    run_head_start = config.index("location ^~ /api/v1/run {")
    run_body = _location_body(config, "location ^~ /api/v1/run {")
    assert "limit_req zone=perip burst=20 nodelay;" in run_body

    # 公开只读与静态路径不得限流
    for head in ("location ^~ /cal/", "location = / {", "location / {"):
        assert "limit_req" not in _location_body(config, head), head

    # 其余 /api/v1/* 与健康探针的 location 必须存在且不限流
    assert "limit_req" not in _location_body(config, "location ^~ /api/v1/ {")
    health_regex = config[config.index("location ~ ^/(health(/.*)?|jump(/.*)?)$"):]
    health_regex = health_regex[:health_regex.index("}")]
    assert "limit_req" not in health_regex


def test_edge_rejects_raw_dot_segments():
    """边缘必须拒绝原始点段请求：/api/v1/../../x 不得归一化后静默落入静态回退。

    nginx 会把 .. 段归一化掉，使请求脱离 /api/v1/ 前缀并命中 location / 的
    SPA 回退（try_files … /index.html）返回 200 HTML。server 级 rewrite 阶段
    的 $request_uri 门禁先于 preaccess（限流）执行：原始点段一律 400，
    既修正语义也让攻击请求不消耗限流状态。正则只命中整段为 "." / ".."
    的路径段，不误伤 icon.svg、{token}.ics 等含点文件名。
    """
    config = _read("frontend/templates/default.conf.template")
    guard = re.search(r"if \(\$request_uri ~ [^{]+\{\s*return 400;", config)
    assert guard, "server 级必须保留原始点段 400 门禁（防归一化绕过）"
    # 门禁必须挂在 server 块（rewrite 阶段），不得被挪进任何 location 内
    assert config.index("if ($request_uri") < config.index("location = /brand-env.js")


def test_direct_html_paths_disable_caching():
    """直连 /index.html 与 /admin.html 必须禁缓存，且不得自建 add_header（保住 server 级 CSP）。"""
    config = _read("frontend/templates/default.conf.template")
    for head in ("location = /index.html", "location = /admin.html"):
        body = _location_body(config, head)
        assert "expires epoch;" in body, head
        assert "add_header" not in body, head
    assert "expires 1h;" in config  # 静态兜底目录保持 1h 缓存（HTML 精确路径已单独禁缓存）


def test_jump_bridge_hash_matches_rendered_page():
    """/jump/go 桥接页有独立 CSP：其内联脚本哈希必须与渲染结果一致。

    防止批量替换内联脚本哈希时误伤桥接页（历史上发生过一次）。
    """
    from app.jwxt_http import Handler

    html = Handler._jump_bridge(None, "code-for-hash-check")
    script = re.search(r"<script>(.*?)</script>", html, re.S).group(1)
    digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
    config = _read("frontend/templates/default.conf.template")
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
    config = _read("frontend/templates/default.conf.template")
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


def test_rendered_markup_has_no_inline_style_attributes():
    """CSP style-src 'self'（无 unsafe-inline）会拦截内联 style 属性。

    历史：行级联 --row-i 曾以内联 style 属性发射，静态预览无 CSP 头测不出来，
    在 nginx 后被静默拦截（行级联退化为同时入场）。现为 data-row-i 属性 +
    CSSOM setProperty（CSSOM 不受 CSP 限制）；本测试防止内联 style 属性回潮。
    """
    for name in ("app.js", "admin.js", "notice.js", "brand.js"):
        source = _read("frontend/static/" + name)
        assert 'style="' not in source, name + " 发射了内联 style 属性"
    app_js = _read("frontend/static/app.js")
    assert 'data-row-i="' in app_js
    assert 'setProperty("--row-i"' in app_js


def test_pwa_assets_are_external_and_uncached():
    """PWA：sw.js 必须外置且页面仅引用不内联；nginx 对 sw.js/manifest 禁缓存
    且不得自建 add_header（保住 server 级 CSP 继承）。"""
    index_html = _read("frontend/static/index.html")
    assert '<link rel="manifest" href="/manifest.json">' in index_html
    assert 'apple-touch-icon' in index_html
    assert "serviceWorker" in _read("frontend/static/app.js")  # 注册逻辑外置在 app.js

    config = _read("frontend/templates/default.conf.template")
    for head in ("location = /sw.js", "location = /manifest.json"):
        body = _location_body(config, head)
        assert "expires -1;" in body, head
        assert "add_header" not in body, head
    # manifest 图标必须真实存在于镜像产物中
    for icon in ("icon-192.png", "icon-512.png", "icon-maskable-192.png",
                 "icon-maskable-512.png", "apple-touch-icon.png"):
        assert (ROOT / "frontend/static/image" / icon).exists(), icon
    manifest = json.loads(_read("frontend/static/manifest.json"))
    assert manifest["start_url"] == "/?source=pwa"
    assert {icon["purpose"] for icon in manifest["icons"]} >= {"any", "maskable"}


def test_jump_and_calendar_url_exits_are_scheme_gated():
    """跳转链接与订阅地址的四个前端出口必须有协议门禁（安全加固 R2）。

    renew 锚点（免密 CTA）与日历 feedUrl（webcal 唤起/外链托管/ics 下载/
    复制文本共同来源）都会把后端值直接交给导航类 API：仅接受 http(s)/webcal
    与服务端签发的 /cal/ 相对路径，javascript:/data: 一律降级。
    """
    app_js = _read("frontend/static/app.js")
    assert "function calendarSafeFeedUrl(" in app_js
    assert app_js.count("calendarSafeFeedUrl(data.feed_url)") == 3, "feedUrl 三个赋值点须全部过门禁"
    # renew 锚点：非 http(s) 一律按「签发异常」降级，不得直接赋 href
    assert "/^https?:\\/\\//i.test(String(data.login_url" in app_js
    assert "anchor.href = data.login_url;" not in app_js
