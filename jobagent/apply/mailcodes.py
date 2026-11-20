"""One-time sign-in help from Master's mail: Workday verification links and emailed security codes.

Mail is read only through ``GmailReader.recent`` (EXAMINE + BODY.PEEK: never marked read or changed).
Email bodies are untrusted: a link is followed only when its host is a Workday domain, and a code is
used only when the sender is allowlisted, the mail names the job's company, and exactly one code in
the expected format is present. Codes are short-lived secrets: they are typed, never logged.
"""

from __future__ import annotations

import email
import html
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from email import policy
from typing import Protocol

from jobagent.apply.ats import is_workday_url
from jobagent.inbox import InboxEmail, InboxError, company_core

POLL_SECONDS = 10
TIMEOUT_SECONDS = 180
_URL = re.compile(r"https://[^\s\"'<>()]+")
_VERIFY_WORDS = ("verif", "activat", "confirm", "validat")
_WORKDAY_SENDERS = ("myworkdayjobs.com", "myworkday.com", "workday.com")


class MailReader(Protocol):
    def recent(self, senders: list[str], since: datetime, *, limit: int = 10) -> list[tuple[InboxEmail, bytes]]: ...


@dataclass(frozen=True)
class CodeSender:
    """One security-code sender: its mail domains and the code it sends (add more ATSs here)."""

    name: str
    domains: tuple[str, ...]
    code: re.Pattern[str]
    near: str  # the code sits within 300 characters after this phrase


CODE_SENDERS = (
    CodeSender("greenhouse", ("greenhouse-mail.io",), re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{8}(?![A-Za-z0-9])"),
               "security code"),
)
_WORD = re.compile(r"[A-Z]?[a-z]+")  # "Software", "security": words, not codes


def _sender_ok(address: str, domains: tuple[str, ...]) -> bool:
    domain = address.rpartition("@")[2].casefold()
    return any(domain == item or domain.endswith("." + item) for item in domains)


def _bodies(raw: bytes) -> str:
    message = email.message_from_bytes(raw, policy=policy.default)
    parts = []
    for part in message.walk():
        if part.get_content_type() in {"text/plain", "text/html"}:
            try:
                parts.append(str(part.get_content()))
            except (KeyError, LookupError, ValueError):
                continue
    return html.unescape("\n".join(parts))


def verification_link(raw: bytes) -> str | None:
    """The mail's Workday verification link, or None. Only https Workday hosts ever qualify."""
    for url in _URL.findall(_bodies(raw)):
        url = url.rstrip(".,;")
        if is_workday_url(url) and any(word in url.casefold() for word in _VERIFY_WORDS):
            return url
    return None


def security_codes(message: InboxEmail, raw: bytes, company: str, sender: CodeSender) -> list[str] | None:
    """Distinct codes in the mail, or None when the sender or company does not match."""
    if not _sender_ok(message.from_addr, sender.domains):
        return None
    core = company_core(company)
    text = re.sub(r"<[^>]+>", " ", _bodies(raw))
    haystack = f"{message.subject}\n{text}".casefold()
    if not core or core not in re.sub(r"[^a-z0-9]+", " ", haystack):
        return None
    codes: list[str] = []
    for match in re.finditer(re.escape(sender.near), text, flags=re.IGNORECASE):
        for code in sender.code.findall(text[match.end():match.end() + 300]):
            if not _WORD.fullmatch(code) and code not in codes:
                codes.append(code)
    return codes


def wait_for_code(
    mail: MailReader, company: str, since: datetime, *,
    senders: tuple[CodeSender, ...] = CODE_SENDERS, timeout: float = TIMEOUT_SECONDS, poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str | None, str]:
    """(code, "") from the newest matching mail, or (None, why). Polls until ``timeout``."""
    domains = [domain for sender in senders for domain in sender.domains]
    waited, why = 0.0, "no security-code email arrived"
    while True:
        try:
            messages = mail.recent(domains, since)
        except InboxError as error:
            return None, f"Gmail unavailable ({error})"
        for message, raw in messages:  # newest first: the newest matching mail decides
            for sender in senders:
                codes = security_codes(message, raw, company, sender)
                if codes is None:
                    continue
                if len(codes) == 1:
                    return codes[0], ""
                return None, "more than one possible code" if codes else "code format not recognised"
        if messages:
            why = "no email from an allowed sender naming the company"
        if waited >= timeout:
            return None, why
        sleep(poll)
        waited += poll


def wait_for_workday_link(
    mail: MailReader, since: datetime, *, timeout: float = TIMEOUT_SECONDS, poll: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str | None, str]:
    """(link, "") from the newest Workday mail after ``since``, or (None, why)."""
    waited = 0.0
    while True:
        try:
            messages = mail.recent(list(_WORKDAY_SENDERS), since)
        except InboxError as error:
            return None, f"Gmail unavailable ({error})"
        for message, raw in messages:
            if _sender_ok(message.from_addr, _WORKDAY_SENDERS) or "workday" in message.from_addr.casefold():
                link = verification_link(raw)
                if link:
                    return link, ""
        if waited >= timeout:
            return None, "no Workday verification email with a Workday link arrived"
        sleep(poll)
        waited += poll
