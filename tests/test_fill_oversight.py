"""Snapshots, review, practice mode and runtime oversight around the form filler (headless fixtures only)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

playwright_sync_api = pytest.importorskip("playwright.sync_api")

from jobagent.apply.ats import ApplicationForm
from jobagent.apply.filler import _fill_page
from jobagent.apply.oversight import Oversight
from jobagent.apply.profile import ApplicationProfile
from jobagent.apply.snapshot import load_snapshot, read_fields

FIXTURES = Path(__file__).parent / "fixtures"
PROFILE = ApplicationProfile.model_validate(
    yaml.safe_load((Path(__file__).parent.parent / "candidate" / "application.example.yaml").read_text())
)
FORM = ApplicationForm(provider="test", form_url="https://boards.example.test/apply", form_selector="form",
                       resume_input_selector="input[type=file]")


def _chromium_available() -> bool:
    try:
        with playwright_sync_api.sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium is not installed for Playwright")


@pytest.fixture
def page():
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def test_snapshot_records_every_control_and_what_jobagent_filled(page, tmp_path: Path) -> None:
    page.set_content((FIXTURES / "apply_greenhouse.html").read_text())
    page.fill("#phone", "typed by Master")  # Master's own typing is not JobAgent's
    fill = _fill_page(page, FORM, PROFILE, resume_path=None)
    sources = {item.label: item.source for item in fill}
    assert sources["Email"] == "contact.email"
    assert sources["How did you hear about this position?"] == "answer:how did you hear"

    oversight = Oversight(job_id=7, snapshot_root=tmp_path)
    oversight.after_step(page, FORM, "sig", fill)
    snap = load_snapshot(7, root=tmp_path)
    fields = {item["label"]: item for item in snap["fields"]}
    assert fields["Email"]["value"] == "example.candidate@example.test"
    assert fields["Email"]["filled_by_jobagent"] is True and fields["Email"]["source"] == "contact.email"
    assert fields["Phone"]["filled_by_jobagent"] is False  # left as Master typed it
    assert fields["Are you authorized to work in the US?"]["options"] == ["Yes", "No"]
    assert fields["Are you authorized to work in the US?"]["value"] == "Yes"
    assert fields["How did you hear about this position?"]["value"] == "Company website"
    assert fields["Why do you want to work here?"]["filled_by_jobagent"] is False
    assert (tmp_path / "7" / "01.png").is_file()
    assert snap["fill"] and snap["step"] == 1
    oversight.after_step(page, FORM, "sig2", [])
    assert load_snapshot(7, root=tmp_path)["step"] == 2  # steps continue, never overwrite


def test_snapshot_script_only_reads_the_page(page) -> None:
    page.set_content((FIXTURES / "apply_greenhouse.html").read_text())
    before = page.content()
    read_fields(page, "form")
    assert page.content() == before


def test_watcher_hands_each_settled_step_to_oversight_once(monkeypatch) -> None:
    from jobagent.apply import filler

    steps = iter(["step-1"] * 6 + ["step-2"] * 6)
    clock = iter(range(0, 1000, 2))

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list = []

    class FakePage:
        context = FakeContext()
        on_step = "step-1"

        def is_closed(self) -> bool:
            return False

        def wait_for_timeout(self, _ms: int) -> None:
            pass

        def evaluate(self, _script: str) -> str:
            try:
                self.on_step = next(steps)
            except StopIteration:
                self.context.pages = []
            return self.on_step

    seen: list[tuple[str, int]] = []

    class Spy(Oversight):
        def after_step(self, page, form, signature, fill):  # type: ignore[override]
            seen.append((signature, len(fill)))

    fake = FakePage()
    fake.context.pages = [fake]
    monkeypatch.setattr(filler.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(filler, "_workday_autofill_upload", lambda *a: None)
    monkeypatch.setattr(filler, "_workday_my_experience", lambda *a: [])
    monkeypatch.setattr(filler, "_fill_page", lambda *a, **k: [filler.FilledField("x", "text", "filled")])
    form = ApplicationForm(provider="workday", form_url="https://x.test", form_selector="body", resume_input_selector="input", multi_page=True)
    filler._watch_steps(fake, form, None, resume_path=None, cover_letter_path=None, oversight=Spy())  # type: ignore[arg-type]
    assert [signature for signature, _ in seen] == ["step-1", "step-2"]
    assert all(count > 0 for _, count in seen)


def test_search_box_with_no_options_never_gets_a_yes_no_answer_typed(page) -> None:
    page.set_content('<form><label for="r">Are you willing to relocate?</label>'
                     '<input id="r" name="relocate" role="combobox" aria-autocomplete="list"></form>')
    fill = _fill_page(page, FORM, PROFILE, resume_path=None)
    assert page.input_value("#r") == ""
    assert fill[0].outcome == "skipped" and "only contact and education" in fill[0].reason


def test_one_work_history_entry_timing_out_does_not_skip_the_next(page, monkeypatch) -> None:
    from playwright.sync_api import Error as PlaywrightError

    from jobagent.apply import filler
    from jobagent.apply.profile import WorkEntry

    entry = '<input id="{p}--jobTitle"><input id="{p}--companyName"><input id="{p}--location"><textarea id="{p}--roleDescription"></textarea>'
    page.set_content('<div aria-labelledby="Work-Experience-section">' + entry.format(p="w1") + entry.format(p="w2") + "</div>")
    jobs = [WorkEntry(title=f"Title {n}", company=f"Co {n}", location="", start="2024-01", end=None, description="Did things")
            for n in (1, 2)]
    calls = []

    def flaky_date(*args):  # type: ignore[no-untyped-def]
        calls.append(args)
        if len(calls) == 1:
            raise PlaywrightError("Timeout 5000ms exceeded")

    monkeypatch.setattr(filler, "_date", flaky_date)
    filler._workday_my_experience(page, PROFILE.model_copy(update={"work_history": jobs}), None)
    assert page.input_value("#w1--jobTitle") == "Title 1"
    assert page.input_value("#w2--jobTitle") == "Title 2" and page.input_value("#w2--roleDescription") == "Did things"


def _finding(label: str, problem: str, correct: str, fill: list, page, **extra):  # type: ignore[no-untyped-def]
    from jobagent.apply.snapshot import read_fields

    field = next(item for item in read_fields(page, "form") if item["label"] == label)
    return {"label": label, "problem": problem, "current_value": field["value"], "correct_value": correct, "reason": "",
            "field": field["field"], "kind": field["kind"], "options": field["options"], "source": field["source"],
            "filled_by_jobagent": field["filled_by_jobagent"], "fact": "", **extra}


def test_runtime_review_runs_in_the_background_and_shows_a_panel(page, tmp_path: Path) -> None:
    import threading

    page.set_content((FIXTURES / "apply_greenhouse.html").read_text())
    fill = _fill_page(page, FORM, PROFILE, resume_path=None)
    release = threading.Event()
    sent: list[str] = []

    finding = _finding("Email", "wrong_value", "example.candidate@example.test", fill, page)

    def reviewer(snapshot, profile):  # type: ignore[no-untyped-def]  # runs in a thread: no Playwright here
        release.wait(5)
        return [finding]

    oversight = Oversight(job_id=3, job_label="#3 Acme", snapshot_root=tmp_path, profile=PROFILE, reviewer=reviewer,
                          notify=sent.append, notify_findings=True)
    oversight.after_step(page, FORM, "sig", fill)  # returns at once: the review runs in a thread
    assert oversight._pending is not None
    oversight.poll(page, "sig")
    assert page.locator("#jobagent-oversight").count() == 0  # not done yet
    release.set()
    oversight._pending[0].result()
    oversight.poll(page, "sig")
    assert "JobAgent review: 1 finding" in page.inner_text("#jobagent-oversight")
    assert "solid" in page.eval_on_selector("#email", "e => e.style.outline")
    assert sent and sent[0].startswith("JobAgent review #3 Acme step 1")
    assert (tmp_path / "3" / "01.review.claude.json").is_file()


def test_review_failure_never_blocks_and_a_moved_on_step_gets_no_panel(page, tmp_path: Path) -> None:
    page.set_content((FIXTURES / "apply_greenhouse.html").read_text())

    def broken(snapshot, profile):  # type: ignore[no-untyped-def]
        raise RuntimeError("Claude fill review timed out")

    oversight = Oversight(job_id=4, snapshot_root=tmp_path, profile=PROFILE, reviewer=broken)
    snapshot = oversight.after_step(page, FORM, "sig", [])
    oversight.close()
    assert "timed out" in snapshot["review_error"]

    moved = Oversight(job_id=5, snapshot_root=tmp_path, profile=PROFILE,
                      reviewer=lambda s, p: [{"label": "Email", "problem": "should_be_empty", "current_value": "",
                                              "correct_value": "", "reason": "", "field": "1", "kind": "text",
                                              "options": [], "source": "", "filled_by_jobagent": True, "fact": ""}])
    moved.after_step(page, FORM, "sig", [])
    moved._pending[0].result()
    moved.poll(page, "")  # Master already clicked Continue: different step
    assert page.locator("#jobagent-oversight").count() == 0


def test_oversight_fix_corrects_only_jobagent_filled_choice_fields_to_allowed_values(page, tmp_path: Path) -> None:
    page.set_content((FIXTURES / "apply_greenhouse.html").read_text())
    fill = _fill_page(page, FORM, PROFILE, resume_path=None)
    page.select_option("#how_heard", label="Referral")  # pretend Master chose this himself
    page.evaluate("document.querySelector('#how_heard').removeAttribute('data-jobagent-filled')")
    findings = [
        _finding("Are you authorized to work in the US?", "wrong_value", "No", fill, page),
        _finding("Are you authorized to work in the US?", "wrong_value", "Maybe", fill, page),  # not allowed
        _finding("Email", "wrong_value", "example.candidate@example.test", fill, page),  # text: never retyped
        _finding("How did you hear about this position?", "wrong_value", "Company website", fill, page),  # Master's
    ]
    for fix, expected in ((False, "yes"), (True, "no")):
        page.check('input[name="authorized"][value="yes"]')
        oversight = Oversight(job_id=6, snapshot_root=tmp_path, profile=PROFILE, fix=fix)
        oversight._finish(page, {"step": 1}, tmp_path, findings)
        assert page.eval_on_selector('input[name="authorized"]:checked', "e => e.value") == expected
    assert page.eval_on_selector("#how_heard", "e => e.selectedOptions[0].textContent") == "Referral"
    assert [item.source for item in oversight.extra_fields] == ["oversight_fix"]
    assert page.evaluate("window.__submitted === true") is False


def test_cli_review_switches(monkeypatch, tmp_path: Path) -> None:
    import argparse

    from jobagent import cli

    job = type("Job", (), {"id": 1, "company": "Acme", "title": "Analyst", "description_text": ""})()
    args = argparse.Namespace(apply_command="fill", no_ai_answers=True, no_review=False, review_model=None,
                              practice=False, notify=False, oversight_fix=False)
    assert cli._oversight_for(job, args).reviewer is not None
    monkeypatch.setenv("JOBAGENT_FILL_REVIEW", "off")
    assert cli._oversight_for(job, args).reviewer is None
    assert cli.main(["--database", str(tmp_path / "t.sqlite3"), "apply", "fill", "--job-id", "1", "--practice"]) == 1


def test_snapshot_redacts_passwords_codes_and_tokens_before_writing(page, tmp_path: Path, monkeypatch) -> None:
    """Northwind #52001: the Workday password was captured as a field value and sent to the reviewer."""
    monkeypatch.setenv("WORKDAY_PASSWORD", "Hunter2!pw")
    page.set_content("""<form>
      <label for="a">Password</label><input id="a" type="password" value="Hunter2!pw">
      <label for="b">Verify New Password</label><input id="b" type="password" value="Hunter2!pw">
      <label for="c">Account PIN</label><input id="c" value="4821">
      <label for="d">Enter the security code</label><input id="d" value="48213907">
      <input aria-label="code 1" maxlength="1" value="4">
      <label for="e">Notes</label><textarea id="e">my pw is Hunter2!pw</textarea>
      <label for="f">Email</label><input id="f" value="example.candidate@example.test">
    </form>""")
    Oversight(job_id=8, snapshot_root=tmp_path).after_step(page, FORM, "sig", [])
    written = (tmp_path / "8" / "01.json").read_text()
    assert "Hunter2!pw" not in written and "4821" not in written and "48213907" not in written
    values = [item["value"] for item in load_snapshot(8, root=tmp_path)["fields"]]
    assert values[:5] == ["[redacted]"] * 5 and values[-1] == "example.candidate@example.test"
    assert values[5] == "my pw is [redacted]"


def test_snapshot_url_hides_activation_tokens() -> None:
    from jobagent.apply.snapshot import _TOKEN_PATH

    url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/t0k3n?x=1"
    assert _TOKEN_PATH.sub(r"\1[redacted]", url) == "https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/[redacted]?x=1"
