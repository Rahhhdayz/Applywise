"""Offline daily-pipeline tests with injected fakes; nothing leaves the process."""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.db import JobRepository
from jobagent.pipeline import run_cycle
from jobagent.ranking import build_queue
from jobagent.scoring import AssessmentService
from jobagent.scrape.service import ScrapeSummary
from tests.test_milestone5 import FakeScorer, candidate, posting

TODAY = "2026-09-22"


class Recorder:
    def __init__(self) -> None:
        self.calls: list[str] = []


class RecordingScorer(FakeScorer):
    def __init__(self, recorder: Recorder) -> None:
        super().__init__()
        self.recorder = recorder

    def score(self, truth, job):  # type: ignore[no-untyped-def]
        self.recorder.calls.append("score")
        return super().score(truth, job)


class FakeTracker:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder

    def setup(self) -> list[str]:
        return []

    def statuses(self) -> dict[int, str]:
        return {}

    def renumber(self, rank: dict[int, int]) -> int:
        return 0

    def upsert(self, packet, job, assessment, queue_date, outreach=None) -> str:  # type: ignore[no-untyped-def]
        self.recorder.calls.append("notion")
        assert assessment is not None and queue_date == TODAY
        return "page-1"


@pytest.fixture
def repository() -> JobRepository:
    repository = JobRepository(":memory:")
    repository.initialize()
    return repository


def _cycle(repository: JobRepository, recorder: Recorder, scrape, *, dry_run: bool = False):  # type: ignore[no-untyped-def]
    return run_cycle(
        repository,
        candidate(),
        scrape=scrape,
        scorer=RecordingScorer(recorder),
        tracker=FakeTracker(recorder),
        today=TODAY,
        dry_run=dry_run,
    )


def test_full_cycle_runs_every_step_in_order(repository: JobRepository) -> None:
    recorder = Recorder()

    def scrape() -> ScrapeSummary:
        recorder.calls.append("scrape")
        job = repository.add_or_get_job(posting("scraped-1")).job
        repository.save_packet(job_id=job.id, fingerprint="earlier", status="ready", packet_directory="out/x")
        return ScrapeSummary(received=1, new=1)

    metrics = _cycle(repository, recorder, scrape)

    assert metrics["errors"] == {}
    assert metrics["scrape"]["new"] == 1  # type: ignore[index]
    assert metrics["eligibility"] == {"eligible": 1}
    assert metrics["scoring"]["scored"] == 1  # type: ignore[index]
    assert metrics["queue"] == {"queued": 1}
    assert metrics["notion"]["synced"] == 1  # type: ignore[index]
    assert recorder.calls == ["scrape", "score", "notion"]
    assert repository.get_latest_ready_packet(1).notion_page_id == "page-1"  # type: ignore[union-attr]


def test_scrape_failure_still_processes_existing_jobs(repository: JobRepository) -> None:
    repository.add_or_get_job(posting("existing-1"))
    recorder = Recorder()

    def scrape() -> ScrapeSummary:
        raise RuntimeError("linkedin blocked us")

    metrics = _cycle(repository, recorder, scrape)

    assert "linkedin blocked us" in metrics["errors"]["scrape"]  # type: ignore[index]
    assert metrics["scoring"]["scored"] == 1 and metrics["queue"] == {"queued": 1}  # type: ignore[index]
    assert "prepare" in metrics and "notion" in metrics


def test_dry_run_skips_ai_writing_and_notion(repository: JobRepository) -> None:
    recorder = Recorder()
    repository.add_or_get_job(posting("dry-1"))

    metrics = _cycle(repository, recorder, lambda: ScrapeSummary(), dry_run=True)

    assert recorder.calls == []
    assert set(metrics) == {"queue_date", "scrape", "eligibility", "errors"}


