"""日历订阅前端入口守卫：页脚入口、结果区区块、后台标签页与接口路径。"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_footer_entry_and_result_block_exist():
    page = _read("frontend/static/index.html")
    assert 'id="calendarEntry"' in page                 # 页脚常驻入口
    assert 'id="calendarModal"' in page                 # 订阅面板
    assert 'id="calendarBlock"' in page                 # 结果区区块
    assert 'id="calendarUrl"' in page and 'id="calendarCopy"' in page
    assert 'id="calendarEnable"' in page and 'id="calendarStop"' in page
    assert 'id="calendarCheck"' in page                 # 查询该学号是否开启
    assert 'id="calendarUseForm"' in page               # 复用查询表单的学号密码


def test_page_reuses_same_credentials_and_api_paths():
    page = _read("frontend/static/index.html")
    assert 'usernameInput.value' in page and 'passwordInput.value' in page
    for path in ("/api/v1/calendars/config", "/api/v1/calendars/status",
                 "/api/v1/calendars/refresh", "/api/v1/calendars/rotate",
                 '/api/v1/calendars"'):
        assert path in page, path
    assert "calendarShowResultBlock" in page            # 查询成功后自动显示状态
    assert "meta.calendar" not in page or "body.meta" in page


def test_page_never_persists_subscription_url():
    page = _read("frontend/static/index.html")
    # 订阅地址不得写入 localStorage / sessionStorage / URL 查询串
    for line in page.splitlines():
        if "calendarState.feedUrl" in line or "feed_url" in line:
            assert "localStorage" not in line and "sessionStorage" not in line
    assert "updateUrlParams" in page
    assert "feedUrl" not in page.split("function updateUrlParams", 1)[1].split("function", 1)[0]


def test_admin_calendar_tab_and_actions():
    page = _read("frontend/static/admin.html")
    script = _read("frontend/static/admin.js")
    assert 'id="tabCalendars"' in page and 'id="calendarsPanel"' in page
    assert 'id="calendarsBody"' in page and 'id="calendarQuery"' in page
    assert 'admin.js?v=22' in page
    assert 'request("calendars' in script
    for action in ("/refresh", "/pause", "/resume", "/rotate"):
        assert action in script, action
    assert 'method: "DELETE"' in script
    assert "feed_url_masked" in script                  # 后台只展示掩码地址


def test_admin_error_detail_is_normalized():
    """后台 request() 必须把 FastAPI 的对象数组 detail 转成可读文本（避免 [object Object]）。"""
    script = _read("frontend/static/admin.js")
    assert "function errorDetail(" in script
    assert "Array.isArray(detail)" in script
    assert "throw new Error(errorDetail(" in script
    assert "String(payload.detail)" not in script


def test_calendar_panel_reuses_semester_dropdown():
    """面板学期必须复用首页同一套下拉组件与选项，而不是自由文本输入。"""
    page = _read("frontend/static/index.html")
    assert 'id="calendarSemesterTrigger"' in page and 'id="calendarSemesterMenu"' in page
    assert 'id="calendarSemester" value="" type="hidden"' in page or 'type="hidden" id="calendarSemester"' in page
    assert "calendarSemesterTrigger" in page
    assert "calendarSemesterMenu" in page
    assert 'getElementById("semestersMenu")' in page      # 选项从首页菜单克隆
    assert 'class="calendar-section-title"' in page       # 分区布局


def test_calendar_panel_is_responsive():
    """弹窗必须是小屏可用的三段式：头部/底部固定、内容区滚动、按钮不溢出。"""
    css = _read("frontend/src/app.css")
    start = css.index(".calendar-panel {")
    panel = css[start:css.index("}", start)]
    assert "flex-direction: column" in panel          # 头部 + 内容 + 底部三段式
    assert "max-height: min(90dvh" in panel           # 高度受视口约束
    assert "overflow: hidden" in panel

    body_start = css.index(".calendar-body {")
    body = css[body_start:css.index("}", body_start)]
    assert "overflow-y: auto" in body                 # 内容区自身滚动
    assert "min-height: 0" in body                    # flex 子项允许收缩

    assert "@media (max-width: 560px)" in css
    small = css[css.index("@media (max-width: 560px)"):]
    small = small[:small.index("@media (max-width: 400px)")]
    assert ".calendar-actions .btn," in small         # 小屏按钮整行
    assert ".calendar-url-row .input" in small        # 地址行换行
    assert ".calendar-panel .modal-footer .btn" in small   # 底部操作按钮铺满


def test_subscription_guide_detects_platform_and_offers_one_tap_import():
    """订阅指引必须识别设备/内置浏览器并给出按平台的一键导入入口。"""
    page = _read("frontend/static/index.html")
    for anchor in ('id="calendarGuideSection"', 'id="calendarGuideDevice"', 'id="calendarGuideWebview"',
                   'id="calendarGuidePrimary"', 'id="calendarGuideSteps"', 'id="calendarGuideMore"',
                   'id="calendarGuideTip"', 'id="calendarGuidePrivacy"',
                   'id="calendarQrButton"', 'id="calendarQrBox"', 'id="calendarQrCanvas"'):
        assert anchor in page, anchor
    assert "function calendarDetectGuide()" in page
    assert "function calendarGuideActions(" in page
    # 各平台的一键导入目标：系统日历（webcal）、Google、Outlook，以及系统分享
    assert 'feedUrl.replace(/^https?:\\/\\//i, "webcal://")' in page
    assert "calendar.google.com/calendar/render?cid=" in page
    assert "outlook.live.com/calendar/0/addfromweb" in page
    assert "navigator.canShare" in page
    # 唤起失败必须回退提示，而不是假装检测成功
    assert "visibilitychange" in page
    assert "没有检测到系统打开" in page
    # 内置浏览器（微信/QQ 等）必须给出「在浏览器中打开」与扫码提示
    assert "内置浏览器" in page and "在浏览器中打开" in page


def test_subscription_guide_keeps_secret_local_and_warns_third_parties():
    page = _read("frontend/static/index.html")
    # 第三方日历服务会抓取长期密钥：必须在文案中标注
    assert "第三方抓取" in page
    assert "订阅地址即长期密钥" in page
    # 二维码：本地生成（POST 到自家 API），显式点击才请求，关闭面板即清除
    assert '"/api/v1/calendars/qr"' in page
    assert "async function calendarQrShow()" in page
    close_body = page.split("function calendarClosePanel()", 1)[1].split("}", 1)[0]
    assert "calendarQrReset" in close_body
    render_body = page.split("function calendarGuideRender()", 1)[1].split("function calendarOpen", 1)[0]
    assert "fetch(" not in render_body            # 渲染指引不得自动发起网络请求
    share_body = page.split("async function calendarShare()", 1)[1].split("function calendarQrReset", 1)[0]
    assert "fetch(calendarState.feedUrl" in share_body
    # 订阅地址不得进入本地存储
    for line in page.splitlines():
        if "calendarState.feedUrl" in line or "feed_url" in line:
            assert "localStorage" not in line and "sessionStorage" not in line


def test_subscription_guide_styles_exist():
    css = _read("frontend/src/app.css")
    for selector in (".calendar-guide-warning {", ".calendar-guide-steps {", ".calendar-guide-more {",
                     ".calendar-guide-qr-box {"):
        assert selector in css, selector
    assert "image-rendering: pixelated" in css
    # 使用语义令牌，不引入新的硬编码颜色
    guide = css[css.index(".calendar-guide-warning {"):css.index(".calendar-guide-qr-box {")]
    assert "#" not in guide


def test_subscription_guide_resets_mask_and_confirms_third_party():
    page = _read("frontend/static/index.html")
    open_body = page.split("function calendarOpen()", 1)[1].split("function", 1)[0]
    assert "calendarUrlVisible = false" in open_body          # 打开面板先掩码
    rotate_body = page.split("async function calendarRotate()", 1)[1].split("async function", 1)[0]
    assert "calendarUrlVisible = false" in rotate_body        # 轮换后仍先掩码
    button_body = page.split("function calendarGuideButton(", 1)[1].split("function calendarGuideOpen", 1)[0]
    open_body = page.split("function calendarGuideOpen(action)", 1)[1].split("function calendarGuideDownload", 1)[0]
    assert "window.confirm" in open_body                      # 第三方日历服务二次确认
    assert "link.href" not in button_body                     # 渲染期不把长期密钥写入 DOM
    assert "window.location.href = action.href" in open_body  # 点击后才导航
    assert 'window.open(action.href' in open_body


def _extract_function(page: str, signature: str) -> str:
    """从内联脚本里按大括号配平抽出整个函数（用于在 node 中执行同一份实现）。"""
    start = page.index("function " + signature)
    brace = page.index("{", start)
    depth = 0
    for index in range(brace, len(page)):
        char = page[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return page[start:index + 1]
    raise AssertionError("函数未闭合: " + signature)


def test_subscription_guide_warns_when_feed_address_is_not_reachable():
    page = _read("frontend/static/index.html")
    assert 'id="calendarGuideReach"' in page
    assert "function calendarGuideReachability(" in page
    assert '"loopback"' in page and '"private"' in page
    assert "PUBLIC_BASE_URL" in page and "同一 Wi-Fi" in page
    render = page.split("function calendarGuideRender()", 1)[1].split("function calendarOpen", 1)[0]
    assert "calendarGuideReachability" in render and "guideReach" in render
    qr = page.split("async function calendarQrShow()", 1)[1].split("function calendarGuideButton", 1)[0]
    assert "calendarGuideReachability" in qr          # 扫码文案复用同一结论


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能执行前端片段")
def test_reachability_classification(tmp_path):
    """loopback / 内网 / 公网三类必须判定正确（在 node 中直接跑页面里的同一份实现）。"""
    page = _read("frontend/static/index.html")
    source = _extract_function(page, "calendarGuideReachability(feedUrl)")
    cases = [
        ("http://127.0.0.1:8000/cal/x.ics", "loopback"),
        ("http://localhost:8000/cal/x.ics", "loopback"),
        ("http://[::1]:8000/cal/x.ics", "loopback"),
        ("http://10.19.212.113:8000/cal/x.ics", "private"),
        ("http://192.168.1.20:8000/cal/x.ics", "private"),
        ("http://172.16.0.9:8000/cal/x.ics", "private"),
        ("http://172.32.0.9:8000/cal/x.ics", "public"),
        ("http://169.254.3.4:8000/cal/x.ics", "private"),
        ("http://mac.local:8000/cal/x.ics", "private"),
        ("https://edu.example.edu.cn/cal/x.ics", "public"),
    ]
    harness = tmp_path / "reachability.mjs"
    # 注意：必须用 JSON 数组，Python tuple 的括号在 JS 里是逗号表达式
    harness.write_text(
        source + "\nconst cases = " + json.dumps(cases) + ";\n"
        "for (const [url, want] of cases) {\n"
        "  const got = calendarGuideReachability(url).kind;\n"
        "  if (got !== want) { console.error(url + ' => ' + got + ', want ' + want); process.exit(1); }\n"
        "}\nconsole.log('ok');\n",
        encoding="utf-8")
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_guide_prefers_copy_over_webcal_deeplink():
    """webcal 深链在部分系统会被当成一次性导入或直接报错：主操作必须是复制地址。"""
    page = _read("frontend/static/index.html")
    assert page.count("primary: [copy]") == 7          # 7 个平台（含 unknown）同一策略
    assert "attempt: true" in page                      # webcal 明确标记为可选尝试
    assert "导入错误" in page and "新建日历订阅" in page   # 失败回退路径写在界面里


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 node 才能执行前端片段")
def test_every_platform_plan_uses_copy_as_primary(tmp_path):
    """在 node 中跑页面同一份 calendarGuideActions，逐平台校验主操作与 webcal 标记。"""
    page = _read("frontend/static/index.html")
    source = _extract_function(page, "calendarGuideActions(env, feedUrl)")
    harness = tmp_path / "guide.mjs"
    harness.write_text(
        source + "\nconst platforms = " +
        json.dumps(["ios", "ipados", "macos", "android", "windows", "linux", "unknown"]) + ";\n"
        "for (const platform of platforms) {\n"
        "  const plan = calendarGuideActions({ platform: platform, webview: '' },\n"
        "    'http://127.0.0.1:8000/cal/x.ics');\n"
        "  if (plan.primary.length !== 1 || plan.primary[0].kind !== 'copy') {\n"
        "    console.error(platform, '主操作不是复制订阅地址'); process.exit(1); }\n"
        "  if (!plan.steps.length || !plan.tip) { console.error(platform, '缺少步骤/提示'); process.exit(1); }\n"
        "  for (const action of plan.more.filter((item) => item.scheme === 'webcal')) {\n"
        "    if (action.attempt !== true) { console.error(platform, 'webcal 未标记 attempt'); process.exit(1); }\n"
        "  }\n"
        "}\nconsole.log('ok');\n", encoding="utf-8")
    result = subprocess.run(["node", str(harness)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr or result.stdout


def test_entry_hides_when_calendar_feature_is_disabled():
    """部署未开启 CALENDAR_ENABLED 时 /config 返回 404，入口必须自动收起而不是报错。"""
    page = _read("frontend/static/index.html")
    assert "function calendarDisableEntry()" in page
    assert "resp.status === 404" in page
    body = page.split("function calendarDisableEntry()", 1)[1].split("function ", 1)[0]
    assert 'calendarEls.entry.classList.add("hidden")' in body
    assert 'calendarEls.block.classList.add("hidden")' in body
    init = page.split("sanitizeStoredHistory();", 1)[0]
    assert "calendarLoadConfig();" in init                  # 页面初始化即探测，不必等用户点击
    assert "function calendarFilterSemesterOptions()" in page
    assert "calendarConfig.semesters" in page               # 学期按后端配置过滤
