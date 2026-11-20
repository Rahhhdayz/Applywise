"""Workday sign-in help and emailed security codes: read-only Gmail, allowlisted senders and hosts, no clicks."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path

import pytest
import yaml

from jobagent.apply.mailcodes import (
    CODE_SENDERS,
    security_codes,
    verification_link,
    wait_for_code,
    wait_for_workday_link,
)
from jobagent.inbox import GmailReader, InboxEmail, parse_email

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)
COMPANY = "Contoso Interactive, Inc."


def mail(sender: str, subject: str, body: str, *, minutes: int = 1, html: bool = False) -> tuple[InboxEmail, bytes]:
    message = EmailMessage()
    message["From"], message["Subject"], message["Message-ID"] = sender, subject, f"<{subject}{minutes}@x>"
    message["Date"] = (NOW + timedelta(minutes=minutes)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    if html:
        message.add_alternative(body, subtype="html")
    else:
        message.set_content(body)
    raw = message.as_bytes()
    return parse_email(raw), raw


GOOD = mail("no-reply@us.greenhouse-mail.io", f"Security code for your application to {COMPANY}",
            "Copy and paste this code into the security code field on your application: 48213907\nThanks, Greenhouse")


class FakeMail:
    def __init__(self, *batches: list[tuple[InboxEmail, bytes]]) -> None:
        self.batches, self.calls = list(batches), 0

    def recent(self, senders, since, *, limit=10):  # type: ignore[no-untyped-def]
        self.calls += 1
        return self.batches.pop(0) if self.batches else []


def test_code_happy_path_and_newest_mail_wins() -> None:
    older = mail("no-reply@us.greenhouse-mail.io", f"Security code for your application to {COMPANY}",
                 "security code field: 11112222", minutes=0)
    assert wait_for_code(FakeMail([GOOD, older]), "Contoso Interactive", NOW, sleep=lambda _s: None) == ("48213907", "")


@pytest.mark.parametrize(("message", "why"), [
    (mail("no-reply@greenhouse-mail.io.evil.test", f"Security code for your application to {COMPANY}",
          "security code field: 48213907"), "no email from an allowed sender naming the company"),
    (mail("no-reply@us.greenhouse-mail.io", "Security code for your application to Acme Corp",
          "security code field: 48213907"), "no email from an allowed sender naming the company"),
    (mail("no-reply@us.greenhouse-mail.io", f"Security code for your application to {COMPANY}",
          "security code: 48213907 or try 55554444"), "more than one possible code"),
    (mail("no-reply@us.greenhouse-mail.io", f"Security code for your application to {COMPANY}",
          "your security code is 4821"), "code format not recognised"),
])
def test_code_is_not_used_unless_sender_company_and_single_code_match(message, why) -> None:
    sleeps: list[float] = []
    assert wait_for_code(FakeMail([message]), COMPANY, NOW, timeout=20, sleep=sleeps.append) == (None, why)


def test_code_wait_times_out_after_polling() -> None:
    sleeps: list[float] = []
    reader = FakeMail()
    assert wait_for_code(reader, COMPANY, NOW, timeout=180, sleep=sleeps.append) == (None, "no security-code email arrived")
    assert sleeps == [10] * 18 and reader.calls == 19


def test_words_near_the_phrase_are_not_codes() -> None:
    message, raw = mail("no-reply@us.greenhouse-mail.io", f"Security code for {COMPANY}",
                        "Enter this security code in Contoso Software portal: Ab3dE9xY")
    assert security_codes(message, raw, COMPANY, CODE_SENDERS[0]) == ["Ab3dE9xY"]


def test_workday_link_only_from_workday_hosts() -> None:
    good = '<a href="https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/tok?x=1&amp;y=2">Verify</a>'
    assert verification_link(mail("w@myworkday.com", "Verify", good, html=True)[1]) == \
        "https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/tok?x=1&y=2"
    for bad in ("https://acme.myworkdayjobs.com.evil.test/activate/t", "https://evilmyworkdayjobs.com/activate/t",
                "http://acme.wd5.myworkdayjobs.com/activate/t", "https://acme.wd5.myworkdayjobs.com/en-US/careers/jobs"):
        assert verification_link(mail("w@myworkday.com", "Verify", f'<a href="{bad}">Verify</a>', html=True)[1]) is None
    phish = mail("w@myworkday.com", "Verify", '<a href="https://phish.test/verify">Verify</a>', html=True)
    assert wait_for_workday_link(FakeMail([phish]), NOW, timeout=0, sleep=lambda _s: None)[0] is None


class ReadOnlyFakeImap:
    """Serves raw messages; any state-changing IMAP call fails the test."""

    def __init__(self, messages: list[bytes]) -> None:
        self.messages, self.calls = messages, []

    def login(self, user, password):  # type: ignore[no-untyped-def]
        self.calls.append(("LOGIN",))
        return "OK", [b""]

    def list(self):  # type: ignore[no-untyped-def]
        return "OK", [b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']

    def select(self, mailbox, readonly=False):  # type: ignore[no-untyped-def]
        assert readonly is True  # EXAMINE, never SELECT
        self.calls.append(("EXAMINE", mailbox))
        return "OK", [b"1"]

    def uid(self, command, *args):  # type: ignore[no-untyped-def]
        self.calls.append((command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(str(i + 1) for i in range(len(self.messages))).encode()]
        assert command == "FETCH" and "BODY.PEEK[]" in args[1]
        return "OK", [(f"{uid} (UID {uid} BODY[] {{1}}".encode(), self.messages[int(uid) - 1]) for uid in args[0].split(",")]

    def logout(self):  # type: ignore[no-untyped-def]
        pass

    def __getattr__(self, name):  # type: ignore[no-untyped-def]
        raise AssertionError(f"forbidden IMAP call: {name}")


def test_gmail_recent_is_read_only_and_drops_mail_from_before_the_request() -> None:
    old = mail("no-reply@us.greenhouse-mail.io", "Old code", "security code: 99998888", minutes=-10)
    fake = ReadOnlyFakeImap([old[1], GOOD[1]])
    found = GmailReader("me@example.test", "app pass", imap_factory=lambda: fake).recent(["greenhouse-mail.io"], NOW)
    assert [message.subject for message, _ in found] == [GOOD[0].subject]
    assert ("SEARCH", "SINCE", "19-Nov-2026", "FROM", '"greenhouse-mail.io"') in fake.calls
    assert {call[0] for call in fake.calls} == {"LOGIN", "EXAMINE", "SEARCH", "FETCH"}


# ---- browser: the 8-box page and the Workday account flow -------------------------------------
playwright_sync_api = pytest.importorskip("playwright.sync_api")


def _chromium_available() -> bool:
    try:
        with playwright_sync_api.sync_playwright() as playwright:
            playwright.chromium.launch(headless=True).close()
        return True
    except Exception:  # noqa: BLE001
        return False


browser_only = pytest.mark.skipif(not _chromium_available(), reason="Chromium is not installed for Playwright")


@pytest.fixture
def context():
    with playwright_sync_api.sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            yield browser.new_context()
        finally:
            browser.close()


def _code_page(context):  # type: ignore[no-untyped-def]
    page = context.new_page()
    page.set_content((FIXTURES / "apply_security_code.html").read_text())
    return page


@browser_only
def test_security_code_is_typed_one_character_per_box_without_any_click(context, capsys) -> None:
    from jobagent.apply.oversight import Oversight

    sent: list[str] = []
    page = _code_page(context)
    oversight = Oversight(company="Contoso Interactive", mail=FakeMail([GOOD]), notify=sent.append, sleep=lambda _s: None)
    oversight.poll(page)
    oversight.poll(page)  # handled once
    assert "".join(page.input_value(f"#security-input-{i}") for i in range(8)) == "48213907"
    assert page.evaluate("window.__clicks") == [] and page.evaluate("window.__submitted") is False
    assert [item.source for item in oversight.extra_fields] == ["email_code"]
    assert "48213907" not in str([asdict(item) for item in oversight.extra_fields]) + capsys.readouterr().out
    assert sent == []


@browser_only
def test_security_code_not_found_types_nothing_and_tells_master(context, capsys) -> None:
    from jobagent.apply.oversight import Oversight

    sent: list[str] = []
    page = _code_page(context)
    wrong_sender = mail("no-reply@greenhouse.evil.test", f"Security code for your application to {COMPANY}",
                        "security code field: 48213907")
    oversight = Oversight(company="Contoso Interactive", job_label="Contoso Interactive", mail=FakeMail([wrong_sender]),
                          notify=sent.append, mail_timeout=0, sleep=lambda _s: None)
    oversight.poll(page)
    assert all(page.input_value(f"#security-input-{i}") == "" for i in range(8))
    assert sent and sent[0].startswith("needs you: Contoso Interactive security code not found")
    assert "needs you:" in capsys.readouterr().out and page.evaluate("window.__clicks") == []
    assert oversight.extra_fields[0].outcome == "skipped" and oversight.extra_fields[0].source == "email_code"


_WORKDAY = """<div id="app">
<input data-automation-id="email"><input data-automation-id="password" type="password">
<input data-automation-id="verifyPassword" type="password">
<div><button data-automation-id="createAccountSubmitButton" type="button">Create Account</button></div>
</div>
<script>
window.__events = [];
document.querySelector('[data-automation-id="createAccountSubmitButton"]').onclick = () => {
  window.__events.push("create");
  document.getElementById("app").innerHTML = '<p>Please verify your email to activate your account.</p>'
    + '<button data-automation-id="signInLink" type="button">Sign In</button>';
  document.querySelector('[data-automation-id="signInLink"]').onclick = () => {
    window.__events.push("signInLink");
    document.getElementById("app").innerHTML = '<input data-automation-id="email"><input data-automation-id="password">'
      + '<div><button data-automation-id="signInSubmitButton" type="button">Sign In</button></div>';
    document.querySelector('[data-automation-id="signInSubmitButton"]').onclick = () => window.__events.push("signIn");
  };
};
</script>"""


def _profile():  # type: ignore[no-untyped-def]
    from jobagent.apply.profile import ApplicationProfile

    example = yaml.safe_load((Path(__file__).parent.parent / "candidate" / "application.example.yaml").read_text())
    return ApplicationProfile.model_validate(example).model_copy(update={"account_password": "pw-from-env"})


@browser_only
def test_workday_account_follows_the_verification_link_and_signs_in(context) -> None:
    from jobagent.apply.filler import _workday_account
    from jobagent.apply.oversight import Oversight

    visited: list[str] = []
    context.route("https://acme.wd5.myworkdayjobs.com/**",
                  lambda route: (visited.append(route.request.url), route.fulfill(body="<p>Verified</p>")))
    link = mail("noreply@myworkday.com", "Verify your candidate account",
                '<a href="https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/t0k3n">Verify Account</a>',
                minutes=0, html=True)
    page = context.new_page()
    page.set_content(_WORKDAY)
    _workday_account(page, _profile(), Oversight(mail=FakeMail([link]), sleep=lambda _s: None))
    assert visited == ["https://acme.wd5.myworkdayjobs.com/en-US/careers/activate/t0k3n"]
    assert page.evaluate("window.__events") == ["create", "signInLink", "signIn"]
    assert len(context.pages) == 1  # the verification tab was closed


@browser_only
def test_workday_account_stops_on_captcha_and_on_a_foreign_link(context) -> None:
    from jobagent.apply.filler import _workday_account
    from jobagent.apply.oversight import Oversight

    sent: list[str] = []
    page = context.new_page()
    page.set_content('<iframe src="https://www.google.com/recaptcha/api2/anchor" width="300" height="80"></iframe>' + _WORKDAY)
    _workday_account(page, _profile(), Oversight(job_label="#1 Acme Analyst", notify=sent.append))
    assert sent == ["needs you: #1 Acme Analyst Workday sign-in: CAPTCHA"]
    assert page.evaluate("window.__events") == []  # nothing clicked

    visited: list[str] = []
    context.route("**/*", lambda route: (visited.append(route.request.url), route.abort()))
    phish = mail("noreply@myworkday.com", "Verify", '<a href="https://phish.test/activate/x">Verify</a>', minutes=0, html=True)
    oversight = Oversight(job_label="#1", mail=FakeMail([phish]), notify=sent.append, mail_timeout=0, sleep=lambda _s: None)
    assert oversight.verify_workday(page, NOW) is False
    assert visited == [] and sent[-1].startswith("needs you: #1 Workday email verification")


# Northwind #52001: the tenant opened on Sign In, Master had no account there, and the rejected sign-in stopped silently.
_WORKDAY_SIGN_IN_FIRST = """<div id="app">
<input data-automation-id="email"><input data-automation-id="password" type="password">
<div><button data-automation-id="signInSubmitButton" type="button">Sign In</button></div>
<button data-automation-id="createAccountLink" type="button">Create Account</button>
</div>
<script>
window.__events = [];
document.querySelector('[data-automation-id="signInSubmitButton"]').onclick = () => {
  window.__events.push("signIn");
  document.getElementById("app").insertAdjacentHTML("afterbegin",
    "<p>You may have entered the wrong email address or password or your account might be locked.</p>");
};
document.querySelector('[data-automation-id="createAccountLink"]').onclick = () => {
  window.__events.push("createAccountLink");
  document.getElementById("app").innerHTML = '<input data-automation-id="email"><input data-automation-id="password">'
    + '<input data-automation-id="verifyPassword"><div><button data-automation-id="createAccountSubmitButton"'
    + ' type="button">Create Account</button></div>';
  document.querySelector('[data-automation-id="createAccountSubmitButton"]').onclick = () => {
    window.__events.push("create");
    document.getElementById("app").innerHTML = window.__exists
      ? '<p>An account using this email address already exists.</p><input data-automation-id="email">'
        + '<div><button data-automation-id="createAccountSubmitButton" type="button">Create Account</button></div>'
      : '<p>Signed in</p>';
  };
};
</script>"""


@browser_only
def test_rejected_sign_in_creates_the_account_or_tells_master(context) -> None:
    from jobagent.apply.filler import _workday_account
    from jobagent.apply.oversight import Oversight

    page = context.new_page()
    page.set_content(_WORKDAY_SIGN_IN_FIRST)
    sent: list[str] = []
    _workday_account(page, _profile(), Oversight(job_label="#52001", notify=sent.append))
    assert page.evaluate("window.__events") == ["signIn", "createAccountLink", "create"] and sent == []

    page.set_content(_WORKDAY_SIGN_IN_FIRST)
    page.evaluate("window.__exists = true")
    _workday_account(page, _profile(), Oversight(job_label="#52001", notify=sent.append))
    assert page.evaluate("window.__events") == ["signIn", "createAccountLink", "create"]
    assert len(sent) == 1 and sent[0].startswith("needs you: #52001 Workday sign-in rejected")


@browser_only
def test_create_account_ticks_only_the_sign_up_terms_box(context) -> None:
    """Master, 2026-11-19: tick terms / privacy acknowledgements; never a truthfulness certification."""
    from jobagent.apply.filler import _tick_account_terms
    from jobagent.apply.oversight import Oversight

    boxes = ('<div><input type="checkbox" id="t"><label for="t">I understand and acknowledge the terms of use for'
             ' Acme.</label></div><div><input type="checkbox" id="c"><label for="c">I certify the information is'
             ' true</label></div><div><input type="checkbox" id="p"><label for="p">I consent to the privacy'
             ' notice</label></div>')
    page = context.new_page()
    page.set_content(boxes + '<button data-automation-id="createAccountSubmitButton" type="button">Create</button>')
    oversight = Oversight(snapshot_root=None)
    _tick_account_terms(page, oversight)
    assert [page.is_checked(f"#{name}") for name in "tcp"] == [True, False, True]  # privacy too (2026-11-19 rule)
    assert [(f.outcome, f.source) for f in oversight.extra_fields] == [("filled", "consent.terms")] * 2

    page.set_content(boxes)  # not the Create Account page (e.g. an application step): nothing is ticked
    _tick_account_terms(page, oversight)
    assert [page.is_checked(f"#{name}") for name in "tcp"] == [False, False, False]
