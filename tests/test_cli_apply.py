"""CLI-level tests for `jobagent apply fill`. Never launches a real browser here."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from jobagent import cli
from jobagent.apply.filler import FilledField, FillReport
from jobagent.db import JobRepository
from jobagent.models import JobPosting


@pytest.fixture(autouse=True)
def no_notion(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """`apply fill` pulls Notion statuses first; unit tests must never reach the real Notion."""
    def unavailable() -> None:
        raise RuntimeError("Notion not configured in tests")

    monkeypatch.setattr(cli, "notion_tracker", unavailable)
    monkeypatch.setattr(cli, "resolve_application_url", lambda *a, **k: None)  # never the real network


def make_job(repository: JobRepository, **overrides: object):
    values: dict[str, object] = {
        "source": "greenhouse",
        "source_job_id": "123",
        "company": "Example Corp",
        "title": "Security Engineer",
        "location": "Remote",
        "apply_url": "https://boards.greenhouse.io/examplecompany/jobs/123",
        "canonical_url": "https://boards.greenhouse.io/examplecompany/jobs/123",
        "discovered_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    values.update(overrides)
    return repository.add_or_get_job(JobPosting(**values)).job


def test_apply_fill_requires_a_ready_packet(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job = make_job(repository)

    assert cli.main(["--database", str(database), "apply", "fill", "--job-id", str(job.id)]) == 1
    assert "no ready packet" in capsys.readouterr().out


def test_apply_fill_reports_unsupported_ats(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job = make_job(
        repository,
        source="manual_url",
        source_job_id=None,
        apply_url="https://careers.example.com/openings/1",
        canonical_url="https://careers.example.com/openings/1",
    )
    repository.save_packet(job_id=job.id, fingerprint="fp", status="ready", packet_directory=str(tmp_path))

    assert cli.main(["--database", str(database), "apply", "fill", "--job-id", str(job.id)]) == 1
    assert "unsupported ATS" in capsys.readouterr().out


def test_apply_fill_reports_job_not_found(tmp_path, capsys) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"

    assert cli.main(["--database", str(database), "apply", "fill", "--job-id", "999"]) == 1
    assert "Job not found: 999" in capsys.readouterr().out


def test_apply_fill_records_prefilled_status_and_writes_report(tmp_path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job = make_job(repository)
    packet_dir = tmp_path / "packet"
    packet_dir.mkdir()
    repository.save_packet(job_id=job.id, fingerprint="fp", status="ready", packet_directory=str(packet_dir))

    def fake_fill(form, profile, *, job_id, resume_path, cover_letter_path=None, slow_mo=0, profile_directory=None, oversight=None):  # type: ignore[no-untyped-def]
        return FillReport(job_id=job_id, form_url=form.form_url, fields=[FilledField("Email", "text", "filled")])

    monkeypatch.setattr(cli, "fill_application_form", fake_fill)
    monkeypatch.setattr(cli, "FILL_REPORTS", tmp_path / "fill_reports")

    assert cli.main(["--database", str(database), "apply", "fill", "--job-id", str(job.id)]) == 0
    assert "Filled: 1" in capsys.readouterr().out
    assert (tmp_path / "fill_reports" / f"job_{job.id}.json").exists()
    assert repository.status_counts() == {"prefilled": 1}


def test_apply_fill_ready_tabs_skips_applied_and_sign_in_forms(tmp_path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    open_job = make_job(repository)
    applied = make_job(repository, source_job_id="456", apply_url="https://boards.greenhouse.io/x/jobs/456", canonical_url="https://boards.greenhouse.io/x/jobs/456")
    sign_in = make_job(repository, source="workday", source_job_id="789", apply_url="https://x.wd1.myworkdayjobs.com/en-US/x/job/789", canonical_url="https://x.wd1.myworkdayjobs.com/en-US/x/job/789")
    for job in (open_job, applied, sign_in):
        repository.save_packet(job_id=job.id, fingerprint=f"fp{job.id}", status="ready", packet_directory=str(tmp_path))
    repository.set_application_status(applied.id, "applied")
    seen = []

    def fake_tabs(tabs, profile, *, profile_directory, slow_mo, on_report):  # type: ignore[no-untyped-def]
        seen.extend(tab.job_id for tab in tabs)
        for tab in tabs:
            on_report(FillReport(job_id=tab.job_id, form_url=tab.form.form_url, fields=[]))

    monkeypatch.setattr(cli, "fill_in_tabs", fake_tabs)
    monkeypatch.setattr(cli, "FILL_REPORTS", tmp_path / "fill_reports")

    assert cli.main(["--database", str(database), "apply", "fill", "--ready", "--tabs"]) == 0
    assert seen == [open_job.id]
    assert f"Job #{sign_in.id}: skipped" in capsys.readouterr().out
    assert repository.latest_statuses()[open_job.id] == "prefilled"


def test_lever_apply_url_goes_before_the_query_string(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jobagent.apply.ats import application_form

    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    url = "https://jobs.lever.co/rover/f56c/?lever-source=Indeed"
    job = make_job(repository, source="lever", apply_url=url, canonical_url=url)
    form = application_form(job)
    assert form is not None and form.form_url == "https://jobs.lever.co/rover/f56c/apply?lever-source=Indeed"


def test_icims_form_opens_outside_its_iframe(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jobagent.apply.ats import application_form

    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    url = "https://careers-x.icims.com/jobs/1/role/login?mobile=false"
    job = make_job(repository, source="icims", apply_url=url, canonical_url=url)
    form = application_form(job)
    assert form is not None and form.form_url == url + "&in_iframe=1" and form.multi_page


def test_ashby_form_url_keeps_the_query_after_the_application_path() -> None:
    from jobagent.apply.ats import _ashby_url
    from jobagent.db import StoredJob

    def job(url: str) -> StoredJob:
        now = datetime.now(UTC)
        return StoredJob(1, "k", "ashby", "OpenAI", "SE", "Remote", "id", url, now, now, apply_url=url)

    base = "https://jobs.ashbyhq.com/openai/f956"
    assert _ashby_url(job(f"{base}/application?source=LinkedIn")) == f"{base}/application?source=LinkedIn"
    assert _ashby_url(job(f"{base}?source=LinkedIn")) == f"{base}/application?source=LinkedIn"
    assert _ashby_url(job(f"{base}/")) == f"{base}/application"


def _fake_fill(form, profile, *, job_id, resume_path, cover_letter_path=None, slow_mo=0, profile_directory=None, oversight=None):  # type: ignore[no-untyped-def]
    return FillReport(job_id=job_id, form_url=form.form_url, fields=[FilledField("Email", "text", "filled")])


def test_apply_fill_resolves_a_linkedin_link_to_its_ats_form_and_saves_it(tmp_path, capsys, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    linkedin = "https://www.linkedin.com/jobs/view/42"
    job = make_job(repository, source="linkedin", source_job_id="42", apply_url=linkedin, canonical_url=linkedin)
    repository.save_packet(job_id=job.id, fingerprint="fp", status="ready", packet_directory=str(tmp_path))
    form = "https://job-boards.greenhouse.io/embed/job_app?for=example&token=7"
    monkeypatch.setattr(cli, "resolve_application_url", lambda *a, **k: form)
    monkeypatch.setattr(cli, "fill_application_form", _fake_fill)
    monkeypatch.setattr(cli, "FILL_REPORTS", tmp_path / "fill_reports")

    assert cli.main(["--database", str(database), "apply", "fill", "--job-id", str(job.id)]) == 0
    assert repository.get_job(job.id).apply_url == form  # saved, so the next fill needs no lookup


def _apply_url(tmp_path, monkeypatch, *, applied: bool):  # type: ignore[no-untyped-def]
    """An older LinkedIn copy (with a packet) plus the newly pasted ATS link of the same opening."""
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    old = make_job(repository, source="linkedin", source_job_id="1", apply_url="https://www.linkedin.com/jobs/view/1",
                   canonical_url="https://www.linkedin.com/jobs/view/1")
    repository.save_packet(job_id=old.id, fingerprint="fp", status="ready", packet_directory=str(tmp_path))
    if applied:
        repository.set_application_status(old.id, "applied", source="notion")
    new = make_job(repository, source_job_id="2", apply_url="https://boards.greenhouse.io/example/jobs/2",
                   canonical_url="https://boards.greenhouse.io/example/jobs/2", company="EXAMPLE CORP")

    class Intake:
        def __init__(self, repository) -> None:  # type: ignore[no-untyped-def]
            pass

        def import_url(self, url):  # type: ignore[no-untyped-def]
            return type("Result", (), {"job": new})()

    form = "https://job-boards.greenhouse.io/embed/job_app?for=example&token=2"
    monkeypatch.setattr(cli, "UrlIntakeService", Intake)
    monkeypatch.setattr(cli, "resolve_application_url", lambda *a, **k: form)
    monkeypatch.setattr(cli, "fill_application_form", _fake_fill)
    monkeypatch.setattr(cli, "FILL_REPORTS", tmp_path / "fill_reports")
    code = cli.main(["--database", str(database), "apply", "url", "https://example.com/careers/2"])
    return code, repository, old, new, form


def test_apply_url_keeps_the_copy_with_a_packet_skips_the_duplicate_and_fills(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    code, repository, old, new, form = _apply_url(tmp_path, monkeypatch, applied=False)
    assert code == 0
    assert repository.latest_statuses() == {old.id: "prefilled", new.id: "skipped"}
    assert repository.get_job(old.id).apply_url == form


def test_apply_url_stops_on_an_opening_already_applied_to(tmp_path, monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    code, repository, old, _new, _ = _apply_url(tmp_path, monkeypatch, applied=True)
    assert code == 1
    assert "already applied" in capsys.readouterr().out
    assert repository.latest_statuses() == {old.id: "applied"}  # nothing skipped, nothing filled


def test_apply_url_imports_from_the_resolved_ats_not_the_careers_listing_page(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # STARK, Oct 8: the pasted listing page imported as a junk job "View Open Job Positions".
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job = make_job(repository)
    imported: list[str] = []

    class Intake:
        def __init__(self, repository) -> None:  # type: ignore[no-untyped-def]
            pass

        def import_url(self, url):  # type: ignore[no-untyped-def]
            imported.append(url)
            return type("Result", (), {"job": job})()

    ashby = "https://jobs.ashbyhq.com/stark/abc"
    monkeypatch.setattr(cli, "UrlIntakeService", Intake)
    monkeypatch.setattr(cli, "resolve_application_url", lambda *a, **k: ashby)
    monkeypatch.setattr(cli, "_run_prepare", lambda *a: 1)  # stop after import; this test is about the source
    cli.main(["--database", str(database), "apply", "url", "https://stark.ai/careers/open-positions/?ashby_jid=abc"])
    assert imported == [ashby]


def test_apply_track_adds_notion_row_without_documents_or_form_fill(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    job = make_job(repository)

    class Intake:
        def __init__(self, repository) -> None:  # type: ignore[no-untyped-def]
            pass

        def import_url(self, url):  # type: ignore[no-untyped-def]
            return type("Result", (), {"job": job})()

    synced: list[int] = []
    monkeypatch.setattr(cli, "UrlIntakeService", Intake)
    monkeypatch.setattr(cli, "_sync_one_to_notion", lambda repo, job_id: synced.append(job_id))
    monkeypatch.setattr(cli, "_run_prepare", lambda *a: pytest.fail("track must not write a resume"))
    monkeypatch.setattr(cli, "fill_application_form", lambda *a, **k: pytest.fail("track must not fill a form"))
    assert cli.main(["--database", str(database), "apply", "track", "https://example.com/jobs/1"]) == 0
    packet = repository.get_latest_ready_packet(job.id)
    assert packet is not None and packet.resume_pdf_path is None and packet.cover_letter_pdf_path is None
    assert synced == [job.id]


def test_workable_posting_opens_its_apply_page(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from jobagent.apply.ats import application_form

    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    url = "https://apply.workable.com/j/3DAD137619"
    job = make_job(repository, source="workable", apply_url=url, canonical_url=url)
    form = application_form(job)
    assert form is not None and form.form_url == url + "/apply/" and not form.multi_page
