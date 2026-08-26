"""PDF 渲染测试：Markdown 子集确定性转换与 XML 特殊字符转义。"""
from __future__ import annotations

from app.pdf import markdown_to_pdf


def test_markdown_to_pdf_returns_pdf_bytes():
    md = ("## 我的成绩\n\n"
          "| 学期 | 课程 | 成绩 |\n|---|---|---|\n| 1 | 高数 | 90 |")
    out = markdown_to_pdf(md)
    assert out.startswith(b"%PDF")


def test_pdf_escapes_xml_special_chars():
    # 课程/姓名字段含 < > & 时不应因 reportlab XML 解析失败而抛异常
    out = markdown_to_pdf("课程 C<语言> 与 数学 & 物理（90 分）")
    assert out.startswith(b"%PDF")
