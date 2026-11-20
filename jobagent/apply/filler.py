"""Playwright-driven, human-reviewed pre-fill of ATS application forms.

This module never submits an application. It only ever calls ``fill``,
``select_option``, ``check``/``uncheck``, and ``set_input_files`` on form
controls, opens combobox menus with ArrowDown (or by clicking a dropdown's own
``aria-haspopup="listbox"`` toggle, which is ``type="button"``) / closes them
with Escape, and clicks exactly one ``[role="option"]`` list item to pick an
answer. On Ashby it clicks Yes/No toggle buttons only when they belong to no ``<form>``
(so they cannot submit). On Workday it also clicks the posting's own "Apply" / "Autofill with Resume" (or "Apply Manually")
navigation buttons and, when a password is configured, its "Create Account" /
"Sign In" buttons (authorized by Master), opening an emailed Workday verification link (Workday hosts only)
in a tab of the same browser. It never clicks a step's "Continue", "Next", "Save and Continue"
or "Submit": it fills the step on screen, Master reviews it and clicks Continue himself, and the next
step is filled when it appears. The one exception is practice mode (training rounds only, ``apply train``
/ ``--practice``, see ``practice.py``): it may click exactly Next / Continue / Save and Continue after a
step was filled, snapshotted and reviewed; any submit-like control blocks the click in code. Emailed
security codes are typed with ``fill`` only. It never clicks any other button or link and never calls ``submit``. It presses Enter only inside a
Workday search prompt (``_search_pick``), where Enter runs the prompt's search, never on a form or button,
so there is no code path here that can trigger a browser-side form submission.
The human reviews the filled form and submits it themselves.

Native controls plus ARIA comboboxes (the React dropdowns Greenhouse uses instead
of ``<select>``) are supported; a combobox's options are read from the open menu
and matched exactly like a native select's, never guessed.
"""

from __future__ import annotations

import json
import re
import select
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from jobagent.apply.answers import wants_draft
from jobagent.apply.ats import ApplicationForm
from jobagent.apply.certifications import FALLBACK_NOTE, choose_certification
from jobagent.apply.fields import (
    FieldDescriptor,
    FillAction,
    decide,
    is_terms_acknowledgement,
    match_option,
)
from jobagent.apply.oversight import Oversight
from jobagent.apply.profile import ApplicationProfile, EducationEntry
from jobagent.apply.snapshot import LABEL_FUNCTIONS
from jobagent.intake.urls import validate_public_url
from jobagent.normalization import normalize_title

DEFAULT_PROFILE_DIRECTORY = Path("data/browser-profile")

# Walks one form, tagging each supported control with a stable index attribute
# and returning a plain-data descriptor per field. Radio buttons are grouped by
# their shared `name` and each option is additionally tagged with its index so
# Python can select the exact option `decide()` chose without ever guessing.
_DESCRIPTOR_SCRIPT = """
(formSelector) => {
    const form = document.querySelector(formSelector);
    if (!form) return [];

""" + LABEL_FUNCTIONS + """
    const results = [];
    const seenRadioGroups = new Set();
    // Continue numbering after fields tagged by earlier passes still on the page.
    let index = 1 + Math.max(-1, ...Array.from(document.querySelectorAll("[data-jobagent-field]"))
        .map((el) => Number(el.getAttribute("data-jobagent-field"))));
    const controls = Array.from(form.querySelectorAll(
        "input, select, textarea, button[aria-haspopup='listbox'], div[role='combobox'], div[role='group']"
    ));

    for (const el of controls) {
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute("type") || "text").toLowerCase();
        if (tag === "input" && ["submit", "button", "image", "hidden", "reset"].includes(type)) {
            continue;
        }
        // Already handled by an earlier pass (later passes pick up questions revealed by answers).
        if (el.hasAttribute("data-jobagent-field")) continue;
        // Rippling Yes/No: a role=group of role=checkbox tiles (data-value); expose it as a radio group.
        if (tag === "div" && el.getAttribute("role") === "group") {
            const tiles = Array.from(el.querySelectorAll('[role="checkbox"][data-value]'));
            if (!tiles.length) continue;
            tiles.forEach((tile, optionIndex) => {
                tile.setAttribute("data-jobagent-field", String(index));
                tile.setAttribute("data-jobagent-option-index", String(optionIndex));
            });
            el.setAttribute("data-jobagent-field", String(index));
            el.querySelectorAll("input").forEach((input) => input.setAttribute("data-jobagent-field", String(index)));
            results.push({
                index, label: (labelFor(el) || "").trim(), kind: "radio", name: el.id || "", required: false,
                options: tiles.map((tile) => tile.getAttribute("data-value") || ""),
            });
            index += 1;
            continue;
        }
        if (tag === "div" && el.getAttribute("role") !== "combobox") continue;
        // Ashby Yes/No: a hidden checkbox behind two toggle buttons; expose it as a radio group.
        if (type === "checkbox" && el.parentElement && el.parentElement.classList.contains("ashby-application-form-input-yesno")) {
            const entry = el.closest(".ashby-application-form-field-entry");
            const title = entry ? entry.querySelector("label") : null;
            const buttons = Array.from(el.parentElement.querySelectorAll("button"));
            el.setAttribute("data-jobagent-field", String(index));
            buttons.forEach((button, optionIndex) => {
                button.setAttribute("data-jobagent-field", String(index));
                button.setAttribute("data-jobagent-option-index", String(optionIndex));
            });
            results.push({
                index,
                label: title ? title.innerText.trim() : "",
                kind: "radio",
                name: el.getAttribute("name") || "",
                required: Boolean(title && title.className.includes("required")),
                options: buttons.map((button) => (button.innerText || "").trim()),
            });
            index += 1;
            continue;
        }
        // React dropdowns keep an unlabelled, id-less proxy input for validation; the combobox is the real field.
        if (!el.id && !el.getAttribute("name") && !(labelFor(el) || "").trim()) continue;
        // Hidden controls can't be filled (Playwright would wait on them); styled file inputs are the exception.
        if (type !== "file" && el.getClientRects().length === 0) continue;

        if (tag === "input" && type === "radio") {
            const name = el.getAttribute("name") || "";
            if (!name || seenRadioGroups.has(name)) continue;
            seenRadioGroups.add(name);
            const group = Array.from(
                form.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`)
            );
            const fieldset = el.closest("fieldset");
            const legend = fieldset ? fieldset.querySelector("legend") : null;
            const groupLabel = legend ? (legend.innerText || legend.textContent || "") : (leverQuestion(el) || name);
            const options = [];
            group.forEach((radio, optionIndex) => {
                radio.setAttribute("data-jobagent-field", String(index));
                radio.setAttribute("data-jobagent-option-index", String(optionIndex));
                options.push((labelFor(radio) || "").trim());
            });
            results.push({
                index,
                label: groupLabel.trim(),
                kind: "radio",
                name,
                required: group.some((radio) => radio.required),
                options,
            });
            index += 1;
            continue;
        }

        let kind = "text";
        if (tag === "select") kind = "select";
        else if (tag === "textarea") kind = "textarea";
        else if (type === "checkbox") kind = "checkbox";
        else if (type === "file") kind = "file";
        else if (el.getAttribute("role") === "combobox" || tag === "button" || tag === "div"
            || el.getAttribute("data-uxi-widget-type") === "selectinput") kind = "combobox";

        let options = [];
        if (kind === "select") {
            options = Array.from(el.querySelectorAll("option"))
                .map((option) => (option.textContent || "").trim())
                .filter((text) => text.length > 0);
        }

        el.setAttribute("data-jobagent-field", String(index));
        // Greenhouse labels both uploads just "Attach"; the id ("resume", "cover_letter") says which.
        // Workday puts "Resume/CV" on the surrounding section, so include its heading too.
        const section = el.closest("section, fieldset, [role='group']");
        const heading = section ? section.querySelector("h1, h2, h3, h4, legend") : null;
        const label = kind === "file"
            ? `${labelFor(el)} ${el.id || ""} ${el.getAttribute("data-testid") || ""} ${heading ? heading.innerText : ""} ${section ? section.getAttribute("aria-labelledby") || "" : ""}`
            : labelFor(el);
        // The nth visible control with this label (a second "Field of Study" is the second degree's).
        const trimmed = (label || "").trim();
        const occurrence = kind === "file" ? 0 : Math.max(0, controls.filter((other) => other === el
            || (other.tagName === el.tagName && other.getClientRects().length > 0
                && (labelFor(other) || "").trim() === trimmed)).indexOf(el));
        results.push({
            index,
            label: trimmed,
            kind,
            name: el.getAttribute("name") || "",
            required: Boolean(el.required),
            options,
            occurrence,
        });
        index += 1;
    }
    return results;
}
"""


