"""What happens around a fill: per-step snapshot + review, practice advance, sign-in mail, "needs you".

Everything here is opt-in through ``Oversight``; a fill without one behaves exactly as before.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from jobagent.apply.ats import ApplicationForm, is_workday_url
from jobagent.apply.mailcodes import (
    TIMEOUT_SECONDS,
    MailReader,
    wait_for_code,
    wait_for_workday_link,
)
from jobagent.apply.practice import SubmitBlocked, advance, save_for_later
from jobagent.apply.profile import ApplicationProfile
from jobagent.apply.review import format_findings, is_allowed, save_review
from jobagent.apply.snapshot import SNAPSHOT_ROOT, next_step, redact, take_snapshot

if TYPE_CHECKING:
    from jobagent.apply.filler import FilledField, FillReport


@dataclass
class Oversight:
    """Per-fill extras. ``snapshot_root=None`` turns snapshots off (tests, ``fill_in_tabs``)."""

    job_id: int = 0
    job_label: str = ""
    company: str = ""
    job_title: str = ""  # unattended: matched against Workday's candidate home to confirm the draft
    snapshot_root: Path | None = SNAPSHOT_ROOT
    notify: Callable[[str], None] | None = None  # Telegram; None = terminal only
    mail: MailReader | None = None  # read-only Gmail for verification links and security codes
    mail_timeout: float = TIMEOUT_SECONDS
    drafter: Callable[[str], str | None] | None = None  # AI answers for free-text questions (ai_draft)
    profile: ApplicationProfile | None = None
    reviewer: Callable[[dict[str, Any], ApplicationProfile], list[dict[str, Any]]] | None = None
    reviewer_name: str = "claude"
    practice: bool = False  # training only: click Next / Continue / Save and Continue after review
    notify_findings: bool = False  # --notify: findings go to Telegram too
    unattended: bool = False  # --unattended (Telegram /fill): practice to the final step, then finish
    finished: str = ""  # unattended: why the run ended (set once; ends the step watch)
    at_final: bool = False  # unattended: the Review / final step was reached
    saved_for_later: bool = False  # unattended: an exact "Save for Later" was clicked on the final step
    fix: bool = False  # --oversight-fix: correct JobAgent-filled choice fields to validated allowed values
    _pending: tuple[Future[Any], dict[str, Any], Path] | None = None
    _executor: ThreadPoolExecutor | None = None
    sleep: Callable[[float], None] = time.sleep
    snapshots: list[dict[str, Any]] = field(default_factory=list)
    extra_fields: list[FilledField] = field(default_factory=list)  # e.g. the emailed security code
    code_handled: bool = False

    def needs_you(self, reason: str) -> None:
        """Stop and tell Master: terminal always, Telegram when configured."""
        message = f"needs you: {self.job_label or self.company or f'job {self.job_id}'} {reason}"
        print(message, flush=True)
        if self.notify is not None:
            try:
                self.notify(message)
            except Exception as error:  # noqa: BLE001 - a failed notification must not stop the fill
                print(f"Telegram notification failed: {type(error).__name__}", flush=True)

    def verify_workday(self, page: Page, since: datetime) -> bool:
        """Follow the newest Workday verification link (Workday hosts only) in a tab of this browser."""
        if self.mail is None:
            self.needs_you("Workday email verification (Gmail is not configured)")
            return False
        print("Workday asked for email verification; checking Gmail (read-only)...", flush=True)
        link, why = wait_for_workday_link(self.mail, since, timeout=self.mail_timeout, sleep=self.sleep)
        if link is None or not is_workday_url(link):
            self.needs_you(f"Workday email verification: {why or 'link is not a Workday address'}")
            return False
        tab = page.context.new_page()
        try:  # the link is a one-time credential: opened, never printed
            tab.goto(link, wait_until="domcontentloaded")
            tab.wait_for_timeout(3_000)
        finally:
            tab.close()
        return True

    def poll(self, page: Page, signature: str | None = None) -> None:
        """Called while the window is watched: shows a finished review; types an emailed security code once."""
        if self._pending is not None and self._pending[0].done():
            future, snapshot, directory = self._pending
            self._pending = None
            same_step = signature is None or signature == snapshot.get("signature")
            self._finish(page if same_step else None, snapshot, directory, future.result())
        if self.code_handled:
            return
        found = page.evaluate(_CODE_INPUT_SCRIPT)
        if not found:
            return
        from jobagent.apply.filler import FilledField

        self.code_handled = True
        label = "Security code"
        if self.mail is None or not self.company:
            reason = "Gmail is not configured" if self.mail is None else "no company to match"
        else:
            print("Security code requested; checking Gmail (read-only) for up to 3 minutes...", flush=True)
            since = datetime.now(UTC) - timedelta(seconds=30)  # the mail can beat the page by a moment
            code, reason = wait_for_code(self.mail, self.company, since, timeout=self.mail_timeout, sleep=self.sleep)
            if code is not None and found["boxes"] not in (1, len(code)):
                code, reason = None, "code format not recognised"
            if code is not None:
                chunks = [code] if found["boxes"] == 1 else list(code)
                for index, chunk in enumerate(chunks):
                    page.locator(f'[data-jobagent-code="{index}"]').fill(chunk)  # fill only: never a click
                self.extra_fields.append(FilledField(label, "text", "filled", "typed from email", source="email_code"))
                print("Security code typed from your email; review and submit yourself.", flush=True)
                return
        self.extra_fields.append(FilledField(label, "text", "skipped", reason, source="email_code"))
        self.needs_you(f"security code not found ({reason})")

    def after_step(self, page: Page, form: ApplicationForm, signature: str, fill: list[FilledField]) -> dict[str, Any] | None:
        """Snapshot one settled step once. Never navigates, never clicks."""
        if self.snapshot_root is None:
            return None
        directory = self.snapshot_root / str(self.job_id)
        try:
            snapshot = take_snapshot(
                page, form.form_selector, directory, job_id=self.job_id, form_url=form.form_url,
                step=next_step(directory), signature=signature, fill=[asdict(item) for item in fill],
            )
        except PlaywrightError as error:  # the page navigated mid-read: that step goes unrecorded
            print(f"Snapshot skipped: {str(error)[:80]}", flush=True)
            return None
        self.snapshots.append(snapshot)
        if self.reviewer is not None and self.profile is not None:
            if self.practice:  # practice advances only a reviewed step, so wait for it
                self._finish(page, snapshot, directory, self._run_review(snapshot))
            else:  # in the background: Master keeps working while Claude reads the step
                self.close()
                self._executor = self._executor or ThreadPoolExecutor(max_workers=1)
                self._pending = (self._executor.submit(self._run_review, snapshot), snapshot, directory)
        return snapshot

    def _run_review(self, snapshot: dict[str, Any]) -> list[dict[str, Any]] | Exception:
        assert self.reviewer is not None and self.profile is not None
        try:
            return self.reviewer(snapshot, self.profile)
        except Exception as error:  # noqa: BLE001 - a failed review (timeout, bad output) never blocks the fill
            return error

    def close(self) -> None:
        """Wait for a review still running (the window closed) and save it."""
        if self._pending is not None:
            future, snapshot, directory = self._pending
            self._pending = None
            self._finish(None, snapshot, directory, future.result())

    def _finish(self, page: Page | None, snapshot: dict[str, Any], directory: Path,
                result: list[dict[str, Any]] | Exception) -> None:
        """Save, print, and (on the same step) show findings on the page; then notify / fix if asked."""
        step = snapshot["step"]
        if isinstance(result, Exception):
            snapshot["review_error"] = str(result)[:200]
            print(f"Step {step} review failed (fill continues): {str(result)[:120]}", flush=True)
            return
        snapshot["findings"] = result
        save_review(directory, step, result, reviewer=self.reviewer_name)
        print(f"Step {step} review:\n{format_findings(result)}", flush=True)
        if not result:
            return
        if self.notify_findings and self.notify is not None:
            try:
                self.notify(f"JobAgent review {self.job_label} step {step}:\n{format_findings(result)}")
            except Exception as error:  # noqa: BLE001
                print(f"Telegram notification failed: {type(error).__name__}", flush=True)
        if page is not None:
            try:
                page.evaluate(_PANEL_SCRIPT, [{"field": item["field"], "text": f"[{item['problem']}] {item['label']}"
                                               + (f" -> {item['correct_value']}" if item["correct_value"] else "")}
                                              for item in result])
                if self.fix:
                    self._fix(page, result)
            except PlaywrightError as error:
                print(f"Review panel skipped: {str(error)[:80]}", flush=True)

    def _fix(self, page: Page, findings: list[dict[str, Any]]) -> None:
        """Only JobAgent-filled choice fields, only to a validated value that is one of the field's options.

        Selects an option or opens a listbox; never types over text, never navigates, never clicks a button.
        """
        from jobagent.apply.fields import FieldDescriptor, FillAction
        from jobagent.apply.filler import FilledField, _apply, _mark_filled, _pick

        for item in findings:
            index, correct, kind = str(item.get("field") or ""), item["correct_value"], item["kind"]
            if (item["problem"] != "wrong_value" or not item["filled_by_jobagent"] or not correct
                    or not index.isdigit() or kind not in {"select", "radio", "combobox"}
                    or self.profile is None or not is_allowed(correct, item["options"], self.profile)):
                continue  # re-checked here: only the candidate's own values, never a reviewer's
            selector = f'[data-jobagent-field="{index}"]'
            if kind == "combobox":
                _pick(page, selector, correct)  # opens the listbox and clicks the matching option only
            elif correct in item["options"]:
                _apply(page, int(index), FieldDescriptor(item["label"], kind, options=tuple(item["options"])),
                       FillAction("value", value=correct))
            else:
                continue
            _mark_filled(page, selector, "oversight_fix")
            self.extra_fields.append(FilledField(item["label"], kind, "filled", "corrected by review",
                                                 tuple(item["options"]), correct, "oversight_fix"))
            print(f"Oversight fix: {item['label']!r} -> {correct!r}", flush=True)

    def practice_step(self, page: Page, snapshot: dict[str, Any] | None) -> None:
        """Training rounds: advance a filled, snapshotted and reviewed step; stop for good at Submit."""
        if not self.practice:
            return
        if snapshot is None or "findings" not in snapshot:
            return self._stop_practice("this step was not snapshotted and reviewed")
        try:
            status, detail = advance(page)
        except SubmitBlocked as error:
            return self._stop_practice(str(error))
        if status == "stop":
            return self._final_step(page, detail) if self.unattended else self._stop_practice(detail)
        print(f"Practice: {'clicked ' + repr(detail) if status == 'clicked' else detail}", flush=True)

    def _final_step(self, page: Page, detail: str) -> None:
        """Unattended: the final step is open. Click an exact "Save for Later" if there is one; never Submit."""
        self.practice, self.at_final = False, True
        try:
            self.saved_for_later = save_for_later(page)
        except SubmitBlocked as error:
            print(f"Save for Later not clicked: {error}", flush=True)
        except PlaywrightError as error:
            print(f"Save for Later not clicked: {str(error)[:80]}", flush=True)
        self.finished = detail + ("; clicked Save for Later" if self.saved_for_later else "")
        print(f"Unattended: {self.finished}. JobAgent never submits.", flush=True)

    def _stop_practice(self, reason: str) -> None:
        self.practice = False
        if self.unattended:
            self.finished = reason
        self.needs_you(f"practice stopped: {reason}. JobAgent never submits; review, then submit or close the window")


# A security / verification code prompt: 4+ one-character boxes, or one input named for the code.
_CODE_INPUT_SCRIPT = """() => {
    const visible = (e) => e.getClientRects().length > 0;
    if (!/verification code was sent|security code|verification code/i.test(document.body.innerText || "")) return null;
    const inputs = Array.from(document.querySelectorAll("input")).filter((e) => visible(e)
        && !["hidden", "submit", "button", "checkbox", "radio", "file", "image", "reset"].includes((e.type || "text").toLowerCase()));
    const boxes = inputs.filter((e) => e.maxLength === 1);
    const describe = (e) => [e.labels && e.labels[0] ? e.labels[0].innerText : "", e.getAttribute("aria-label") || "",
        e.name || "", e.id || "", e.getAttribute("autocomplete") || ""].join(" ");
    const single = inputs.find((e) => /code|one-time-code/i.test(describe(e)) && /secur|verif|one-time/i.test(describe(e)));
    const chosen = boxes.length >= 4 ? boxes : (single ? [single] : []);
    if (!chosen.length || chosen.some((e) => e.value)) return null;  // nothing to fill, or already typed
    chosen.forEach((e, i) => e.setAttribute("data-jobagent-code", String(i)));
    return {boxes: chosen.length};
}"""


# A small fixed panel listing findings, with red outlines on the fields they name. Text only
# (textContent), pointer-events off so it never blocks Master's clicks; replaced on each review.
_PANEL_SCRIPT = """(items) => {
    const old = document.getElementById("jobagent-oversight");
    if (old) old.remove();
    const panel = document.createElement("div");
    panel.id = "jobagent-oversight";
    panel.style.cssText = "position:fixed;right:12px;bottom:12px;z-index:2147483647;max-width:380px;max-height:40vh;"
        + "overflow:auto;background:#fff;color:#111;border:2px solid #c00;border-radius:6px;padding:8px 10px;"
        + "font:12px/1.4 system-ui,sans-serif;pointer-events:none;box-shadow:0 2px 10px rgba(0,0,0,.25)";
    const title = document.createElement("b");
    title.textContent = `JobAgent review: ${items.length} finding(s)`;
    panel.appendChild(title);
    for (const item of items) {
        const line = document.createElement("div");
        line.textContent = item.text;
        panel.appendChild(line);
        if (item.field && /^[0-9]+$/.test(String(item.field))) {
            document.querySelectorAll(`[data-jobagent-field="${item.field}"]`).forEach((e) => {
                e.style.outline = "2px solid #c00"; e.style.outlineOffset = "2px";
            });
        }
    }
    document.body.appendChild(panel);
}"""


def unattended_summary(report: FillReport, oversight: Oversight, provider: str) -> str:
    """The Telegram message after an unattended fill: where the draft is, what to review. No field values
    except the AI-drafted answers Master must read; secrets are redacted again before it leaves."""
    lines = [f"JobAgent fill: {oversight.job_label or f'job {report.job_id}'}"]
    if report.draft == "confirmed":
        lines += ["Draft saved on Workday. Review and submit it yourself:", *report.links]
    elif report.draft == "not confirmed":
        lines += [("Draft NOT confirmed on Workday's candidate home. The filled form is open on your Mac; review and "
                   "submit there. Links to try:"), *report.links]
    else:
        lines.append(f"No draft support on {provider}: the filled form is open on your Mac. Review and submit there.")
    if report.finished:
        lines.append(f"Stopped: {report.finished}")
    filled = sum(item.outcome == "filled" for item in report.fields)
    skipped = sum(item.outcome == "skipped" for item in report.fields)
    steps = max((item.step for item in report.fields), default=1)
    lines.append(f"Steps: {steps}; filled {filled}, skipped {skipped}, required empty {len(report.required_empty)}")
    if report.required_empty:
        lines.append("Required, still empty: " + "; ".join(item.label[:60] for item in report.required_empty[:10]))
    drafts = [item for item in report.fields if item.source == "ai_draft" and item.outcome == "filled"]
    if drafts:
        lines.append("AI-drafted answers (read these before submitting):")
        lines += [f"- {item.label[:60]}: {item.value[:80]}…" for item in drafts]
    for snapshot in oversight.snapshots:
        if snapshot.get("findings"):
            lines.append(f"Review, step {snapshot['step']}:\n{format_findings(snapshot['findings'])}")
    lines.append("JobAgent never submits. /fillstatus for the log.")
    return str(redact("\n".join(lines), os.getenv("WORKDAY_PASSWORD", "")))
