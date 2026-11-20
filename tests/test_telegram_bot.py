"""Telegram bot: Telegram, intake, prepare and Notion are all mocked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jobagent.db import JobRepository, StoredJob
from jobagent.intake import IntakeError
from jobagent.telegram_bot import Bot, job_flags, load_offset, parse_message, save_offset, trim

CHAT = "12345"
NOW = datetime(2026, 11, 17, tzinfo=UTC)


def stored(**overrides: object) -> StoredJob:
    values: dict[str, object] = {
        "id": 7, "canonical_dedupe_key": "k", "canonical_source": "greenhouse", "company": "Acme",
        "title": "Security Engineer", "location": "Boston, MA", "source_job_id": None, "canonical_url": "https://x",
        "first_seen_at": NOW, "last_seen_at": NOW,
        "description_text": "We need 5+ years of experience. Active security clearance required.",
    }
    return StoredJob(**(values | overrides))  # type: ignore[arg-type]


@dataclass
class Imported:
    job: StoredJob
    created: bool


@pytest.fixture
def bot(tmp_path: Path) -> Bot:
    repository = MagicMock(spec=JobRepository)
    repository.get_job.return_value = stored()
    sent: list[str] = []
    instance = Bot("TOKEN123", CHAT, repository, str(tmp_path / "db.sqlite3"), http=MagicMock(), send=sent.append,
                   importer=MagicMock(), prepare=MagicMock(return_value="Ready: 1"), sync=MagicMock(return_value="Notion: synced"),
                   start_cycle=MagicMock(return_value=999), find_cycle=MagicMock(return_value=None),
                   offset_path=tmp_path / "offset", logs=tmp_path / "logs")
    instance.sent = sent  # type: ignore[attr-defined]
    return instance


def update(text: str, chat: object = CHAT, update_id: int = 1) -> dict:
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "text": text}}


def test_other_chats_are_ignored(bot: Bot) -> None:
    bot.handle_update(update("/applied 7", chat=999))
    bot.handle_update(update("/help", chat="999"))
    assert bot.sent == []  # type: ignore[attr-defined]
    bot.repository.set_application_status.assert_not_called()  # type: ignore[attr-defined]


def test_url_message_queues_then_imports_prepares_and_replies(bot: Bot) -> None:
    bot.handle_update(update("Look at this https://boards.greenhouse.io/acme/jobs/1 please"))
    assert bot.sent == ["on it…"]  # type: ignore[attr-defined]
    assert bot.urls.get_nowait() == "https://boards.greenhouse.io/acme/jobs/1"
    repository = MagicMock(spec=JobRepository)
    repository.get_latest_ready_packet.return_value = MagicMock(cover_letter_pdf_path="c.pdf")
    bot.importer.return_value = Imported(stored(), created=True)  # type: ignore[union-attr]
    reply = bot.process_url(repository, "https://boards.greenhouse.io/acme/jobs/1")
    bot.prepare.assert_called_once_with(7)  # type: ignore[union-attr]
    assert "Job #7: Acme / Security Engineer" in reply and "Location: Boston, MA" in reply
    assert "clearance" in reply and "5+ years mentioned" in reply and "Packet: ready" in reply


def test_duplicate_url_reports_existing_id_without_preparing(bot: Bot) -> None:
    repository = MagicMock(spec=JobRepository)
    repository.get_latest_ready_packet.return_value = None
    bot.importer.return_value = Imported(stored(id=42), created=False)  # type: ignore[union-attr]
    assert bot.process_url(repository, "https://x") is None  # sent with a 🛠 Prepare packet button instead
    message = keyboard_messages(bot)[-1]
    assert message["text"].startswith("Duplicate: already in the database as job #42") and "Packet: not prepared" in message["text"]
    assert [button["text"] for row in message["reply_markup"]["inline_keyboard"] for button in row] == ["🛠 Prepare packet"]
    bot.prepare.assert_not_called()  # type: ignore[union-attr]
    repository.get_latest_ready_packet.return_value = MagicMock()
    assert bot.process_url(repository, "https://x").endswith("Packet: ready\n/packet 42")  # type: ignore[union-attr]


def test_import_failure_is_reported(bot: Bot) -> None:
    bot.importer.side_effect = IntakeError("no posting")  # type: ignore[union-attr]
    assert "Could not import" in bot.process_url(MagicMock(), "https://x")


def test_applied_sets_status_and_syncs(bot: Bot) -> None:
    bot.handle_update(update("/applied 7"))
    bot.repository.set_application_status.assert_called_once_with(7, "applied", source="telegram")  # type: ignore[attr-defined]
    bot.sync.assert_called_once_with(bot.repository, 7)  # type: ignore[union-attr]
    assert "applied" in bot.sent[0] and "Notion: synced" in bot.sent[0]  # type: ignore[attr-defined]


def test_status_rejects_unknown_state_with_valid_list(bot: Bot) -> None:
    bot.handle_update(update("/status 7 hired"))
    assert "Invalid state 'hired'" in bot.sent[0] and "interview" in bot.sent[0]  # type: ignore[attr-defined]
    bot.repository.set_application_status.assert_not_called()  # type: ignore[attr-defined]


def test_job_ids_must_be_integers(bot: Bot) -> None:
    bot.handle_update(update("/applied 7;rm -rf /"))
    assert bot.sent == ["Usage: /applied <id>"]  # type: ignore[attr-defined]


def test_run_refused_while_a_cycle_is_running(bot: Bot) -> None:
    bot.find_cycle.return_value = 4321  # type: ignore[attr-defined]
    bot.handle_update(update("/run"))
    assert "already running (PID 4321)" in bot.sent[0]  # type: ignore[attr-defined]
    bot.start_cycle.assert_not_called()  # type: ignore[union-attr]


def test_run_starts_a_cycle(bot: Bot) -> None:
    bot.handle_update(update("/run"))
    bot.start_cycle.assert_called_once()  # type: ignore[union-attr]
    assert "Cycle started" in bot.sent[0] and "PID 999" in bot.sent[0]  # type: ignore[attr-defined]


def test_packet_sends_two_documents(bot: Bot, tmp_path: Path) -> None:
    resume, letter = tmp_path / "r.pdf", tmp_path / "c.pdf"
    resume.write_bytes(b"%PDF"); letter.write_bytes(b"%PDF")
    bot.repository.get_latest_ready_packet.return_value = MagicMock(resume_pdf_path=str(resume), cover_letter_pdf_path=str(letter))  # type: ignore[attr-defined]
    bot.handle_update(update("/packet 7"))
    calls = bot.http.post.call_args_list  # type: ignore[attr-defined]
    assert len(calls) == 2 and all(call.args[0].endswith("/sendDocument") for call in calls)
    assert [call.kwargs["files"]["document"][0] for call in calls] == ["r.pdf", "c.pdf"]
    assert all(call.kwargs["data"]["chat_id"] == CHAT for call in calls)
    assert bot.sent == []  # type: ignore[attr-defined]


def test_offset_persists_and_poll_does_not_replay(bot: Bot) -> None:
    assert load_offset(bot.offset_path) == 0
    save_offset(43, bot.offset_path)
    assert load_offset(bot.offset_path) == 43
    responses = [MagicMock(json=MagicMock(return_value={"result": [update("/help", update_id=50)]})), KeyboardInterrupt()]
    bot.http.get.side_effect = responses  # type: ignore[attr-defined]
    with pytest.raises(KeyboardInterrupt):
        bot.poll_forever(lambda: MagicMock())
    assert bot.http.get.call_args_list[0].kwargs["params"]["offset"] == 43  # type: ignore[attr-defined]
    assert load_offset(bot.offset_path) == 51


def test_replies_are_trimmed_to_telegram_limit(bot: Bot) -> None:
    assert len(trim("x" * 5000)) == 4096 and trim("short") == "short"
    bot.reply("y" * 9000)
    assert len(bot.sent[0]) == 4096  # type: ignore[attr-defined]


def test_parse_message() -> None:
    assert parse_message("/status@JobAgentBot 7 applied") == ("status", ["7", "applied"])
    assert parse_message("ftp://x.example/job") == ("unknown", [])
    assert parse_message("hello") == ("unknown", [])


def test_flags_remote_onsite_and_near_misses() -> None:
    assert "remote" in job_flags(stored(location="Remote, US", description_text=""))
    assert job_flags(stored(location="Austin, TX", description_text=""))[0].startswith("onsite outside Boston")
    # near miss: "Mandatory" must not read as the state "MA"
    assert job_flags(stored(location="Mandatory Office", description_text=""))[0].startswith("onsite outside Boston")
    assert job_flags(stored(description_text="Great team.")) == []


def wait_for(condition: object, seconds: float = 5) -> None:
    import threading

    for _ in range(int(seconds / 0.05)):
        if condition():  # type: ignore[operator]
            return
        threading.Event().wait(0.05)
    raise AssertionError("timed out")


def keyboard_messages(bot: Bot) -> list[dict]:
    return [call.kwargs["json"] for call in bot.http.post.call_args_list  # type: ignore[attr-defined]
            if call.args[0].endswith("/sendMessage") and "reply_markup" in call.kwargs["json"]]


def tap(bot: Bot, label: str) -> None:
    data = next(button["callback_data"] for row in keyboard_messages(bot)[-1]["reply_markup"]["inline_keyboard"]
                for button in row if button["text"].startswith(label))
    bot.handle_update({"update_id": 2, "callback_query": {"id": "q", "data": data, "message": {"chat": {"id": CHAT}}}})


@pytest.fixture
def inbox_bot(tmp_path: Path) -> tuple[Bot, JobRepository, dict[str, int]]:
    """A real SQLite file; Gmail is a fake IMAP server with one rejection and one receipt."""
    from jobagent.inbox import GmailReader, propose_inbox, scan_inbox
    from tests.test_inbox import PASSWORD, REJECTION, FakeImap, job, raw_email

    database = tmp_path / "db.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    ids = {"quantia": repository.add_or_get_job(job("f", "Quantia, Inc.", "Security Engineer", "https://job-boards.greenhouse.io/quantia/jobs/1")).job.id,
           "acme": repository.add_or_get_job(job("a", "Acme Robotics", "Cloud Engineer", "https://jobs.lever.co/acme/1")).job.id}
    for job_id in ids.values():
        repository.set_application_status(job_id, "applied")
    messages = [raw_email("<r1@x>", "no-reply@greenhouse-mail.io", "Your application to Quantia", REJECTION),
                raw_email("<c1@x>", "no-reply@lever.co", "Thank you for applying to Acme Robotics", "We have received your application.")]

    def scan() -> object:
        own = JobRepository(database)  # the inbox thread's own engine, as in production
        reader = GmailReader("me@example.com", PASSWORD, imap_factory=lambda: FakeImap(messages))
        result = scan_inbox(own, reader)
        propose_inbox(own, result)
        return result

    sent: list[str] = []
    instance = Bot("TOKEN123", CHAT, repository, str(database), http=MagicMock(), send=sent.append, importer=MagicMock(),
                   prepare=MagicMock(), sync=MagicMock(), start_cycle=MagicMock(), find_cycle=MagicMock(return_value=None),
                   scan_inbox=scan, offset_path=tmp_path / "offset", logs=tmp_path / "logs")  # type: ignore[arg-type]
    instance.sent = sent  # type: ignore[attr-defined]
    return instance, repository, ids


def test_inbox_dry_runs_then_applies_exactly_the_proposals_on_yes(inbox_bot: tuple[Bot, JobRepository, dict[str, int]],
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    bot, repository, ids = inbox_bot
    monkeypatch.setattr("jobagent.telegram_bot.notion_tracker", MagicMock(side_effect=RuntimeError("no notion")))
    assert bot.dispatch("/inbox").startswith("Checking Gmail (dry run")
    wait_for(lambda: keyboard_messages(bot))
    prompt = keyboard_messages(bot)[-1]["text"]
    assert f"Quantia, Inc. #{ids['quantia']} → rejected" in prompt and "Apply these changes?" in prompt
    assert repository.latest_statuses()[ids["quantia"]] == "applied"  # nothing changed before the tap
    assert [row.message_id for row in repository.list_email_responses()] == ["<c1@x>"]  # only the no-change receipt
    bot.scan_inbox = MagicMock(side_effect=AssertionError("must not re-scan"))
    tap(bot, "✅")
    assert repository.latest_statuses() == {ids["quantia"]: "rejected", ids["acme"]: "applied"}
    assert bot.sent[-1].startswith("Applied 1 status change(s).")  # type: ignore[attr-defined]
    row = next(row for row in repository.list_email_responses() if row.message_id == "<r1@x>")
    assert row.applied_status_change == "rejected" and row.mailbox == "[Gmail]/All Mail"
    tap(bot, "✅")  # a second tap does nothing
    assert bot.sent[-1] == "That request is no longer pending."  # type: ignore[attr-defined]


def test_inbox_no_changes_nothing_and_flags_for_review(inbox_bot: tuple[Bot, JobRepository, dict[str, int]]) -> None:
    bot, repository, ids = inbox_bot
    bot.dispatch("/inbox")
    wait_for(lambda: keyboard_messages(bot))
    tap(bot, "❌")
    assert repository.latest_statuses()[ids["quantia"]] == "applied"
    assert bot.sent[-1].startswith("Cancelled: no status changed. 1 email(s) moved to review")  # type: ignore[attr-defined]
    row = next(row for row in repository.list_email_responses() if row.message_id == "<r1@x>")
    assert row.needs_review and row.applied_status_change is None


def test_inbox_without_proposals_has_no_buttons(bot: Bot) -> None:
    from jobagent.inbox import InboxResult

    bot.scan_inbox = MagicMock(return_value=InboxResult("JobAgent", 0))
    bot.dispatch("/inbox")
    wait_for(lambda: bot.sent)  # type: ignore[attr-defined]
    assert bot.sent == ["Inbox: 0 proposed status change(s)."]  # type: ignore[attr-defined]
    assert not keyboard_messages(bot)
    bot.handle_update(update("/inbox", chat=999))  # another chat: ignored


def photo(caption: str | None) -> dict:
    message: dict = {"chat": {"id": CHAT}, "photo": [{"file_id": "abc"}]}
    if caption is not None:
        message["caption"] = caption
    return {"update_id": 3, "message": message}


def test_photo_caption_is_the_message(bot: Bot) -> None:
    bot.handle_update(photo("Apply to this https://boards.greenhouse.io/acme/jobs/1"))
    assert bot.sent == ["on it…"] and bot.urls.get_nowait() == "https://boards.greenhouse.io/acme/jobs/1"  # type: ignore[attr-defined]
    bot.intents = MagicMock()
    bot.intents.handle_text.return_value = "routed"
    bot.handle_update(photo("did I get a reply from Dunder Mifflin?"))
    bot.intents.handle_text.assert_called_once_with("did I get a reply from Dunder Mifflin?")
    assert bot.sent[-1] == "routed"  # type: ignore[attr-defined]


def test_photo_without_caption_says_images_are_unsupported(bot: Bot) -> None:
    bot.handle_update(photo(None))
    bot.handle_update(photo("   "))
    assert bot.sent == ["I can't read images yet — send the link or describe what you need."] * 2  # type: ignore[attr-defined]



def test_email_command_shows_only_recorded_emails_as_plain_text(bot: Bot) -> None:
    from email.message import EmailMessage

    from jobagent.db import EmailResponse

    bot.repository.get_email_response.return_value = None  # type: ignore[attr-defined]
    assert bot.dispatch("/email 999").startswith("No recorded email #999")
    assert bot.dispatch("/email abc").startswith("Usage: /email")
    message = EmailMessage()
    message["From"], message["Subject"], message["Date"] = "Recruiter <r@acme.example>", "Re: your *application*", "Fri, 13 Nov 2026 15:16:28 +0000"
    message.set_content("Hello <b>Master</b>, see https://acme.example/x\n\nOn Thu, Nov 12 Alex wrote:\n> my earlier text")
    message.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename="offer.pdf")
    bot.repository.get_email_response.return_value = EmailResponse(  # type: ignore[attr-defined]
        "<m1@acme>", None, NOW, "r@acme.example", "Re: your application", "other", 0.0, None, True, id=5)
    fetched: list[str] = []
    bot.fetch_email = lambda message_id: fetched.append(message_id) or message.as_bytes()
    bot.intents = MagicMock()
    assert bot.dispatch("/email 5") == "Fetching email #5 (read-only)…"
    wait_for(lambda: bot.http.post.call_args_list)  # type: ignore[attr-defined]
    payload = bot.http.post.call_args_list[-1].kwargs["json"]  # type: ignore[attr-defined]
    assert fetched == ["<m1@acme>"]
    assert "parse_mode" not in payload and payload["disable_web_page_preview"] is True
    text = payload["text"]
    assert text.startswith("Email #5\nFrom: Recruiter <r@acme.example>\nDate: 2026-11-13") and "Subject: Re: your *application*" in text
    assert "Hello <b>Master</b>" in text and "my earlier text" not in text and "(quoted history trimmed)" in text
    assert "Attachments (not sent): offer.pdf" in text
    bot.intents.handle_text.assert_not_called()  # the body is never routed
