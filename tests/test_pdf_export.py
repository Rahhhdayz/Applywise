"""PDF export choice and the LibreOffice exporter used on the VPS (subprocess mocked)."""

import subprocess
from pathlib import Path

import pytest
from pypdf import PdfWriter

from jobagent import runtime
from jobagent.writing import resume


def _fake_soffice(monkeypatch: pytest.MonkeyPatch, *, pages: int = 1, returncode: int = 0) -> list[list[str]]:
    calls: list[list[str]] = []

    def run(command, **kwargs):  # type: ignore[no-untyped-def]
        calls.append(command)
        outdir = Path(command[command.index("--outdir") + 1])
        if returncode == 0:
            writer = PdfWriter()
            for _ in range(pages):
                writer.add_blank_page(612, 792)
            with (outdir / f"{Path(command[-1]).stem}.pdf").open("wb") as handle:
                writer.write(handle)
        return subprocess.CompletedProcess(command, returncode, "", "boom" if returncode else "")

    monkeypatch.setattr(resume.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(resume.subprocess, "run", run)
    return calls


def test_libreoffice_export_writes_the_pdf_with_a_private_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_soffice(monkeypatch)
    resume.libreoffice_to_pdf(tmp_path / "Candidate.docx", tmp_path / "out" / "Candidate.pdf")
    assert (tmp_path / "out" / "Candidate.pdf").stat().st_size > 0
    assert any(arg.startswith("-env:UserInstallation=file:") for arg in calls[0])  # never the shared profile
    assert "--headless" in calls[0]


def test_libreoffice_failure_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_soffice(monkeypatch, returncode=1)
    with pytest.raises(RuntimeError, match="LibreOffice PDF export failed: boom"):
        resume.libreoffice_to_pdf(tmp_path / "Candidate.docx", tmp_path / "Candidate.pdf")


def test_converter_is_word_on_the_mac_libreoffice_elsewhere_and_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBAGENT_PDF_CONVERTER", raising=False)
    monkeypatch.setattr(resume.sys, "platform", "darwin")
    assert resume.default_pdf_converter() is resume.word_to_pdf
    monkeypatch.setattr(resume.sys, "platform", "linux")
    assert resume.default_pdf_converter() is resume.libreoffice_to_pdf
    monkeypatch.setenv("JOBAGENT_PDF_CONVERTER", "word")
    assert resume.default_pdf_converter() is resume.word_to_pdf


def test_doctor_checks_libreoffice_on_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOBAGENT_PDF_CONVERTER", raising=False)
    monkeypatch.setattr(resume.sys, "platform", "linux")
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    check = runtime._pdf_export_check()
    assert (check.name, check.state) == ("LibreOffice", "FAIL")