@dataclass(frozen=True)
class FilledField:
    """The outcome recorded for one form field after a fill attempt."""

    label: str
    kind: str
    outcome: str  # "filled" | "skipped" | "required_empty"
    reason: str = ""
    options: tuple[str, ...] = ()
    value: str = ""  # what JobAgent entered (blank when it entered nothing)
    source: str = ""  # the rule key from ``decide`` ("contact.email", "answer:referred", "ai_draft", ...)
    step: int = 0  # wizard step it was filled on (1 = first page)


@dataclass(frozen=True)
class FillReport:
    """A per-job record of what was filled, skipped, or left required-but-empty."""

    job_id: int
    form_url: str
    fields: list[FilledField]

    @property
    def required_empty(self) -> list[FilledField]:
        return [item for item in self.fields if item.outcome == "required_empty"]

    def write(self, path: Path) -> None:
        payload = {
            "job_id": self.job_id,
            "form_url": self.form_url,
            "fields": [asdict(item) for item in self.fields],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2))


def fill_application_form(
    form: ApplicationForm,
    profile: ApplicationProfile,
    *,
    job_id: int,
    resume_path: Path | None,
    cover_letter_path: Path | None = None,
    profile_directory: Path = DEFAULT_PROFILE_DIRECTORY,
    headless: bool = False,
    slow_mo: float = 0,
    wait_for_review: bool = True,
    oversight: Oversight | None = None,
) -> FillReport:
    """Open the form in a dedicated browser and pre-fill known fields.

    This never submits the form. When ``wait_for_review`` is true (the default,
    disabled only in tests), it blocks until the human presses Enter after
    reviewing and, if satisfied, submitting the form themselves.
    """
    global _TYPE_DELAY_MS
    _TYPE_DELAY_MS = 60 if slow_mo else 0  # a watched (--slow-mo) fill types text out like a person
    safe_url = validate_public_url(form.form_url)
    profile_directory.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(profile_directory), headless=headless, slow_mo=slow_mo
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            fields = _open_and_fill(page, safe_url, form, profile, resume_path=resume_path,
                                    cover_letter_path=cover_letter_path, oversight=oversight)
            fields = [replace(item, step=1) for item in fields]
            if form.multi_page:
                print("Review each step and click Continue yourself (JobAgent never does); each new step is "
                      "filled as it appears. Close the window when done.", flush=True)
                fields = _watch_steps(page, form, profile, resume_path=resume_path, cover_letter_path=cover_letter_path,
                                      oversight=oversight, initial=fields)
            else:
                if oversight is not None:
                    oversight.after_step(page, form, "", fields)
                if wait_for_review:
                    _wait_for_review(page, on_poll=oversight.poll if oversight is not None else None)
            if oversight is not None:
                oversight.close()
                fields += oversight.extra_fields
        finally:
            context.close()
    return FillReport(job_id=job_id, form_url=safe_url, fields=fields)


def _wait_for_review(page: Page, *, interactive: bool | None = None, on_poll: Callable[[Page], None] | None = None) -> None:
    """Hold the window open for Master's review. With a terminal, Enter ends it; without one (a
    background run, a VNC session, cron), closing the window does: input() would hit EOF at once
    and close the browser before Master saw the form. Closing the window always ends it.

    ``on_poll`` runs every 2 s meanwhile (an emailed security code asked for after Master's Submit).
    """
    tty = interactive if interactive is not None else sys.stdin.isatty()
    print("Review the filled application in the browser, then " + ("press Enter here" if tty else "close the window")
          + " (JobAgent will not submit it for you).", flush=True)
    while any(not candidate.is_closed() for candidate in page.context.pages):  # ponytail: 2s polling
        if on_poll is not None and not page.is_closed():
            try:
                on_poll(page)
            except PlaywrightError:
                pass  # navigating; try again on the next poll
        if not tty:
            time.sleep(2)
        elif select.select([sys.stdin], [], [], 2)[0]:
            sys.stdin.readline()
            return


@dataclass(frozen=True)
class TabJob:
    job_id: int
    form: ApplicationForm
    resume_path: Path | None
    cover_letter_path: Path | None


def fill_in_tabs(
    jobs: list[TabJob],
    profile: ApplicationProfile,
    *,
    profile_directory: Path = DEFAULT_PROFILE_DIRECTORY,
    slow_mo: float = 0,
    on_report: Callable[[FillReport], None],
) -> None:
    """Pre-fill single-page forms, one tab each, in one browser; leave every tab open for review.

    Never submits. ``on_report`` runs as each tab finishes; this returns when the human closes the browser.
    """
    profile_directory.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(str(profile_directory), headless=False, slow_mo=slow_mo)
        try:
            blank = context.pages[0] if context.pages else None
            for job in jobs:
                page = blank or context.new_page()
                blank = None
                try:
                    safe_url = validate_public_url(job.form.form_url)
                    fields = _open_and_fill(page, safe_url, job.form, profile, resume_path=job.resume_path, cover_letter_path=job.cover_letter_path)
                except (PlaywrightError, RuntimeError, ValueError) as error:
                    print(f"Job #{job.job_id}: could not fill — {error}", flush=True)  # tab stays open for manual fill
                    continue
                on_report(FillReport(job_id=job.job_id, form_url=safe_url, fields=fields))
            print("All tabs filled. Review each tab and submit yourself; close the browser when done.", flush=True)
            context.wait_for_event("close", timeout=0)
        finally:
            context.close()


def _open_and_fill(
    page: Page,
    url: str,
    form: ApplicationForm,
    profile: ApplicationProfile,
    *,
    resume_path: Path | None,
    cover_letter_path: Path | None,
    oversight: Oversight | None = None,
) -> list[FilledField]:
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector(form.form_selector, timeout=15_000)
    try:
        # React boards (Greenhouse) re-render on hydration and wipe values filled earlier.
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        pass  # ponytail: some pages never go idle; fill anyway after 15s
    page.set_default_timeout(5_000)  # a field that won't respond is skipped, not waited on for 30s
    if form.provider == "workday":
        _start_workday_application(page)
        _workday_account(page, profile, oversight)
        _workday_autofill_upload(page, resume_path)
        filled = _workday_my_experience(page, profile, resume_path)
        return filled + _fill_page(page, form, profile, resume_path=resume_path, cover_letter_path=cover_letter_path,
                                   drafter=oversight.drafter if oversight else None)
    return _fill_page(page, form, profile, resume_path=resume_path, cover_letter_path=cover_letter_path,
                      drafter=oversight.drafter if oversight else None)


def _start_workday_application(page: Page) -> None:
    """Click "Apply" then "Autofill with Resume" ("Apply Manually" if a tenant lacks it): navigation only.

    Autofill lets Workday parse the tailored resume, so My Experience (skills included) starts pre-filled
    and is then corrected from the truth files. "Use My Last Application" is never chosen (Master's call):
    it would bring back an older resume and answers.
    """
    try:
        page.locator('[data-automation-id="adventureButton"]').first.click(timeout=10_000)
        start = page.locator('[data-automation-id="autofillWithResume"], [data-automation-id="applyManually"]')
        start.first.wait_for(timeout=10_000)
        autofill = page.locator('[data-automation-id="autofillWithResume"]')
        (autofill if autofill.count() else page.locator('[data-automation-id="applyManually"]')).first.click()
        page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeoutError:
        return  # already past this step (e.g. signed in with a saved application); the human takes over


