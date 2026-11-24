"""Unattended fill (Telegram /fill): practice to the final step, an exact "Save for Later" only, never Submit."""

from __future__ import annotations

import time

import pytest

import tests.test_fill_practice as practice_tests
from jobagent.apply.filler import FilledField, FillReport
from jobagent.apply.oversight import Oversight, unattended_summary
from jobagent.apply.practice import SubmitBlocked, candidate_home_url, guard_save_click
from tests.test_fill_practice import _RECORDER, _clicks, browser_only

page = practice_tests.page  # the shared headless page fixture


def test_save_for_later_is_exact_text_only_and_never_a_submit() -> None:
    guard_save_click("Save for Later", {"type": "button", "submits_form": "false"})
    guard_save_click("  save FOR later ", {"type": "button"})
    for text, attributes in [("Save", {}), ("Save Draft", {}), ("Save for later and submit", {}), ("Save and Continue", {}),
                             ("Submit", {}), ("Apply", {}), ("Review and Submit", {}), ("Finish", {}),
                             ("Save for Later", {"type": "submit"}), ("Save for Later", {"submits_form": "true"}),
                             ("Save for Later", {"data-automation-id": "pageFooterSubmitButton"})]:
        with pytest.raises(SubmitBlocked):
            guard_save_click(text, attributes)


def test_candidate_home_url_keeps_the_career_site_and_rejects_other_hosts() -> None:
    assert candidate_home_url("https://cyber.wd12.myworkdayjobs.com/en-US/CyberdyneCareers/job/Remote/Analyst_R1?a=1") == \
        "https://cyber.wd12.myworkdayjobs.com/en-US/CyberdyneCareers/userHome"
    assert candidate_home_url("https://acme.wd1.myworkdayjobs.com/External/job/Boston/Engineer_R2") == \
        "https://acme.wd1.myworkdayjobs.com/External/userHome"
    for url in ("https://evilmyworkdayjobs.com/External/job/x", "http://acme.wd1.myworkdayjobs.com/External/job/x",
                "https://boards.greenhouse.io/acme/jobs/1", "https://acme.wd1.myworkdayjobs.com/"):
        assert candidate_home_url(url) is None


_FINAL = _RECORDER + """<div id="app"></div><script>
const steps = [
  '<h2>My Information</h2><button type="button" data-automation-id="pageFooterNextButton">Next</button>',
  '<h2>My Experience</h2><button type="button">Back</button>'
    + '<button type="button" data-automation-id="pageFooterNextButton">Save and Continue</button>',
  '<h2>Review</h2><button type="button">Save for Later</button>'
    + '<button type="button" data-automation-id="bottom-navigation-next-button">Submit</button>',
];
let at = 0;
function render() {
  document.getElementById("app").innerHTML = steps[at];
  const next = document.querySelector('[data-automation-id="pageFooterNextButton"]');
  if (next) next.onclick = () => { at += 1; render(); };
}
render();
</script>"""


def _fake_steps(monkeypatch, oversight: Oversight) -> None:  # type: ignore[no-untyped-def]
    from jobagent.apply import filler

    reviewed: list[int] = []
    monkeypatch.setattr(oversight, "after_step", lambda p, f, s, fill: reviewed.append(1) or {"step": len(reviewed), "findings": []})
    monkeypatch.setattr(filler, "_SETTLE_SECONDS", 0)
    monkeypatch.setattr(filler, "_workday_autofill_upload", lambda *a: None)
    monkeypatch.setattr(filler, "_workday_my_experience", lambda *a: [])
    monkeypatch.setattr(filler, "_fill_page", lambda *a, **k: [])


_FORM_KW = {"provider": "workday", "form_url": "https://x.test", "form_selector": "body", "resume_input_selector": "input",
            "multi_page": True}


@browser_only
def test_unattended_walks_to_review_clicks_save_for_later_and_ends_by_itself(page, monkeypatch) -> None:
    from jobagent.apply import filler
    from jobagent.apply.ats import ApplicationForm

    page.set_content(_FINAL)
    messages: list[str] = []
    oversight = Oversight(snapshot_root=None, practice=True, unattended=True, notify=messages.append, reviewer_name="fake")
    _fake_steps(monkeypatch, oversight)
    filler._watch_steps(page, ApplicationForm(**_FORM_KW), None, resume_path=None, cover_letter_path=None,  # type: ignore[arg-type]
                        oversight=oversight, deadline=time.monotonic() + 60)
    assert [click["text"] for click in _clicks(page)] == ["Next", "Save and Continue", "Save for Later"]  # never Back/Submit
    assert oversight.at_final and oversight.saved_for_later and not page.is_closed()  # ended without Master closing it
    assert oversight.finished == "the Review page is open; clicked Save for Later" and messages == []


@pytest.mark.parametrize("controls", [
    '<button type="button">Next</button><button type="button">Submit</button>',
    '<button type="button">Next</button><a role="button" href="#">Apply</a>',
    '<button type="button">Review and Submit</button>',
    '<button type="button">Finish</button>',
    '<input type="submit" value="Next">',
    '<form><button>Continue</button></form>',
    '<button type="button">Submit</button><form><button>Save for Later</button></form>',  # a form submit: not clicked
    '<button type="button">Submit</button><button type="button">Save draft for later</button>',  # not the exact text
])
@browser_only
def test_unattended_final_step_never_clicks_a_submit_like_control(page, controls: str) -> None:
    page.set_content(_RECORDER + controls)
    oversight = Oversight(snapshot_root=None, practice=True, unattended=True)
    oversight.practice_step(page, {"step": 1, "findings": []})
    assert _clicks(page) == [] and oversight.finished and oversight.saved_for_later is False


