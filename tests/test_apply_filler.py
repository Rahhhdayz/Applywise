"""Tests for the Playwright-driven form filler using in-memory HTML fixtures.

Each fixture form is loaded via ``page.set_content`` (no network request) and
wires its submit handler to set ``window.__submitted = true``, so these tests
can assert that nothing was ever submitted. Skipped entirely if Playwright or
its Chromium browser is not installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

playwright_sync_api = pytest.importorskip("playwright.sync_api")

from jobagent.apply.ats import ApplicationForm
from jobagent.apply.filler import _fill_page
from jobagent.apply.profile import ApplicationProfile

EXAMPLE_PROFILE = Path(__file__).parent.parent / "candidate" / "application.example.yaml"

FIXTURES = Path(__file__).parent / "fixtures"


def _chromium_available() -> bool:
    try:
        with playwright_sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            browser.close()
        return True
    except Exception:  # noqa: BLE001 - any launch failure means "not installed here"
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(), reason="Chromium is not installed for Playwright")

_FORM = ApplicationForm(
    provider="test", form_url="about:blank", form_selector="form", resume_input_selector="input[type=file]"
)

_EXTRACT_VALUES_SCRIPT = """
() => {
    const form = document.querySelector('form');
    const data = {};
    for (const el of form.querySelectorAll('input, select, textarea')) {
        if (el.type === 'file') { data[el.name || el.id] = el.files.length > 0 ? el.files[0].name : null; continue; }
        if (el.type === 'radio') { if (el.checked) data[el.name] = el.value; continue; }
        data[el.name || el.id] = el.value;
    }
    return data;
}
"""


def _load_and_fill(fixture_name: str, *, resume_path: Path | None):
    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text()))
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content((FIXTURES / fixture_name).read_text())
            fields = _fill_page(page, _FORM, profile, resume_path=resume_path)
            submitted = page.evaluate("window.__submitted === true")
            values = page.evaluate(_EXTRACT_VALUES_SCRIPT)
        finally:
            browser.close()
    return fields, submitted, values


def test_greenhouse_style_form_is_filled_without_submitting(tmp_path: Path) -> None:
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF fake resume")

    fields, submitted, values = _load_and_fill("apply_greenhouse.html", resume_path=resume)

    assert submitted is False
    assert values["first_name"] == "Example"
    assert values["last_name"] == "Candidate"
    assert values["email"] == "example.candidate@example.test"
    assert values["authorized"] == "yes"
    assert values["how_heard"] == "website"
    assert values["resume"] == "resume.pdf"
    assert values["cover_note"] == ""  # custom free-text question left for the human

    outcomes = {item.label.strip(): item.outcome for item in fields}
    assert outcomes["Why do you want to work here?"] == "skipped"
    assert all(item.outcome != "required_empty" for item in fields)


def test_lever_style_form_with_wrapping_labels_is_filled_and_resume_is_flagged_required() -> None:
    fields, submitted, values = _load_and_fill("apply_lever.html", resume_path=None)

    assert submitted is False
    assert values.get("firstName") == "Example"
    assert values.get("lastName") == "Candidate"
    assert values.get("email") == "example.candidate@example.test"
    assert values.get("urls[LinkedIn]") == "https://www.linkedin.com/in/example-candidate"
    assert values.get("relocate") == "yes"
    assert values.get("resume") is None  # no resume path supplied in this test

    outcomes = {item.label.strip(): item.outcome for item in fields}
    assert outcomes["Resume"] == "required_empty"


def test_ashby_style_form_with_aria_labels_declines_eeo_question() -> None:
    fields, submitted, values = _load_and_fill("apply_ashby.html", resume_path=None)

    assert submitted is False
    assert values.get("firstName") == "Example"
    assert values.get("github") == "https://github.com/example-candidate"
    assert values.get("gender") == "decline"

    outcomes = {item.label.strip(): item.outcome for item in fields}
    assert outcomes["Cover Letter"] == "skipped"


def test_step_watcher_survives_navigation_errors_and_ends_only_when_window_closes(monkeypatch) -> None:
    from playwright.sync_api import Error as PlaywrightError

    from jobagent.apply import filler

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list[FakePage] = []

    class FakePage:
        context = FakeContext()
        checks = 0
        closed = False

        def is_closed(self) -> bool:
            return self.closed

        def wait_for_timeout(self, _ms: int) -> None:
            pass

        def evaluate(self, _script: str) -> str:
            self.checks += 1
            if self.checks in (2, 3):  # Workday navigates mid-check (sign-in / Save and Continue)
                raise PlaywrightError("Execution context was destroyed")
            if self.checks == 5:
                self.closed = True  # Master closes the window
            return "step-1"

    page = FakePage()
    page.context.pages = [page]
    monkeypatch.setattr(filler.time, "sleep", lambda _s: None)
    monkeypatch.setattr(filler, "_workday_my_experience", lambda *a: [])
    monkeypatch.setattr(filler, "_fill_page", lambda *a, **k: [])
    form = ApplicationForm(provider="workday", form_url="https://x.test", form_selector="body", resume_input_selector="input", multi_page=True)

    assert filler._watch_steps(page, form, None, resume_path=None, cover_letter_path=None) == []  # type: ignore[arg-type]
    assert page.checks == 5  # kept watching through both navigation errors


def test_fill_never_clicks_continue_next_or_submit_and_keeps_typed_values() -> None:
    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text()))
    buttons = "".join(
        f'<button type="button" data-automation-id="{auto}" onclick="window.__clicked.push(this.innerText)">{label}</button>'
        for auto, label in [("pageFooterNextButton", "Save and Continue"), ("bottom-navigation-next-button", "Next"),
                            ("continue", "Continue"), ("submit", "Submit")]
    )
    html = (
        '<form onsubmit="window.__submitted = true; return false">'
        '<label for="fn">First Name</label><input id="fn" name="first_name">'
        '<label for="ln">Last Name</label><input id="ln" name="last_name" value="Typed By Master">'
        f'{buttons}<input type="submit" value="Submit" onclick="window.__clicked.push(\'input-submit\')"></form>'
    )
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            page.evaluate("window.__clicked = []; window.__submitted = false")
            _fill_page(page, _FORM, profile, resume_path=None)
            assert page.evaluate("window.__clicked") == []  # no Continue / Next / Save and Continue / Submit
            assert page.evaluate("window.__submitted") is False
            assert page.input_value("#fn") == "Example"  # empty field filled
            assert page.input_value("#ln") == "Typed By Master"  # what Master typed is never overwritten
        finally:
            browser.close()


def test_step_watcher_fills_each_new_step_master_moves_to(monkeypatch) -> None:
    from jobagent.apply import filler

    steps = iter(["step-1"] * 3 + ["step-2"] * 3)

    class FakeContext:
        def __init__(self) -> None:
            self.pages: list[FakePage] = []

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
                self.context.pages = []  # Master closes the window
            return self.on_step

    page = FakePage()
    page.context.pages = [page]
    filled: list[str] = []
    monkeypatch.setattr(filler, "_workday_autofill_upload", lambda *a: None)
    monkeypatch.setattr(filler, "_workday_my_experience", lambda *a: [])
    monkeypatch.setattr(filler, "_fill_page", lambda p, *a, **k: filled.append(p.on_step) or [])
    form = ApplicationForm(provider="workday", form_url="https://x.test", form_selector="body", resume_input_selector="input", multi_page=True)

    filler._watch_steps(page, form, None, resume_path=None, cover_letter_path=None)  # type: ignore[arg-type]
    assert "step-1" in filled and "step-2" in filled  # the step Master clicked Continue to was filled too
    assert not hasattr(filler, "_workday_next")  # the auto-advance path is gone


def test_skill_label_matching_accepts_exact_parenthesized_and_acronym_forms_only() -> None:
    from jobagent.apply.filler import _match_label

    labels = ["Splunk Administration", "Domain Name System (DNS)", "Python (Programming Language)", "Jira"]
    assert _match_label(labels, "jira", prefix_ok=True) == 3
    assert _match_label(labels, "DNS", prefix_ok=True) == 1
    assert _match_label(labels, "Python", prefix_ok=True) == 2
    assert _match_label(labels, "Splunk", prefix_ok=True) is None  # never a different, related skill
    assert _match_label(labels, "Python", prefix_ok=False) is None  # schools/fields need exact names


def test_skill_aliases_never_match_an_unrelated_parenthetical() -> None:
    """Northwind #52001: aliases picked "Microsoft Remote Desktop Services (RDS)" for Amazon RDS, "Unity 3D - Entity
    Component System (ECS)" for ECS Fargate, "ETL (Informatica)" for ETL/ELT, and Orca/Nokia/EPSS look-alikes."""
    from jobagent.apply.filler import _match_label

    picked = ["Microsoft Remote Desktop Services (RDS)", "Unity 3D - Entity Component System (ECS)",
              "Electronic Performance Support Systems (EPSS)", "ETL (Informatica)",
              "Nokia Service Quality Manager (SQM)", "Orca Cloud Security Posture Management (CSPM)"]
    for alias in ("RDS", "ECS", "EPSS", "ETL", "SQM"):
        assert _match_label(picked, alias, prefix_ok=True, canonical=False) is None, alias
    assert _match_label(picked, "CSPM", prefix_ok=True) is None  # canonical, but "Orca ..." doesn't spell CSPM
    assert _match_label(["Amazon RDS"], "RDS", prefix_ok=True, canonical=False) is None
    assert _match_label(["Cloud Security Posture Management (CSPM)"], "CSPM", prefix_ok=True) == 0