# Files a file input already holds: its own files, or the filename chips its section shows (Workday's
# uploaded-file list, Greenhouse's attached filename, Rippling's "Total N"). Workday empties the input itself.
_ATTACHED_SCRIPT = """(e) => {
    if (e.getAttribute("data-jobagent-uploaded") || (e.files && e.files.length)) return ["(this fill)"];
    const box = e.closest('[aria-labelledby$="-section"], [data-automation-id^="formField-"], [data-testid="field"],'
        + ' fieldset, .field, .application-question') || e.parentElement;
    if (!box) return [];
    const text = box.innerText || "";
    const names = text.match(/[^\\s/\\\\]+\\.(pdf|docx?|rtf|txt)\\b/gi) || [];
    if (!names.length && (/Total [1-9]/.test(text) || box.querySelector('[data-automation-id="file-upload-successful"]')))
        return ["(attached)"];
    return names;
}"""


def _already_attached(target: Locator) -> bool:
    """True when this file input (or its section) already shows a file; duplicates are reported, never removed."""
    try:
        names = target.evaluate(_ATTACHED_SCRIPT)
    except PlaywrightError:
        return False
    if len(names) > 1:
        print(f"Upload needs you: {len(names)} files attached here ({', '.join(names[:5])}); "
              "remove the extras before Save and Continue.", flush=True)
    return bool(names)


def _workday_autofill_upload(page: Page, resume_path: Path | None) -> None:
    """On the "Autofill with Resume" step, attach the tailored resume once (set_input_files only).

    Workday parses it when Master clicks "Continue"; nothing is sent before that.
    """
    if resume_path is None or not resume_path.is_file() or "autofillwithresume" not in page.url.casefold():
        return
    upload = page.locator('input[type="file"]')
    if upload.count() and not _already_attached(upload.first):
        upload.first.set_input_files(str(resume_path))
        upload.first.evaluate("e => e.setAttribute('data-jobagent-uploaded', '1')")
        page.wait_for_timeout(3_000)  # let the upload finish before anyone clicks Continue


def _workday_account(page: Page, profile: ApplicationProfile, oversight: Oversight | None = None) -> None:
    """Create the candidate account, or sign in when it already exists (Master authorized both clicks).

    Uses ``$WORKDAY_PASSWORD`` and the email in candidate/application.yaml. Workday puts a
    ``click_filter`` overlay over its account buttons; clicking the overlay inside the same form is
    what Workday expects. When Workday asks to verify the email, ``oversight`` follows the link
    from Master's mail (Workday hosts only) and signs in again. A CAPTCHA or human check, or no
    verification mail in time, stops here and tells Master ("needs you").
    """
    if not profile.account_password:
        return
    email, password = profile.contact.email, profile.account_password

    def submit(button_id: str, fields: dict[str, str]) -> None:
        for automation_id, value in fields.items():
            page.locator(f'input[data-automation-id="{automation_id}"]').fill(value)
        if button_id == "createAccountSubmitButton":
            _tick_account_terms(page, oversight)
        button = page.locator(f'[data-automation-id="{button_id}"]')
        overlay = button.locator("xpath=..").locator('[data-automation-id="click_filter"]')
        (overlay if overlay.count() else button).first.click()
        page.wait_for_timeout(4_000)

    def sign_in() -> None:
        if page.locator('[data-automation-id="signInLink"]').count() and not page.locator(
                '[data-automation-id="signInSubmitButton"]').count():
            page.locator('[data-automation-id="signInLink"]').first.click()
            page.wait_for_timeout(1_500)
        if page.locator('[data-automation-id="signInSubmitButton"]').count():
            submit("signInSubmitButton", {"email": email, "password": password})

    def stopped() -> bool:
        reason = page.evaluate(_HUMAN_CHECK_SCRIPT)
        if reason and oversight is not None:
            oversight.needs_you(f"Workday sign-in: {reason}")
        elif reason:
            print(f"Workday sign-in needs you: {reason}", flush=True)
        return bool(reason)

    create = page.locator('[data-automation-id="createAccountSubmitButton"]')
    try:
        if stopped():
            return
        requested = datetime.now(UTC) - timedelta(seconds=30)
        if create.count() and create.first.is_visible():
            submit("createAccountSubmitButton", {"email": email, "password": password, "verifyPassword": password})
        if stopped():
            return
        if page.locator('[data-automation-id="createAccountSubmitButton"]').count():  # still here: account exists
            page.locator('[data-automation-id="signInLink"]').first.click()
            page.wait_for_timeout(1_500)
        if page.evaluate(_VERIFY_EMAIL_SCRIPT):
            if oversight is None or not oversight.verify_workday(page, requested):
                if oversight is None:
                    print("Workday account step needs you: verify your email", flush=True)
                return
            sign_in()
        elif page.locator('[data-automation-id="signInSubmitButton"]').count():
            submit("signInSubmitButton", {"email": email, "password": password})
            link = page.locator(_CREATE_ACCOUNT_LINK)
            if page.evaluate(_SIGN_IN_REJECTED_SCRIPT) and link.count():  # no account on this tenant yet: create it
                link.first.click()
                page.wait_for_timeout(1_500)
                if stopped():
                    return
                requested = datetime.now(UTC) - timedelta(seconds=30)
                if create.count() and create.first.is_visible():
                    submit("createAccountSubmitButton", {"email": email, "password": password, "verifyPassword": password})
                if stopped():
                    return
            if page.evaluate(_VERIFY_EMAIL_SCRIPT) and oversight is not None and oversight.verify_workday(page, requested):
                sign_in()
        if stopped():
            return
        if page.evaluate(_SIGN_IN_REJECTED_SCRIPT) or (create.count() and create.first.is_visible()):
            reason = "Workday sign-in rejected (account exists with another password, or locked): sign in or reset it"
            if oversight is not None:
                oversight.needs_you(reason)
            else:
                print(f"Workday account step needs you: {reason}", flush=True)
    except PlaywrightError as error:
        print(f"Workday account step needs you: {str(error)[:120]}", flush=True)


# Terms-of-use / privacy acknowledgement boxes on Workday's Create Account page (Master, 2026-11-19); the same
# ``is_terms_acknowledgement`` rule ticks them inside applications. Certifications, signatures, EEO, arbitration,
# background checks and opt-ins are never ticked.
_ACCOUNT_BOXES_SCRIPT = """() => {
    if (!document.querySelector('[data-automation-id="createAccountSubmitButton"]')) return [];
    const visible = (e) => e.getClientRects().length > 0;
    const labelOf = (box) => ((box.labels && box.labels[0] ? box.labels[0].innerText : "")
        || box.getAttribute("aria-label") || (box.parentElement ? box.parentElement.innerText : "") || "").trim();
    return Array.from(document.querySelectorAll('input[type="checkbox"]')).filter(visible).map((box, index) => {
        box.setAttribute("data-jobagent-account-box", String(index));
        return {index, label: labelOf(box).slice(0, 200), checked: box.checked};
    });
}"""


def _tick_account_terms(page: Page, oversight: Oversight | None) -> None:
    """Tick the Create Account page's terms / privacy acknowledgement and record it in the fill report."""
    for box in page.evaluate(_ACCOUNT_BOXES_SCRIPT):
        if not is_terms_acknowledgement(box["label"]):
            continue
        target = page.locator(f'[data-jobagent-account-box="{box["index"]}"]')
        if not box["checked"]:
            target.check()  # a checkbox, never a submit control
        checked = target.is_checked()
        print(f"Workday account: terms {'acknowledged' if checked else 'NOT ticked'} ({box['label'][:80]!r})", flush=True)
        if oversight is not None:
            oversight.extra_fields.append(FilledField(box["label"][:120], "checkbox", "filled" if checked else "skipped",
                                                      "terms acknowledgement", (), "checked" if checked else "",
                                                      "consent.terms"))


