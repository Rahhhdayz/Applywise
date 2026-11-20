"""Outcome tracking: lifecycle rules, Notion outcome pull and option merge, quiet applications, funnel, follow-ups."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from jobagent import cli
from jobagent.db import JobRepository
from jobagent.followup import FollowupDraft, FollowupService, validate_followup
from jobagent.lifecycle import accepts_pulled, status_from_label
from jobagent.outcomes import due_for_followup, funnel, quiet_applications
from jobagent.pipeline import pull_notion_statuses
from tests.test_milestone5 import candidate, posting
from tests.test_notion import FakeNotion

LATER = datetime.now(UTC) + timedelta(days=11)  # "11 days after the statuses below were recorded"


@pytest.fixture
def repository() -> JobRepository:
    repository = JobRepository(":memory:")
    repository.initialize()
    return repository


@pytest.mark.parametrize(("current", "new", "accepted"), [
    (None, "applied", True), ("prefilled", "skipped", True), ("applied", "interview", True),
    ("interview", "applied", False),  # a stale Notion "Applied" never undoes a later local state
    ("applied", "rejected", True), ("interview", "rejected", True),
    ("rejected", "interview", False), ("skipped", "applied", False),  # closed stays closed
    ("ghosted", "interview", True), ("ghosted", "applied", False),  # a reply reopens a ghosted one
    ("applied", "applied", False), ("applied", "bogus", False),
])
def test_accepts_pulled(current: str | None, new: str, accepted: bool) -> None:
    assert accepts_pulled(current, new) is accepted


def test_status_from_label() -> None:
    assert status_from_label("Recruiter Screen") == "recruiter_screen"
    assert status_from_label("Offer") == "offer"


def test_notion_pull_records_interviews_and_offers_after_applying(repository: JobRepository) -> None:
    ids = [repository.add_or_get_job(posting(f"o-{index}")).job.id for index in range(3)]
    for job_id in ids:
        repository.set_application_status(job_id, "applied")

    class NotionRows:
        def statuses(self) -> dict[int, str]:
            return {ids[0]: "Interview", ids[1]: "Recruiter Screen", ids[2]: "Offer"}

    assert pull_notion_statuses(repository, NotionRows()) == {"updated": 3}  # type: ignore[arg-type]
    assert [repository.latest_statuses()[job_id] for job_id in ids] == ["interview", "recruiter_screen", "offer"]


def test_setup_merges_new_status_options_and_keeps_existing_ones() -> None:
    notion = FakeNotion()
    notion.properties = {**notion.properties, "Status": {"type": "select", "select": {"options": [
        {"id": "a", "name": "Ready"}, {"id": "b", "name": "Applied"}, {"id": "c", "name": "Waitlist"}]}}}

    added = notion.tracker().setup()

    assert "Status: Interview" in added and "Status: Ready" not in added
    body = notion.requests[-1][2]
    names = [option["name"] for option in body["properties"]["Status"]["select"]["options"]]
    assert names[:3] == ["Ready", "Applied", "Waitlist"]  # existing (and Master's own) options kept, in order
    assert {"Interview", "Offer", "Ghosted", "Recruiter Screen"} <= set(names)


def test_set_status_rejects_unknown_states(repository: JobRepository) -> None:
    job_id = repository.add_or_get_job(posting("bad")).job.id
    with pytest.raises(ValueError, match="Unsupported application status"):
        repository.set_application_status(job_id, "interviewing")


def test_quiet_applications_and_followup_cap(repository: JobRepository) -> None:
    applied, interview, closed, fresh = (repository.add_or_get_job(posting(f"q-{index}")).job.id for index in range(4))
    for job_id in (applied, interview, closed):
        repository.set_application_status(job_id, "applied")
    repository.set_application_status(interview, "interview")
    repository.set_application_status(closed, "rejected")
    repository.set_application_status(fresh, "ready")

    assert {item.job_id for item in quiet_applications(repository, days=10, now=LATER)} == {applied, interview}
    assert quiet_applications(repository, days=10) == []  # nothing is quiet yet today

    repository.add_followup(applied, "s", "b", None)  # drafted but not sent: not due again
    assert {item.job_id for item in due_for_followup(repository, days=10, now=LATER)} == {interview}
    repository.mark_followup_sent(applied)  # sending (today) restarts the quiet clock
    assert applied not in {item.job_id for item in due_for_followup(repository, days=12, now=LATER)}
    due = {item.job_id: item for item in due_for_followup(repository, days=10, now=LATER)}
    assert due[applied].followups_sent == 1  # 11 quiet days after the first one: due again
    repository.add_followup(applied, "s2", "b2", None)
    repository.mark_followup_sent(applied)
    assert applied not in {item.job_id for item in due_for_followup(repository, days=10, now=LATER)}  # cap of 2


def test_funnel_counts_every_stage_a_job_reached(repository: JobRepository) -> None:
    ids = [repository.add_or_get_job(posting(f"f-{index}")).job.id for index in range(4)]
    for job_id in ids:
        repository.set_application_status(job_id, "applied")
    repository.set_application_status(ids[0], "interview")  # skipped the screen: still counts as screened
    repository.set_application_status(ids[0], "offer")
    repository.set_application_status(ids[1], "recruiter_screen")
    repository.set_application_status(ids[1], "rejected")
    skipped = repository.add_or_get_job(posting("never-applied")).job.id
    repository.set_application_status(skipped, "skipped")

    result = funnel(repository)

    assert result.reached == {"applied": 4, "recruiter_screen": 2, "interview": 1, "offer": 1}
    assert result.closed == {"rejected": 1, "ghosted": 0}
    assert result.rate("interview") == 0.25
    assert sum(week["applied"] for week in result.by_week.values()) == 4


GOOD = FollowupDraft(subject="Following up on my Security Engineer application", paragraphs=[
    ("I applied for the Security Engineer role earlier this month and wanted to follow up. The work your team is "
     "doing on detection engineering is exactly where I want to grow, and I built security automation that "
     "cut investigation time for our analysts."),
    "Is there any update on the timeline or next steps for the role? I would be glad to share more about my work.",
], evidence_ids=["fact_1"])


def test_validate_followup_accepts_good_and_flags_rule_breaks() -> None:
    truth = candidate()
    assert validate_followup(GOOD, truth) == []
    bad = GOOD.model_copy(update={"paragraphs": ["Hi there, I lack experience but [Name] said apply."], "evidence_ids": ["nope"]})
    issues = " ".join(validate_followup(bad, truth))
    for expected in ("Unknown evidence IDs", "words", "placeholders", "greeting", "shortcoming"):
        assert expected in issues


class FakeFollowupWriter:
    def __init__(self) -> None:
        self.numbers: list[int] = []

    def draft(self, job, truth, *, number, applied_on):  # type: ignore[no-untyped-def]
        self.numbers.append(number)
        assert applied_on is not None
        return GOOD


def test_service_writes_numbered_drafts_beside_the_packet(repository: JobRepository, tmp_path: Path) -> None:
    job = repository.add_or_get_job(posting("svc")).job
    repository.save_packet(job_id=job.id, fingerprint="f", status="ready", packet_directory=str(tmp_path / "Example_Corp"))
    repository.set_application_status(job.id, "applied")
    writer = FakeFollowupWriter()
    service = FollowupService(repository, candidate(), writer, "Example Person")  # type: ignore[arg-type]

    first = service.draft(job)
    second = service.draft(job)

    assert writer.numbers == [1, 2] and (first.number, second.number) == (1, 2)
    text = Path(first.draft_path or "").read_text()
    assert first.draft_path.endswith("Follow_Up_1.txt") and "Example_Corp" in first.draft_path  # type: ignore[union-attr]
    assert "Hello," in text and "never sends email" in text and "Example Person" in text
    assert "followup sent" in text


def test_cli_followup_sent_and_outcome_sweep_dry_run(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job_id = repository.add_or_get_job(posting("cli")).job.id
    repository.set_application_status(job_id, "applied")

    assert cli.main(["--database", str(database), "followup", "sent", str(job_id)]) == 1  # nothing drafted yet
    repository.add_followup(job_id, "s", "b", None)
    assert cli.main(["--database", str(database), "followup", "sent", str(job_id)]) == 0
    assert repository.list_followups(job_id)[0].sent_at is not None

    assert cli.main(["--database", str(database), "outcome", "sweep", "--days", "0"]) == 0
    assert "re-run with --yes" in capsys.readouterr().out
    assert repository.latest_statuses()[job_id] == "applied"  # dry run changes nothing
    assert cli.main(["--database", str(database), "outcome", "sweep", "--days", "0", "--yes"]) == 0
    assert repository.latest_statuses()[job_id] == "ghosted"
    assert cli.main(["--database", str(database), "status", "set", str(job_id), "interviewing"]) == 1
