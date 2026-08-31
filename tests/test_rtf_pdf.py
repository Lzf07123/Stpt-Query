from pathlib import Path

import pytest

import rtf_pdf
from rtf_pdf import PdfConversionError, rtf_to_pdf


def _fake_popen(pdf_bytes=b"%PDF-1.7\n", returncode=0):
    class FakeProcess:
        def __init__(self, command, **kwargs):
            self.command = command
            self.returncode = returncode
            self.pid = 1234
            outdir = Path(command[command.index("--outdir") + 1])
            if returncode == 0:
                (outdir / "schedule.pdf").write_bytes(pdf_bytes)

        def communicate(self, timeout=None):
            return "", ""

    return FakeProcess


def test_rtf_to_pdf_uses_writer_exporter_with_isolated_profile(monkeypatch):
    seen = {}

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        seen["source"] = Path(command[-1]).read_bytes()
        return _fake_popen()(command, **kwargs)

    monkeypatch.setattr("rtf_pdf.subprocess.Popen", fake_popen)
    monkeypatch.setattr("rtf_pdf.shutil.which", lambda _: "/usr/bin/soffice")
    pdf = rtf_to_pdf(b"{\\rtf1 document}")

    assert pdf == b"%PDF-1.7\n"
    command = seen["command"]
    assert command[0].endswith("soffice")
    assert "-env:UserInstallation=file://" in command[1]
    assert command[command.index("--convert-to") + 1] == "pdf:writer_pdf_Export"
    assert "--" not in command
    assert seen["kwargs"]["start_new_session"] is True
    assert seen["kwargs"]["cwd"].startswith("/private/tmp") or seen["kwargs"]["cwd"].startswith("/tmp")
    assert seen["source"] == b"{\\rtf1 document}"


def test_rtf_to_pdf_rejects_empty_and_oversized_input(monkeypatch):
    monkeypatch.setattr("rtf_pdf.PDF_MAX_INPUT_BYTES", 4)
    with pytest.raises(PdfConversionError, match="为空"):
        rtf_to_pdf(b"")
    with pytest.raises(PdfConversionError, match="过大"):
        rtf_to_pdf(b"12345")


def test_rtf_to_pdf_rejects_failed_conversion(monkeypatch):
    monkeypatch.setattr("rtf_pdf.subprocess.Popen", _fake_popen(returncode=1))
    monkeypatch.setattr("rtf_pdf.shutil.which", lambda _: "/usr/bin/soffice")
    with pytest.raises(PdfConversionError, match="转换失败"):
        rtf_to_pdf(b"{\\rtf1 document}")


def test_rtf_to_pdf_kills_timed_out_process_group(monkeypatch):
    class TimeoutProcess:
        pid = 4321
        returncode = None

        def communicate(self, timeout=None):
            if timeout is not None:
                raise rtf_pdf.subprocess.TimeoutExpired(cmd="soffice", timeout=timeout)
            return "", ""

    killed = []
    monkeypatch.setattr("rtf_pdf.subprocess.Popen", lambda *args, **kwargs: TimeoutProcess())
    monkeypatch.setattr("rtf_pdf.shutil.which", lambda _: "/usr/bin/soffice")
    monkeypatch.setattr("rtf_pdf.os.killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr("rtf_pdf.PDF_TIMEOUT", 1)

    with pytest.raises(PdfConversionError, match="超时"):
        rtf_to_pdf(b"{\\rtf1 document}")
    assert killed == [(4321, rtf_pdf.signal.SIGKILL)]