# A visible CAPTCHA / human check: Master's, never JobAgent's.
_HUMAN_CHECK_SCRIPT = """() => {
    const visible = (e) => e.getClientRects().length > 0;
    const frame = Array.from(document.querySelectorAll("iframe")).find((f) => visible(f)
        && /captcha|hcaptcha|recaptcha|turnstile|arkose|challenge/i.test(f.src || ""));
    if (frame) return "CAPTCHA";
    if (/verify you are (a )?human|i'm not a robot|are you a robot|complete the security check/i.test(document.body.innerText || ""))
        return "human check";
    return "";
}"""
_SIGN_IN_REJECTED_SCRIPT = """() => /wrong email address or password|account might be locked|invalid (email|username) or password|already exists/i
    .test(document.body.innerText || "")"""
_CREATE_ACCOUNT_LINK = '[data-automation-id="createAccountLink"], button:text-is("Create Account"), a:text-is("Create Account")'
_VERIFY_EMAIL_SCRIPT = """() => /verify your (email|account)|verification (email|link)|activate your account|check your email/i
    .test(document.body.innerText || "")"""


def _workday_my_experience(page: Page, profile: ApplicationProfile, resume_path: Path | None) -> list[FilledField]:
    """On Workday's "My Experience" step, add one entry per job and degree from the truth files.

    Work and education entries Workday already pre-filled (resume parse, saved draft) are corrected
    in place, in truth-file order; only missing ones are added, and each section runs once (filled
    controls are tagged), so re-scans never duplicate entries. Parsed jobs beyond the truth list are deleted
    (each entry's own Delete button). The "Add" buttons only insert blank entries; nothing is saved until Master clicks
    "Save and Continue". Filled controls are tagged so the generic pass leaves them alone.
    Returns each degree's Field of Study outcome for the fill report.
    """
    report: list[FilledField] = []
    work = page.locator('[aria-labelledby="Work-Experience-section"]')
    school = page.locator('[aria-labelledby="Education-section"]')

    def work_history() -> None:
        titles = page.locator('input[id$="--jobTitle"]')
        if work.count() and not page.locator('input[id$="--jobTitle"][data-jobagent-field]').count():
            existing = titles.count()
            for index, job in enumerate(profile.work_history):
                try:  # one entry that times out (a slow date widget) must not skip the jobs after it
                    if index < existing:  # from Workday's resume parse or a saved draft: correct it in place
                        prefix = (titles.nth(index).get_attribute("id") or "").rsplit("--", 1)[0]
                    else:
                        prefix = _add_entry(page, work, "jobTitle")
                    _type(page, f"#{prefix}--jobTitle", job.title)
                    _type(page, f"#{prefix}--companyName", job.company)
                    _type(page, f"#{prefix}--location", job.location)
                    _date(page, f"{prefix}--startDate", job.start)
                    if job.end:
                        _date(page, f"{prefix}--endDate", job.end)
                    _type(page, f"#{prefix}--roleDescription", job.description)  # fill(), never typed out
                except PlaywrightError as error:
                    print(f"Work Experience #{index + 1} ({job.company}) needs you: {str(error)[:120]}", flush=True)
            # Entries past the truth list came from the resume parse (a split job, or a competition such as
            # the Examplerange Challenge Cup): never employment, so they are removed rather than left looking filled.
            for index in range(existing - 1, len(profile.work_history) - 1, -1):
                if not _delete_entry(page, titles.nth(index)):
                    print(f"Work Experience: delete parsed entry #{index + 1} before Save and Continue.", flush=True)

    def education() -> None:
        if school.count() and not page.locator(_SCHOOL_DONE).count():
            existing = page.locator(_SCHOOL_INPUT).count()
            for index, entry in enumerate(profile.education):
                if index < existing:  # from Workday's resume parse or a saved draft: correct it in place
                    prefix = (page.locator(_SCHOOL_INPUT).nth(index).get_attribute("id") or "").rsplit("--", 1)[0]
                else:
                    prefix = _add_entry(page, school, _SCHOOL_INPUT)
                if page.locator(f"#{prefix}--schoolName").count():
                    _type(page, f"#{prefix}--schoolName", entry.school)  # plain text box on some tenants
                else:
                    _search_pick(page, f"#{prefix}--school", [*_choices(entry.school_search), entry.school])
                _pick(page, f"#{prefix}--degree", entry.degree)
                if page.locator(f"#{prefix}--fieldOfStudy").count():
                    report.append(_workday_field_of_study(page, f"#{prefix}--fieldOfStudy", entry))
                _type(page, f"#{prefix}--gradeAverage", entry.gpa)
                for suffix, year in (("firstYearAttended", entry.start_year), ("lastYearAttended", entry.end_year)):
                    target = page.locator(f"#{prefix}--{suffix}-dateSectionYear-input")
                    if year and target.count() and target.input_value() != year:
                        target.focus()  # a display overlay covers the spinbutton
                        target.press_sequentially(year, delay=50)

    def skills() -> None:
        skills = page.locator("#skills--skills")
        if skills.count() and profile.skills and not skills.get_attribute("data-jobagent-field"):
            skills.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")  # once per page, even if some miss
            # Chosen skills render as pills with the same data-automation-id, outside the open list.
            chosen = {label.casefold() for label in page.locator(
                '[aria-labelledby="Skills-section"] [data-automation-id="promptOption"]'
            ).evaluate_all("es => es.map(e => e.getAttribute('data-automation-label') || '')")}
            present = [*chosen, *_chosen_labels(page, "#skills--skills")]
            skipped = [names[0] for names in profile.skills if _already_chosen(present, names)]
            missed = [
                names[0] for names in profile.skills if names[0] not in skipped
                and not _search_pick(page, "#skills--skills", names, prefix_ok=True, selected=chosen)
            ]
            if missed:
                print(f"Skills Workday has no entry for: {', '.join(missed)}", flush=True)
            added = len(profile.skills) - len(skipped) - len(missed)
            print(f"Skills: {added} added, {len(skipped)} already present (skipped), {len(missed)} not found", flush=True)
            report.append(FilledField("Skills", "combobox", "filled" if added or skipped else "skipped",
                                      f"{added} added, {len(skipped)} already present, {len(missed)} not found",
                                      source="skills"))

    def resume() -> None:
        upload = page.locator('[aria-labelledby="Resume/CV-section"] input[type="file"]')
        if upload.count() and resume_path is not None and resume_path.is_file() and not _already_attached(upload.first):
            upload.first.set_input_files(str(resume_path))  # the field has no id/name/label for the generic pass
            upload.first.evaluate("e => e.setAttribute('data-jobagent-uploaded', '1')")

    def websites() -> None:
        websites = page.locator('[aria-labelledby="Websites-section"]')
        if websites.count() and profile.links.linkedin and not page.locator('input[id$="--url"]').count():
            prefix = _add_entry(page, websites, "url")
            _type(page, f"#{prefix}--url", profile.links.linkedin)

    # One blocked click (e.g. a degree dropdown) must not skip the sections after it.
    def certifications() -> None:
        report.extend(_workday_certifications(page, profile))

    for name, section in (("work history", work_history), ("education", education), ("skills", skills),
                          ("certifications", certifications), ("resume", resume), ("websites", websites)):
        try:
            section()
        except PlaywrightError as error:
            print(f"My Experience {name} needs you: {str(error)[:120]}", flush=True)
    return report


_CERT_SECTION = '[aria-labelledby="Certifications-section"]'
_CERT_DATES = (("issued_date", ("issuedDate", "issueDate", "dateIssued")),
               ("expiration_date", ("expirationDate", "expiryDate", "expireDate")),
               ("exam_date", ("examDate", "dateTaken")))


