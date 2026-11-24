"""Resolution persistence/CLI tests use only newly created synthetic databases."""

from dataclasses import replace
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, inspect

from jobagent import cli
from jobagent.apply.ats import application_form
from jobagent.db import JobRepository
from jobagent.intake.resolve import DestinationResolution
from jobagent.models import JobPosting

URL = "https://www.linkedin.com/jobs/view/1"
ATS = "https://jobs.lever.co/acme/123"


@pytest.fixture
def repository():
    repo = JobRepository(engine=create_engine("sqlite://"))
    repo.initialize()
    return repo


def add(repo, number=1):
    return repo.add_or_get_job(JobPosting(source="linkedin", source_job_id=str(number),
        company="Acme", title="Security Engineer", apply_url=URL, canonical_url=URL + str(number),
        discovered_at=datetime(2026, 1, 1, tzinfo=UTC))).job


def eligible(repo, job, status="eligible", fingerprint="rules"):
    repo.upsert_eligibility(job_id=job.id, status=status, reasons=[], flags=[], detected_requirements={},
                            rules_version="1", rules_fingerprint=fingerprint)


def save(repo, job, ats="lever", url=ATS):
    repo.set_job_resolution(job.id, destination_url=url, ats=ats, evidence="linkedin_offsite;title+company", confidence=0.95)


def test_resolution_round_trip_keeps_urls_and_provenance(repository):
    job = add(repository)
    save(repository, job)
    stored = repository.get_job(job.id)
    assert stored.apply_url == job.apply_url
    assert stored.canonical_url == job.canonical_url
    assert stored.canonical_source == job.canonical_source
    assert stored.source_metadata == job.source_metadata
    assert stored.resolved_destination_url == ATS
    assert stored.resolved_ats == "lever"
    assert stored.resolution_evidence == "linkedin_offsite;title+company"
    assert stored.resolution_confidence == 0.95
    assert stored.resolved_at.tzinfo == UTC
    assert application_form(stored).form_url == ATS + "/apply"
    assert add(repository).resolved_destination_url == ATS  # rediscovery preserves the result


def test_additive_migration_existing_schema_and_repeated_initialize(repository):
    job = add(repository)
    with repository._engine.begin() as connection:
        for name in ("resolved_destination_url", "resolved_ats", "resolution_evidence", "resolution_confidence", "resolved_at"):
            connection.exec_driver_sql(f"ALTER TABLE jobs DROP COLUMN {name}")
    repository.initialize()
    repository.initialize()
    assert repository.get_job(job.id).apply_url == job.apply_url
    assert "resolved_at" in {column["name"] for column in inspect(repository._engine).get_columns("jobs")}
    save(repository, job)
    assert repository.get_job(job.id).resolved_destination_url == ATS


def test_pending_selection_and_limit(repository):
    jobs = [add(repository, n) for n in range(1, 8)]
    for job in jobs[:5]:
        eligible(repository, job)
    save(repository, jobs[1])
    save(repository, jobs[2], "linkedin_easy_apply", None)
    repository.set_application_status(jobs[3].id, "applied")
    eligible(repository, jobs[4], "ineligible", "new_rules")
    repository.add_queue_entries("2026-11-20", [{"job_id": jobs[5].id, "rank": 1, "ranking_score": 90,
                                           "exceptional": False, "reason": "test"}])
    assert [job.id for job in repository.list_pending_resolutions()] == [jobs[0].id, jobs[5].id]
    assert [job.id for job in repository.list_pending_resolutions(1)] == [jobs[0].id]


def test_fill_prefers_saved_destination_without_lookup(repository, monkeypatch):
    job = add(repository)
    save(repository, job)
    def fail(*args, **kwargs):
        pytest.fail("Saved resolution must not trigger HTTP")
    monkeypatch.setattr(cli, "resolve_destination", fail)
    _, form = cli._form_for(repository, repository.get_job(job.id))
    assert form.provider == "lever"
    assert form.form_url == ATS + "/apply"
    save(repository, job, "linkedin_easy_apply", None)
    assert cli._form_for(repository, repository.get_job(job.id))[1] is None


def test_fill_uses_destination_in_every_existing_url_builder(repository):
    job = add(repository)
    for url, expected in [("https://jobs.ashbyhq.com/acme/1", "/application"),
                          ("https://acme.icims.com/jobs/1", "?in_iframe=1"),
                          ("https://apply.workable.com/acme/j/1", "/apply/"),
                          ("https://acme.myworkdayjobs.com/jobs/1", "")]:
        assert application_form(replace(job, resolved_destination_url=url)).form_url == url + expected


def test_cli_pending_dry_run_and_counts(repository, monkeypatch, capsys):
    for n in range(1, 4):
        eligible(repository, add(repository, n))
    monkeypatch.setattr(cli, "load_local_env", lambda: None)
    monkeypatch.setattr(cli, "JobRepository", lambda *args: repository)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    seen = []
    def resolver(*args):
        seen.append(args)
        return DestinationResolution(ATS, "lever", "link;title+company", 0.95)
    monkeypatch.setattr(cli, "resolve_destination", resolver)
    assert cli.main(["resolve", "--pending", "--limit", "2", "--dry-run"]) == 0
    assert not seen
    assert "Would resolve: 2" in capsys.readouterr().out
    assert cli.main(["resolve", "--pending", "--limit", "2"]) == 0
    assert len(seen) == 2
    assert "lever: 2" in capsys.readouterr().out
    assert len(repository.list_pending_resolutions()) == 1
    assert cli.main(["resolve", "--pending", "--limit", "0"]) == 1
