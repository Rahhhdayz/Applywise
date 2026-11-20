"""Read-only per-step snapshots of a filled form: what each visible control asks and now holds.

A snapshot is plain data (label, question, kind, options, value, required, filled-by-JobAgent and the
rule key that filled it) written to ``logs/fill_snapshots/<job>/<step>.json`` with a screenshot beside
it. The script only reads the page; page text in it is untrusted data for the restricted reviewer.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

SNAPSHOT_ROOT = Path("logs/fill_snapshots")

# Shared with the filler's descriptor script, so a snapshot's labels are the labels the rules saw.
LABEL_FUNCTIONS = """
    function leverQuestion(el) {
        const card = el.closest(".application-question.custom-question");
        const text = card ? card.querySelector(".application-label .text") : null;
        return text ? (text.innerText || text.textContent || "").trim() : "";
    }

    function labelFor(el) {
        // Workday: the question text is the <label>/<legend> of the surrounding formField container.
        const field = el.closest('[data-automation-id^="formField-"]');
        if (field) {
            const own = field.querySelector("legend label, legend, label");
            const text = own ? (own.innerText || own.textContent || "") : "";
            if (text.trim() && !(el.type === "radio" || el.type === "checkbox")) return text;
        }
        // Rippling: the question is a "...-label" span inside, or the paragraph just before, the field block.
        const block = el.closest('[data-testid="field"]');
        if (block) {
            const own = block.querySelector('[id$="-label"]');
            const prev = block.previousElementSibling;
            const text = (own && own.innerText) || (prev && prev.innerText) || "";
            if (text.trim()) return text;
        }
        // Lever custom questions: the text sits in the question card, not in a <label> for the control.
        const lever = leverQuestion(el);
        if (lever && !(el.type === "radio" || el.type === "checkbox")) return lever;
        if (el.labels && el.labels.length > 0) {
            let text = el.labels[0].innerText || el.labels[0].textContent || "";
            // JazzHR wraps the <select> in its <label>; drop the option text or "Hispanic or Latino" leaks in.
            if (el.labels[0].contains(el)) text = text.replace(el.innerText || "", "");
            if (text.trim()) return text;
        }
        const ariaLabel = el.getAttribute("aria-label");
        if (ariaLabel) return ariaLabel;
        const labelledBy = el.getAttribute("aria-labelledby");
        if (labelledBy) {
            const referenced = document.getElementById(labelledBy);
            if (referenced) {
                const text = referenced.innerText || referenced.textContent || "";
                if (text.trim()) return text;
            }
        }
        const placeholder = el.getAttribute("placeholder");
        // Ashby search boxes ("Start typing...") carry their question on the surrounding field entry.
        const entry = el.closest(".ashby-application-form-field-entry, [class*='_fieldEntry_']");
        const entryLabel = entry && entry.querySelector("label") ? entry.querySelector("label").innerText : "";
        if (placeholder || entryLabel) return `${placeholder || ""} ${entryLabel}`.trim();
        return el.getAttribute("name") || "";
    }