def test_workday_education_corrects_prefilled_entries_in_place() -> None:
    from jobagent.apply.filler import _workday_my_experience
    from jobagent.apply.profile import EducationEntry

    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"skills": [], "education": [
        EducationEntry(school="Riverside University", degree="Masters", gpa="3.5"),
        EducationEntry(school="Lakeside University", degree="Bachelors", gpa="3.4"),
    ]})
    entry = '<input id="education-{0}--schoolName" value="Riverside University"><input id="education-{0}--gradeAverage">'
    html = (
        '<div aria-labelledby="Education-section">' + entry.format(1) + entry.format(2)
        + '<button data-automation-id="add-button" onclick="window.__added = true">Add</button></div>'
    )
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            _workday_my_experience(page, profile, None)
            assert page.input_value("#education-1--schoolName") == "Riverside University"
            assert page.input_value("#education-2--schoolName") == "Lakeside University"  # resume parse said Riverside
            assert page.input_value("#education-2--gradeAverage") == "3.4"
            assert page.evaluate("window.__added !== true")  # corrected in place, nothing duplicated
        finally:
            browser.close()


def test_autocomplete_types_the_first_part_and_clicks_the_exact_option() -> None:
    from jobagent.apply.filler import _autocomplete

    html = """<input id="loc" role="combobox" aria-autocomplete="list"><div id="list"></div>
    <script>
      document.getElementById('loc').addEventListener('input', e => {
        const names = ['Boston, Lincolnshire, England', 'Boston, Massachusetts, United States'];
        document.getElementById('list').innerHTML = names.map(n =>
          `<div role="option" onclick="document.getElementById('loc').value=this.innerText">${n}</div>`).join('');
      });
    </script>"""
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            _autocomplete(page, page.locator("#loc"), "Boston, Massachusetts, United States")
            assert page.input_value("#loc") == "Boston, Massachusetts, United States"  # not the Lincolnshire one
        finally:
            browser.close()