def _workday_certifications(page: Page, profile: ApplicationProfile) -> list[FilledField]:
    """Workday's Certifications section: one entry for the first certification on file, Master's pick order.

    An entry already holding a certification is left alone (no second entry). The choice is the exact name, else
    "Other" plus the typed name, else "Security+", which is flagged in the report every time. The score stays
    blank, dates go only into their date widgets, and the certificate PDF is attached at most once.
    """
    section = page.locator(_CERT_SECTION)
    if not section.count() or not profile.certifications:
        return []
    cert = profile.certifications[0]
    label = f"Certification ({cert.name})"
    fields = section.locator('[id$="--certification"]')
    if fields.count():
        prefix = (fields.first.get_attribute("id") or "").rsplit("--", 1)[0]
        typed = fields.first.evaluate("e => (e.tagName === 'INPUT' ? e.value : '').trim()")
        if _chosen_labels(page, f"#{prefix}--certification") or typed:
            fields.first.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")
            return [FilledField(label, "combobox", "skipped", "a certification is already entered; left as is")]
    else:
        prefix = _add_entry(page, section, "certification")
    selector = f"#{prefix}--certification"
    choice = None
    for query in (cert.name, "Other", "Security+"):
        labels = _search_labels(page, selector, query)
        choice = choose_certification(labels, cert.name)
        if choice is not None and (query == "Security+" or choice.branch != "security_plus"):
            break
        choice = None
    if choice is None:
        page.locator(selector).first.fill("")
        print(f"{label}: no SecAI+, Other or Security+ option; left blank", flush=True)
        return [FilledField(label, "combobox", "skipped", "no SecAI+, Other or Security+ option")]
    page.locator(f"{_PROMPT_OPTIONS}[data-automation-label={json.dumps(choice.option)}]").first.click()
    page.wait_for_timeout(400)
    page.locator(selector).first.press("Escape")
    _mark_filled(page, selector, "certification")
    reason, source = "", f"certification.{choice.branch}"
    if choice.branch == "other":
        other = page.locator(f'#{prefix}--otherCertification, #{prefix}--certificationName')
        if other.count():
            _type(page, f"#{other.first.get_attribute('id')}", cert.name, "certification.other")
        else:
            reason = f"type {cert.name} into the Other field"
    elif choice.branch == "security_plus":
        source, reason = "certification.fallback", FALLBACK_NOTE
        print(f"{label}: {FALLBACK_NOTE}", flush=True)
    _type(page, f"#{prefix}--certificationNumber", cert.number, "certification.number")
    for key, suffixes in _CERT_DATES:
        value = getattr(cert, key)
        for suffix in suffixes:
            if value and page.locator(f"#{prefix}--{suffix}-dateSectionMonth-input").count():
                _date_mdy(page, f"{prefix}--{suffix}", value)
                break
    upload = section.locator('input[type="file"]')
    attachment = Path(cert.file).expanduser() if cert.file else None
    if upload.count() and attachment is not None and attachment.is_file() and not _already_attached(upload.first):
        upload.first.set_input_files(str(attachment))
        upload.first.evaluate("e => e.setAttribute('data-jobagent-uploaded', '1')")
        page.wait_for_timeout(2_000)
    return [FilledField(label, "combobox", "filled", reason, (), choice.option, source)]


def _search_labels(page: Page, selector: str, query: str) -> list[str]:
    """Run one Workday prompt search and return the result labels; a lone result Workday auto-picked is removed."""
    box = page.locator(selector).first
    if not box.count():
        return []
    before, pills_before = _prompt_labels(page), Counter(_chosen_labels(page, selector))
    box.focus()
    box.fill("")
    box.press_sequentially(query, delay=30)
    box.press("Enter")
    labels: list[str] = []
    previous: list[str] | None = None
    for _ in range(20):
        page.wait_for_timeout(250)
        labels = _prompt_labels(page)
        if labels and labels != before and labels == previous:
            break
        previous = labels
    added = list((Counter(_chosen_labels(page, selector)) - pills_before).elements())
    if added:
        _remove_pills(page, selector, added)
    return labels


def _date_mdy(page: Page, prefix: str, value: str) -> None:
    """MM/DD/YYYY into Workday's Month / Day / Year spinbuttons (whichever the widget has)."""
    month, day, year = (value.split("/") + ["", "", ""])[:3]
    for part, digits in (("Month", month), ("Day", day), ("Year", year)):
        target = page.locator(f"#{prefix}-dateSection{part}-input")
        if digits and target.count():
            target.focus()
            target.press_sequentially(digits, delay=50)


def _workday_field_of_study(page: Page, selector: str, entry: EducationEntry) -> FilledField:
    """This degree's own field of study, exact option only; otherwise cleared and reported as skipped.

    A wrong subject from Workday's resume parse (the bachelor's "Computer Science" on the master's
    entry) is removed rather than left looking filled.
    """
    label = f"Field of Study ({entry.school})"
    choices = _choices(entry.field_of_study)
    pills = _pills(page, selector)
    current = [text.strip() for text in pills.all_inner_texts()]
    if current and any(_match_label(current, choice, prefix_ok=False) is not None for choice in choices):
        page.locator(selector).first.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")
        return FilledField(label, "combobox", "filled", "already set")
    for _ in range(len(current)):
        pills.first.locator('[data-automation-id="DELETE_charm"]').click()  # removes a pill; not a submit
        page.wait_for_timeout(300)
    if choices and _search_pick(page, selector, choices):
        return FilledField(label, "combobox", "filled", "")
    page.locator(selector).first.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")  # blank, not guessed
    reason = f"No exact option for {entry.field_of_study!r}; left blank" if choices else "No field of study on file"
    print(f"{label}: {reason}", flush=True)
    return FilledField(label, "combobox", "skipped", reason)


# Split MM/DD/YYYY date widgets: Workday (…-dateSectionMonth-input) and Rippling (…-month spinbuttons).
_DATE_PARTS = {
    # system: (month selector, month id suffix, month/day/year id suffixes)
    "workday": ('input[data-automation-id="dateSectionMonth-input"]', "-dateSectionMonth-input",
                ("-dateSectionMonth-input", "-dateSectionDay-input", "-dateSectionYear-input")),
    "rippling": ('input[role="spinbutton"][id$="-month"]', "-month", ("-month", "-day", "-year")),
}
_DATE_LABEL_SCRIPT = """e => {
    const f = e.closest('[data-automation-id^="formField-"]');
    const l = f && f.querySelector('legend, label');
    if (l) return l.innerText;
    const block = e.closest('[data-testid="field"]');
    const prev = block && block.previousElementSibling;
    return prev ? prev.innerText : '';
}"""


def _split_dates(page: Page, profile: ApplicationProfile) -> None:
    """Fill split date widgets whose question has an MM/DD/YYYY answer ("Date Available", "earliest start date")."""
    for selector, month_suffix, part_suffixes in _DATE_PARTS.values():
        for month in page.locator(selector).all():
            if month.get_attribute("data-jobagent-field"):
                continue
            label = month.evaluate(_DATE_LABEL_SCRIPT)
            prefix = (month.get_attribute("id") or "").removesuffix(month_suffix)
            parts = [page.locator(f"#{prefix}{suffix}") for suffix in part_suffixes]
            for target in parts:
                if target.count():
                    target.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")
            action = decide(FieldDescriptor(label=label, kind="text"), profile, resume_path=None)
            values = (action.value or "").split("/")
            if action.kind == "skip" or len(values) != 3:
                continue  # e.g. My Experience From/To, filled from the truth files instead
            for target, value in zip(parts, values, strict=True):
                if target.count():
                    target.focus()
                    target.press_sequentially(value, delay=50)


_SCHOOL_INPUT = 'input[id$="--schoolName"], input[id$="--school"]'
_SCHOOL_DONE = 'input[id$="--schoolName"][data-jobagent-field], input[id$="--school"][data-jobagent-field]'
_PROMPT_OPTIONS = '[data-popper-placement] [data-automation-id="promptOption"]'  # the open list, not selected pills


