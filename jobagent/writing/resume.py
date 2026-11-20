"""Tailor the master resume DOCX by adding evidenced JD keywords to its skills lines.

The master resume's formatting is never rebuilt: the Word file is copied and only the
three TECHNICAL SKILLS lines get extra comma-separated terms appended to their last run,
so fonts, spacing, and layout stay exactly as Master designed them. A keyword is added
only when it is a skill Master lists in candidate truth (skills.yaml), it appears in the job
description, and the skills lines do not already list it. Skills not in skills.yaml are never added.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from docx import Document
from pypdf import PdfReader, PdfWriter

from jobagent.candidate.models import CandidateTruth

# Master resume skills line label -> candidate-truth skill categories feeding it.
SKILL_LINES: dict[str, tuple[str, ...]] = {
    "Compliance & Frameworks": ("compliance",),
    "Cybersecurity Domains": ("security",),
    "Tools & Technologies": ("security_tools", "programming_and_data", "cloud", "networking"),
}
# ponytail: rough characters per printed skills line on the Garamond 10.5pt master (lines vary
# ~106-118). Deliberately generous: the master page is full, so any wrap spills onto page 2 and
# Word's page count (checked by the caller) removes the lowest-ranked keyword until it fits.
LINE_CHARS = 114
MAX_KEYWORDS = 8
WORD_TIMEOUT_SECONDS = 120
# Word is sandboxed on macOS; files inside its container never trigger a permission prompt.
WORD_CONTAINER = Path.home() / "Library/Containers/com.microsoft.Word/Data/jobagent"

PdfConverter = Callable[[Path, Path], None]


def _mentions(text: str, phrase: str) -> bool:
    return re.search(rf"(?<!\w){re.escape(phrase.casefold())}(?!\w)", text) is not None


def jd_keywords(description: str | None, truth: CandidateTruth, existing_skills: str) -> list[tuple[str, str]]:
    """(skills line, keyword) for skills.yaml skills the JD mentions and the skills lines lack, best first."""
    jd, resume = (description or "").casefold(), existing_skills.casefold()
    found: list[tuple[int, str, str]] = []
    for label, categories in SKILL_LINES.items():
        for category in categories:
            for skill in truth.skills.get(category, []):
                names = (skill.canonical_name, *skill.aliases)
                if any(_mentions(resume, name) for name in names):
                    continue
                hits = [(len(re.findall(rf"(?<!\w){re.escape(n.casefold())}(?!\w)", jd)), n) for n in names]
                count, name = max(hits)
                if count:
                    found.append((count, label, name))
    found.sort(key=lambda item: -item[0])
    return [(label, name) for _, label, name in found[:MAX_KEYWORDS]]


def tailor_docx(master: Path, output: Path, keywords: list[tuple[str, str]]) -> list[str]:
    """Write a copy of ``master`` with keywords appended to its skills lines; return added, best first."""
    document = Document(str(master))
    while document.paragraphs and not document.paragraphs[-1].text.strip():
        element = document.paragraphs[-1]._element
        element.getparent().remove(element)
    added: list[str] = []
    for paragraph in document.paragraphs:
        label = next((name for name in SKILL_LINES if paragraph.text.strip().startswith(name)), None)
        runs = [run for run in paragraph.runs if run.text.strip()]
        wanted = [name for line, name in keywords if line == label]
        if label is None or not wanted or not runs:
            continue
        text = paragraph.text.rstrip()
        period = text.endswith(".")
        base = text.rstrip(".")
        room = math.ceil(len(text) / LINE_CHARS) * LINE_CHARS  # never add a printed line
        extra: list[str] = []
        for keyword in wanted:
            if len(base) + len(", " + keyword) <= room:
                base += ", " + keyword
                extra.append(keyword)
        if not extra:
            continue
        last = runs[-1]
        last.text = last.text.rstrip().rstrip(".") + ", " + ", ".join(extra) + ("." if period else "")
        added.extend(extra)
    output.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(output))
    order = [name for _, name in keywords]
    return sorted(added, key=order.index)


def word_to_pdf(docx: Path, pdf: Path) -> None:
    """Export with Microsoft Word itself so the PDF matches Master's own Word exports."""
    if not Path("/Applications/Microsoft Word.app").exists():
        raise RuntimeError("Microsoft Word is required to export resume PDFs")
    WORD_CONTAINER.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=WORD_CONTAINER) as directory:
        # Word addresses documents by name: a fixed name collides with any copy left open by an earlier
        # timeout and every later export hangs, so each export gets its temp directory's unique name.
        staged_docx = Path(directory) / f"{Path(directory).name}.docx"
        staged_pdf = staged_docx.with_suffix(".pdf")
        shutil.copy(docx, staged_docx)
        script = f'''
            tell application "Microsoft Word"
                open file name (POSIX file "{staged_docx}" as text)
                -- open's return value is unreliable when other documents are open (-1708 on close)
                set doc to document "{staged_docx.name}"
                save as doc file name "{staged_pdf}" file format format PDF
                -- a stuck Word can refuse "close" (-1708) after a good export; the PDF is what matters
                try
                    close doc saving no
                end try
            end tell'''
        try:
            result = subprocess.run(
                ["osascript", "-e", script], capture_output=True, text=True, timeout=WORD_TIMEOUT_SECONDS, check=False
            )
        except subprocess.TimeoutExpired as error:
            _close_in_word(staged_docx.name)
            raise RuntimeError("Word PDF export timed out (is a Word dialog open?)") from error
        if result.returncode != 0 or not staged_pdf.exists() or staged_pdf.stat().st_size == 0:
            raise RuntimeError(f"Word PDF export failed: {result.stderr.strip()[:300]}")
        _write_without_blank_trailing_pages(staged_pdf, pdf)


