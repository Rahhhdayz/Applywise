from datetime import UTC, datetime

from jobagent.db import JobRepository
from jobagent.models import JobPosting


def make_job(**overrides: object) -> JobPosting:
    values: dict[str, object] = {
        "source": "example",
        "source_job_id": "abc-123",
        "company": "Example Labs",
        "title": "Software Engineer",
        "location": "Remote",
        "canonical_url": "https://jobs.example.test/abc-123",
        "discovered_at": datetime(2026, 1, 1, tzinfo=UTC),
        "source_metadata": {"source": "unit-test"},
    }
    values.update(overrides)
    return JobPosting(**values)


def test_repository_persists_and_lists_jobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()

    result = repository.add_or_get_job(make_job())

    assert result.created is True
    assert repository.count_jobs() == 1
    assert repository.get_job(result.job.id) == result.job
    assert repository.list_jobs() == [result.job]


def test_repository_deduplicates_source_and_canonical_jobs(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()

    first = repository.add_or_get_job(make_job())
    same_source = repository.add_or_get_job(make_job(discovered_at=datetime(2026, 1, 2, tzinfo=UTC)))
    same_canonical_job = repository.add_or_get_job(
        make_job(source="another-source", source_job_id="other-456")
    )

    assert first.created is True
    assert same_source.created is False
    assert same_canonical_job.created is False
    assert repository.count_jobs() == 1
    assert repository.list_jobs()[0].last_seen_at == datetime(2026, 1, 2, tzinfo=UTC)


def test_status_counts_reflects_each_jobs_most_recent_status(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    first = repository.add_or_get_job(make_job()).job
    second = repository.add_or_get_job(
        make_job(source="other", source_job_id="other-1", canonical_url="https://jobs.example.test/other-1")
    ).job

    repository.set_application_status(first.id, "ready")
    repository.set_application_status(first.id, "applied")
    repository.set_application_status(second.id, "ready")

    assert repository.status_counts() == {"applied": 1, "ready": 1}


def test_identical_repeat_with_posted_at_is_not_an_update(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    job = make_job(posted_at=datetime(2026, 9, 20, tzinfo=UTC))
    repository.add_or_get_job(job)
    assert repository.add_or_get_job(job).updated is False


def test_legacy_unique_dedupe_index_is_relaxed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    path = tmp_path / "legacy.sqlite3"
    repository = JobRepository(path)
    repository.initialize()
    with repository._engine.begin() as connection:  # simulate an early-milestone database
        connection.exec_driver_sql("DROP INDEX ix_jobs_canonical_dedupe_key")
        connection.exec_driver_sql("CREATE UNIQUE INDEX ix_jobs_canonical_dedupe_key ON jobs (canonical_dedupe_key)")
    repository = JobRepository(path)
    repository.initialize()
    repository.add_or_get_job(make_job())
    same_fields = repository.add_or_get_job(
        make_job(source_job_id="other", canonical_url="https://jobs.example.test/other")
    )
    assert same_fields.created is True and repository.count_jobs() == 2


def test_new_packet_for_a_job_keeps_its_notion_page(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    job = repository.add_or_get_job(make_job()).job
    first = repository.save_packet(job_id=job.id, fingerprint="v1", status="ready")
    repository.set_packet_notion_page(first.id, "page-1")
    second = repository.save_packet(job_id=job.id, fingerprint="v2", status="ready")
    assert second.notion_page_id == "page-1"


def test_rescrape_keeps_resolved_ats_link_fuller_description_and_hand_pick(tmp_path) -> None:  # type: ignore[no-untyped-def]
    import sqlite3

    database = tmp_path / "jobs.sqlite3"
    repository = JobRepository(database)
    repository.initialize()
    board = "https://www.linkedin.com/jobs/view/1"
    job = repository.add_or_get_job(make_job(apply_url=board, description_text="short")).job
    with sqlite3.connect(database) as db:  # the ATS link and full text resolved after discovery
        db.execute(
            "update jobs set apply_url='https://jobs.ashbyhq.com/x/1', description_text='full description', "
            "source_metadata=json_set(source_metadata, '$.submitted_url', 'https://x.test/1') where id=?",
            (job.id,),
        )

    result = repository.add_or_get_job(make_job(apply_url=board, description_text="short", title="Software Engineer II"))

    assert result.job.apply_url == "https://jobs.ashbyhq.com/x/1"
    assert result.job.description_text == "full description"
    assert result.job.title == "Software Engineer II"  # other fields still refresh
    assert result.job.source_metadata["submitted_url"] == "https://x.test/1"


def test_cycle_run_returns_the_latest_run_for_a_queue_date(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    first = repository.start_workflow_run()
    repository.complete_workflow_run(first, status="partial", metrics={"queue_date": "2026-11-17", "queue": {"queued": 3}})
    second = repository.start_workflow_run()
    repository.complete_workflow_run(second, status="success", metrics={"queue_date": "2026-11-17", "queue": {"queued": 20}})
    status, started, completed, metrics = repository.cycle_run("2026-11-17")  # type: ignore[misc]
    assert (status, metrics["queue"], started.tzinfo, completed is not None) == ("success", {"queued": 20}, UTC, True)
    assert repository.cycle_run("2026-11-16") is None