def _choices(alternatives: str) -> list[str]:
    return [choice.strip() for choice in alternatives.split("|") if choice.strip()]


def _pills(page: Page, selector: str) -> Locator:
    """The chosen-item pills of one Workday prompt (empty when it is not inside a multiselect container)."""
    container = page.locator(selector).first.locator("xpath=ancestor::*[@data-automation-id='multiSelectContainer'][1]")
    return container.locator('[data-automation-id="selectedItem"]')


def _chosen_labels(page: Page, selector: str) -> list[str]:
    """The labels a Workday multi-select prompt already shows as chosen (pills)."""
    return [text.strip() for text in _pills(page, selector).all_inner_texts()]


def _pill_forms(label: str) -> set[str]:
    """"Active Directory (AD)" -> {"active directory ad", "active directory", "ad"}: case/punctuation-free."""
    inner = re.findall(r"\(([^)]*)\)", label)
    outer = re.sub(r"\([^)]*\)", " ", label)
    return {form for form in (normalize_title(label), normalize_title(outer), *map(normalize_title, inner)) if form}


def _already_chosen(present: list[str], choices: list[str]) -> bool:
    """A choice (or its alias) is already a pill, compared without case, punctuation or "(AD)" parentheticals."""
    have = set().union(*map(_pill_forms, present)) if present else set()
    return any(normalize_title(choice) in have for choice in choices if normalize_title(choice))


def _remove_pills(page: Page, selector: str, labels: list[str]) -> None:
    """Remove chosen pills by label with their delete charm (a pill's own x; never a form control)."""
    for label in labels:
        pill = _pills(page, selector).filter(has_text=label).first
        if pill.count():
            pill.locator('[data-automation-id="DELETE_charm"]').click()
            page.wait_for_timeout(300)


def _search_pick(page: Page, selector: str, choices: list[str], *, prefix_ok: bool = False, selected: set[str] | None = None) -> bool:
    """Pick from a Workday search prompt: type, press Enter, click the first matching result.

    Enter here only runs the prompt's search (Workday shows "No Items" until it is pressed);
    it is pressed inside a ``selectinput`` search box, never on a form or button. With
    ``prefix_ok`` (skills) "Python" also accepts "Python (Programming Language)" and "DNS" accepts
    "Domain Name System (DNS)". A label already in ``selected`` counts as done and is never
    clicked again (a second click would deselect it). No match: left empty.
    """
    box = page.locator(selector).first
    if not box.count():
        return False
    box.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")
    if _already_chosen(_chosen_labels(page, selector), choices):  # Master's / Workday's pill: never search or toggle
        return True
    for choice in choices:
        before = _prompt_labels(page)
        pills_before = [text.strip() for text in _pills(page, selector).all_inner_texts()]
        try:
            box.click(timeout=2_000)
        except PlaywrightTimeoutError:
            box.focus()  # a long row of chosen pills can cover the box
        box.fill("")
        box.press_sequentially(choice, delay=30)
        box.press("Enter")
        labels: list[str] = []
        previous: list[str] | None = None
        for _ in range(20):  # wait for this search's results to settle, not the type-ahead list mid-update
            page.wait_for_timeout(250)
            labels = _prompt_labels(page)
            if labels and labels != before and labels == previous:
                break
            previous = labels
        index = _match_label(labels, choice, prefix_ok=prefix_ok, canonical=choice == choices[0])
        new = Counter(text.strip() for text in _pills(page, selector).all_inner_texts()) - Counter(pills_before)
        added = list(new.elements())
        if index is None and added:  # Workday picked its only result on Enter ("Management Information Systems")
            _remove_pills(page, selector, added)
        elif duplicates := [label for label in added if label in pills_before]:  # auto-picked a pill already chosen
            _remove_pills(page, selector, duplicates)  # removes the extra copy this search made; one always stays
        if index is not None:
            present = _chosen_labels(page, selector)
            if _already_chosen(present, [labels[index]]):  # another skill already chose this option: never twice
                box.press("Escape")
                box.fill("")
                return True
            if labels[index].casefold() not in (selected or set()):
                # By label, not position: the list can re-render between reading it and clicking.
                page.locator(f"{_PROMPT_OPTIONS}[data-automation-label={json.dumps(labels[index])}]").first.click()
                page.wait_for_timeout(400)
            box.press("Escape")
            _mark_filled(page, selector, "truth")
            return True
        box.press("Escape")
    box.fill("")  # no exact option: leave the prompt empty, not holding typed text
    return False


def _prompt_labels(page: Page) -> list[str]:
    return [label or "" for label in page.locator(_PROMPT_OPTIONS).evaluate_all(
        "es => es.map(e => e.getAttribute('data-automation-label'))"
    )]


def _expands(label: str, acronym: str) -> bool:
    """"Domain Name System (DNS)": the words before the parenthesis spell the acronym (not "Orca Cloud ... (CSPM)")."""
    words = normalize_title(label.rsplit("(", 1)[0]).split()
    return "".join(word[0] for word in words) == normalize_title(acronym).replace(" ", "")


def _match_label(labels: list[str], choice: str, *, prefix_ok: bool, canonical: bool = True) -> int | None:
    """Exact (case/punctuation-free) match. With ``prefix_ok``, and only for a skill's canonical name: "Python"
    accepts "Python (Programming Language)" and "DNS" accepts "Domain Name System (DNS)". An alias never matches
    a parenthetical: "RDS" is not "Microsoft Remote Desktop Services (RDS)", "ETL" is not "ETL (Informatica)"."""
    wanted = choice.casefold()
    rules = [lambda label: label == wanted, lambda label: normalize_title(label) == normalize_title(wanted)]
    if prefix_ok and canonical:
        rules += [lambda label: label.startswith(f"{wanted} ("),
                  lambda label: label.endswith(f"({wanted})") and _expands(label, wanted)]
    for rule in rules:
        index = next((i for i, label in enumerate(labels) if rule(label.casefold())), None)
        if index is not None:
            return index
    return None


# The smallest ancestor of one entry's field that also holds that entry's own Delete button.
_ENTRY_DELETE_SCRIPT = """(field) => {
    for (let box = field.parentElement; box && box !== document.body; box = box.parentElement) {
        if (box.querySelectorAll('input[id$="--jobTitle"]').length > 1) return false;
        const button = Array.from(box.querySelectorAll("button")).find((b) =>
            /^delete$/i.test((b.innerText || b.getAttribute("aria-label") || "").trim())
            || /delete/i.test(b.getAttribute("data-automation-id") || ""));
        if (button) { button.click(); return true; }
    }
    return false;
}"""


def _delete_entry(page: Page, field: Locator) -> bool:
    """Remove one repeated entry (a parsed job) with its own Delete button; never a form control."""
    try:
        deleted = bool(field.evaluate(_ENTRY_DELETE_SCRIPT))
    except PlaywrightError:
        return False
    page.wait_for_timeout(500)
    return deleted


def _add_entry(page: Page, section: Locator, first_field: str) -> str:
    """Click the section's "Add" (inserts a blank entry) and return the new entry's id prefix.

    ``first_field`` is an id suffix ("jobTitle") or a full selector for tenants that vary it.
    """
    selector = first_field if first_field.startswith("input") else f'[id$="--{first_field}"]'
    before = page.locator(selector).count()
    new_field = page.locator(selector).nth(before)
    for _ in range(2):  # a first click can be spent closing a still-open dropdown
        section.locator('button[data-automation-id="add-button"]').first.click()
        try:
            new_field.wait_for(timeout=3_000)
            break
        except PlaywrightTimeoutError:
            continue
    new_field.wait_for()
    element_id = page.locator(selector).nth(before).get_attribute("id") or ""
    return element_id.rsplit("--", 1)[0]


def _type(page: Page, selector: str, value: str, source: str = "truth") -> None:
    target = page.locator(selector)
    if value and target.count():
        target.fill(value)
        target.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")
        _mark_filled(page, selector, source)


