"""Prepare a tailored resume and cover letter (DOCX + PDF) per job in out/<queue date>/<Company>/."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from jobagent.candidate.models import CandidateTruth
from jobagent.db import ApplicationPacket, JobRepository, StoredJob
from jobagent.eligibility import rules_fingerprint
from jobagent.scoring import job_content_fingerprint
from jobagent.writing.cover_letter import ClaudeCoverLetterWriter, render_cover_letter
from jobagent.writing.resume import (
    PdfConverter,
    default_pdf_converter,
    jd_keywords,
    page_count,
    skills_text,
    tailor_docx,
)

TAILOR_VERSION = "master-docx-keywords-v3"


class PreparationService:
    def __init__(
        self,
        repository: JobRepository,
        candidate: CandidateTruth,
        *,
        master: Path = Path("candidate/master_resume.docx"),
        output_root: Path = Path("out"),
        pdf_converter: PdfConverter | None = None,
        cover_letter_writer: ClaudeCoverLetterWriter | None = None,
        cover_letter_template: Path = Path("candidate/cover_letter_template.docx"),
    ) -> None:
        self.repository, self.candidate, self.master = repository, candidate, master
        self.output_root, self.pdf_converter = output_root, pdf_converter or default_pdf_converter()
        self.letters, self.letter_template = cover_letter_writer, cover_letter_template

    def prepare(
        self, job: StoredJob, *, queue_date: str, force: bool = False, master_picked: bool = False,
    ) -> tuple[ApplicationPacket | None, str | None]:
        """``master_picked``: Master named this job himself, so the eligibility filter only warns (CLI)."""
        eligibility = self.repository.get_eligibility(job.id, rules_fingerprint(self.candidate))
        if not master_picked and (not eligibility or eligibility.status == "ineligible"):
            return None, "Job is not eligible for preparation"
        if not self.master.is_file():
            return None, f"Master resume not found: {self.master}"
        fingerprint = hashlib.sha256(json.dumps({
            "version": TAILOR_VERSION,
            "candidate": self.candidate.fingerprint,
            "job": job_content_fingerprint(job),
            "master": hashlib.sha256(self.master.read_bytes()).hexdigest(),
            "cover_letter": _file_hash(self.letter_template) if self.letters else None,
        }, sort_keys=True).encode()).hexdigest()
        cached = self.repository.get_packet_by_fingerprint(job.id, fingerprint)
        if cached and cached.status == "ready" and not force:
            return cached, None
        directory = self.output_root / queue_date / _slug(job.company)
        docx, pdf = self._paths(job, directory)
        try:
            keywords = jd_keywords(job.description_text, self.candidate, skills_text(self.master))
            added = tailor_docx(self.master, docx, keywords)
            self.pdf_converter(docx, pdf)
            # ponytail: the line budget is approximate; drop newest keywords until Word says one page.
            while page_count(pdf) > 1 and added:
                dropped = added[-1]  # lowest-ranked keyword that made it in
                keywords = [(line, name) for line, name in keywords if name != dropped]
                added = tailor_docx(self.master, docx, keywords)
                self.pdf_converter(docx, pdf)
            if page_count(pdf) > 1:
                raise ValueError("Tailored resume does not fit on one page")
            paths = {"resume_docx_path": str(docx), "resume_pdf_path": str(pdf)}
            metadata: dict[str, object] = {"added_keywords": added}
            if self.letters:
                # A failed letter never blocks the resume; the error is kept for review.
                try:
                    paths |= self._cover_letter(job, docx)
                except (OSError, RuntimeError, ValueError, StopIteration) as error:
                    metadata["cover_letter_error"] = str(error)[:500]
            packet = self.repository.save_packet(
                job_id=job.id, fingerprint=fingerprint, status="ready", packet_directory=str(directory),
                paths=paths, validation_state="valid", writing_model=TAILOR_VERSION, skill_metadata=metadata,
            )
            return packet, None
        except (OSError, RuntimeError, ValueError) as error:
            # Partial files would make the retry think another job owns this name; keep a ready packet's files.
            own = self.repository.get_latest_ready_packet(job.id)
            if own is None or own.resume_pdf_path != str(pdf):
                docx.unlink(missing_ok=True)
                pdf.unlink(missing_ok=True)
            return self.repository.save_packet(job_id=job.id, fingerprint=fingerprint, status="failed", failure_reason=str(error)), str(error)

    def _cover_letter(self, job: StoredJob, resume_docx: Path) -> dict[str, str]:
        if not self.letter_template.is_file():
            raise RuntimeError(f"Cover letter template not found: {self.letter_template}")
        draft = self.letters.draft(job, self.candidate)  # type: ignore[union-attr]
        docx = resume_docx.with_name(resume_docx.stem.replace("Resume_", "CoverLetter_", 1) + ".docx")
        pdf = docx.with_suffix(".pdf")
        try:
            render_cover_letter(self.letter_template, docx, company=job.company, paragraphs=draft.paragraphs)
            self.pdf_converter(docx, pdf)
            if page_count(pdf) > 1:
                raise ValueError("Cover letter does not fit on one page")
        except (OSError, RuntimeError, ValueError):
            docx.unlink(missing_ok=True)
            pdf.unlink(missing_ok=True)
            raise
        return {"cover_letter_docx_path": str(docx), "cover_letter_pdf_path": str(pdf)}

    def _paths(self, job: StoredJob, directory: Path) -> tuple[Path, Path]:
        """Resume_<Name> (the out/<date>/<Company>/ folder keeps packets apart; Master, 2026-11-19).

        A job ID is appended only if another job (same company, same day) already owns that name.
        """
        stem = resume_stem(self.candidate.profile.display_name)
        own = self.repository.get_latest_ready_packet(job.id)
        if (directory / f"{stem}.pdf").exists() and (own is None or own.resume_pdf_path != str(directory / f"{stem}.pdf")):
            stem = f"{stem}_{job.id}"
        return directory / f"{stem}.docx", directory / f"{stem}.pdf"


def resume_stem(display_name: str) -> str:
    """"Example Candidate" -> "Resume_ExampleCandidate"; the cover letter is the same with "CoverLetter_"."""
    return "Resume_" + (re.sub(r"[^A-Za-z0-9]+", "", display_name) or "Candidate")


def _file_hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", value).strip("_") or "Job"
