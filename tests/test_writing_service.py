"""Offline tests for master-resume keyword tailoring; Word is replaced by a fake converter."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from docx import Document
from pypdf import PdfWriter
from reportlab.pdfgen.canvas import Canvas

from jobagent.candidate.models import (
    CandidateProfile,
    CandidateTruth,
    Experience,
    ExperienceFact,
    Preferences,
    Skill,
)
from jobagent.db import JobRepository
from jobagent.eligibility import EligibilityEngine, rules_fingerprint
from jobagent.models import JobPosting
from jobagent.writing.resume import (
    _write_without_blank_trailing_pages,
    jd_keywords,
    page_count,
    resume_text,
    skills_text,
    tailor_docx,
)
from jobagent.writing.service import PreparationService

JD = "We need Splunk and Globex Falcon experience. Splunk dashboards daily. Terraform a plus. Kubernetes too. Python required."


def candidate() -> CandidateTruth:
    fact = ExperienceFact(id="fact_1", text="Detected threats with Splunk; used Globex.", skills=["Splunk"])
    return CandidateTruth(
        profile=CandidateProfile(candidate_id="c", display_name="Example Candidate", professional_years=1),
        experiences=[Experience(id="exp_1", organization="Example", title="Intern", start_date="2025-01", facts=[fact])],
        skills={
            "security_tools": [
                Skill(canonical_name="Splunk", evidence_ids=["fact_1"]),
                Skill(canonical_name="Globex", aliases=["Globex Falcon"], evidence_ids=["fact_1"]),
                Skill(canonical_name="Terraform"),  # listed by the candidate without evidence
            ],
            "programming_and_data": [Skill(canonical_name="Python", evidence_ids=["fact_1"])],
        },
        preferences=Preferences(primary_roles=["Security Engineer"]),
    )


def master(tmp_path: Path) -> Path:
    document = Document()
    document.add_paragraph("Example Candidate")
    line = document.add_paragraph()
    line.add_run("Tools & Technologies: ").bold = True
    line.add_run("Python, AWS.")
    document.add_paragraph("Cybersecurity Domains: Network Security")
    document.add_paragraph("")  # trailing empty paragraph that must be dropped
    path = tmp_path / "master.docx"
    document.save(str(path))
    return path


def posting(description: str = JD) -> JobPosting:
    return JobPosting(
        source="test", source_job_id="job-1", company="News Corp", title="Cyber Security Analyst (SOC)",
        location="Remote", employment_type="full_time", description_text=description,
        apply_url="https://example.test/job-1", canonical_url="https://example.test/job-1", posted_at=datetime.now(UTC),
    )


def fake_pdf(pages: int | Callable[[Path], int] = 1):  # type: ignore[no-untyped-def]
    calls: list[Path] = []

    def convert(docx: Path, pdf: Path) -> None:
        calls.append(docx)
        writer = PdfWriter()
        for _ in range(pages(docx) if callable(pages) else pages):
            writer.add_blank_page(612, 792)
        with pdf.open("wb") as handle:
            writer.write(handle)

    convert.calls = calls  # type: ignore[attr-defined]
    return convert


def _eligible(tmp_path: Path, truth: CandidateTruth):  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    stored = repository.add_or_get_job(posting()).job
    result = EligibilityEngine().evaluate(stored, truth)
    repository.upsert_eligibility(
        job_id=stored.id, status=result.status, reasons=result.reasons, flags=result.flags,
        detected_requirements=result.detected_requirements, rules_version=result.rules_version,
        rules_fingerprint=result.rules_fingerprint,
    )
    return repository, stored


def test_keywords_are_evidenced_jd_mentioned_and_missing_from_resume(tmp_path: Path) -> None:
    keywords = jd_keywords(JD, candidate(), skills_text(master(tmp_path)))
    # Splunk (2 mentions) first; Globex uses the JD's own wording; Python is already on a
    # skills line; Kubernetes is not in skills.yaml, so it is never added.
    assert keywords[:2] == [("Tools & Technologies", "Splunk"), ("Tools & Technologies", "Globex Falcon")]
    assert ("Tools & Technologies", "Terraform") in keywords
    assert all(name != "Kubernetes" for _, name in keywords)


def test_tailor_appends_to_skills_line_and_keeps_formatting(tmp_path: Path) -> None:
    output = tmp_path / "out.docx"
    added = tailor_docx(master(tmp_path), output, [("Tools & Technologies", "Splunk"), ("Tools & Technologies", "Globex Falcon")])
    document = Document(str(output))
    line = next(p for p in document.paragraphs if p.text.startswith("Tools & Technologies"))
    assert added == ["Splunk", "Globex Falcon"]
    assert line.text == "Tools & Technologies: Python, AWS, Splunk, Globex Falcon."
    assert line.runs[0].bold and not line.runs[1].bold
    assert document.paragraphs[-1].text.strip()  # trailing blank paragraph removed


def test_tailor_never_adds_a_printed_line(tmp_path: Path) -> None:
    long = ["Keyword " + "x" * 30 + str(index) for index in range(5)]
    added = tailor_docx(master(tmp_path), tmp_path / "out.docx", [("Tools & Technologies", kw) for kw in long])
    assert len("Tools & Technologies: Python, AWS" + "".join(", " + kw for kw in added) + ".") <= 114


def test_prepare_writes_only_docx_and_pdf_named_for_the_job(tmp_path: Path) -> None:
    truth = candidate()
    repository, stored = _eligible(tmp_path, truth)
    converter = fake_pdf()
    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out", pdf_converter=converter)

    packet, error = service.prepare(stored, queue_date="2026-09-24")
    again, _ = service.prepare(stored, queue_date="2026-09-24")

    assert error is None and packet is not None and packet.status == "ready"
    folder = tmp_path / "out" / "2026-09-24" / "News_Corp"
    assert sorted(path.name for path in folder.iterdir()) == [
        "Resume_ExampleCandidate.docx",
        "Resume_ExampleCandidate.pdf",
    ]
    assert packet.resume_pdf_path == str(folder / "Resume_ExampleCandidate.pdf")
    assert again == packet and len(converter.calls) == 1  # type: ignore[attr-defined]


def test_failed_prepare_leaves_no_files_so_retry_keeps_the_plain_name(tmp_path: Path) -> None:
    truth = candidate()
    repository, stored = _eligible(tmp_path, truth)
    working = fake_pdf()

    def timed_out(docx: Path, pdf: Path) -> None:
        working(docx, pdf)  # Word wrote the PDF, then the export timed out
        raise RuntimeError("Word PDF export timed out")

    failing = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out", pdf_converter=timed_out)
    _, error = failing.prepare(stored, queue_date="2026-09-24")
    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out", pdf_converter=working)
    packet, _ = service.prepare(stored, queue_date="2026-09-24")

    folder = tmp_path / "out" / "2026-09-24" / "News_Corp"
    assert error and packet is not None
    assert sorted(path.name for path in folder.iterdir()) == [
        "Resume_ExampleCandidate.docx",
        "Resume_ExampleCandidate.pdf",
    ]

    # A failed --force retry keeps both the good packet record and its files.
    kept, error = failing.prepare(stored, queue_date="2026-09-24", force=True)
    assert error and kept is not None and kept.status == "ready"
    assert repository.get_latest_ready_packet(stored.id) == packet
    assert Path(packet.resume_pdf_path).exists()  # type: ignore[arg-type]


def test_prepare_drops_keywords_until_one_page(tmp_path: Path) -> None:
    truth = candidate()
    repository, stored = _eligible(tmp_path, truth)

    def pages(docx: Path) -> int:
        return 2 if "Globex" in resume_text(docx) else 1

    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out", pdf_converter=fake_pdf(pages))
    packet, error = service.prepare(stored, queue_date="2026-09-24")

    assert error is None and packet is not None
    text = resume_text(Path(packet.resume_pdf_path.replace(".pdf", ".docx")))  # type: ignore[union-attr]
    assert "Splunk" in text and "Globex" not in text


def test_prepare_refuses_a_job_evaluated_under_stale_eligibility_rules(tmp_path: Path) -> None:
    truth = candidate()
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    stored = repository.add_or_get_job(posting()).job
    repository.upsert_eligibility(
        job_id=stored.id, status="eligible", reasons=["ok"], flags=[], detected_requirements={},
        rules_version="old", rules_fingerprint="stale-fingerprint",
    )
    assert repository.get_eligibility(stored.id, rules_fingerprint(truth)) is None
    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out", pdf_converter=fake_pdf())
    assert service.prepare(stored, queue_date="2026-09-24") == (None, "Job is not eligible for preparation")
    packet, error = service.prepare(stored, queue_date="2026-09-24", master_picked=True)  # Master's own pick
    assert error is None and packet is not None


def test_blank_trailing_pdf_pages_are_dropped(tmp_path: Path) -> None:
    source, destination = tmp_path / "in.pdf", tmp_path / "out.pdf"
    canvas = Canvas(str(source))
    canvas.drawString(72, 720, "Example Candidate")
    canvas.showPage()
    canvas.showPage()  # blank second page, as Word sometimes emits
    canvas.save()
    _write_without_blank_trailing_pages(source, destination)
    assert page_count(source) == 2 and page_count(destination) == 1


def letter_template(tmp_path: Path) -> Path:
    document = Document()
    document.add_paragraph("Example Candidate").runs[0].bold = True
    for text in ("September 22, 2026", "Netrio", "Dear Hiring Team,", "Old body one.", "Old body two.", "Sincerely,"):
        document.add_paragraph(text)
    path = tmp_path / "template.docx"
    document.save(str(path))
    return path


BODY = ["I am applying for the Cyber Security Analyst role. " + "word " * 70] * 3 + ["I finish my MS in December. " + "word " * 20]


class FakeLetters:
    model_identifier = "fake"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def draft(self, job, truth):  # type: ignore[no-untyped-def]
        from jobagent.writing.cover_letter import CoverLetterDraft

        if self.fail:
            raise RuntimeError("claude offline")
        return CoverLetterDraft(paragraphs=BODY, evidence_ids=["fact_1"])


def test_render_cover_letter_fills_template_and_keeps_header_and_signoff(tmp_path: Path) -> None:
    from jobagent.writing.cover_letter import render_cover_letter

    output = tmp_path / "letter.docx"
    render_cover_letter(letter_template(tmp_path), output, company="News Corp", paragraphs=["First.", "Second.", "Third."],
                        today=datetime(2026, 9, 23, tzinfo=UTC))
    texts = [paragraph.text for paragraph in Document(str(output)).paragraphs]
    assert texts == ["Example Candidate", "September 23, 2026", "News Corp", "Dear Hiring Team,", "First.", "Second.", "Third.", "Sincerely,"]
    assert Document(str(output)).paragraphs[0].runs[0].bold


def test_cover_letter_draft_validation() -> None:
    from jobagent.writing.cover_letter import CoverLetterDraft, validate_draft

    truth = candidate()
    assert validate_draft(CoverLetterDraft(paragraphs=BODY, evidence_ids=["fact_1"]), truth) == []
    issues = validate_draft(CoverLetterDraft(paragraphs=["Dear team, short.", "b", "c"], evidence_ids=["made_up"]), truth)
    assert any("Unknown evidence IDs" in issue for issue in issues)
    assert any("words" in issue for issue in issues)
    assert any("Dear " in issue for issue in issues)
    gap = CoverLetterDraft(paragraphs=BODY[:3] + ["I'll be straight about the gap: I haven't run a SIEM. " + "word " * 20], evidence_ids=["fact_1"])
    assert any("shortcoming" in issue for issue in validate_draft(gap, truth))


def test_past_letters_lose_gap_sentences_but_keep_paragraphs() -> None:
    from jobagent.writing.cover_letter import _without_shortcomings

    letter = "I built a pipeline. I'll be straight about the gap: I haven't held a SOC title. It ran daily.\nSecond paragraph."
    assert _without_shortcomings(letter) == "I built a pipeline. It ran daily.\nSecond paragraph."


def test_prepare_writes_resume_and_cover_letter_in_company_folder(tmp_path: Path) -> None:
    truth = candidate()
    repository, stored = _eligible(tmp_path, truth)
    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out",
                                 pdf_converter=fake_pdf(), cover_letter_writer=FakeLetters(),  # type: ignore[arg-type]
                                 cover_letter_template=letter_template(tmp_path))
    packet, error = service.prepare(stored, queue_date="2026-09-24")

    assert error is None and packet is not None and packet.cover_letter_pdf_path
    folder = tmp_path / "out" / "2026-09-24" / "News_Corp"
    assert sorted(path.name for path in folder.iterdir()) == [
        "CoverLetter_ExampleCandidate.docx",
        "CoverLetter_ExampleCandidate.pdf",
        "Resume_ExampleCandidate.docx",
        "Resume_ExampleCandidate.pdf",
    ]


def test_failed_cover_letter_never_blocks_the_resume(tmp_path: Path) -> None:
    truth = candidate()
    repository, stored = _eligible(tmp_path, truth)
    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out",
                                 pdf_converter=fake_pdf(), cover_letter_writer=FakeLetters(fail=True),  # type: ignore[arg-type]
                                 cover_letter_template=letter_template(tmp_path))
    packet, error = service.prepare(stored, queue_date="2026-09-24")
    assert error is None and packet is not None and packet.status == "ready"
    assert packet.resume_pdf_path and packet.cover_letter_pdf_path is None


def test_packet_files_are_named_resume_and_coverletter_with_a_job_id_only_on_a_same_folder_clash(tmp_path: Path) -> None:
    """Master, 2026-11-19: Resume_ExampleCandidate / CoverLetter_ExampleCandidate; the company/date folder keeps them apart."""
    truth = candidate()
    repository, first = _eligible(tmp_path, truth)
    other = posting().model_copy(update={"source_job_id": "job-2", "title": "SOC Analyst II",
                                         "apply_url": "https://example.test/job-2",
                                         "canonical_url": "https://example.test/job-2"})
    second = repository.add_or_get_job(other).job
    result = EligibilityEngine().evaluate(second, truth)
    repository.upsert_eligibility(
        job_id=second.id, status=result.status, reasons=result.reasons, flags=result.flags,
        detected_requirements=result.detected_requirements, rules_version=result.rules_version,
        rules_fingerprint=result.rules_fingerprint,
    )
    service = PreparationService(repository, truth, master=master(tmp_path), output_root=tmp_path / "out",
                                 pdf_converter=fake_pdf())
    a, _ = service.prepare(first, queue_date="2026-11-19")
    b, _ = service.prepare(second, queue_date="2026-11-19")
    assert a is not None and b is not None
    assert Path(a.resume_pdf_path or "").name == "Resume_ExampleCandidate.pdf"
    assert Path(b.resume_pdf_path or "").name == f"Resume_ExampleCandidate_{second.id}.pdf"  # same company, same day