def _date(page: Page, prefix: str, year_month: str) -> None:
    """Workday dates are Month/Year spinbuttons: click each and type the digits."""
    year, _, month = year_month.partition("-")
    for part, value in (("Month", month), ("Year", year)):
        target = page.locator(f"#{prefix}-dateSection{part}-input")
        if value and target.count():
            target.focus()  # a display overlay covers the spinbutton, so focus instead of clicking
            target.press_sequentially(value, delay=50)


def _pick(page: Page, selector: str, value: str) -> None:
    """Open a dropdown button and click the option ``value`` names; leave it for Master if none match."""
    button = page.locator(selector)
    if not button.count():
        return
    button.evaluate("e => e.setAttribute('data-jobagent-field', '-1')")
    for _ in range(2):  # Workday sometimes loads the list slowly (Soylent's degree list): reopen once
        button.click()  # a listbox toggle (type="button"), never a submit control
        try:
            page.locator(_VISIBLE_OPTIONS).first.wait_for(timeout=6_000)
            page.wait_for_timeout(300)  # let the rest of the list render
            break
        except PlaywrightTimeoutError:
            button.press("Escape")
    options = _options(page)
    matched = match_option(tuple(options), value)
    if matched is None:
        button.press("Escape")
        print(f"No dropdown option matched {value!r}; options: {options[:12]}", flush=True)
        return
    page.locator(_VISIBLE_OPTIONS).nth(options.index(matched)).click()
    page.wait_for_timeout(500)
    _mark_filled(page, selector, "truth")


# Which step the human is on: address + visible headings + open dialogs (Eightfold's Apply is a modal).
_STEP_SIGNATURE = """() => {
    const visible = (e) => e.getClientRects().length > 0;
    const headings = Array.from(document.querySelectorAll("h1, h2, h3")).filter(visible).map((e) => e.innerText.trim());
    const dialogs = Array.from(document.querySelectorAll('[role="dialog"], dialog[open]')).filter(visible).length;
    return location.pathname + "|" + dialogs + "|" + headings.join("|").slice(0, 500);
}"""
_SETTLE_SECONDS = 6  # follow-up questions an answer reveals (race after "Hispanic/Latino?") appear within this


def _watch_steps(
    page: Page,
    form: ApplicationForm,
    profile: ApplicationProfile,
    *,
    resume_path: Path | None,
    cover_letter_path: Path | None,
    oversight: Oversight | None = None,
    initial: list[FilledField] | None = None,
) -> list[FilledField]:
    """Fill each wizard step when the human reaches it, then leave it alone for review.

    It never clicks Continue/Next/Submit: Master does, and the new step (a URL, heading or dialog
    change) is filled when it appears. Only empty fields are filled.

    A step is filled for a few seconds after it appears (to catch revealed follow-ups) and never
    again, so edits Master makes are not overwritten while he reviews. When it has settled, the
    step is handed to ``oversight`` once (snapshot, review).
    """
    results: list[FilledField] = list(initial or [])
    try:
        current = page.evaluate(_STEP_SIGNATURE)
    except PlaywrightError:
        return results
    settle_until = time.monotonic() + _SETTLE_SECONDS
    step, settled = 1, False
    while True:  # ponytail: 2s polling until Master closes the window
        open_pages = [candidate for candidate in page.context.pages if not candidate.is_closed()]
        if not open_pages:  # window closed
            break
        try:
            if open_pages[-1] is not page:  # "Apply" opened the form in a new tab (or this tab closed): follow it
                page = open_pages[-1]
                page.wait_for_load_state("domcontentloaded")
                page.set_default_timeout(5_000)
            page.wait_for_timeout(2_000)
            signature = page.evaluate(_STEP_SIGNATURE)
            if oversight is not None:
                oversight.poll(page, signature if signature == current else "")
            if signature != current:  # Master moved to a new step
                current, settle_until = signature, time.monotonic() + _SETTLE_SECONDS
                step, settled = step + 1, False
            if time.monotonic() <= settle_until:
                _workday_autofill_upload(page, resume_path)
                filled = _workday_my_experience(page, profile, resume_path)
                filled += _fill_page(page, form, profile, resume_path=resume_path, cover_letter_path=cover_letter_path,
                                     drafter=oversight.drafter if oversight else None)
                results += [replace(item, step=step) for item in filled]
            elif not settled:
                settled = True
                if oversight is not None:
                    snapshot = oversight.after_step(page, form, current, [item for item in results if item.step == step])
                    oversight.practice_step(page, snapshot)
        except PlaywrightError:
            # Usually a navigation mid-check (sign-in, Save and Continue destroys the page context);
            # retry on the next poll. Only an actually closed window ends the watch.
            time.sleep(1)
    return results


def _fill_page(
    page: Page,
    form: ApplicationForm,
    profile: ApplicationProfile,
    *,
    resume_path: Path | None,
    cover_letter_path: Path | None = None,
    drafter: Callable[[str], str | None] | None = None,
) -> list[FilledField]:
    """Fill every recognised control. ``drafter`` (AI answers) fills empty free-text questions no rule covers."""
    results: list[FilledField] = []
    for _ in range(3):  # answers can reveal follow-ups (race appears after "Hispanic/Latino?")
        try:
            _split_dates(page, profile)
        except PlaywrightError:
            pass
        descriptors_raw = page.evaluate(_DESCRIPTOR_SCRIPT, form.form_selector)
        if not descriptors_raw:
            break
        for raw in descriptors_raw:
            descriptor = FieldDescriptor(
                label=raw["label"],
                kind=raw["kind"],
                name=raw["name"],
                required=raw["required"],
                options=tuple(raw["options"]),
                occurrence=raw.get("occurrence", 0),
            )
            action = decide(
                replace(descriptor, kind="text") if descriptor.kind == "combobox" else descriptor,
                profile, resume_path=resume_path, cover_letter_path=cover_letter_path,
            )
            if (drafter is not None and action.kind == "skip" and action.source == "custom"
                    and wants_draft(descriptor.label, descriptor.kind) and _is_empty(page, raw["index"])):
                drafted = drafter(descriptor.label)
                if drafted:
                    action = FillAction("value", value=drafted, source="ai_draft")
                    print(f"AI draft typed into {descriptor.label[:70]!r}: review it before submitting.", flush=True)
            source, value = action.source, ""
            try:
                if descriptor.kind == "combobox":
                    outcome, reason, value = _apply_combobox(page, raw["index"], descriptor, profile)
                else:
                    outcome, reason = _apply(page, raw["index"], descriptor, action)
                    value = action.value or (action.file_path.name if action.file_path else "")
                if outcome == "filled" and reason == "":
                    _mark_filled(page, f'[data-jobagent-field="{raw["index"]}"]', source)
                else:
                    value = ""
            except PlaywrightError as error:  # one stubborn field must not stop the rest
                outcome, reason, value = "skipped", f"Browser error: {str(error)[:80]}", ""
            if outcome == "skipped" and descriptor.required:
                outcome = "required_empty"
            results.append(FilledField(descriptor.label, descriptor.kind, outcome, reason, descriptor.options, value, source))
    return results


def _is_empty(page: Page, field_index: int) -> bool:
    target = page.locator(f'[data-jobagent-field="{field_index}"]').first
    return not target.input_value().strip()


def _mark_filled(page: Page, selector: str, source: str) -> None:
    """Tag what JobAgent filled (and by which rule), so a snapshot can tell it from Master's typing."""
    page.locator(selector).evaluate_all("(es, s) => es.forEach((e) => e.setAttribute('data-jobagent-filled', s))",
                                        source or "jobagent")


# Search-as-you-type boxes take only these answers (a city, a country, a school); never Yes/No or a date.
_TYPED_SOURCES = ("contact.", "education.")
# Workday lists the field's current selection as role="option" pills too; those aren't choices.
_VISIBLE_OPTIONS = '[role="option"]:visible:not([data-automation-id="selectedItem"])'


