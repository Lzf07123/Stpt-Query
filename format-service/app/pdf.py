"""Markdown -> PDF：替代 Dify 的 md_exporter 插件。

使用 reportlab + 内置 CID 中文字体（STSong-Light），无需外挂字体文件与系统库；
仅支持本服务输出的 Markdown 子集（段落、引用、表格），保证确定性渲染。
"""
from __future__ import annotations

import io
import re
from typing import Any, List, Tuple

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

_STYLE_BODY = ParagraphStyle("body", fontName="STSong-Light", fontSize=10, leading=15)
_STYLE_QUOTE = ParagraphStyle("quote", fontName="STSong-Light", fontSize=9,
                              leading=13, textColor=colors.HexColor("#555555"))


def _split_markdown(text: str) -> Tuple[List[str], List[List[str]]]:
    """把 Markdown 拆成文本行与表格（表格段落被替换为空占位）。"""
    lines = text.splitlines()
    paragraphs: List[str] = []
    tables: List[List[str]] = []
    buffer: List[str] = []

    def flush() -> None:
        if buffer:
            paragraphs.append("\n".join(buffer))
            buffer.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("|") and i + 1 < len(lines) and \
                re.match(r"^\s*\|?[\s:|-]+\|?\s*$", lines[i + 1]):
            flush()
            table: List[List[str]] = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                cells = [c.strip() for c in lines[j].strip().strip("|").split("|")]
                if not re.match(r"^[\s:|-]+$", "".join(cells)):
                    table.append(cells)
                j += 1
            tables.append(table)
            paragraphs.append("")  # 占位
            i = j
            continue
        if line.strip() == "":
            flush()
        else:
            buffer.append(line)
        i += 1
    flush()
    return paragraphs, tables


def markdown_to_pdf(text: str, title: str = "教务信息查询结果") -> bytes:
    """返回 PDF 字节流。"""
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, title=title,
        leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm, bottomMargin=18 * mm)
    story: List[Any] = []
    paragraphs, tables = _split_markdown(text or "")

    def _xml_escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def add_md(block: str) -> None:
        for raw in block.splitlines():
            raw = raw.strip()
            if not raw:
                continue
            if raw.startswith(">"):
                story.append(Paragraph(_xml_escape(raw.lstrip("> ")), _STYLE_QUOTE))
            else:
                story.append(Paragraph(_xml_escape(raw), _STYLE_BODY))
            story.append(Spacer(1, 3))

    table_idx = 0
    for block in paragraphs:
        if block == "" and table_idx < len(tables):
            story.append(_build_table(tables[table_idx]))
            table_idx += 1
        elif block:
            add_md(block)
        story.append(Spacer(1, 6))

    doc.build(story)
    return buf.getvalue()


def _build_table(rows: List[List[str]]) -> Table:
    table = Table(rows, hAlign="LEFT")
    style = [
        ("FONTNAME", (0, 0), (-1, -1), "STSong-Light"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#bbbbbb")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0f0f0")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    table.setStyle(TableStyle(style))
    return table