"""

_SNAPSHOT_SCRIPT = """
(formSelector) => {
    const form = document.querySelector(formSelector) || document.body;
""" + LABEL_FUNCTIONS + """
    const visible = (e) => e.getClientRects().length > 0;
    const text = (e) => (e ? (e.innerText || e.textContent || "") : "").trim();
    const question = (el) => {
        const box = el.closest('[data-automation-id^="formField-"], fieldset, [data-testid="field"], '
            + '.application-question, .ashby-application-form-field-entry');
        return text(box).slice(0, 400);
    };
    const SECRET_NAME = /password|passcode|\\bpin\\b|security.?code|verification.?code|one.?time|otp/i;
    const out = [];
    const seenGroups = new Set();
    const controls = form.querySelectorAll("input, select, textarea, button[aria-haspopup='listbox'], [role='combobox']");
    for (const el of controls) {
        const tag = el.tagName.toLowerCase();
        const type = (el.getAttribute("type") || "text").toLowerCase();
        if (tag === "input" && ["submit", "button", "image", "hidden", "reset"].includes(type)) continue;
        if (el.closest("#jobagent-oversight")) continue;
        if (type !== "file" && !visible(el)) continue;
        const filledBy = el.getAttribute("data-jobagent-filled") || "";
        const base = {
            field: el.getAttribute("data-jobagent-field"),
            required: Boolean(el.required) || el.getAttribute("aria-required") === "true",
        };
        if (tag === "input" && type === "radio") {
            const name = el.getAttribute("name") || "";
            if (!name || seenGroups.has(name)) continue;
            seenGroups.add(name);
            const group = Array.from(form.querySelectorAll(`input[type="radio"][name="${CSS.escape(name)}"]`));
            const fieldset = el.closest("fieldset");
            const legend = fieldset ? fieldset.querySelector("legend") : null;
            const checked = group.find((radio) => radio.checked);
            const by = group.map((radio) => radio.getAttribute("data-jobagent-filled") || "").find(Boolean) || "";
            out.push({...base, label: (legend ? text(legend) : (leverQuestion(el) || name)), question: question(el),
                kind: "radio", options: group.map((radio) => (labelFor(radio) || "").trim()),
                value: checked ? (labelFor(checked) || "").trim() : "",
                required: group.some((radio) => radio.required), filled_by_jobagent: Boolean(by), source: by});
            continue;
        }
        let kind = "text", options = [], value = "";
        if (tag === "select") {
            kind = "select";
            options = Array.from(el.options).map((o) => (o.textContent || "").trim()).filter(Boolean);
            const chosen = el.selectedOptions[0];
            value = chosen && chosen.value !== "" ? (chosen.textContent || "").trim() : "";
        } else if (tag === "textarea") {
            kind = "textarea"; value = el.value;
        } else if (type === "checkbox") {
            kind = "checkbox"; value = el.checked ? "checked" : "";
        } else if (type === "file") {
            kind = "file"; value = el.files && el.files.length ? el.files[0].name : "";
        } else if (tag === "button" || el.getAttribute("role") === "combobox"
            || el.getAttribute("data-uxi-widget-type") === "selectinput") {
            kind = "combobox";
            const box = el.closest('[data-automation-id="multiSelectContainer"]');
            const pills = box ? Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]')).map(text) : [];
            value = pills.length ? pills.join(" | ") : (tag === "input" ? el.value : text(el));
            if (/^select one$/i.test(value)) value = "";
        } else {
            value = el.value;
        }
        const label = (labelFor(el) || "").trim();
        const names = [label, el.getAttribute("name"), el.getAttribute("id"), el.getAttribute("autocomplete"),
            el.getAttribute("data-automation-id"), el.getAttribute("aria-label")].join(" ");
        if (value && (type === "password" || SECRET_NAME.test(names) || (tag === "input" && el.maxLength === 1))) value = "[redacted]";
        out.push({...base, label, question: question(el), kind, options, value,
            filled_by_jobagent: Boolean(filledBy), source: filledBy});
    }
    return out;
}
"""


_TOKEN_PATH = re.compile(r"(/(?:activate|verify|token|reset)[^/?#]*/)[^?#]+", re.IGNORECASE)


def redact(data: Any, secret: str) -> Any:
    """Replace the account password anywhere in a snapshot, before it is written or reviewed."""
    if not secret:
        return data
    if isinstance(data, str):
        return data.replace(secret, "[redacted]")
    if isinstance(data, dict):
        return {key: redact(value, secret) for key, value in data.items()}
    if isinstance(data, list | tuple):
        return [redact(value, secret) for value in data]
    return data


def read_fields(page: Page, form_selector: str) -> list[dict[str, Any]]:
    """Every visible control's state, read-only."""
    return page.evaluate(_SNAPSHOT_SCRIPT, form_selector)


def take_snapshot(
    page: Page,
    form_selector: str,
    directory: Path,
    *,
    job_id: int,
    form_url: str,
    step: int,
    signature: str = "",
    fill: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write ``<directory>/<step>.json`` (and ``.png``) and return the snapshot."""
    snapshot = {
        "job_id": job_id,
        "form_url": form_url,
        "page_url": _TOKEN_PATH.sub(r"\1[redacted]", page.url),
        "step": step,
        "signature": signature,
        "taken_at": datetime.now(UTC).isoformat(),
        "fields": read_fields(page, form_selector),
        "fill": fill or [],
    }
    snapshot = redact(snapshot, os.getenv("WORKDAY_PASSWORD", ""))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{step:02d}.json").write_text(json.dumps(snapshot, indent=2))
    try:
        page.screenshot(path=str(directory / f"{step:02d}.png"), full_page=True)
    except PlaywrightError:
        pass  # ponytail: a screenshot is a nice-to-have; the JSON is what the reviewer reads
    return snapshot


def next_step(directory: Path) -> int:
    """Step numbers continue after snapshots an earlier fill of this job left."""
    return 1 + max((int(path.stem) for path in directory.glob("[0-9][0-9].json")), default=0)


def load_snapshot(job_id: int, step: int | None = None, root: Path = SNAPSHOT_ROOT) -> dict[str, Any]:
    """One saved snapshot (the newest step when ``step`` is None)."""
    directory = root / str(job_id)
    steps = sorted(directory.glob("[0-9][0-9].json"))
    if not steps:
        raise FileNotFoundError(f"No snapshots for job {job_id} in {directory}")
    path = directory / f"{step:02d}.json" if step is not None else steps[-1]
    return json.loads(path.read_text())