def test_queue_excludes_jobs_queued_earlier_or_already_actioned(repository: JobRepository) -> None:
    truth = candidate()
    service = AssessmentService(repository, FakeScorer(), truth)
    jobs = [repository.add_or_get_job(posting(f"job-{index}", title=f"Security Engineer {index}")).job for index in range(3)]
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in jobs:
        service.evaluate(job)
    repository.add_queue_entries("2026-09-21", [{"job_id": jobs[0].id, "rank": 1, "ranking_score": 1.0, "exceptional": False, "reason": "t"}])
    repository.set_application_status(jobs[1].id, "prefilled")

    built = build_queue(repository, truth, TODAY)

    assert [entry.job_id for entry in built.entries] == [jobs[2].id]


def test_later_run_tops_up_todays_queue_to_the_maximum(repository: JobRepository) -> None:
    truth = candidate()
    truth = truth.model_copy(update={"preferences": truth.preferences.model_copy(update={
        "daily_queue": truth.preferences.daily_queue.model_copy(update={"minimum_target": 1, "target": 2, "maximum_target": 3})})})
    service = AssessmentService(repository, FakeScorer(), truth)
    first = [repository.add_or_get_job(posting(f"a-{index}", title=f"Security Engineer A{index}")).job for index in range(2)]
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in first:
        service.evaluate(job)
    assert len(build_queue(repository, truth, TODAY).entries) == 2

    later = [repository.add_or_get_job(posting(f"b-{index}", title=f"Security Engineer B{index}")).job for index in range(2)]
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in later:
        service.evaluate(job)
    entries = build_queue(repository, truth, TODAY).entries
    assert [entry.rank for entry in entries] == [1, 2, 3]


def test_scoring_stops_at_codex_usage_limit(repository: JobRepository) -> None:
    from jobagent.scoring import score_batch

    class LimitedScorer(FakeScorer):
        def score(self, truth, job):  # type: ignore[no-untyped-def]
            self.calls += 1
            raise RuntimeError("ERROR: You've hit your usage limit. Try again at 6:02 PM.")

    scorer = LimitedScorer()
    jobs = [repository.add_or_get_job(posting(f"u-{index}")).job for index in range(5)]
    result = score_batch(AssessmentService(repository, scorer, candidate()), jobs, limit=100)
    assert scorer.calls == 1 and result["failed"] == 1


def test_notion_pull_copies_decisions_without_undoing_later_local_states(repository: JobRepository) -> None:
    from jobagent.pipeline import pull_notion_statuses

    ids = [repository.add_or_get_job(posting(f"pull-{index}")).job.id for index in range(4)]
    repository.set_application_status(ids[1], "prefilled")
    repository.set_application_status(ids[2], "interview")
    repository.set_application_status(ids[3], "applied")

    class NotionRows:
        def statuses(self) -> dict[int, str]:
            return {ids[0]: "Applied", ids[1]: "Skipped", ids[2]: "Applied", ids[3]: "Rejected", 999: "Applied"}

    assert pull_notion_statuses(repository, NotionRows()) == {"updated": 3}  # type: ignore[arg-type]
    latest = repository.latest_statuses()
    assert [latest[job_id] for job_id in ids] == ["applied", "skipped", "interview", "rejected"]


def test_closed_posting_is_expired_and_next_best_job_takes_its_slot(repository: JobRepository) -> None:
    truth = candidate()
    truth = truth.model_copy(update={"preferences": truth.preferences.model_copy(update={
        "daily_queue": truth.preferences.daily_queue.model_copy(update={"minimum_target": 1, "target": 1, "maximum_target": 2})})})
    service = AssessmentService(repository, FakeScorer(), truth)
    jobs = [repository.add_or_get_job(posting(f"live-{index}", title=f"Security Engineer {index}")).job for index in range(3)]
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in jobs:
        service.evaluate(job)
    closed = jobs[1].id

    entries = build_queue(repository, truth, TODAY, lambda job: job.id == closed).entries

    assert len(entries) == 2 and closed not in [entry.job_id for entry in entries]
    assert repository.latest_statuses()[closed] == "expired"


