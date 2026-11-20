"""The daily pipeline: scrape -> eligibility -> score -> queue -> prepare -> Notion.

Every dependency is injected. A failed step is recorded and later steps still run
on whatever is already in SQLite.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from time import monotonic

from jobagent.candidate.models import CandidateTruth
from jobagent.db import JobRepository, StoredJob
from jobagent.eligibility import refresh_eligibility, rules_fingerprint
from jobagent.intake.urls import detect_source
from jobagent.integrations import Tracker
from jobagent.lifecycle import accepts_pulled, status_from_label
from jobagent.liveness import job_closed
from jobagent.outreach import OutreachService
from jobagent.ranking import build_queue, is_early_career, is_recent
from jobagent.regions import is_international
from jobagent.scoring import AssessmentService, JobScorer, score_batch
from jobagent.scrape.service import ScrapeSummary
from jobagent.writing.service import PreparationService

logger = logging.getLogger(__name__)


def _progress[T](items: Sequence[T], stage: str, *, every: int = 10, seconds: float = 60.0) -> Iterator[T]:
    """Yield ``items``, logging "<stage> i/total" on the first item, every ``every`` items, or after ``seconds``."""
    last = monotonic()
    for index, item in enumerate(items, 1):
        if index == 1 or index % every == 0 or monotonic() - last >= seconds:
            last = monotonic()
            logger.info("%s %d/%d", stage, index, len(items))
        yield item


def run_cycle(
    repository: JobRepository,
    candidate: CandidateTruth,
    *,
    scrape: Callable[[], ScrapeSummary] | None,
    scorer: JobScorer | None,
    tracker: Tracker | None,
    preparer: PreparationService | None = None,
    outreach: OutreachService | None = None,
    resolve: Callable[[StoredJob], str | None] | None = None,
    today: str,
    max_packets: int = 20,
    dry_run: bool = False,
    notify: Callable[[str], None] | None = None,
    inbox: Callable[[], dict[str, object]] | None = None,
) -> dict[str, object]:
    """Run each step in order and return JSON-safe metrics, including per-step errors.

    ``notify`` (e.g. Telegram) gets one message at start, one per failed stage, and one at the end.
    """
    metrics: dict[str, object] = {"queue_date": today}
    errors: dict[str, str] = {}
    started = monotonic()

    def tell(text: str) -> None:
        if notify is None:
            return
        try:
            notify(text)
        except Exception:  # a notification problem must never stop the cycle
            logger.warning("cycle notification failed", exc_info=True)

    def step(name: str, available: object, action: Callable[[], object]) -> None:
        if available is None:
            metrics[name] = "not configured"
            logger.info("stage %s skipped: not configured", name)
            return
        logger.info("stage %s started", name)
        began = monotonic()
        try:
            metrics[name] = action()
        except Exception as error:  # one failed step must not block the rest
            logger.exception("stage %s failed after %.1fs", name, monotonic() - began)
            errors[name] = str(error)[:500]
            tell(f"JobAgent cycle {today}: stage {name} failed: {errors[name][:300]}")
            return
        logger.info("stage %s finished in %.1fs: %s", name, monotonic() - began, metrics[name])

    def finish() -> dict[str, object]:
        metrics["errors"] = errors
        summary = _headline(metrics, monotonic() - started)
        logger.info("cycle finished: %s", summary)
        tell(f"JobAgent cycle {today} finished: {summary}")
        return metrics

    logger.info("cycle started for %s%s", today, " (dry run)" if dry_run else "")
    tell(f"JobAgent cycle {today} started{' (dry run)' if dry_run else ''}.")

    if not dry_run:  # Master's Notion Applied/Skipped marks must land before anything reads statuses
        step("notion_pull", tracker, lambda: pull_notion_statuses(repository, tracker))
        step("inbox", inbox, lambda: inbox())  # employer replies (Gmail) move statuses before anything reads them
    step("scrape", scrape, lambda: asdict(scrape()))
    step("eligibility", True, lambda: dict(Counter(
        result.status for result in refresh_eligibility(repository, candidate, repository.list_jobs())
    )))
    if dry_run:
        return finish()
    step("scoring", scorer, lambda: _score(repository, candidate, scorer))
    step("queue", True, lambda: {"queued": len(build_queue(repository, candidate, today, job_closed).entries)})
    step("resolve", resolve, lambda: _resolve_queue(repository, resolve, today))
    step("prepare", preparer, lambda: _prepare(repository, preparer, today, max_packets))
    step("outreach", outreach, lambda: run_outreach(repository, outreach, [e.job_id for e in repository.list_queue(today)[:max_packets]]))
    step("notion", tracker, lambda: sync_to_notion(
        repository, tracker, [entry.job_id for entry in repository.list_queue(today)]
    ))
    return finish()


def _headline(metrics: dict[str, object], elapsed: float) -> str:
    """One line: elapsed time plus the counts Master cares about, from whichever stages ran."""
    picks = [("scrape", "new", "new jobs"), ("scoring", "scored", "scored"), ("queue", "queued", "queued"),
             ("prepare", "ready", "packets ready"), ("notion", "synced", "synced to Notion")]
    parts = [f"{int(elapsed // 60)}m{int(elapsed % 60):02d}s"]
    parts += [f"{value[key]} {label}" for stage, key, label in picks
              if isinstance(value := metrics.get(stage), dict) and key in value]
    failed = metrics.get("errors") or {}
    parts.append(f"failed stages: {', '.join(failed)}" if failed else "no stage errors")
    return ", ".join(parts)


def _resolve_queue(
    repository: JobRepository, resolve: Callable[[StoredJob], str | None], today: str
) -> dict[str, int]:
    """Point today's LinkedIn/careers-page jobs at their real ATS form, so Notion links and fills go direct."""
    counts: Counter[str] = Counter()
    for entry in _progress(repository.list_queue(today), "resolve"):
        job = repository.get_job(entry.job_id)
        if job is None or detect_source(job.apply_url or job.canonical_url or "") != "manual_url":
            continue
        url = resolve(job)
        if url:
            repository.set_job_apply_url(job.id, url)
        counts["resolved" if url else "unresolved"] += 1
    return dict(counts)


