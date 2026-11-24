"""Search a disposable database built only from synthetic job records."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import ValidationError
from sqlalchemy import URL, create_engine, event, insert, update
from sqlalchemy.exc import OperationalError

from jobagent.db import JobRepository
from jobagent.db.models import JobAssessmentRecord, JobRecord
from jobagent.db.repository import EmailResponse
from jobagent.job_query import (
    JobQuery,
    QueryRow,
    escape,
    format_results,
    readonly_engine,
    run_query,
)
from jobagent.models.job import JobPosting
from jobagent.scoring import JobMatchAssessment

TODAY = date(2026, 11, 19)
NOW = datetime.combine(TODAY, time(12)).astimezone(UTC)


@pytest.fixture
def records(tmp_path, monkeypatch):
    path = tmp_path / "db.sqlite3"
    repository = JobRepository(path)
    repository.initialize()
    jobs = []
    definitions = [
        (
            "Moody's",
            "SOC Analyst",
            "Boston",
            "linkedin",
            "https://a.myworkdayjobs.com/job/1",
            "applied",
            3,
        ),
        ("Old SOC", "soc engineer", "Remote", "workday", None, "applied", 20),
        ("Queue", "SOC intern", None, "manual_url", None, None, 0),
        (
            "Host",
            "Remote engineer",
            "Boston",
            "indeed",
            "https://a.myworkdaysite.com/job/4",
            "drafted",
            2,
        ),
        ("Source", "Security engineer", "NYC", "workday", None, "drafted", 1),
        ("Percent", "100% Analyst", "Boston", "manual", None, "rejected", 4),
    ]
    for n, (company, title, location, source, url, status, age) in enumerate(definitions):
        stamp = NOW - timedelta(days=age)
        monkeypatch.setattr("jobagent.db.repository.utc_now", lambda stamp=stamp: stamp)
        result = repository.add_or_get_job(
            JobPosting(
                source=source,
                source_job_id=str(n),
                company=company,
                title=title,
                location=location,
                apply_url=url,
                discovered_at=stamp,
            )
        )
        jobs.append(result.job.id)
        if status:
            repository.set_application_status(result.job.id, "ready")
            repository.set_application_status(result.job.id, status)
    repository.add_queue_entries(
        TODAY.isoformat(), [{"job_id": jobs[2], "rank": 1, "ranking_score": 80, "reason": "test"}]
    )
    for job_id, score in ((jobs[0], 85), (jobs[1], 90), (jobs[0], 88)):
        repository.add_assessment(
            job_id=job_id,
            assessment=JobMatchAssessment(
                overall_score=score,
                role_alignment_score=score,
                skills_score=score,
                experience_score=score,
                career_direction_score=score,
                summary_reason="synthetic",
                recommendation="match",
            ),
            scorer="fake",
            model_identifier="fake",
            scoring_version=str(score),
            candidate_fingerprint="fake",
            job_content_fingerprint="fake",
        )
    repository.add_email_response(
        EmailResponse(
            "fake-message",
            jobs[0],
            NOW,
            "fake@example.test",
            "synthetic",
            "interview",
            1,
            None,
            False,
        )
    )
    repository.add_email_response(
        EmailResponse(
            "fake-message-2",
            jobs[0],
            NOW,
            "fake@example.test",
            "synthetic",
            "confirmation",
            1,
            None,
            False,
        )
    )
    repository.save_packet(job_id=jobs[0], fingerprint="fake", status="ready")
    repository.save_packet(job_id=jobs[1], fingerprint="fake", status="failed")
    engine = readonly_engine(path)
    yield repository, engine, jobs
    engine.dispose()
    repository._engine.dispose()


@pytest.mark.parametrize(
    "values",
    [
        {"sql": "SELECT *"},
        *(
            {"title_words": [word]}
            for word in ["x' OR 1=1 --", "%", "a;b", "a_b", "a\nb", "(a)", "*", "a=b"]
        ),
        {"title_words": ["a"] * 6},
        {"title_words": ["a" * 41]},
        {"location_words": ["a"] * 4},
        {"limit": 26},
        {"limit": 0},
        {"status_in": ["queued"]},
        {"source_in": ["bad"]},
        {"ats_in": ["other"]},
        {"reply_kind_in": ["bad"]},
        {"sort": "id"},
        {"found_from": "2026-02-02", "found_to": "2026-02-01"},
        {"status_from": "2026-02-02", "status_to": "2026-02-01"},
        {"found_from": "2023-01-01", "found_to": "2026-01-01"},
        {"status_from": "2023-01-01", "status_to": "2026-01-01"},
        {"min_score": 90, "max_score": 80},
        {"min_score": -1},
        {"max_score": 101},
        {"company": "a%b"},
        {"company": "a;b"},
    ],
)
def test_query_validation(values):
    with pytest.raises(ValidationError):
        JobQuery.model_validate(values)


def ids(engine, **query):
    return [row.job_id for row in run_query(engine, JobQuery(**query))[1]]


def test_applied_soc_and_current_status(records):
    repository, engine, jobs = records
    assert ids(engine, title_words=["SoC"], status_in=["applied"]) == jobs[:2]
    repository.set_application_status(jobs[0], "interview")
    assert ids(engine, title_words=["SOC"], status_in=["applied"]) == [jobs[1]]


def test_status_dates_and_discovered_dates(records):
    _, engine, jobs = records
    start = TODAY - timedelta(days=7)
    assert ids(engine, status_in=["applied"], status_from=start, status_to=TODAY) == [jobs[0]]
    day = TODAY - timedelta(days=3)
    assert ids(engine, status_from=day, status_to=day) == [jobs[0]]
    assert ids(engine, found_from=day, found_to=day) == [jobs[0]]
    assert ids(engine, found_from=TODAY) == [jobs[2]]
    assert ids(engine, found_to=TODAY - timedelta(days=20)) == [jobs[1]]
    assert ids(engine, status_to=TODAY - timedelta(days=20)) == [jobs[1]]


def test_workday_count_and_full_status_breakdown(records):
    _, engine, jobs = records
    query = JobQuery(ats_in=["workday"], status_in=["drafted"], count_only=True, limit=1)
    total, rows = run_query(engine, query)
    assert total == 2 and rows[0].job_id == jobs[4]
    assert format_results(query, total, rows) == "2 job(s) match.\ndrafted: 2"
    query = JobQuery(count_only=True, limit=1)
    total, rows = run_query(engine, query)
    assert (
        format_results(query, total, rows)
        == "6 job(s) match.\napplied: 2\ndrafted: 2\nno status: 1\nrejected: 1"
    )


def test_all_filters_and_sort_orders(records):
    _, engine, jobs = records
    assert ids(engine, min_score=86, max_score=89) == [jobs[0]]
    assert ids(engine, company="Moody's") == [jobs[0]]
    assert ids(engine, location_words=["boston", "nyc"]) == [jobs[4], jobs[3], jobs[0], jobs[5]]
    assert ids(engine, remote=True) == [jobs[3], jobs[1]]
    assert ids(engine, remote=False) == [jobs[2], jobs[4], jobs[0], jobs[5]]
    assert ids(engine, has_reply=True) == [jobs[0]]
    assert jobs[0] not in ids(engine, has_reply=False)
    assert ids(engine, reply_kind_in=["interview"]) == [jobs[0]]
    assert ids(engine, reply_kind_in=["offer"]) == []
    assert ids(engine, packet_ready=True) == [jobs[0]]
    assert jobs[0] not in ids(engine, packet_ready=False)
    assert jobs[1] in ids(engine, packet_ready=False)
    assert ids(engine, source_in=["manual"]) == [jobs[2], jobs[5]]
    assert ids(engine, source_in=["linkedin"]) == [jobs[0]]
    assert ids(engine, sort="score") == [jobs[1], jobs[0], *reversed(jobs[2:])]
    assert ids(engine, sort="newest") == [jobs[2], jobs[4], jobs[3], jobs[0], jobs[5], jobs[1]]
    assert ids(engine, sort="status_date") == [jobs[4], jobs[3], jobs[0], jobs[5], jobs[1], jobs[2]]


def test_failed_assessment_does_not_hide_latest_success(records):
    repository, engine, jobs = records
    with repository._engine.begin() as connection:
        connection.execute(
            update(JobAssessmentRecord)
            .where(JobAssessmentRecord.overall_score == 88)
            .values(error_category="failed")
        )
    assert ids(engine, min_score=86, max_score=89) == []
    assert ids(engine, min_score=85, max_score=85) == [jobs[0]]


def test_limit_formatting_literal_data_and_bound_parameters(records):
    _, engine, jobs = records
    query = JobQuery(limit=2)
    total, rows = run_query(engine, query)
    assert total == 6 and len(rows) == 2
    assert "6 match(es), showing 2:" in format_results(query, total, rows)
    assert ids(engine, title_words=["100"]) == [jobs[5]]
    assert ids(engine, title_words=["100X"]) == []
    assert escape("a%_\\b") == r"a\%\_\\b"
    sql = []
    event.listen(
        engine,
        "before_cursor_execute",
        lambda conn, cursor, statement, parameters, context, many: sql.append(
            (statement, parameters)
        ),
    )
    assert ids(engine, company="Moody's") == [jobs[0]]
    assert all("Moody's" not in statement for statement, _ in sql)
    assert any("%Moody's%" in parameters for _, parameters in sql)
    empty = JobQuery(title_words=["missing"])
    assert (
        format_results(empty, *run_query(engine, empty))
        == "No jobs match.\nFilters: title_words=['missing']"
    )
    text = format_results(JobQuery(), 1, [QueryRow(1, "A" * 61, "B" * 61, None, None, 0)])
    assert text == "1 match(es):\n#1 " + "A" * 60 + " / " + "B" * 60 + " — no status · score 0"


@pytest.mark.parametrize(
    "operation", [insert(JobRecord).values(company="bad"), update(JobRecord).values(title="bad")]
)
def test_readonly_engine_rejects_writes(records, operation):
    _, engine, _ = records
    with engine.connect() as connection, pytest.raises(OperationalError, match="readonly"):
        connection.execute(operation)


def test_missing_readonly_database_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    engine = readonly_engine(path)
    with pytest.raises(OperationalError), engine.connect():
        pass
    assert not path.exists()
    engine.dispose()


def test_historical_status_window_is_distinct_from_current_status(records, monkeypatch):
    repository, engine, jobs = records
    day = TODAY - timedelta(days=3)
    monkeypatch.setattr("jobagent.db.repository.utc_now", lambda: NOW)
    repository.set_application_status(jobs[0], "interview")
    assert jobs[0] not in ids(engine, status_in=["applied"], status_from=day, status_to=day)
    assert jobs[0] not in ids(engine, status_in=["interview"], status_from=day, status_to=day)
    assert ids(engine, status_from=day, status_to=day) == [jobs[0]]


def test_local_dates_use_exclusive_midnight_and_dst(records, monkeypatch):
    import time as system_time

    repository, engine, jobs = records
    monkeypatch.setenv("TZ", "America/New_York")
    system_time.tzset()
    try:
        # This local day is 25 hours: Nov 1, 2026 is the fall DST transition.
        start = datetime(2026, 11, 1, 4, tzinfo=UTC)
        end = datetime(2026, 11, 2, 5, tzinfo=UTC)
        for job_id, when in zip(
            jobs[:4],
            (start - timedelta(microseconds=1), start, end - timedelta(microseconds=1), end),
            strict=True,
        ):
            with repository._engine.begin() as connection:
                connection.execute(
                    update(JobRecord).where(JobRecord.id == job_id).values(first_seen_at=when)
                )
        assert ids(engine, found_from=date(2026, 11, 1), found_to=date(2026, 11, 1)) == [
            jobs[2],
            jobs[1],
        ]
    finally:
        monkeypatch.undo()
        system_time.tzset()


@pytest.mark.parametrize(
    ("ats", "host"),
    [
        ("workday", "myworkdayjobs.com"),
        ("workday", "myworkdaysite.com"),
        ("greenhouse", "greenhouse.io"),
        ("lever", "lever.co"),
        ("ashby", "ashbyhq.com"),
        ("icims", "icims.com"),
        ("smartrecruiters", "smartrecruiters.com"),
        ("oracle", "oraclecloud.com"),
    ],
)
def test_fixed_ats_host_patterns(records, ats, host):
    repository, engine, jobs = records
    with repository._engine.begin() as connection:
        connection.execute(
            update(JobRecord)
            .where(JobRecord.id == jobs[2])
            .values(apply_url=f"https://jobs.{host}/role")
        )
    assert jobs[2] in ids(engine, ats_in=[ats])


def test_readonly_uri_escapes_filename(tmp_path):
    path = tmp_path / "db?mode=rw&special#name.sqlite3"
    repository = JobRepository(engine=create_engine(URL.create("sqlite", database=str(path))))
    repository.initialize()
    engine = readonly_engine(path)
    try:
        assert run_query(engine, JobQuery())[0] == 0
        with engine.connect() as connection, pytest.raises(OperationalError, match="readonly"):
            connection.execute(update(JobRecord).values(title="bad"))
    finally:
        engine.dispose()
        repository._engine.dispose()


def test_latest_score_uses_assessment_time_with_id_tiebreak(records):
    repository, engine, jobs = records
    with repository._engine.begin() as connection:
        connection.execute(
            update(JobAssessmentRecord)
            .where(JobAssessmentRecord.overall_score == 85)
            .values(created_at=NOW)
        )
    assert ids(engine, min_score=85, max_score=85) == [jobs[0]]
    assert ids(engine, min_score=88, max_score=88) == []