def _options(page: Page) -> list[str]:
    return [text.strip() for text in page.locator(_VISIBLE_OPTIONS).all_inner_texts()]


def _apply_combobox(
    page: Page, field_index: int, descriptor: FieldDescriptor, profile: ApplicationProfile
) -> tuple[str, str, str]:
    """Open the menu, read its options, and click the one ``decide`` matches, like a native select.

    Workday menus nest (Career Websites > Corporate Website): after each click, if the field still
    has no selection and a new level of options appeared, the same answer is matched again.
    """
    target = page.locator(f'[data-jobagent-field="{field_index}"]')
    container = target.locator("xpath=ancestor::*[@data-automation-id='multiSelectContainer'][1]")
    pills = container.locator('[data-automation-id="selectedItem"]')
    try:
        if pills.count():  # Workday already holds a value: keep it if right, clear it if wrong
            current = tuple(text.strip() for text in pills.all_inner_texts())
            if decide(replace(descriptor, kind="select", options=current), profile, resume_path=None).kind != "skip":
                return "filled", "already set", ""
            for _ in range(pills.count()):
                pills.first.locator('[data-automation-id="DELETE_charm"]').click()  # removes a pill; not a submit
                page.wait_for_timeout(300)
        opener = target.evaluate(
            "e => e.tagName === 'BUTTON' || e.tagName === 'DIV' || e.dataset.uxiWidgetType === 'selectinput'"
        )
        if opener:
            target.click()  # a listbox toggle / search box, never a submit control
        else:
            target.focus()
            target.press("ArrowDown")  # opens the menu; never Enter
        for attempt in range(2):
            try:  # menus can render a beat after opening (Workday: "Select One" first, choices later)
                page.locator(_VISIBLE_OPTIONS).nth(1).wait_for(timeout=2_500)
                break
            except PlaywrightTimeoutError:
                if opener and attempt == 0:
                    target.click()  # the first click only blurred the previous text field
        options = _options(page)
        if not options:  # search-as-you-type box (e.g. city): type the configured answer first
            typed = decide(replace(descriptor, kind="text"), profile, resume_path=None)
            if typed.kind == "skip" or not typed.value:
                return "skipped", typed.reason or "No configured answer for this field", ""
            if not typed.source.startswith(_TYPED_SOURCES):
                target.press("Escape")  # "Yes" or a date typed into a search box picks whatever it finds
                return "skipped", "Menu showed no options; only contact and education answers are typed", ""
            target.press_sequentially(typed.value, delay=50)
            try:
                page.locator(_VISIBLE_OPTIONS).first.wait_for(timeout=5_000)
            except PlaywrightTimeoutError:
                pass
            options = _options(page)
            # Several "Boston"s (England, Virginia, ...): take the one in Master's own state.
            state = profile.contact.state.casefold()
            local = [o for o in options if state and typed.value.casefold() in o.casefold() and state in o.casefold()]
            if local:
                page.locator(_VISIBLE_OPTIONS).nth(options.index(local[0])).click()
                return "filled", "", local[0]
        elif target.evaluate("e => e.tagName") == "INPUT":
            # Oracle-style search box: the first list shown is just the top of the catalog
            # (Afghanistan, Albania, ...). If the answer isn't there, type it and read the filtered list.
            first = decide(replace(descriptor, kind="select", options=tuple(options)), profile, resume_path=None)
            typed = decide(replace(descriptor, kind="text"), profile, resume_path=None)
            if first.kind == "skip" and typed.kind != "skip" and typed.value:
                target.fill("")
                target.press_sequentially(typed.value, delay=50)
                page.wait_for_timeout(1_500)
                options = _options(page) or options
        for level in range(3):
            action = decide(replace(descriptor, kind="select", options=tuple(options)), profile, resume_path=None)
            if action.kind == "skip" or action.value is None:
                target.press("Escape")
                return "skipped", action.reason if level == 0 else f"No submenu option matched: {action.reason}", ""
            page.locator(_VISIBLE_OPTIONS).nth(options.index(action.value)).click()
            page.wait_for_timeout(800)
            next_options = _options(page)
            if pills.count() or not next_options or next_options == options:
                if opener:
                    target.press("Escape")  # close Workday's still-open list
                return "filled", "", action.value
            options = next_options
        target.press("Escape")
        return "skipped", "Menu nested deeper than expected", ""
    except PlaywrightTimeoutError as error:
        return "skipped", f"Combobox did not respond: {str(error)[:80]}", ""


def _apply(page: Page, field_index: int, descriptor: FieldDescriptor, action: FillAction) -> tuple[str, str]:
    """Apply one decided action. Never clicks, never submits, never presses Enter."""
    if action.kind == "skip":
        return "skipped", action.reason
    target: Locator = page.locator(f'[data-jobagent-field="{field_index}"]')
    if action.kind == "file":
        assert action.file_path is not None
        if _already_attached(target):  # each file input gets the file at most once (steps, re-fills, restarts)
            return "filled", "already attached"
        target.set_input_files(str(action.file_path))
        target.evaluate("e => e.setAttribute('data-jobagent-uploaded', '1')")
        page.wait_for_timeout(3_000)  # Rippling parses the resume and autofills; let it finish first
        return "filled", ""
    assert action.value is not None
    if descriptor.kind in {"text", "textarea"} and target.input_value().strip():
        return "filled", "already has a value; left as is"  # never overwrite what Master (or a saved draft) typed
    if descriptor.kind == "select":
        target.select_option(label=action.value)
    elif descriptor.kind == "radio":
        option_index = descriptor.options.index(action.value)
        radio = page.locator(f'[data-jobagent-field="{field_index}"][data-jobagent-option-index="{option_index}"]')
        if radio.evaluate("e => e.tagName") == "DIV":  # Rippling checkbox tile: plain div, cannot submit
            if radio.get_attribute("aria-checked") != "true":
                radio.click()
            return "filled", ""
        if radio.evaluate("e => e.tagName") == "BUTTON":
            # Ashby Yes/No toggle. Only ever clicked when it belongs to no <form>, so it cannot submit.
            if radio.evaluate("e => e.form !== null"):
                return "skipped", "Toggle button belongs to a form; left for you"
            radio.click()
            return "filled", ""
        try:
            radio.check()
        except PlaywrightError:  # custom-drawn radios (Workday): click the option's own label instead
            page.locator(f'label[for="{radio.get_attribute("id")}"]').click()
    elif descriptor.kind == "checkbox":
        if action.value.strip().casefold() in {"yes", "true", "checked"}:
            target.check()
        else:
            target.uncheck()
    elif target.get_attribute("aria-autocomplete") == "list" or target.get_attribute("name") == "location":  # Ashby, Lever
        _autocomplete(page, target, action.value)
    elif _TYPE_DELAY_MS:
        delay = min(_TYPE_DELAY_MS, 20_000 / max(len(action.value), 1))  # long answers: ~20 s of typing at most
        target.fill("")
        target.press_sequentially(action.value, delay=delay, timeout=len(action.value) * delay + 30_000)
    else:
        target.fill(action.value)
    return "filled", ""


# ponytail: module-level, set per fill_application_form call; thread it through if fills ever run concurrently
_TYPE_DELAY_MS = 0


def _autocomplete(page: Page, target: Locator, value: str) -> None:
    """Type-ahead location box (Ashby, Lever): type the first part, click the option that reads exactly ``value``.

    No exact option: the typed text stays for Master to finish. Options are list items, never submit controls.
    """
    target.fill("")
    target.press_sequentially(value.split(",")[0], delay=_TYPE_DELAY_MS or 30)
    options = page.locator('[role="option"], .dropdown-location')  # Ashby, Lever
    try:
        options.first.wait_for(timeout=4_000)
    except PlaywrightTimeoutError:
        return
    labels = [text.strip().casefold() for text in options.all_inner_texts()]
    if value.strip().casefold() in labels:
        options.nth(labels.index(value.strip().casefold())).click()