def test_linkedin_closed_reads_the_apply_button() -> None:
    import httpx

    from jobagent.liveness import linkedin_closed

    def page(html: str):  # type: ignore[no-untyped-def]
        return lambda *a, **k: httpx.Response(200, text=html)

    url = "https://www.linkedin.com/jobs/view/1"
    assert linkedin_closed(url, page('<h1 class="top-card-layout__title">AI Security Engineer</h1>'))
    assert not linkedin_closed(url, page('<h1 class="top-card-layout__title">x</h1><a class="apply-button">'))
    assert not linkedin_closed(url, page("<form>Sign in</form>"))  # login wall: unknown, keep the job
    assert not linkedin_closed("https://jobs.lever.co/x/1", page(""))  # other hosts are not checked


def test_linkedin_closed_keeps_the_job_on_network_errors() -> None:
    import httpx

    from jobagent.liveness import linkedin_closed

    def fail(*a, **k):  # type: ignore[no-untyped-def]
        raise httpx.ConnectError("down")

    assert not linkedin_closed("https://www.linkedin.com/jobs/view/1", fail)


def test_one_opening_listed_in_several_cities_is_queued_once(repository: JobRepository) -> None:
    from jobagent.models import JobPosting

    truth = candidate()
    service = AssessmentService(repository, FakeScorer(), truth)
    cities = [posting(f"city-{city}").model_copy(update={"location": city}) for city in ("Boston, MA", "Austin, TX")]
    other = posting("other", title="Detection Engineer")
    jobs = [repository.add_or_get_job(JobPosting.model_validate(p.model_dump())).job for p in [*cities, other]]
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in jobs:
        service.evaluate(job)

    queued = {entry.job_id for entry in build_queue(repository, truth, TODAY).entries}

    assert len(queued & {jobs[0].id, jobs[1].id}) == 1 and jobs[2].id in queued
    assert build_queue(repository, truth, "2026-09-23").entries == []  # the other city never comes back later


def test_posted_within_hours_queues_fresh_jobs_and_undated_jobs_first_seen_recently(
    repository: JobRepository,
) -> None:
    from datetime import UTC, datetime, timedelta

    from jobagent.models import JobPosting

    truth = candidate()
    truth = truth.model_copy(update={"preferences": truth.preferences.model_copy(update={
        "daily_queue": truth.preferences.daily_queue.model_copy(update={"posted_within_hours": 24})})})
    service = AssessmentService(repository, FakeScorer(), truth)
    now = datetime.now(UTC)
    ages = {"fresh": now - timedelta(hours=3), "old": now - timedelta(hours=30), "undated": None}
    jobs = {name: repository.add_or_get_job(JobPosting.model_validate(
        posting(name, title=f"Security Engineer {name}").model_copy(update={"posted_at": when}).model_dump())).job
        for name, when in ages.items()}
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in jobs.values():
        service.evaluate(job)

    # The undated job was first seen just now, so it counts as fresh; only the 30-hour-old one is dropped.
    queued = {entry.job_id for entry in build_queue(repository, truth, TODAY).entries}
    assert queued == {jobs["fresh"].id, jobs["undated"].id}


def test_resolve_step_points_queued_careers_page_jobs_at_their_ats_form(repository: JobRepository) -> None:
    recorder = Recorder()
    form = "https://job-boards.greenhouse.io/embed/job_app?for=example&token=1"

    def scrape() -> ScrapeSummary:
        repository.add_or_get_job(posting("scraped-1"))
        return ScrapeSummary(received=1, new=1)

    metrics = run_cycle(repository, candidate(), scrape=scrape, scorer=RecordingScorer(recorder),
                        tracker=FakeTracker(recorder), resolve=lambda job: form, today=TODAY)

    assert metrics["resolve"] == {"resolved": 1}
    assert repository.get_job(1).apply_url == form  # type: ignore[union-attr]


