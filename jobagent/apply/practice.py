"""Practice mode (training rounds only): click a wizard's Next / Continue / Save and Continue for Master.

Off unless asked for (``apply train`` / ``apply fill --practice``). It advances only after the step
was filled, snapshotted and reviewed, and stops for good on the Review page or as soon as any
Submit-like or final Apply control is visible. Every click goes through ``guard_click``, which
raises ``SubmitBlocked`` for anything that looks like a submit: there is no override.
"""

from __future__ import annotations

from typing import Any

from playwright.sync_api import Page

from jobagent.normalization import normalize_title

ADVANCE_TEXTS = ("save and continue", "continue", "next")  # exact visible text, nothing else
_FINAL_TEXTS = {"apply", "apply now", "finish", "send", "send application", "complete application", "confirm and apply",
                "review and submit", "done"}
_ACCOUNT_HEADINGS = {"sign in", "create account", "create account sign in"}  # Workday's account step, before step 1
_ATTRIBUTES = ("type", "data-automation-id", "id", "name", "aria-label", "value", "class", "title", "formaction")
_PREFERRED_IDS = ("pageFooterNextButton", "bottom-navigation-next-button")


class SubmitBlocked(RuntimeError):
    """A click on a submit-like control was attempted. This is a bug, never a user choice."""


def looks_like_submit(text: str, attributes: dict[str, str]) -> bool:
    """Submit-ish by text ("Submit", "Apply", "Finish") or by any attribute, or a button that would submit a form."""
    words = normalize_title(text)
    if "submit" in words or words in _FINAL_TEXTS or words.startswith("apply ") or words.endswith(" apply"):
        return True
    if attributes.get("submits_form") == "true":
        return True
    return any("submit" in str(attributes.get(name, "")).casefold() for name in _ATTRIBUTES)


def guard_click(text: str, attributes: dict[str, str]) -> None:
    if looks_like_submit(text, attributes) or normalize_title(text) not in ADVANCE_TEXTS:
        raise SubmitBlocked(f"refused to click {text!r}: only Next / Continue / Save and Continue, never Submit")


_CONTROLS_SCRIPT = """() => {
    const visible = (e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden";
    const controls = Array.from(document.querySelectorAll(
        "button, input[type=submit], input[type=button], input[type=image], a[role=button], [role=button]"
    )).filter(visible).filter((e) => !e.closest("#jobagent-oversight"));
    const headings = Array.from(document.querySelectorAll("h1, h2, h3")).filter(visible).map((e) => (e.innerText || "").trim());
    const password = Array.from(document.querySelectorAll("input[type=password]")).some(visible);
    return {headings, password, controls: controls.map((e, index) => {
        e.setAttribute("data-jobagent-control", String(index));
        const attributes = {};
        for (const name of ATTRIBUTES) { const value = e.getAttribute(name); if (value) attributes[name] = value; }
        attributes.submits_form = String(e.type === "submit" && e.form !== null);
        return {index, text: (e.innerText || e.value || e.getAttribute("aria-label") || "").trim(), attributes};
    })};
}""".replace("ATTRIBUTES", repr(list(_ATTRIBUTES)))

# Re-checked in the page at the moment of the click: same text, still not submitting a form.
_CLICK_SCRIPT = """(e, expected) => {
    const text = (e.innerText || e.value || e.getAttribute("aria-label") || "").trim().toLowerCase().replace(/\\s+/g, " ");
    if (text !== expected || /submit/i.test(e.outerHTML.slice(0, 400)) || (e.type === "submit" && e.form !== null)) return false;
    e.click();
    return true;
}"""


def advance(page: Page) -> tuple[str, str]:
    """("clicked", text) | ("stop", why) | ("wait", why). Never clicks anything submit-like."""
    found: dict[str, Any] = page.evaluate(_CONTROLS_SCRIPT)
    if any(normalize_title(heading) == "review" for heading in found["headings"]):
        return "stop", "the Review page is open"
    if found["password"] and any(normalize_title(heading) in _ACCOUNT_HEADINGS for heading in found["headings"]):
        return "wait", "account step: waiting for sign-in (its Sign In / Create Account buttons are never practice clicks)"
    final = [item for item in found["controls"] if looks_like_submit(item["text"], item["attributes"])]
    if final:
        return "stop", f"a {final[0]['text'] or 'submit'!r} control is visible"
    candidates = [item for item in found["controls"] if normalize_title(item["text"]) in ADVANCE_TEXTS]
    if not candidates:
        return "wait", "no Next / Continue button on this step"
    chosen = next((item for item in candidates if item["attributes"].get("data-automation-id") in _PREFERRED_IDS), candidates[0])
    guard_click(chosen["text"], chosen["attributes"])
    target = page.locator(f'[data-jobagent-control="{chosen["index"]}"]').first
    if not target.evaluate(_CLICK_SCRIPT, " ".join(chosen["text"].casefold().split())):
        return "wait", "the button changed before the click"
    return "clicked", chosen["text"]