def test_lever_custom_question_cards_supply_the_label() -> None:
    from jobagent.apply.filler import _DESCRIPTOR_SCRIPT

    card = (
        '<li class="application-question custom-question"><div class="application-label"><div class="text">{q}</div></div>'
        '<div class="application-field">{control}</div></li>'
    )
    radios = ''.join(f'<label><input type="radio" name="cards[a][field0]" value="{v}">{v}</label>' for v in ("Yes", "No"))
    html = "<form><ul>" + card.format(q="Are you currently eligible to work in the United States?", control=radios) + card.format(
        q="If yes, name who referred you", control='<textarea name="cards[b][field0]"></textarea>') + "</ul></form>"
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            found = {raw["kind"]: raw for raw in page.evaluate(_DESCRIPTOR_SCRIPT, "form")}
            assert found["radio"]["label"] == "Are you currently eligible to work in the United States?"
            assert found["radio"]["options"] == ["Yes", "No"]  # options keep their own labels
            assert found["textarea"]["label"] == "If yes, name who referred you"
        finally:
            browser.close()


def test_pick_waits_for_a_slow_workday_list() -> None:
    from jobagent.apply.filler import _pick

    html = """<button id="degree" type="button">Select One</button><div id="list"></div>
    <script>
      const button = document.getElementById('degree');
      button.addEventListener('click', () => setTimeout(() => {
        document.getElementById('list').innerHTML = '<div role="option">Bachelors</div><div role="option">Masters</div>';
        document.querySelectorAll('[role=option]').forEach(o => o.addEventListener('click', () => { button.innerText = o.innerText; }));
      }, 3000));
    </script>"""
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            _pick(page, "#degree", "Masters")
            assert page.inner_text("#degree") == "Masters"  # the old 2.5 s wait gave up on this list
        finally:
            browser.close()