@browser_only
def test_unattended_deadline_ends_the_watch_and_pings(page, monkeypatch) -> None:
    from jobagent.apply import filler
    from jobagent.apply.ats import ApplicationForm

    page.set_content(_RECORDER + "<h2>Questions</h2><p>no buttons</p>")
    messages: list[str] = []
    oversight = Oversight(snapshot_root=None, practice=True, unattended=True, notify=messages.append, job_label="#7 Acme")
    _fake_steps(monkeypatch, oversight)
    filler._watch_steps(page, ApplicationForm(**_FORM_KW), None, resume_path=None, cover_letter_path=None,  # type: ignore[arg-type]
                        oversight=oversight, deadline=time.monotonic() + 3)
    assert oversight.finished == "timed out on step 1" and _clicks(page) == []
    assert messages == ["needs you: #7 Acme unattended fill timed out on step 1; the form is open on your Mac"]


@browser_only
def test_draft_is_confirmed_only_when_the_candidate_home_lists_the_job() -> None:
    from playwright.sync_api import sync_playwright

    from jobagent.apply.practice import draft_listed

    home = "https://acme.wd1.myworkdayjobs.com/External/userHome"
    opened: list[str] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        context.route("**/*", lambda route: (opened.append(route.request.url), route.fulfill(
            content_type="text/html", body="<h1>My Applications</h1><p>Security Engineer - In Progress</p>"))[-1])
        assert draft_listed(context, home, "Security Engineer")
        assert not draft_listed(context, home, "Data Scientist")
        assert not draft_listed(context, "https://evil.test/userHome", "Security Engineer")
        browser.close()
    assert set(opened) == {home}


def test_finish_unattended_links_and_status(monkeypatch) -> None:
    from jobagent.apply import filler
    from jobagent.apply.ats import ApplicationForm

    form = ApplicationForm(**(_FORM_KW | {"form_url": "https://acme.wd1.myworkdayjobs.com/External/job/B/E_R1"}))
    report = FillReport(job_id=7, form_url=form.form_url, fields=[])
    monkeypatch.setattr(filler, "draft_listed", lambda context, home, title: title == "Security Engineer")
    reached = Oversight(snapshot_root=None, unattended=True, job_title="Security Engineer", finished="review", at_final=True)
    done = filler._finish_unattended(None, form, reached, report)  # type: ignore[arg-type]
    assert done.draft == "confirmed" and done.links == ("https://acme.wd1.myworkdayjobs.com/External/userHome", form.form_url)
    stuck = Oversight(snapshot_root=None, unattended=True, job_title="Security Engineer", finished="timed out on step 2")
    assert filler._finish_unattended(None, form, stuck, report).draft == "not confirmed"  # type: ignore[arg-type]
    greenhouse = ApplicationForm(provider="greenhouse", form_url="https://boards.greenhouse.io/a/jobs/1", form_selector="form",
                                 resume_input_selector="input")
    single = filler._finish_unattended(None, greenhouse, reached, report)  # type: ignore[arg-type]
    assert single.draft == "" and single.links == ()


def test_summary_lists_what_to_review_and_never_leaks_secrets(monkeypatch) -> None:
    monkeypatch.setenv("WORKDAY_PASSWORD", "Hunter2!secret")
    fields = [
        FilledField("Email", "text", "filled", value="m@example.test", source="contact.email", step=1),
        FilledField("Password", "password", "filled", value="Hunter2!secret", source="workday_account", step=1),
        FilledField("Why us?", "textarea", "filled", value="I like securing systems " * 3 + "Hunter2!secret", source="ai_draft", step=3),
        FilledField("Salary", "text", "required_empty", step=3),
    ]
    oversight = Oversight(snapshot_root=None, unattended=True, job_label="#7 Acme Security Engineer")
    oversight.snapshots = [{"step": 3, "findings": [{"field": "4", "label": "Salary", "problem": "missing_truth", "kind": "text",
                                                      "correct_value": "", "filled_by_jobagent": False, "options": [],
                                                      "current_value": "", "reason": "no salary on file"},
                                                     {"field": "5", "label": "Verify", "problem": "wrong_value", "kind": "text",
                                                      "correct_value": "", "filled_by_jobagent": True, "options": [],
                                                      "current_value": "Hunter2!secret", "reason": "a leak"}]}]
    confirmed = FillReport(7, "https://acme.wd1.myworkdayjobs.com/External/job/x", fields, finished="the Review page is open",
                           draft="confirmed", links=("https://acme.wd1.myworkdayjobs.com/External/userHome",))
    text = unattended_summary(confirmed, oversight, "workday")
    assert "Draft saved on Workday" in text and "External/userHome" in text and "Steps: 3; filled 3, skipped 0, required empty 1" in text
    assert "Required, still empty: Salary" in text and "AI-drafted answers" in text and "Why us?" in text
    assert "Salary" in text.split("Review, step 3")[1] and "never submits" in text
    assert "Hunter2!secret" not in text and "m@example.test" not in text
    single = unattended_summary(FillReport(7, "https://boards.greenhouse.io/a/jobs/1", fields[:1], finished="single-page"),
                                oversight, "greenhouse")
    assert "No draft support on greenhouse: the filled form is open on your Mac" in single and "http" not in single.split("Stopped")[0]
