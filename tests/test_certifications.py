"""Certifications (Master, 2026-11-19): exact name, else Other + typed name, else Security+ flagged; nothing else."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from playwright import sync_api as playwright_sync_api

from jobagent.apply.certifications import FALLBACK_NOTE, choose_certification
from jobagent.apply.fields import FieldDescriptor, decide
from jobagent.apply.profile import ApplicationProfile, CertificationEntry

EXAMPLE = Path(__file__).parent.parent / "candidate" / "application.example.yaml"
NAME = "CompTIA SecAI+"


def _profile(tmp_path: Path | None = None) -> ApplicationProfile:
    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE.read_text()))
    file = ""
    if tmp_path is not None:
        (tmp_path / "cert.pdf").write_bytes(b"%PDF-1.4")
        file = str(tmp_path / "cert.pdf")
    cert = CertificationEntry(name=NAME, number="EXAMPLE0001", exam_date="01/15/2026", issued_date="01/15/2026",
                              expiration_date="01/15/2029", file=file)
    return profile.model_copy(update={"certifications": [cert], "education": [], "work_history": [], "skills": []})


@pytest.mark.parametrize(("options", "expected", "branch"), [
    (["CISSP", "CompTIA SecAI+", "Other"], "CompTIA SecAI+", "exact"),
    (["SecAI+ - CompTIA", "Security+ - CompTIA - CompTia"], "SecAI+ - CompTIA", "exact"),
    (["CISSP", "Security+ - CompTIA - CompTia", "Other (please specify)"], "Other (please specify)", "other"),
    (["CISSP", "Security+ - CompTIA - CompTia", "CySA+ - CompTIA"], "Security+ - CompTIA - CompTia", "security_plus"),
])
def test_master_order_exact_then_other_then_security_plus(options, expected, branch) -> None:  # type: ignore[no-untyped-def]
    choice = choose_certification(options, NAME)
    assert choice is not None and (choice.option, choice.branch) == (expected, branch)


def test_never_any_other_certification() -> None:
    assert choose_certification(["CISSP", "CySA+ - CompTIA", "Security Fundamentals", "Otherwise"], NAME) is None


def test_generic_forms_flag_the_security_plus_fallback_and_leave_the_score_blank() -> None:
    profile = _profile()
    pick = decide(FieldDescriptor("Certification", "select", options=("CISSP", "Security+ - CompTIA")), profile,
                  resume_path=None)
    assert (pick.value, pick.source, pick.reason) == ("Security+ - CompTIA", "certification.fallback", FALLBACK_NOTE)
    assert decide(FieldDescriptor("Certification Number", "text"), profile, resume_path=None).value == "EXAMPLE0001"
    assert decide(FieldDescriptor("Certification Expiration Date", "text"), profile,
                  resume_path=None).value == "01/15/2029"
    assert decide(FieldDescriptor("Certification Exam Score", "text"), profile, resume_path=None).kind == "skip"
    assert decide(FieldDescriptor("Do you hold any certifications?", "select", options=("Yes", "No")), profile,
                  resume_path=None).source != "certification.name"


_SECTION = """<div aria-labelledby="Certifications-section">ENTRIES
<button data-automation-id="add-button" type="button" onclick="addEntry()">Add</button></div>
<div id="popper" data-popper-placement="bottom"></div>
<script>
const OPTIONS = OPTION_LIST;
window.__uploads = 0;
function addEntry() {
  document.querySelector('[aria-labelledby="Certifications-section"]').insertAdjacentHTML("afterbegin",
    '<div data-automation-id="multiSelectContainer"><input id="certifications-1--certification"></div>'
    + '<input id="certifications-1--otherCertification">'
    + '<input id="certifications-1--certificationNumber">'
    + '<input id="certifications-1--issuedDate-dateSectionMonth-input">'
    + '<input id="certifications-1--issuedDate-dateSectionDay-input">'
    + '<input id="certifications-1--issuedDate-dateSectionYear-input">'
    + '<input type="file" id="cert-file" onchange="window.__uploads += 1; this.value = \\'\\'">');
}
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !event.target.id.endsWith("--certification")) return;
  const typed = event.target.value.toLowerCase().replace("comptia ", "");
  const popper = document.getElementById("popper");
  popper.innerHTML = OPTIONS.filter((o) => o.toLowerCase().includes(typed)).map(
    (o) => `<div data-automation-id="promptOption" data-automation-label="${o}">${o}</div>`).join("");
  popper.querySelectorAll("[data-automation-id=promptOption]").forEach((o) => o.onclick = () =>
    event.target.parentElement.insertAdjacentHTML("afterbegin",
      `<div data-automation-id="selectedItem">${o.dataset.automationLabel}</div>`));
});
</script>"""


def _run(html: str, profile: ApplicationProfile, times: int = 1):  # type: ignore[no-untyped-def]
    from jobagent.apply.filler import _workday_certifications

    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            reports = [_workday_certifications(page, profile) for _ in range(times)]
            state = {
                "pills": page.locator('[data-automation-id="selectedItem"]').all_inner_texts(),
                "other": page.input_value("#certifications-1--otherCertification")
                if page.locator("#certifications-1--otherCertification").count() else "",
                "number": page.input_value("#certifications-1--certificationNumber")
                if page.locator("#certifications-1--certificationNumber").count() else "",
                "uploads": page.evaluate("window.__uploads"),
            }
        finally:
            browser.close()
    return reports, state


def _html(options: list[str], entries: str = "") -> str:
    return _SECTION.replace("OPTION_LIST", json.dumps(options)).replace("ENTRIES", entries)


def test_workday_picks_secai_and_attaches_the_certificate_once(tmp_path: Path) -> None:
    reports, state = _run(_html(["SecAI+ - CompTIA", "Security+ - CompTIA - CompTia", "Other"]), _profile(tmp_path), 2)
    assert state["pills"] == ["SecAI+ - CompTIA"] and state["number"] == "EXAMPLE0001"
    assert state["uploads"] == 1  # second pass: the entry is already present, nothing added or attached again
    assert reports[0][0].source == "certification.exact" and reports[1][0].reason.startswith("a certification is")


def test_workday_other_types_the_name(tmp_path: Path) -> None:
    _, state = _run(_html(["Other", "Security+ - CompTIA - CompTia"]), _profile(tmp_path))
    assert state["pills"] == ["Other"] and state["other"] == NAME


def test_workday_security_plus_fallback_is_flagged(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    reports, state = _run(_html(["Security+ - CompTIA - CompTia", "CySA+ - CompTIA"]), _profile(tmp_path))
    assert state["pills"] == ["Security+ - CompTIA - CompTia"]
    assert (reports[0][0].source, reports[0][0].reason) == ("certification.fallback", FALLBACK_NOTE)
    assert FALLBACK_NOTE in capsys.readouterr().out


def test_workday_existing_certification_is_left_alone(tmp_path: Path) -> None:
    existing = ('<div data-automation-id="multiSelectContainer"><div data-automation-id="selectedItem">CISSP</div>'
                '<input id="certifications-1--certification"></div>')
    reports, state = _run(_html(["SecAI+ - CompTIA"], existing), _profile(tmp_path))
    assert state["pills"] == ["CISSP"] and state["uploads"] == 0
    assert reports[0][0].outcome == "skipped"