def test_city_search_box_picks_the_city_in_the_candidates_state() -> None:
    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"contact": profile.contact.model_copy(update={"state": "Massachusetts"})})
    html = """<form><label for="city">Location (City)</label>
    <input id="city" name="city" role="combobox" aria-expanded="false"><div id="list"></div></form>
    <script>
      const box = document.getElementById('city');
      box.addEventListener('input', () => {
        const names = ['Boston, England, United Kingdom', 'Boston, Massachusetts, United States'];
        document.getElementById('list').innerHTML = names.map(n => `<div role="option">${n}</div>`).join('');
        document.querySelectorAll('[role=option]').forEach(o => o.addEventListener('click', () => { box.value = o.innerText; }));
      });
    </script>"""
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            _fill_page(page, _FORM, profile, resume_path=None)
            assert page.input_value("#city") == "Boston, Massachusetts, United States"  # not Boston, England
        finally:
            browser.close()


def test_workday_blocked_degree_dropdown_still_fills_skills(monkeypatch: pytest.MonkeyPatch) -> None:
    from playwright.sync_api import Error as PlaywrightError

    from jobagent.apply import filler
    from jobagent.apply.profile import EducationEntry

    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"skills": [["Python"]], "education": [
        EducationEntry(school="Riverside University", degree="Masters", gpa="3.5"),
    ]})

    def blocked(*_: object) -> None:
        raise PlaywrightError("Locator.click: Timeout 5000ms exceeded.")

    picked: list[str] = []
    monkeypatch.setattr(filler, "_pick", blocked)
    monkeypatch.setattr(filler, "_search_pick", lambda page, selector, *a, **k: picked.append(selector) or True)
    html = ('<div aria-labelledby="Education-section"><input id="education-1--schoolName"></div>'
            '<div aria-labelledby="Skills-section"><input id="skills--skills"></div>')
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            filler._workday_my_experience(page, profile, None)
            assert picked == ["#skills--skills"]  # the degree timeout no longer skips the Skills section
        finally:
            browser.close()


