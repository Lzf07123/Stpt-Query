"""渲染纯函数测试：与原 Dify 代码节点行为一致。"""
from __future__ import annotations

from app.render import assemble, extract_session, format_grades, format_schedule, preprocess_grades


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
    assert out.startswith("[点击下载课表（Word 文件）](https://x/dl?t=1)")
    assert "请勿外传" in out
