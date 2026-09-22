"""日历订阅前端入口守卫：页脚入口、结果区区块、后台标签页与接口路径。"""
from __future__ import annotations

from pathlib import Path

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
    assert 'admin.js?v=21' in page
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