def test_review_without_a_terminal_waits_for_the_window_to_close_instead_of_reading_stdin() -> None:
    from jobagent.apply.filler import _wait_for_review

    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.close()  # Master closed the window
            _wait_for_review(page, interactive=False)  # returns; never calls input() (EOF in background runs)
        finally:
            browser.close()


def test_workday_start_prefers_autofill_with_resume_over_apply_manually() -> None:
    from jobagent.apply.filler import _start_workday_application

    def run(dialog: str) -> str:
        html = ('<button data-automation-id="adventureButton" onclick="document.getElementById(\'d\').hidden=false">'
                f'Apply</button><div id="d" hidden>{dialog}</div>')
        with playwright_sync_api.sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content(html)
                _start_workday_application(page)
                return page.evaluate("window.__picked || ''")
            finally:
                browser.close()

    button = '<button data-automation-id="{0}" onclick="window.__picked=\'{0}\'">x</button>'
    all_three = "".join(button.format(i) for i in ("useMyLastApplication", "applyManually", "autofillWithResume"))
    assert run(all_three) == "autofillWithResume"  # never "Use My Last Application"
    assert run(button.format("applyManually")) == "applyManually"  # tenants without autofill


def test_workday_work_history_corrects_parsed_entries_in_place(capsys: pytest.CaptureFixture[str]) -> None:
    from jobagent.apply.filler import _workday_my_experience
    from jobagent.apply.profile import WorkEntry

    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"skills": [], "education": [], "work_history": [
        WorkEntry(title="Security Engineer", company="Exemplar", location="Boston, MA", start="2024-01",
                  end=None, description="Real work."),
    ]})
    # Northwind #52001: the parse added the Examplerange Challenge Cup competition as job #2; it is deleted, not left.
    entry = ('<div role="group"><h4>Work Experience {0}</h4><button data-automation-id="panel-set-delete-button"'
             ' onclick="this.parentElement.remove()">Delete</button>'
             '<input id="workExperience-{0}--jobTitle" value="{1}"><input id="workExperience-{0}--companyName"></div>')
    html = ('<div aria-labelledby="Work-Experience-section">' + entry.format(1, "Parsed")
            + entry.format(2, "Blue Team Competitor")
            + '<button data-automation-id="add-button" onclick="window.__added = true">Add</button></div>')
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            _workday_my_experience(page, profile, None)
            _workday_my_experience(page, profile, None)  # re-scan: no second pass
            assert page.input_value("#workExperience-1--jobTitle") == "Security Engineer"
            assert page.input_value("#workExperience-1--companyName") == "Exemplar"
            assert page.locator("#workExperience-2--jobTitle").count() == 0  # the parsed extra is gone
            assert page.evaluate("window.__added !== true")
            page.set_content(html.replace("panel-set-delete-button", "x").replace(">Delete<", "><"))
            _workday_my_experience(page, profile, None)  # no Delete button found: Master is told
            assert page.locator("#workExperience-2--jobTitle").count() == 1
        finally:
            browser.close()
    assert capsys.readouterr().out.count("delete parsed entry #2") == 1


def test_greenhouse_second_discipline_select_gets_the_bachelors_subject() -> None:
    from jobagent.apply.profile import EducationEntry

    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"education": [
        EducationEntry(school="Riverside University", degree="Masters", field_of_study="Cybersecurity | Information Security"),
        EducationEntry(school="Lakeside University", degree="Bachelors", field_of_study="Computer Science"),
    ]})
    subjects = "".join(f"<option>{s}</option>" for s in ("", "Computer Science", "Cybersecurity Law and Policy", "Nursing"))
    html = ("<form>" + "".join(
        f'<label for="d{i}">Discipline</label><select id="d{i}" name="d{i}">{subjects}</select>' for i in (0, 1)
    ) + "</form>")
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            fields = _fill_page(page, _FORM, profile, resume_path=None)
            values = page.evaluate(_EXTRACT_VALUES_SCRIPT)
        finally:
            browser.close()
    assert values["d0"] == ""  # no exact Cybersecurity / Information Security: blank, not "Cybersecurity Law..."
    assert values["d1"] == "Computer Science"
    assert [f.outcome for f in fields] == ["skipped", "filled"]