def libreoffice_to_pdf(docx: Path, pdf: Path) -> None:
    """Headless LibreOffice export for Linux hosts (the VPS has no Word).

    Layout is close to Word's but not identical: the one-page checks still run on the result.
    """
    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice:
        raise RuntimeError("LibreOffice (soffice) is required to export PDFs")
    with tempfile.TemporaryDirectory(prefix="jobagent-lo-") as directory:
        # A private profile per call: a concurrent or crashed soffice never blocks the next export.
        profile = Path(directory, "profile").as_uri()
        try:
            result = subprocess.run(
                [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore",
                 "--convert-to", "pdf", "--outdir", directory, str(docx)],
                capture_output=True, text=True, timeout=WORD_TIMEOUT_SECONDS, check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("LibreOffice PDF export timed out") from error
        staged = Path(directory) / f"{docx.stem}.pdf"
        if result.returncode != 0 or not staged.exists() or staged.stat().st_size == 0:
            raise RuntimeError(f"LibreOffice PDF export failed: {result.stderr.strip()[:300]}")
        _write_without_blank_trailing_pages(staged, pdf)


def default_pdf_converter() -> PdfConverter:
    """Word on the Mac (matches Master's own exports), LibreOffice elsewhere; JOBAGENT_PDF_CONVERTER overrides."""
    choice = os.getenv("JOBAGENT_PDF_CONVERTER") or ("word" if sys.platform == "darwin" else "libreoffice")
    return word_to_pdf if choice == "word" else libreoffice_to_pdf


def _close_in_word(name: str) -> None:
    """Best effort: don't leave a timed-out copy open in Word."""
    script = f'tell application "Microsoft Word" to if exists document "{name}" then close document "{name}" saving no'
    try:
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=15, check=False)
    except subprocess.TimeoutExpired:
        pass


def _write_without_blank_trailing_pages(source: Path, destination: Path) -> None:
    """Word can emit an empty last page when text ends at the bottom margin; drop it."""
    reader = PdfReader(str(source))
    pages = list(reader.pages)
    while len(pages) > 1 and not (pages[-1].extract_text() or "").strip():
        pages.pop()
    writer = PdfWriter()
    for page in pages:
        writer.add_page(page)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        writer.write(handle)


def page_count(pdf: Path) -> int:
    return len(PdfReader(str(pdf)).pages)


def resume_text(master: Path) -> str:
    return "\n".join(paragraph.text for paragraph in Document(str(master)).paragraphs)


def skills_text(master: Path) -> str:
    """Only the TECHNICAL SKILLS lines: a skill shown only in a bullet still earns a skills-line spot."""
    return "\n".join(
        paragraph.text for paragraph in Document(str(master)).paragraphs
        if any(paragraph.text.strip().startswith(label) for label in SKILL_LINES)
    )
