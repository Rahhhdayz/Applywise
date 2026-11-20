"""Practice mode: advances only reviewed steps, never clicks anything submit-like, stops for good at Submit."""

from __future__ import annotations

import pytest

from jobagent.apply.practice import SubmitBlocked, guard_click, looks_like_submit

playwright_sync_api = pytest.importorskip("playwright.sync_api")


@pytest.mark.parametrize(("text", "attributes"), [
    ("Submit", {}), ("Submit Application", {}), ("Apply", {}), ("Apply now", {}), ("Finish", {}),
    ("Next", {"data-automation-id": "pageFooterSubmitButton"}), ("Continue", {"aria-label": "Submit application"}),
    ("Next", {"type": "submit"}), ("Next", {"submits_form": "true"}), ("Continue", {"class": "btn-submit"}),
])
def test_submit_like_controls_are_recognised_and_blocked(text: str, attributes: dict[str, str]) -> None:
    assert looks_like_submit(text, attributes)
    with pytest.raises(SubmitBlocked):
        guard_click(text, attributes)


def test_only_exact_next_continue_texts_pass_the_guard() -> None:
    for text in ("Next", "Continue", "Save and Continue"):
        guard_click(text, {"type": "button", "data-automation-id": "pageFooterNextButton", "submits_form": "false"})
    for text in ("Save", "Back", "Delete", "Sign In", "Upload"):
        with pytest.raises(SubmitBlocked):
            guard_click(text, {"type": "button"})


def _chromium_available() -> bool:
    try:
        with playwright_sync_api.sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


browser_only = pytest.mark.skipif(not _chromium_available(), reason="Chromium is not installed for Playwright")

# Records every click anywhere on the page (capture phase), before any handler can stop it.
_RECORDER = """<script>
window.__clicks = []; window.__submitted = false;
document.addEventListener("click", (e) => {
  const b = e.target.closest("button, input, a, [role=button]") || e.target;
  const attributes = {};
  for (const n of ["type", "data-automation-id", "id", "name", "aria-label", "value", "class", "title"]) {
    if (b.getAttribute && b.getAttribute(n)) attributes[n] = b.getAttribute(n);
  }
  attributes.submits_form = String(b.type === "submit" && !!b.form);
  window.__clicks.push({text: (b.innerText || b.value || "").trim(), attributes});
}, true);
document.addEventListener("submit", () => { window.__submitted = true; }, true);
</script>"""

_WIZARD = _RECORDER + """<div id="app"></div><script>
const steps = [
  '<h2>My Information</h2><button type="button" data-automation-id="pageFooterNextButton">Next</button>',
  '<h2>My Experience</h2><button type="button">Back</button>'
    + '<button type="button" data-automation-id="pageFooterNextButton">Save and Continue</button>',
  '<h2>Review</h2><button type="button" data-automation-id="bottom-navigation-next-button">Submit</button>',
];
let at = 0;
function render() {
  document.getElementById("app").innerHTML = steps[at];
  const next = document.querySelector('[data-automation-id="pageFooterNextButton"]');
  if (next) next.onclick = () => { at += 1; render(); };
}
render();
</script>"""


@pytest.fixture
def page():
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser.new_page()
        finally:
            browser.close()


def _clicks(page) -> list[dict]:  # type: ignore[no-untyped-def]
    clicks = page.evaluate("window.__clicks")
    assert not [click for click in clicks if looks_like_submit(click["text"], click["attributes"])], clicks  # a bug if hit
    assert page.evaluate("window.__submitted") is False
    return clicks


@browser_only
def test_practice_walks_the_wizard_and_stops_for_good_at_the_review_page(page, monkeypatch) -> None:
    from jobagent.apply import filler
    from jobagent.apply.ats import ApplicationForm
    from jobagent.apply.oversight import Oversight

    page.set_content(_WIZARD)
    reviewed: list[int] = []
    messages: list[str] = []

    def notify(message: str) -> None:
        messages.append(message)
        page.close()  # Master closes the window after practice stops

    oversight = Oversight(snapshot_root=None, practice=True, notify=notify, reviewer_name="fake")
    oversight.profile = object()  # type: ignore[assignment]
    monkeypatch.setattr(oversight, "after_step", lambda p, f, s, fill: reviewed.append(1) or {"step": len(reviewed), "findings": []})
    clicked: list[str] = []
    original = filler.Oversight.practice_step

    def spy(self, p, snapshot):  # type: ignore[no-untyped-def]
        clicked.extend(c["text"] for c in p.evaluate("window.__clicks")[len(clicked):])
        return original(self, p, snapshot)

    monkeypatch.setattr(filler.Oversight, "practice_step", spy)
    monkeypatch.setattr(filler, "_SETTLE_SECONDS", 0)
    monkeypatch.setattr(filler, "_workday_autofill_upload", lambda *a: None)
    monkeypatch.setattr(filler, "_workday_my_experience", lambda *a: [])
    monkeypatch.setattr(filler, "_fill_page", lambda *a, **k: [])
    form = ApplicationForm(provider="workday", form_url="https://x.test", form_selector="body", resume_input_selector="input", multi_page=True)
    filler._watch_steps(page, form, None, resume_path=None, cover_letter_path=None, oversight=oversight)  # type: ignore[arg-type]
    assert clicked == ["Next", "Save and Continue"]  # never Back, never Submit
    assert len(reviewed) == 3 and oversight.practice is False
    assert "practice stopped: the Review page is open" in messages[0]


@pytest.mark.parametrize("controls", [
    ('<button type="button" data-automation-id="pageFooterNextButton">Next</button>'
     '<button type="button" data-automation-id="bottom-navigation-next-button">Submit</button>'),
    '<form><button>Next</button></form>',  # a <button> in a form submits it
    '<button type="button" aria-label="Submit application">Continue</button>',
    '<input type="submit" value="Next">',
    '<button type="button">Next</button><a role="button" href="#">Apply</a>',
])
@browser_only
def test_any_visible_submit_like_control_stops_practice_without_a_click(page, controls: str) -> None:
    from jobagent.apply.oversight import Oversight

    page.set_content(_RECORDER + controls)
    oversight = Oversight(snapshot_root=None, practice=True)
    oversight.practice_step(page, {"step": 1, "findings": []})
    assert _clicks(page) == [] and oversight.practice is False


@browser_only
def test_practice_needs_a_reviewed_step_and_normal_fills_never_advance(page) -> None:
    from jobagent.apply.oversight import Oversight

    page.set_content(_WIZARD)
    Oversight(snapshot_root=None, practice=False).practice_step(page, {"step": 1, "findings": []})
    unreviewed = Oversight(snapshot_root=None, practice=True)
    unreviewed.practice_step(page, {"step": 1, "review_error": "timed out"})
    assert _clicks(page) == [] and unreviewed.practice is False
    reviewed = Oversight(snapshot_root=None, practice=True)
    reviewed.practice_step(page, {"step": 1, "findings": []})
    assert [click["text"] for click in _clicks(page)] == ["Next"] and reviewed.practice is True


@browser_only
def test_account_step_waits_for_sign_in_instead_of_stopping_practice(page) -> None:
    """Northwind #52001: Workday's Sign In button (a form submit) ended practice before step 1."""
    from jobagent.apply.oversight import Oversight

    page.set_content(_RECORDER + '<h2>Sign In</h2><form><input type="email"><input type="password">'
                     '<button>Sign In</button></form>')
    oversight = Oversight(snapshot_root=None, practice=True)
    oversight.practice_step(page, {"step": 1, "findings": []})
    assert _clicks(page) == [] and oversight.practice is True