_WORKDAY_PROMPT = """
<div aria-labelledby="Education-section">{entries}</div>
<div id="popper"></div>
<script>
const SUBJECTS = {subjects};
function entry(i, pill) {{
  return `<div data-automation-id="multiSelectContainer" id="c${{i}}">` +
    (pill ? `<div data-automation-id="selectedItem">${{pill}}<span data-automation-id="DELETE_charm"
       onclick="this.parentElement.remove()">x</span></div>` : "") +
    `<input id="education-${{i}}--fieldOfStudy" data-uxi-widget-type="selectinput"></div>`;
}}
document.querySelector('[aria-labelledby="Education-section"]').innerHTML = {pills}.map((p, i) => entry(i + 1, p)).join("");
document.addEventListener("keydown", (event) => {{
  const box = event.target;
  if (event.key !== "Enter" || !box.id) return;
  const typed = box.value.toLowerCase();
  const popper = document.getElementById("popper");
  popper.setAttribute("data-popper-placement", "bottom");
  popper.innerHTML = SUBJECTS.filter((s) => s.toLowerCase().includes(typed)).map(
    (s) => `<div data-automation-id="promptOption" data-automation-label="${{s}}">${{s}}</div>`).join("");
  popper.querySelectorAll("[data-automation-id=promptOption]").forEach((option) => option.onclick = () => {{
    box.closest("[data-automation-id=multiSelectContainer]").insertAdjacentHTML("afterbegin",
      `<div data-automation-id="selectedItem">${{option.dataset.automationLabel}}</div>`);
  }});
}});
</script>
"""


def test_workday_field_of_study_clears_a_wrong_parse_and_never_guesses() -> None:
    import json

    from jobagent.apply.filler import _workday_field_of_study
    from jobagent.apply.profile import EducationEntry

    masters = EducationEntry(school="Riverside University", degree="Masters",
                             field_of_study="Cybersecurity | Information Technology")
    bachelors = EducationEntry(school="Lakeside University", degree="Bachelors",
                               field_of_study="Computer Science | Computer Science and Engineering")
    subjects = ["Computer Science", "Computer Science Education", "Cybersecurity Law and Policy",
                "Information Technology Management"]
    html = _WORKDAY_PROMPT.format(entries="", subjects=json.dumps(subjects), pills=json.dumps(["Computer Science", ""]))
    pill = '#c{} [data-automation-id="selectedItem"]'
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            first = _workday_field_of_study(page, "#education-1--fieldOfStudy", masters)
            second = _workday_field_of_study(page, "#education-2--fieldOfStudy", bachelors)
            assert page.locator(pill.format(1)).count() == 0  # the parsed bachelor's subject was removed
            assert page.locator(pill.format(2)).all_inner_texts() == ["Computer Science"]  # not "...Education"
        finally:
            browser.close()
    assert (first.outcome, second.outcome) == ("skipped", "filled")
    assert "left blank" in first.reason


def test_workday_field_of_study_removes_the_subject_workday_auto_picked_on_enter() -> None:
    """Northwind #52001: Enter on "Information Systems" auto-selected the only result, "Management Information
    Systems", and the prompt was left with that wrong pill although the fill reported the field blank."""
    import json

    from jobagent.apply.filler import _workday_field_of_study
    from jobagent.apply.profile import EducationEntry

    masters = EducationEntry(school="Riverside University", degree="Masters",
                             field_of_study="Cybersecurity | Information Systems")
    auto_pick = """<script>
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter" || !event.target.id) return;
  setTimeout(() => {
    const options = document.querySelectorAll("#popper [data-automation-id=promptOption]");
    if (options.length !== 1) return;
    event.target.closest("[data-automation-id=multiSelectContainer]").insertAdjacentHTML("afterbegin",
      `<div data-automation-id="selectedItem">${options[0].dataset.automationLabel}<span
        data-automation-id="DELETE_charm" onclick="this.parentElement.remove()">x</span></div>`);
  }, 0);
});
</script>"""
    html = _WORKDAY_PROMPT.format(entries="", subjects=json.dumps(["Management Information Systems", "Cyber Law"]),
                                  pills=json.dumps([""])) + auto_pick
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            result = _workday_field_of_study(page, "#education-1--fieldOfStudy", masters)
            assert page.locator('#c1 [data-automation-id="selectedItem"]').count() == 0
        finally:
            browser.close()
    assert result.outcome == "skipped" and "left blank" in result.reason


