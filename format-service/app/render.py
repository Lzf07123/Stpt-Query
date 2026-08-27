"""成绩/课表渲染：从原 Dify 代码节点 1:1 移植为纯函数。

对应原节点：获取session、成绩数据格式化、课表数据格式化、成绩数据预处理、数据组装。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple
from urllib.parse import urlparse


_IMAGE_SYNTAX = re.compile(r"!\[[^\]]*\]\([^)]+\)")
_HTML_TAG = re.compile(r"</?(?!br>)[a-zA-Z][^>]*>", re.IGNORECASE)
_MD_LINK = re.compile(r"\[([^\]]+)\]\(\s*([^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")


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


def _safe_http_url(raw: Any) -> str:
    url = str(raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return url


def sanitize_markdown(text: str) -> str:
    """只保留无脚本文本、标准 Markdown 和显式 HTTP(S) 链接。"""
    cleaned = str(text or "")
    cleaned = re.sub(r"(?i)<br\s*/?>", "<br>", cleaned)
    cleaned = _IMAGE_SYNTAX.sub("", cleaned)
    cleaned = _HTML_TAG.sub("", cleaned)

    def _link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if _safe_http_url(url):
            return "[%s](%s)" % (label.replace("[", "\\[").replace("]", "\\]"), url)
        return label

    cleaned = _MD_LINK.sub(_link, cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _body_text(data: Any) -> str:
    """取响应文本：dict 优先 login_url/url/body/raw；字符串优先当作 URL，再尝试 JSON。"""
    if isinstance(data, dict):
        for key in ("login_url", "url", "body", "raw"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""
    if isinstance(data, str):
        s = data.strip()
        if s.startswith("http"):
            return s
        d = _as_dict(s)
        if d:
            return _body_text(d)
        return s
    return ""


def _extract_url(raw: Any) -> str:
    return _safe_http_url(_body_text(raw))


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


def strip_login_note(markdown: str) -> str:
    """PDF/历史回看不需要免密登录提示块（跳转码短时有效）。

    去掉以「> 🔗 免密登录教务系统」开头的整段引用，后续内容原样保留。
    """
    lines = markdown.splitlines()
    out: List[str] = []
    skipping = False
    for line in lines:
        if line.startswith("> 🔗 免密登录教务系统"):
            skipping = True
            continue
        if skipping:
            if line.startswith("> "):
                continue
            skipping = False
        out.append(line)
    return "\n".join(out).strip()


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
                % tuple(_table_cell(r.get(key, "")) for key in (
                    "semester", "courseName", "score", "gpa", "credit", "courseType")))
    else:
        lines.append("（暂无成绩数据）")
    return "\n".join(lines)


def _table_cell(value: Any) -> str:
    text = str(value or "").replace("\\", "\\\\").replace("|", "\\|")
    return text.replace("\r", " ").replace("\n", " / ")


def format_grades(http_response: Any, jump_body: Any) -> str:
    """原「成绩数据格式化」节点：返回 链接提示块 + 成绩表 的完整 Markdown。"""
    data = _as_dict(http_response)
    output = str(data.get("output") or "").strip()
    if not output:
        output = _render_grade_table(data)
    note = _login_note(jump_body)
    if note:
        output = note + "\n\n" + output
    return sanitize_markdown(output)


def preprocess_grades(http_response: Any, jump_body: Any) -> Dict[str, str]:
    """原「成绩数据预处理」节点：拆出 prefix/table/stats 供成绩分析使用。"""
    data = _as_dict(http_response)
    output = str(data.get("output") or "").strip()
    table = sanitize_markdown(output) if output else _render_grade_table(data)
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
    return sanitize_markdown("\n\n".join(parts))


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
    def clean(value: Any) -> str:
        return str(value or "").replace("\r", "").replace("\n", "<br>") \
            .replace("|", "\\|")

    bits = [clean(r.get("courseName"))]
    extra = [
        x for x in (
            clean(r.get("teacherName")),
            clean(r.get("classroomName")),
            _fmt_weeks(r.get("weeks")).replace("\r", "").replace("\n", "<br>") \
                .replace("|", "\\|"),
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
    time_names = {
        str(r.get("timeCode") or ""):
            str(r.get("timeName") or "").strip().replace("\r", "").replace("\n", "<br>")
        for r in rows
    }
    day_names = {
        str(r.get("dayOfWeekCode") or ""):
            str(r.get("dayOfWeekName") or "").strip().replace("\r", "").replace("\n", "<br>")
        for r in rows
    }
    grid: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for r in rows:
        grid.setdefault((str(r.get("timeCode") or ""), str(r.get("dayOfWeekCode") or "")), []).append(r)
    lines = ["| 节次 | " + " | ".join(day_names.get(d, d) for d in day_codes) + " |",
             "|" + "---|" * (len(day_codes) + 1)]
    for t in time_codes:
        cells = [time_names.get(t, t)]
        for d in day_codes:
            cells.append(" / ".join(_cell_text(r) for r in grid.get((t, d), [])))
        lines.append("| " + " | ".join(c.replace("|", "\\|") for c in cells) + " |")
    return "\n".join(lines)


def format_schedule(http_response: Any, jump_body: Any) -> str:
    """原「课表数据格式化」节点：下载链接 + 免密提示 + 周课表网格。"""
    data = _as_dict(http_response)
    output = str(data.get("output") or "").strip()
    rows = data.get("rows") or []
    semester = str(data.get("semester") or "").strip() or (
        str(rows[0].get("semester") or "") if rows else "")
    download_url = _safe_http_url(data.get("download_url"))

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
    return sanitize_markdown("\n".join(lines))