def _score(repository: JobRepository, candidate: CandidateTruth, scorer: JobScorer) -> dict[str, object]:
    """Score new eligible/review jobs within the call budget, in priority order:
    posted in the last 24h, then early-career, then newest discovered."""
    fingerprint = rules_fingerprint(candidate)
    tracked = repository.tracked_job_ids()
    jobs = [
        job
        for job in reversed(repository.list_jobs())
        if job.id not in tracked
        and (result := repository.get_eligibility(job.id, fingerprint))
        and result.status in {"eligible", "review"}
    ]
    now, hours = datetime.now(UTC), candidate.preferences.scoring.recent_posting_hours
    jobs.sort(key=lambda job: (
        not is_recent(job.posted_at, now=now, hours=hours),
        not is_early_career(job.title, candidate.preferences.early_career_terms),
    ))
    if candidate.preferences.daily_queue.international_maximum:
        jobs = alternate_regions(jobs)
    service = AssessmentService(repository, scorer, candidate)
    return score_batch(service, _progress(jobs, "scoring"), limit=candidate.preferences.scoring.max_jobs_per_run)


def alternate_regions(jobs: list[StoredJob]) -> list[StoredJob]:
    """US, international, US, ... (priority order kept within each), so the scoring budget feeds
    both halves of the queue instead of the far larger US pool eating it all.
    ponytail: 50/50 matches the 10/10 split; weight by the quota if the split ever changes."""
    us = [job for job in jobs if not is_international(job)]
    abroad = [job for job in jobs if is_international(job)]
    mixed = [job for pair in zip(us, abroad, strict=False) for job in pair]
    shorter = min(len(us), len(abroad))
    return mixed + us[shorter:] + abroad[shorter:]


def _prepare(
    repository: JobRepository, service: PreparationService, today: str, limit: int
) -> dict[str, object]:
    ready, errors = 0, {}
    entries = repository.list_queue(today)[:limit]
    for index, entry in enumerate(entries, 1):
        job = repository.get_job(entry.job_id)
        if job is None:
            continue
        logger.info("prepare packet %d/%d: %s – %s", index, len(entries), job.company, job.title)
        packet, error = service.prepare(job, queue_date=today)
        if error:
            errors[job.id] = error
        elif packet is not None:
            ready += 1
    return {"ready": ready, "failed": len(errors), "errors": errors}


def run_outreach(repository: JobRepository, service: OutreachService, job_ids: list[int], *, force: bool = False) -> dict[str, object]:
    """Contact + draft for each queued job with a ready packet; one failure spares the rest."""
    counts: Counter[str] = Counter()
    errors: dict[int, str] = {}
    for job_id in _progress(job_ids, "outreach"):
        job, packet = repository.get_job(job_id), repository.get_latest_ready_packet(job_id)
        if job is None or packet is None:
            continue
        try:
            outreach, source = service.run(job, packet, force=force)
        except (RuntimeError, ValueError, OSError) as error:
            errors[job.id] = str(error)[:300]
            continue
        counts[source if outreach else "none"] += 1
    return {**counts, "failed": len(errors), "errors": errors}


def pull_notion_statuses(repository: JobRepository, tracker: Tracker) -> dict[str, int]:
    """Copy Master's Notion decisions and outcomes into SQLite, which stays authoritative.

    Only forward moves (applied -> interview -> offer) and closing outcomes are applied
    (``lifecycle.accepts_pulled``); a later state recorded locally is never undone.
    """
    latest, updated = repository.latest_statuses(), 0
    for job_id, notion_status in tracker.statuses().items():
        status, current = status_from_label(notion_status), latest.get(job_id)
        if repository.get_job(job_id) is None:
            continue
        if accepts_pulled(current, status):
            repository.set_application_status(job_id, status, source="notion")
            updated += 1
    return {"updated": updated}


def sync_to_notion(repository: JobRepository, tracker: Tracker, job_ids: list[int]) -> dict[str, object]:
    """Push each job's latest ready packet to Notion; one failed row spares the rest.

    A row Master deleted in Notion means he is done with that job: it is marked skipped, not re-created.
    """
    synced, removed, errors = 0, 0, {}
    statuses = repository.latest_statuses()
    for job_id in job_ids:
        packet, job = repository.get_latest_ready_packet(job_id), repository.get_job(job_id)
        if packet is None or job is None or statuses.get(job_id) in {"skipped", "expired"}:
            continue
        try:
            page_id = tracker.upsert(
                packet, job, repository.get_latest_assessment(job_id), repository.latest_queue_date(job_id),
                repository.get_outreach(job_id),
            )
        except (RuntimeError, OSError) as error:
            if "archived" in str(error):  # Notion: "Can't edit block that is archived" (row in trash)
                repository.set_application_status(job_id, "skipped", source="notion")
                removed += 1
                continue
            errors[job_id] = str(error)[:300]
            continue
        if packet.notion_page_id != page_id:
            repository.set_packet_notion_page(packet.id, page_id)
        synced += 1
    try:  # ponytail: rank by SQLite discovery order (first_seen_at, id), so deletions never leave gaps
        tracker.renumber({job.id: position for position, job in enumerate(repository.list_jobs())})
    except (RuntimeError, OSError) as error:
        errors["renumber"] = str(error)[:300]
    return {"synced": synced, "removed_in_notion": removed, "failed": len(errors), "errors": errors}
