"""渲染纯函数测试：与原 Dify 代码节点行为一致。"""
from __future__ import annotations

from app.render import (assemble, extract_session, format_grades, format_schedule,
                        preprocess_grades, sanitize_markdown)


def test_extract_session_from_json_string():
    body = '{"success": true, "session": "abc123", "token": "tok", "username": "2023000001"}'
    assert extract_session(body) == {"session": "abc123", "token": "tok",
                                     "username": "2023000001"}


def test_format_grades_uses_server_output_and_prefixes_login_note():
    data = {"success": True, "output": "| 学期 | 课程 | 成绩 |\n|---|---|---|\n| 1 | 高数 | 90 |"}
    jump = '{"body": "https://example.com/ticket?st=xyz"}'
    out = format_grades(data, jump)
    assert "免密登录教务系统" in out
    assert "| 1 | 高数 | 90 |" in out
    assert "session" not in out  # 登录响应不拼入输出（迁移修复点）


def test_login_note_from_structured_jump_dict():
    data = {"success": True, "output": "| 学期 | 课程 | 成绩 |\n|---|---|---|"}
    jump = {"success": True, "login_url": "https://example.com/jump/go?code=x"}
    out = format_grades(data, jump)
    assert "https://example.com/jump/go?code=x" in out


def test_format_grades_fallback_from_rows():
    data = {"success": True, "info": {"xm": "张三", "xh": "2023000001"},
            "rows": [{"semester": "1", "courseName": "高数", "score": "90"}]}
    out = format_grades(data, "")
    assert "张三" in out and "| 高数 |" in out


def test_preprocess_and_assemble():
    data = {"success": True, "output": "TABLE", "stats": {"avg": 80}}
    parts = preprocess_grades(data, "")
    assert parts["table_text"] == "TABLE"
    assert parts["stats_text"] == '{"avg": 80}'
    assert assemble(parts["prefix_text"], "分析正文") == "TABLE\n\n分析正文"


def test_format_schedule_puts_download_url_first():
    data = {"success": True, "output": "| 节次 | 周一 |", "download_url": "https://x/dl?t=1"}
    out = format_schedule(data, "")
    assert out.startswith("[点击下载课表（PDF 文件）](https://x/dl?t=1)")
    assert "请勿外传" in out


def test_format_schedule_preserves_cell_line_breaks():
    data = {
        "success": True,
        "output": "| 节次 | 周一 |\n|---|---|\n| 1 | 高数<br>张老师<br>A101 |",
    }
    out = format_schedule(data, "")
    assert "高数<br>张老师<br>A101" in out


def test_format_schedule_fallback_preserves_multiline_fields():
    data = {"success": True, "rows": [{
        "timeCode": "1", "timeName": "第1节", "dayOfWeekCode": "1",
        "dayOfWeekName": "周一", "courseName": "高数", "teacherName": "张老师",
        "classroomName": "A101\nA102", "weeks": "1-2",
    }]}
    out = format_schedule(data, "")
    assert "A101<br>A102" in out
    assert "A101 A102" not in out


def test_strip_login_note_for_pdf_and_history():
    from app.render import strip_login_note
    md = ("> 🔗 免密登录教务系统：[点击进入](https://x/jump/go?code=abc)\n"
          "> ⚠️ 请在浏览器中允许弹出式窗口（弹窗拦截会阻止免密跳转），登陆失败请清理cookie后重试；"
          "移动端兼容性：部分手机浏览器可能无法正常完成跳转，建议使用电脑端浏览器（最新版 Chrome / Edge）打开。\n"
          "> 此链接包含登录令牌，请勿外传。\n\n"
          "## 我的成绩\n\n| 学期 | 课程 | 成绩 |\n|---|---|---|\n| 1 | 高数 | 90 |")
    out = strip_login_note(md)
    assert "免密登录教务系统" not in out
    assert "jump/go" not in out
    assert "我的成绩" in out
    assert "| 1 | 高数 | 90 |" in out


def test_upstream_markdown_is_sanitized_before_output():
    malicious = ("![x](https://attacker.example/pixel)\n"
                 "<script>alert(1)</script>\n"
                 "[文本](javascript:alert(1))\n"
                 "| A |\n|---|\n| B |")
    cleaned = sanitize_markdown(malicious)
    rendered = format_grades({
        "rows": [{"semester": "2025", "courseName": "bad | course", "score": "80"}]
    }, {})
    assert "<script>" not in cleaned
    assert "![" not in cleaned
    assert "javascript:" not in cleaned
    assert "<script>" not in rendered
    assert "![" not in rendered
    assert "javascript:" not in rendered
    assert "bad \\| course" in rendered