def test_us_and_international_fill_their_own_slots(repository: JobRepository) -> None:
    truth = candidate()
    truth = truth.model_copy(update={"preferences": truth.preferences.model_copy(update={
        "daily_queue": truth.preferences.daily_queue.model_copy(update={
            "minimum_target": 1, "target": 2, "maximum_target": 4, "international_maximum": 2})})})
    service = AssessmentService(repository, FakeScorer(), truth)
    us = [posting(f"us-{index}", title=f"Security Engineer U{index}") for index in range(4)]
    abroad = [posting("fr-0", title="Security Engineer F0").model_copy(update={"location": "Paris, France"})]
    jobs = [repository.add_or_get_job(item).job for item in [*us, *abroad]]
    run_cycle(repository, truth, scrape=None, scorer=None, tracker=None, today=TODAY, dry_run=True)
    for job in jobs:
        service.evaluate(job)
    queued = {entry.job_id for entry in build_queue(repository, truth, TODAY).entries}
    assert jobs[-1].id in queued and len(queued) == 3  # 2 US + the only international job; no US backfill
    assert len(build_queue(repository, truth, TODAY).entries) == 3  # US full; the empty slot stays international


def test_scoring_alternates_us_and_international_jobs() -> None:
    from types import SimpleNamespace as Job

    from jobagent.pipeline import alternate_regions

    us = [Job(location=f"City {i}, MA", source_metadata={}) for i in range(3)]
    paris = Job(location="Paris, France", source_metadata={})
    assert alternate_regions([*us, paris]) == [us[0], paris, us[1], us[2]]


def test_cycle_logs_each_stage_and_notifies_start_failure_and_end_once(
    repository: JobRepository, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    ticks = iter(range(0, 10_000, 5))
    monkeypatch.setattr("jobagent.pipeline.monotonic", lambda: float(next(ticks)))
    repository.add_or_get_job(posting("logged-1"))
    sent: list[str] = []

    def scrape() -> ScrapeSummary:
        raise RuntimeError("linkedin blocked us")

    with caplog.at_level("INFO", logger="jobagent.pipeline"):
        run_cycle(repository, candidate(), scrape=scrape, scorer=RecordingScorer(Recorder()),
                  tracker=None, today=TODAY, notify=sent.append)

    lines = [record.getMessage() for record in caplog.records]
    assert "stage eligibility started" in lines
    assert any(line.startswith("stage eligibility finished in ") and "{'eligible': 1}" in line for line in lines)
    assert any(line.startswith("stage scrape failed after ") for line in lines)
    assert "stage notion skipped: not configured" in lines
    assert "scoring 1/1" in lines
    assert sent[0] == f"JobAgent cycle {TODAY} started."
    assert sent[1].startswith(f"JobAgent cycle {TODAY}: stage scrape failed: linkedin blocked us")
    assert len(sent) == 3 and sent[2].startswith(f"JobAgent cycle {TODAY} finished: ")
    assert "1 scored" in sent[2] and "failed stages: scrape" in sent[2]


def test_broken_notifier_never_stops_the_cycle(repository: JobRepository) -> None:
    def notify(text: str) -> None:
        raise RuntimeError("telegram down")

    metrics = run_cycle(repository, candidate(), scrape=None, scorer=None, tracker=None,
                        today=TODAY, dry_run=True, notify=notify)

    assert metrics["errors"] == {}


def test_cycle_log_appends_utc_lines_to_the_dated_file(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    import logging

    from jobagent.cli import _cycle_log, _queue_date

    monkeypatch.chdir(tmp_path)
    for message in ("first run", "second run"):
        with _cycle_log() as path:
            logging.getLogger("jobagent.pipeline").info(message)

    assert path == Path("logs") / f"cycle_{_queue_date(None)}.log"
    path = tmp_path / path
    lines = path.read_text().splitlines()
    assert len(lines) == 2 and lines[1].endswith("INFO jobagent.pipeline: second run")
    assert lines[0][19] == "Z"  # 2026-11-17T12:00:00Z