def test_workday_skills_skip_pills_already_chosen_and_never_remove_them(capsys: pytest.CaptureFixture[str]) -> None:
    """Northwind #52001: skills already on the page (Master's or Workday's) were searched again; a click on a chosen
    option deselects it, and a failed search left "Software Supply Chain Security" typed in the box."""
    from jobagent.apply.filler import _workday_my_experience

    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"education": [], "work_history": [], "skills": [
        ["Active Directory", "AD"], ["cybersecurity"], ["Splunk"], ["Software Supply Chain Security"]]})
    pill = '<div data-automation-id="selectedItem">{}<span data-automation-id="DELETE_charm"></span></div>'
    html = ('<div aria-labelledby="Skills-section"><div data-automation-id="multiSelectContainer">'
            + pill.format("Active Directory (AD)") + pill.format("Cybersecurity.")
            + '<input id="skills--skills" data-uxi-widget-type="selectinput"></div></div><div id="popper"></div>'
            """<script>
window.__searched = [];
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  window.__searched.push(event.target.value);
  const popper = document.getElementById("popper");
  popper.setAttribute("data-popper-placement", "bottom");
  popper.innerHTML = event.target.value === "Splunk"
    ? '<div data-automation-id="promptOption" data-automation-label="Splunk">Splunk</div>' : "";
  popper.querySelectorAll("[data-automation-id=promptOption]").forEach((o) => o.onclick = () =>
    document.querySelector("[data-automation-id=multiSelectContainer]").insertAdjacentHTML("afterbegin",
      `<div data-automation-id="selectedItem">${o.dataset.automationLabel}</div>`));
});
</script>""")
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            report = _workday_my_experience(page, profile, None)
            pills = page.locator('[data-automation-id="selectedItem"]').all_inner_texts()
            assert sorted(p.strip() for p in pills) == ["Active Directory (AD)", "Cybersecurity.", "Splunk"]
            assert page.evaluate("window.__searched") == ["Splunk", "Software Supply Chain Security"]
            assert page.input_value("#skills--skills") == ""  # no leftover search text
        finally:
            browser.close()
    assert [(f.label, f.reason) for f in report] == [("Skills", "1 added, 2 already present, 1 not found")]
    assert "2 already present (skipped)" in capsys.readouterr().out


def test_a_file_input_gets_the_resume_at_most_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Northwind #52001: the resume was attached three times (every re-fill pass, step and restart)."""
    from jobagent.apply.filler import _workday_my_experience

    resume = tmp_path / "Resume_ExampleCandidate.pdf"
    resume.write_bytes(b"%PDF-1.4")
    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"education": [], "work_history": [], "skills": []})
    section = ('<div aria-labelledby="Resume/CV-section"><input type="file" id="f">CHIPS</div>'
               '<script>window.__uploads = 0; document.getElementById("f").addEventListener("change", (e) => {'
               ' window.__uploads += 1; e.target.value = ""; });</script>')  # Workday empties the input itself
    chip = '<div data-automation-id="file-upload-item">{}</div>'
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(section.replace("CHIPS", ""))
            for _ in range(3):  # three re-fill passes on the same step
                _workday_my_experience(page, profile, resume)
            assert page.evaluate("window.__uploads") == 1

            page.set_content(section.replace("CHIPS", chip.format("Resume_ExampleCandidate.pdf")))  # a restart: chip, empty input
            _workday_my_experience(page, profile, resume)
            assert page.evaluate("window.__uploads") == 0

            page.set_content(section.replace("CHIPS", chip.format("a.pdf") + chip.format("b.pdf")))  # duplicates: reported
            _workday_my_experience(page, profile, resume)
            assert page.evaluate("window.__uploads") == 0
            assert page.locator('[data-automation-id="file-upload-item"]').count() == 2  # never removed
        finally:
            browser.close()
    assert "2 files attached here" in capsys.readouterr().out


