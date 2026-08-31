#!/usr/bin/env python3
"""把学校课表导出的文档字节交给 LibreOffice 直接转换为 PDF。

教务系统返回的 `.doc` 文件实际携带 RTF 数据。这里不解析、不重排文档内容，
只写入临时文件后调用 Writer 的 PDF 导出器，保留原表格与版式。
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import tempfile
import threading

from jwxt_core import LOG, _env


class PdfConversionError(RuntimeError):
    """课表文档无法在容器内转换为 PDF。"""


PDF_TIMEOUT = max(1, int(_env("JWXT_PDF_TIMEOUT", 30)))
PDF_CONCURRENCY = max(1, int(_env("JWXT_PDF_CONCURRENCY", 1)))
PDF_MAX_INPUT_BYTES = max(1, int(_env("JWXT_PDF_MAX_INPUT_MB", 5))) * 1024 * 1024

_convert_slot = threading.BoundedSemaphore(PDF_CONCURRENCY)


def rtf_to_pdf(data: bytes) -> bytes:
    """Return the original document rendered as PDF bytes."""
    if not data:
        raise PdfConversionError("课表文档为空，无法转换 PDF")
    if len(data) > PDF_MAX_INPUT_BYTES:
        raise PdfConversionError("课表文档过大，无法转换 PDF")

    soffice = shutil.which("soffice")
    if not soffice:
        raise PdfConversionError("未找到 LibreOffice 转换器")

    with _convert_slot:
        with tempfile.TemporaryDirectory(prefix="jwxt-pdf-") as workdir:
            profile = os.path.join(workdir, "profile")
            output_dir = os.path.join(workdir, "out")
            source = os.path.join(workdir, "schedule.doc")
            target = os.path.join(output_dir, "schedule.pdf")
            os.mkdir(output_dir)
            with open(source, "wb") as fp:
                fp.write(data)

            command = [
                soffice, "-env:UserInstallation=file://" + profile,
                "--headless", "--nologo", "--nodefault", "--nolockcheck",
                "--norestore", "--convert-to", "pdf:writer_pdf_Export",
                "--outdir", output_dir, source,
            ]
            env = dict(os.environ)
            env.update({"HOME": workdir, "LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})
            try:
                proc = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, errors="replace", env=env, cwd=workdir,
                    start_new_session=True)
            except OSError:
                LOG.exception("启动课表 PDF 转换器失败")
                raise PdfConversionError("课表 PDF 转换器启动失败")

            try:
                stdout, stderr = proc.communicate(timeout=PDF_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
                raise PdfConversionError("课表 PDF 转换超时")

            if proc.returncode != 0:
                LOG.warning("LibreOffice 转换失败 rc=%s stdout=%s stderr=%s",
                            proc.returncode, stdout.strip()[:300],
                            stderr.strip()[:300])
                raise PdfConversionError("课表 PDF 转换失败")
            if not os.path.isfile(target):
                raise PdfConversionError("课表 PDF 转换未生成文件")
            with open(target, "rb") as fp:
                pdf = fp.read()
            if not pdf.startswith(b"%PDF-"):
                raise PdfConversionError("课表 PDF 输出无效")
            return pdf
