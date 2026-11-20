"""Gmail response tracker: read-only IMAP, matching, classification, forward-only status changes."""

from __future__ import annotations

import imaplib
import logging
from datetime import UTC, date, datetime, timedelta
from email.message import EmailMessage

import pytest

from jobagent import cli
from jobagent.db import JobRepository
from jobagent.inbox import (
    EmailVerdict,
    GmailReader,
    InboxEmail,
    InboxError,
    ReadOnlyImap,
    adopt_email,
    applied_jobs,
    classify,
    company_core,
    find_emails,
    gmail_filter_xml,
    gmail_search_hint,
    match_job,
    run_inbox,
    short_name,
    title_from_subject,
)
from jobagent.models import JobPosting
from jobagent.pipeline import run_cycle
from tests.test_milestone5 import candidate

PASSWORD = "abcd efgh ijkl mnop"


def raw_email(message_id: str, sender: str, subject: str, body: str, *, name: str = "") -> bytes:
    message = EmailMessage()
    message["Message-ID"] = message_id
    message["From"] = f"{name} <{sender}>" if name else sender
    message["Subject"] = subject
    message["Date"] = "Mon, 16 Nov 2026 15:00:00 +0000"
    message.set_content(body)
    return message.as_bytes()


class FakeImap:
    """Records every IMAP command; serves a fixed mailbox. Any state-changing command fails the test."""

    def __init__(self, messages: list[bytes], *, label: bool = False, login_error: str | None = None) -> None:
        self.messages, self.label, self.login_error = messages, label, login_error
        self.calls: list[tuple[str, ...]] = []

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        self.calls.append(("LOGIN", user))
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return "OK", [b"ok"]

    def list(self) -> tuple[str, list[bytes]]:
        self.calls.append(("LIST",))
        lines = [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All Mail"']
        return "OK", lines + ([b'(\\HasNoChildren) "/" "JobAgent"'] if self.label else [])

    def select(self, mailbox: str, readonly: bool = False) -> tuple[str, list[bytes]]:
        self.calls.append(("SELECT", mailbox, str(readonly)))
        return "OK", [str(len(self.messages)).encode()]

    def uid(self, command: str, *args: str) -> tuple[str, list[object]]:
        self.calls.append(("UID", command, *args))
        if command == "SEARCH":
            return "OK", [" ".join(str(index + 1) for index in range(len(self.messages))).encode()]
        assert command == "FETCH"
        uids, items = args
        data: list[object] = []
        for uid in uids.split(","):
            raw = self.messages[int(uid) - 1]
            if "HEADER.FIELDS" in items:
                header = next(line for line in raw.split(b"\n") if line.lower().startswith(b"message-id"))
                data += [(f"{uid} (UID {uid} BODY[HEADER.FIELDS (MESSAGE-ID)] {{40}}".encode(), header + b"\r\n\r\n"), b")"]
            else:
                data += [(f"{uid} (UID {uid} BODY[] {{{len(raw)}}}".encode(), raw), b")"]
        return "OK", data

    def logout(self) -> None:
        self.calls.append(("LOGOUT",))

    def __getattr__(self, name: str) -> object:  # store, copy, expunge, append, ...
        raise AssertionError(f"forbidden IMAP call: {name}")


def job(identifier: str, company: str, title: str, url: str) -> JobPosting:
    return JobPosting(source="test", source_job_id=identifier, company=company, title=title, location="Remote",
                      employment_type="full_time", description_text="Security work.", apply_url=url, canonical_url=url,
                      posted_at=datetime.now(UTC) - timedelta(days=3))


@pytest.fixture
def repository() -> JobRepository:
    repository = JobRepository(":memory:")
    repository.initialize()
    return repository


@pytest.fixture
def jobs(repository: JobRepository) -> dict[str, int]:
    ids = {
        "quantia": repository.add_or_get_job(job("f", "Quantia, Inc.", "Security Engineer", "https://job-boards.greenhouse.io/quantia/jobs/1")).job.id,
        "tyrell": repository.add_or_get_job(job("n", "Tyrell", "Detection Engineer", "https://tyrell.com/careers/apply/2")).job.id,
        "acme_a": repository.add_or_get_job(job("a1", "Acme Robotics", "Cloud Engineer", "https://jobs.lever.co/acme/1")).job.id,
        "acme_b": repository.add_or_get_job(job("a2", "Acme Robotics", "Platform Engineer", "https://jobs.lever.co/acme/2")).job.id,
        "ready": repository.add_or_get_job(job("r", "Readyco", "Analyst", "https://readyco.com/jobs/3")).job.id,
    }
    for key, job_id in ids.items():
        repository.set_application_status(job_id, "ready" if key == "ready" else "applied")
    repository.set_application_status(ids["tyrell"], "interview")
    return ids


def check(repository: JobRepository, messages: list[bytes], **kwargs: object) -> tuple[FakeImap, object]:
    fake = FakeImap(messages, label=bool(kwargs.pop("label", False)))
    reader = GmailReader("me@example.com", PASSWORD, imap_factory=lambda: fake)
    return fake, run_inbox(repository, reader, since=date(2026, 10, 1), **kwargs)  # type: ignore[arg-type]


REJECTION = "Thank you for applying. Unfortunately, we have decided to move forward with other candidates."


def test_rejection_moves_status_and_is_recorded_once(repository: JobRepository, jobs: dict[str, int]) -> None:
    message = raw_email("<r1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION, name="Quantia Recruiting")
    fake, result = check(repository, [message])
    assert repository.latest_statuses()[jobs["quantia"]] == "rejected"
    assert result.summary()["status_changes"] == 1  # type: ignore[attr-defined]
    # read-only: EXAMINE, BODY.PEEK only, never a state-changing command
    assert ("SELECT", '"[Gmail]/All Mail"', "True") in fake.calls
    fetches = [call for call in fake.calls if call[:2] == ("UID", "FETCH")]
    assert fetches and all("BODY.PEEK[" in call[-1] for call in fetches)
    assert not {call[0] for call in fake.calls} & {"STORE", "COPY", "EXPUNGE", "MOVE", "APPEND"}
    assert not [call for call in fake.calls if call[0] == "UID" and call[1] not in {"SEARCH", "FETCH"}]
    # idempotent by Message-ID: the second run does not download or process it again
    fake, again = check(repository, [message])
    assert again.scanned == 0  # type: ignore[attr-defined]
    assert not [call for call in fake.calls if call[:2] == ("UID", "FETCH") and "BODY.PEEK[]" in call[-1]]
    assert len(repository.list_email_responses()) == 1


def test_label_is_read_instead_of_all_mail(repository: JobRepository, jobs: dict[str, int]) -> None:
    fake, result = check(repository, [], label=True)
    assert ("SELECT", '"JobAgent"', "True") in fake.calls
    assert result.mailbox == "JobAgent"  # type: ignore[attr-defined]


def test_all_mail_search_is_narrowed_to_senders_and_date(repository: JobRepository, jobs: dict[str, int]) -> None:
    fake, _ = check(repository, [])
    searches = " ".join(" ".join(call) for call in fake.calls if call[:2] == ("UID", "SEARCH"))
    assert "SINCE 01-Oct-2026" in searches
    assert '"greenhouse.io"' in searches and '"tyrell.com"' in searches and '"quantia"' in searches
    assert '"readyco.com"' not in searches  # not applied yet


@pytest.mark.parametrize(("subject", "body", "expected"), [
    ("Application received", "Thank you for applying to Quantia. We have received your application.", "confirmation"),
    ("Your update", "Thanks for applying! If there is a fit we will reach out to schedule an interview.", None),
    ("Thank you for applying to Quantia", "If there is a fit we will reach out to schedule an interview.", "confirmation"),
    ("Verify your candidate account", "Your one-time code for your application.", "other"),
    ("Next steps", "We'd love to schedule an interview. Please share your availability.", "interview"),
    ("Quick chat", "Could you book a time on my Calendly for a phone screen?", "recruiter_screen"),
    ("Update", REJECTION, "rejected"),
    ("Update", "Sadly the position has been filled.", "rejected"),
    ("Coding challenge", "Please complete the HackerRank assessment before the interview.", "assessment"),
    ("Offer", "We are pleased to offer you the position. Your offer letter is attached.", "offer"),
    ("Newsletter", "Ten tips for your garden.", None),
    ("Update", "Unfortunately, we will not be moving forward with your application.", "rejected"),
    ("Update", "We regret to inform you that we have decided to move forward with other applicants.", "rejected"),
    ("Update", "Unfortunately the office is closed on Friday.", None),  # a bare "unfortunately" is no rejection
    ("Update", "We will not be moving forward with the onsite yet; next steps are a take-home.", None),  # conflicting

    # paraphrased from the Nov 17 audit: real rejections whose boilerplate mentions next steps / interview stages
    ("Update", "We have decided not to move forward. We only advance a few to our interview stages.", "rejected"),
    ("Update", "We will not be moving you to the next step in the selection process.", "rejected"),
    ("Update", "We won't be able to invite you to the next round of our hiring process.", "rejected"),
    ("Update", "We ultimately moved in another direction for this opportunity.", "rejected"),
    ("Thanks For Applying", "Unfortunately we will not be able to reply to every applicant; your application is on file.", "confirmation"),
])
def test_classification_rules(subject: str, body: str, expected: str | None) -> None:
    verdict = classify(subject, body)
    assert (verdict[0] if verdict else None) == expected


def email_from(sender: str, subject: str, body: str, name: str = "") -> InboxEmail:
    return InboxEmail("<id>", name, sender, subject, datetime.now(UTC), body)


def test_domain_matching(repository: JobRepository, jobs: dict[str, int]) -> None:
    applied = applied_jobs(repository)

    def ids(sender: str, subject: str = "Hi", body: str = "Hello") -> list[int]:
        return [item.job.id for item in match_job(email_from(sender, subject, body), applied)[0]]

    assert ids("talent@tyrell.com", "Next steps") == [jobs["tyrell"]]
    assert ids("recruiting@quantia.com") == [jobs["quantia"]]  # company slug of a Greenhouse job
    assert ids("pat@unknown.org") == []
    assert ids("x@readyco.com") == []  # status "ready": never matched
    assert ids("no-reply@hire.lever.co", "Update from acme") == []  # tenant slug "acme" -> both Acme jobs: ambiguous


def test_company_core() -> None:
    assert company_core("The Quantia, Inc.") == "quantia" and company_core("Brightwave (NY)") == "brightwave"
    assert short_name("soylent technology") == "soylent" and short_name("hooli robotics") == "hooli robotics"


def test_platform_named_company_is_not_matched_by_boilerplate(repository: JobRepository) -> None:
    workday = repository.add_or_get_job(job("w", "Workday, Inc.", "Security Analyst", "https://workday.wd5.myworkdayjobs.com/x/job/1")).job.id
    repository.set_application_status(workday, "applied")
    applied = applied_jobs(repository)
    other_tenant = email_from("sierra@myworkday.com", "Sierra Space | Application Update", "Powered by Workday. " + REJECTION, name="Workday")
    assert match_job(other_tenant, applied)[0] == []
    own = email_from("workday@myworkday.com", "Thank you from Workday", REJECTION)
    assert [item.job.id for item in match_job(own, applied)[0]] == [workday]


def test_copies_of_one_opening_all_move(repository: JobRepository) -> None:
    first = repository.add_or_get_job(job("g1", "Monarch Inc", "Security Analyst", "https://job-boards.greenhouse.io/monarch/jobs/1")).job.id
    second = repository.add_or_get_job(job("g2", "Monarch", "Security Analyst", "https://www.linkedin.com/jobs/view/2")).job.id
    for job_id in (first, second):
        repository.set_application_status(job_id, "applied")
    check(repository, [raw_email("<g@x>", "no-reply@us.greenhouse-mail.io", "Thank you for your interest in Monarch", REJECTION)])
    statuses = repository.latest_statuses()
    assert statuses[first] == statuses[second] == "rejected"


def test_account_mail_is_not_flagged(repository: JobRepository, jobs: dict[str, int]) -> None:
    messages = [raw_email("<o1@x>", "no-reply@us.greenhouse-mail.io", "Security code for your application to Quantia", "Your code is 123456."),
                raw_email("<o2@x>", "no-reply@us.greenhouse-mail.io", "Security code for your application to Zed", "Your code is 1."),
                raw_email("<o3@x>", "alerts@theladders.com", "Interview tips", "Schedule time to prepare for your interview.", name="Ladders")]
    check(repository, messages)
    assert repository.list_email_responses(needs_review=True) == []
    assert repository.latest_statuses()[jobs["quantia"]] == "applied"


def test_ambiguous_match_needs_review_and_changes_nothing(repository: JobRepository, jobs: dict[str, int]) -> None:
    message = raw_email("<a1@x>", "no-reply@hire.lever.co", "Acme Robotics update", REJECTION)
    _, result = check(repository, [message])
    statuses = repository.latest_statuses()
    assert statuses[jobs["acme_a"]] == statuses[jobs["acme_b"]] == "applied"
    [item] = repository.list_email_responses(needs_review=True)
    assert item.job_id is None and item.applied_status_change is None
    assert "ambiguous" in result.decisions[0].reason  # type: ignore[attr-defined]


def test_title_breaks_a_tie(repository: JobRepository, jobs: dict[str, int]) -> None:
    message = raw_email("<a2@x>", "no-reply@hire.lever.co", "Acme Robotics: Platform Engineer", REJECTION)
    check(repository, [message])
    assert repository.latest_statuses()[jobs["acme_b"]] == "rejected"
    assert repository.latest_statuses()[jobs["acme_a"]] == "applied"


def test_never_moves_backwards(repository: JobRepository, jobs: dict[str, int]) -> None:
    message = raw_email("<s1@x>", "talent@tyrell.com", "Phone screen", "Can you book a time on my Calendly for a phone screen?")
    check(repository, [message])
    assert repository.latest_statuses()[jobs["tyrell"]] == "interview"  # recruiter_screen would be backwards
    [item] = repository.list_email_responses()
    assert item.classification == "recruiter_screen" and item.applied_status_change is None and not item.needs_review


def test_dry_run_writes_nothing(repository: JobRepository, jobs: dict[str, int]) -> None:
    message = raw_email("<d1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION)
    _, result = check(repository, [message], dry_run=True)
    assert result.decisions[0].change == "rejected"  # type: ignore[attr-defined]
    assert repository.latest_statuses()[jobs["quantia"]] == "applied"
    assert repository.list_email_responses() == []


class FakeClassifier:
    def __init__(self, verdict: EmailVerdict | Exception) -> None:
        self.verdict, self.prompts = verdict, []

    def classify(self, subject: str, text: str) -> EmailVerdict:
        self.prompts.append(text)
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


def test_ai_fallback_only_when_rules_are_inconclusive(repository: JobRepository, jobs: dict[str, int]) -> None:
    unclear = raw_email("<u1@x>", "talent@tyrell.com", "About your application", "Let's talk about the offer details. " + "x" * 5000)
    clear = raw_email("<u2@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION)
    classifier = FakeClassifier(EmailVerdict(classification="offer", confidence=0.95))
    check(repository, [unclear, clear], classifier=classifier)
    assert len(classifier.prompts) == 1 and len(classifier.prompts[0]) > 2000  # truncation is the classifier's job
    assert repository.latest_statuses()[jobs["tyrell"]] == "offer"


@pytest.mark.parametrize("verdict", [EmailVerdict(classification="offer", confidence=0.5), RuntimeError("Claude down")])
def test_low_confidence_or_unavailable_ai_needs_review(repository: JobRepository, jobs: dict[str, int], verdict: object) -> None:
    message = raw_email("<l1@x>", "talent@tyrell.com", "About your application", "Let's talk.")
    check(repository, [message], classifier=FakeClassifier(verdict))  # type: ignore[arg-type]
    assert repository.latest_statuses()[jobs["tyrell"]] == "interview"
    assert len(repository.list_email_responses(needs_review=True)) == 1


def test_password_never_logged_and_auth_failure_is_categorised(repository: JobRepository, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    fake = FakeImap([], login_error="[AUTHENTICATIONFAILED] Invalid credentials (Failure) " + PASSWORD.replace(" ", ""))
    reader = GmailReader("me@example.com", PASSWORD, imap_factory=lambda: fake)
    with pytest.raises(InboxError, match="auth failed") as error:
        run_inbox(repository, reader, since=date(2026, 10, 1))
    assert PASSWORD.replace(" ", "") not in str(error.value)
    assert PASSWORD.replace(" ", "") not in caplog.text and PASSWORD not in caplog.text


def test_successful_run_logs_no_password(repository: JobRepository, jobs: dict[str, int], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    check(repository, [raw_email("<p1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION)])
    assert PASSWORD.replace(" ", "") not in caplog.text


def test_readonly_wrapper_refuses_marking_fetches() -> None:
    imap = ReadOnlyImap(FakeImap([]))
    with pytest.raises(ValueError):
        imap.fetch([b"1"], "(BODY[])")
    with pytest.raises(ValueError):
        imap.fetch([b"1"], "(RFC822)")
    assert not hasattr(ReadOnlyImap, "store") and not hasattr(ReadOnlyImap, "uid")


def test_notifications_and_notion(repository: JobRepository, jobs: dict[str, int]) -> None:
    sent: list[str] = []

    class Tracker:
        def __init__(self) -> None:
            self.statuses_set: list[tuple[str, str]] = []

        def upsert(self, *args: object) -> str:
            return "page-1"

        def renumber(self, rank: dict[int, int]) -> int:
            return 0

        def set_status(self, page_id: str, status: str) -> None:
            self.statuses_set.append((page_id, status))

    repository.save_packet(job_id=jobs["quantia"], fingerprint="f", status="ready", packet_directory="out/x")
    messages = [raw_email("<n1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION),
                raw_email("<n2@x>", "talent@tyrell.com", "About your application", "Let's talk.")]
    tracker = Tracker()
    check(repository, messages, tracker=tracker, notify=sent.append)
    assert sent == [f"Quantia, Inc. #{jobs['quantia']} → rejected", "Inbox: 1 email(s) need review. Run: jobagent inbox review"]
    assert tracker.statuses_set == [("page-1", "rejected")]


def test_gmail_filter_xml(repository: JobRepository, jobs: dict[str, int]) -> None:
    xml = gmail_filter_xml(applied_jobs(repository))
    assert "greenhouse.io" in xml and "tyrell.com" in xml and "readyco.com" not in xml
    assert "name='label' value='JobAgent'" in xml


def test_cycle_runs_inbox_stage_after_notion_pull(repository: JobRepository) -> None:
    order: list[str] = []

    class Tracker:
        def statuses(self) -> dict[int, str]:
            order.append("notion_pull"); return {}

        def upsert(self, *args: object) -> str:
            return "p"

        def renumber(self, rank: dict[int, int]) -> int:
            return 0

    def scrape() -> object:
        order.append("scrape"); raise RuntimeError("stop")

    run_cycle(repository, candidate(), scrape=scrape, scorer=None, tracker=Tracker(),  # type: ignore[arg-type]
              today="2026-11-17", inbox=lambda: order.append("inbox") or {"new_emails": 0})
    assert order[:3] == ["notion_pull", "inbox", "scrape"]
    skipped = run_cycle(repository, candidate(), scrape=None, scorer=None, tracker=None, today="2026-11-17")
    assert skipped["inbox"] == "not configured"


def test_cli_without_credentials(repository: JobRepository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("GMAIL_ADDRESS", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    monkeypatch.chdir(tmp_path)  # no .env here
    assert cli.main(["--database", str(tmp_path / "db.sqlite3"), "inbox", "check", "--dry-run"]) == 1
    assert "GMAIL_ADDRESS" in capsys.readouterr().out
    assert cli.main(["--database", str(tmp_path / "db.sqlite3"), "inbox", "review"]) == 0


# Paraphrase of a real reply (Dunder Mifflin, Nov 13) that the old rules read as a rejection at 0.9.
BENDING = ("Hi, thanks again for your patience. Unfortunately, I'm not able to schedule an initial call at the moment, "
           "but there will be plenty of time to discuss the opportunity and your questions during the interview phase. "
           "Best, Recruiting")


def test_ongoing_process_is_not_a_rejection(repository: JobRepository) -> None:
    assert classify("Re: A quick note on your application", BENDING) is None  # inconclusive: AI fallback / review
    unmatched = raw_email("<bs1@x>", "recruiter@dundermifflin.example", "Re: A quick note on your application", BENDING)
    _, result = check(repository, [unmatched])
    decision = result.decisions[0]  # type: ignore[attr-defined]
    assert decision.classification == "other" and decision.change is None and decision.needs_review
    # matched to an applied job: the rules defer to the AI, which must not be skipped by a rule verdict
    job_id = repository.add_or_get_job(job("bs", "Dunder Mifflin", "Software Engineer", "https://jobs.dundermifflin.example/1")).job.id
    repository.set_application_status(job_id, "applied")
    asked: list[str] = []

    class Classifier:
        def classify(self, subject: str, text: str) -> EmailVerdict:
            asked.append(subject)
            return EmailVerdict(classification="other", confidence=0.9)

    _, result = check(repository, [raw_email("<bs2@x>", "recruiter@dundermifflin.example", "Re: A quick note on your application", BENDING)],
                      classifier=Classifier())
    assert asked and repository.latest_statuses()[job_id] == "applied"


def test_propose_only_records_no_change_emails_and_notifies(repository: JobRepository, jobs: dict[str, int]) -> None:
    sent: list[str] = []
    messages = [raw_email("<p1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION),
                raw_email("<p2@x>", "no-reply@lever.co", "Thank you for applying to Acme Robotics", "We have received your application.")]
    check(repository, messages, propose_only=True, notify=sent.append)
    assert repository.latest_statuses()[jobs["quantia"]] == "applied"
    assert [row.message_id for row in repository.list_email_responses()] == ["<p2@x>"]  # the proposal stays unprocessed
    assert sent == [f"Inbox: 1 proposed status change(s):\nQuantia, Inc. #{jobs['quantia']} → rejected\nSend /inbox to review and apply them."]


def test_recording_twice_never_applies_twice(repository: JobRepository, jobs: dict[str, int]) -> None:
    from jobagent.inbox import record_decisions

    _, result = check(repository, [raw_email("<t1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION)], dry_run=True)
    proposals = result.proposals  # type: ignore[attr-defined]
    assert record_decisions(repository, proposals, mailbox="JobAgent") == [jobs["quantia"]]
    repository.set_application_status(jobs["quantia"], "applied", source="cli")  # Master moved it back by hand
    assert record_decisions(repository, proposals, mailbox="JobAgent") == []
    assert repository.latest_statuses()[jobs["quantia"]] == "applied"


def stored_email(repository: JobRepository, message_id: str, sender: str, subject: str, *, job_id: int | None = None,
                 classification: str = "other", needs_review: bool = True, name: str = "") -> int:
    from jobagent.db import EmailResponse

    repository.add_email_response(EmailResponse(message_id, job_id, datetime(2026, 11, 13, 15, tzinfo=UTC), sender, subject,
                                                classification, 0.9, None, needs_review, name, "JobAgent"))
    return next(row.id for row in repository.list_email_responses() if row.message_id == message_id)  # type: ignore[return-value]


def test_find_emails_by_company_domain_name_or_job(repository: JobRepository, jobs: dict[str, int]) -> None:
    stored_email(repository, "<e1>", "recruiter@dundermifflin.com", "Re: A quick note on your application", classification="rejected")
    stored_email(repository, "<e2>", "no-reply@dundermifflin.com", "Application received", classification="confirmation", needs_review=False)
    stored_email(repository, "<e3>", "no-reply@greenhouse-mail.io", "Update", job_id=jobs["quantia"], needs_review=False)
    stored_email(repository, "<e4>", "someone@else.com", "Hello")
    rows, total = find_emails(repository, "Dunder Mifflin")
    assert total == 2 and {row.message_id for row in rows} == {"<e1>", "<e2>"}
    assert gmail_search_hint([row for row in rows if row.message_id == "<e1>"], "Dunder Mifflin") == "from:recruiter@dundermifflin.com"
    assert gmail_search_hint(rows, "Dunder Mifflin").startswith("from:(")
    assert [row.message_id for row in find_emails(repository, "Quantia")[0]] == ["<e3>"]
    assert find_emails(repository, "Nonexistent Corp") == ([], 0)
    assert gmail_search_hint([], "Nonexistent Corp") == '"Nonexistent Corp"'


def test_adopt_creates_one_minimal_applied_job_then_links(repository: JobRepository) -> None:
    first = stored_email(repository, "<a1>", "recruiter@dundermifflin.com", "Re: A quick note on your application")
    second = stored_email(repository, "<a2>", "no-reply@dundermifflin.com", "Your application for Product Engineer at Dunder Mifflin")
    job_, created = adopt_email(repository, first, company="Dunder Mifflin")
    assert created and job_.canonical_source == "email" and job_.title == "Unknown role" and job_.company == "Dunder Mifflin"
    assert repository.latest_statuses()[job_.id] == "applied"
    row = repository.get_email_response(first)
    assert row is not None and row.job_id == job_.id and not row.needs_review
    again, created = adopt_email(repository, second)  # company guessed from the domain; same company -> linked
    assert not created and again.id == job_.id
    with pytest.raises(ValueError, match="already linked"):
        adopt_email(repository, first)


def test_title_from_subject() -> None:
    assert title_from_subject("Your application for Product Engineer at Dunder Mifflin") == "Product Engineer"
    assert title_from_subject("Re: Thank you for applying to the Security Analyst position") == "Security Analyst"
    assert title_from_subject("Re: A quick note on your application") is None


def test_cli_adopt_and_review_show_email_ids(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "db.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    email_id = stored_email(repository, "<c1>", "recruiter@dundermifflin.com", "Re: A quick note on your application")
    assert cli.main(["--database", str(database), "inbox", "review"]) == 0
    assert f"email {email_id}" in capsys.readouterr().out
    assert cli.main(["--database", str(database), "inbox", "adopt", str(email_id), "--company", "Dunder Mifflin", "--title", "Engineer"]) == 0
    assert "Created job" in capsys.readouterr().out
    assert cli.main(["--database", str(database), "inbox", "adopt", "999"]) == 1


def test_old_email_table_gets_new_columns(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    database = tmp_path / "old.sqlite3"
    JobRepository(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("ALTER TABLE email_responses DROP COLUMN mailbox")
        connection.execute("ALTER TABLE email_responses DROP COLUMN from_name")
    repository = JobRepository(database)
    repository.initialize()
    assert stored_email(repository, "<m1>", "a@b.com", "Hi") > 0


@pytest.mark.parametrize(("flag", "propose_only"), [(None, True), ("1", False)])
def test_cycle_inbox_stage_only_proposes_unless_auto_apply(repository: JobRepository, monkeypatch: pytest.MonkeyPatch,
                                                           flag: str | None, propose_only: bool) -> None:
    from jobagent.inbox import InboxResult

    calls: list[bool] = []
    monkeypatch.setenv("GMAIL_ADDRESS", "me@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", PASSWORD)
    if flag is None:
        monkeypatch.delenv("JOBAGENT_INBOX_AUTO_APPLY", raising=False)
    else:
        monkeypatch.setenv("JOBAGENT_INBOX_AUTO_APPLY", flag)
    monkeypatch.setattr(cli, "run_inbox", lambda *args, **kwargs: calls.append(kwargs["propose_only"]) or InboxResult("JobAgent", 0))
    stage = cli._inbox_stage(repository, None)
    assert stage is not None and stage()["mode"] == ("proposed" if propose_only else "applied")
    assert calls == [propose_only]



def test_html_only_body_becomes_text() -> None:
    from email.message import EmailMessage

    from jobagent.inbox import html_to_text, render_email

    assert html_to_text("<html><head><style>p{}</style></head><body><p>Hi&nbsp;Alex,</p><p>Next <b>steps</b></p>"
                        "<script>alert(1)</script></body></html>") == "Hi Alex,\n\nNext steps"
    message = EmailMessage()
    message["From"], message["Subject"] = "a@b.example", "Update"
    message.set_content("<div>Thanks for applying.</div><div>Best</div>", subtype="html")
    rendered = render_email(message.as_bytes())
    assert "Thanks for applying.\n\nBest" in rendered and "<div>" not in rendered and "Date: " in rendered


def test_quoted_history_is_trimmed_also_when_wrapped() -> None:
    from jobagent.inbox import trim_quoted

    assert trim_quoted("Reply here.\n\nOn Thu, Nov 13, 2026 at 10:16 AM Alex <r@x.com> wrote:\n> old") == ("Reply here.", True)
    assert trim_quoted("Reply.\nOn Thu, Nov 13, 2026 at 10:16 AM Example Candidate <\nr@x.com> wrote:\n> old") == ("Reply.", True)
    assert trim_quoted("-----Original Message-----") == ("", True)
    assert trim_quoted("No history here.") == ("No history here.", False)


def test_split_for_telegram_caps_size_and_count() -> None:
    from jobagent.inbox import split_for_telegram

    assert split_for_telegram("short", "hint") == ["short"]
    parts = split_for_telegram("\n".join(["x" * 100] * 30), "hint", size=1000)
    assert len(parts) == 4 and all(len(part) <= 1000 for part in parts)
    many = split_for_telegram("y" * 30000, "rfc822msgid:abc")
    assert len(many) == 5 and all(len(part) <= 4000 for part in many)
    assert many[-1].endswith("…truncated, open in Gmail: rfc822msgid:abc")


def test_fetch_one_is_read_only() -> None:
    message = raw_email("<one@x>", "r@acme.example", "Hi", "Body")
    fake = FakeImap([message])
    raw = GmailReader("me@example.com", PASSWORD, imap_factory=lambda: fake).fetch_one("<one@x>")
    assert raw == message
    assert ("SELECT", '"[Gmail]/All Mail"', "True") in fake.calls
    assert ("UID", "SEARCH", "HEADER", "Message-ID", '"<one@x>"') in fake.calls
    fetches = [call for call in fake.calls if call[:2] == ("UID", "FETCH")]
    assert fetches and all("BODY.PEEK[" in call[-1] for call in fetches)
    assert not [call for call in fake.calls if call[0] in {"STORE", "COPY", "EXPUNGE", "MOVE", "APPEND"}]