def test_workday_skills_never_add_the_same_skill_twice() -> None:
    """Northwind #52001 "You cannot enter duplicate skills": Enter auto-picked the only result ("Microsoft Intune")
    and the click on that option added it again; two truth skills can also resolve to one Workday option."""
    from jobagent.apply.filler import _workday_my_experience

    profile = ApplicationProfile.model_validate(yaml.safe_load(EXAMPLE_PROFILE.read_text(encoding="utf-8")))
    profile = profile.model_copy(update={"education": [], "work_history": [], "skills": [
        ["Microsoft Intune", "Intune"], ["Microsoft Entra ID", "Azure Active Directory"], ["Azure AD"]]})
    html = ('<div aria-labelledby="Skills-section"><div data-automation-id="multiSelectContainer">'
            '<input id="skills--skills" data-uxi-widget-type="selectinput"></div></div><div id="popper"></div>'
            """<script>
const OPTIONS = {"microsoft intune": ["Microsoft Intune"], "microsoft entra id": ["Microsoft Entra ID"],
                 "azure ad": ["Microsoft Entra ID"]};
const add = (label) => document.querySelector("[data-automation-id=multiSelectContainer]").insertAdjacentHTML(
  "afterbegin", `<div data-automation-id="selectedItem">${label}<span data-automation-id="DELETE_charm"
    style="display:inline-block;width:8px;height:8px" onclick="this.parentElement.remove()"></span></div>`);
document.addEventListener("keydown", (event) => {
  if (event.key !== "Enter") return;
  const found = OPTIONS[event.target.value.toLowerCase()] || [];
  const popper = document.getElementById("popper");
  popper.setAttribute("data-popper-placement", "bottom");
  popper.innerHTML = found.map((s) => `<div data-automation-id="promptOption" data-automation-label="${s}">${s}</div>`).join("");
  popper.querySelectorAll("[data-automation-id=promptOption]").forEach((o) => o.onclick = () => add(o.dataset.automationLabel));
  if (found.length === 1) setTimeout(() => add(found[0]), 0);  // Workday picks a lone result on Enter
});
</script>""")
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content(html)
            _workday_my_experience(page, profile, None)
            pills = page.locator('[data-automation-id="selectedItem"]').all_inner_texts()
        finally:
            browser.close()
    assert sorted(pills) == ["Microsoft Entra ID", "Microsoft Intune"]


def test_truth_skills_are_deduplicated_by_normalised_name(tmp_path: Path) -> None:
    import shutil

    from jobagent.apply.profile import load_application_profile

    root = Path(__file__).parent.parent / "candidate"
    for name in ("profile", "experience", "skills", "preferences", "application"):
        shutil.copy(root / f"{name}.example.yaml", tmp_path / f"{name}.yaml")
    skills = yaml.safe_load((tmp_path / "skills.yaml").read_text())
    group = next(iter(skills["skills"].values())) if isinstance(skills.get("skills"), dict) else None
    if group is None:
        pytest.skip("example skills layout differs")
    group.append(dict(group[0]))  # the same skill listed twice
    (tmp_path / "skills.yaml").write_text(yaml.safe_dump(skills))
    names = [skill[0] for skill in load_application_profile(tmp_path).skills]
    assert len(names) == len(set(names))
