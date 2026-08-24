"""成绩/课表渲染：从原 Dify 代码节点 1:1 移植为纯函数。

对应原节点：获取session、成绩数据格式化、课表数据格式化、成绩数据预处理、数据组装。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple


def _as_dict(data: Any) -> Dict[str, Any]:
    """兼容 dict / JSON 字符串 / 双重转义字符串；解析失败返回 {}。"""
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        s = data.strip()
        if s.startswith('"') and s.endswith('"'):
            try:
                s = json.loads(s)
            except Exception:
                pass
        try:
            obj = json.loads(s)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _body_text(data: Any) -> str:
    if isinstance(data, dict):
        return str(data.get("body") or "").strip()
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("http"):
            return s
        d = _as_dict(s)
        if d:
            return str(d.get("body") or "").strip()
        return s
    return ""


def _extract_url(raw: Any) -> str:
    if isinstance(raw, str):
        m = re.search(r"https?://[^\s\"'<>，。；]+", raw)
        if m:
            return m.group(0).rstrip(".,;")
    return ""


def _login_note(raw: Any) -> str:
    """从免密登录响应中提取 URL 并生成提示块；无 URL 时返回空串。"""
    url = _extract_url(_body_text(raw))
    if not url:
        return ""
    return (
        "> 🔗 免密登录教务系统：[点击进入](%s)\n"
        "> ⚠️ 请在浏览器中允许弹出式窗口（弹窗拦截会阻止免密跳转），登陆失败请清理cookie后重试；"
        "移动端兼容性：部分手机浏览器可能无法正常完成跳转，建议使用电脑端浏览器（最新版 Chrome / Edge）打开。\n"
        "> 此链接包含登录令牌，请勿外传。"
    ) % url


def extract_session(login_body: Any) -> Dict[str, str]:
    """原「获取session」节点：提取 session/token/username。"""
    data = _as_dict(login_body)
    return {
        "session": str(data.get("session") or "").strip(),
        "token": str(data.get("token") or "").strip(),
        "username": str(data.get("username") or "").strip(),
    }


_GRADE_LABELS: Tuple[Tuple[str, str], ...] = (
    ("xm", "姓名"), ("xh", "学号"), ("xbmc", "性别"), ("nj", "年级"),
    ("xymc", "学院"), ("zyfxmc", "专业"), ("bjmc", "班级"),
)


def _render_grade_table(data: Dict[str, Any]) -> str:
    """output 缺失时按 info + rows 兜底渲染成绩表。"""
    rows = data.get("rows") or []
    info = data.get("info") or {}
    lines: List[str] = []
    if isinstance(info, dict):
        parts = [
            "%s：%s" % (label, str(info.get(k) or "").strip())
            for k, label in _GRADE_LABELS if str(info.get(k) or "").strip()
        ]
        if parts:
            lines.append("｜".join(parts))
            lines.append("")
    if rows:
        lines.append("| 学期 | 课程 | 成绩 | GPA | 学分 | 课程类别 |")
        lines.append("|---|---|---|---|---|---|")
        for r in rows:
            lines.append(
                "| %s | %s | %s | %s | %s | %s |"
                % (r.get("semester", ""), r.get("courseName", ""), r.get("score", ""),
                   r.get("gpa", ""), r.get("credit", ""), r.get("courseType", "")))
    else:
        lines.append("（暂无成绩数据）")
    return "\n".join(lines)


def format_grades(http_response: Any, jump_body: Any) -> str:
    """原「成绩数据格式化」节点：返回 链接提示块 + 成绩表 的完整 Markdown。"""
    data = _as_dict(http_response)
    output = str(data.get("output") or "").strip()
    if not output:
        output = _render_grade_table(data)
    note = _login_note(jump_body)
    if note:
        output = note + "\n\n" + output
    return output


def preprocess_grades(http_response: Any, jump_body: Any) -> Dict[str, str]:
    """原「成绩数据预处理」节点：拆出 prefix/table/stats 供成绩分析使用。"""
    data = _as_dict(http_response)
    output = str(data.get("output") or "").strip()
    table = output or _render_grade_table(data)
    note = _login_note(jump_body)
    prefix = (note + "\n\n" + table) if note else table
    stats = data.get("stats") or {}
    stats_text = json.dumps(stats, ensure_ascii=False) if stats else ""
    return {
        "prefix_text": prefix,
        "table_text": table,
        "stats_text": stats_text,
    }


def assemble(prefix_text: str, analysis_text: str) -> str:
    """原「数据组装」节点：链接提示块 + 成绩表 + 分析正文。"""
    parts = [x for x in (str(prefix_text or "").strip(), str(analysis_text or "").strip()) if x]
    return "\n\n".join(parts)


def _fmt_weeks(w: Any) -> str:
    """周次模式串（如 1-8;10;13-18、1-17 单）转为 1-8、10、13-18周，不展开。"""
    w = str(w or "").strip()
    if not w:
        return ""
    marker = ""
    for m in ("单", "双"):
        if m in w:
            marker = m
            w = w.replace(m, "").strip()
    parts: List[str] = []
    for seg in w.replace("，", ",").split(";"):
        seg = seg.strip()
        if not seg:
            continue
        if "-" in seg:
            a, _, b = seg.partition("-")
            if a.isdigit() and b.isdigit():
                parts.append("%s-%s" % (a, b))
                continue
        parts.append(seg)
    return ("、".join(parts) + marker + "周") if parts else (w + marker + "周")


def _cell_text(r: Dict[str, Any]) -> str:
    bits = [str(r.get("courseName") or "").strip()]
    extra = [
        x for x in (
            str(r.get("teacherName") or "").strip(),
            str(r.get("classroomName") or "").strip(),
            _fmt_weeks(r.get("weeks")),
        ) if x
    ]
    if extra:
        bits.append("（" + "｜".join(extra) + "）")
    return "".join(bits)


def _grid_from_rows(rows: List[Dict[str, Any]]) -> str:
    """无 output 时按 rows 渲染周课表（行=节次升序，列=星期升序）。"""
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return ""
    time_codes = sorted({str(r.get("timeCode") or "") for r in rows})
    day_codes = sorted({str(r.get("dayOfWeekCode") or "") for r in rows})
    time_names = {str(r.get("timeCode") or ""): str(r.get("timeName") or "").strip() for r in rows}
    day_names = {str(r.get("dayOfWeekCode") or ""): str(r.get("dayOfWeekName") or "").strip() for r in rows}
    grid: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        grid.setdefault((str(r.get("timeCode") or ""), str(r.get("dayOfWeekCode") or "")), []).append(r)
    lines = ["| 节次 | " + " | ".join(day_names.get(d, d) for d in day_codes) + " |",
             "|" + "---|" * (len(day_codes) + 1)]
    for t in time_codes:
        cells = [time_names.get(t, t)]
        for d in day_codes:
            cells.append("<br>".join(_cell_text(r) for r in grid.get((t, d), [])))
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


def format_schedule(http_response: Any, jump_body: Any) -> str:
    """原「课表数据格式化」节点：下载链接 + 免密提示 + 周课表网格。"""
    data = _as_dict(http_response)
    output = str(data.get("output") or "").strip()
    rows = data.get("rows") or []
    semester = str(data.get("semester") or "").strip() or (
        str(rows[0].get("semester") or "") if rows else "")
    download_url = str(data.get("download_url") or "").strip()

    idx = output.find("### 课程明细")
    if idx != -1:
        output = output[:idx].rstrip()

    grid = output
    if not grid:
        head = ("学期：%s" % semester) if semester else ""
        body = _grid_from_rows(rows)
        grid = "\n".join(x for x in (head, body) if x) or "（暂无课表数据）"

    lines: List[str] = []
    if download_url:
        lines.append("[点击下载课表（Word 文件）](%s)" % download_url)
    note = _login_note(jump_body)
    if note:
        lines.append(note)
    lines.append(grid)
    if download_url:
        lines.append("")
        lines.append("> ⚠️ 此链接含个人令牌，请勿外传。")
    return "\n".join(lines)
