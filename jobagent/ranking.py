"""Pure deterministic ranking and queue construction rules."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from jobagent.candidate.models import CandidateTruth
from jobagent.db import JobRepository, QueueEntry, StoredAssessment, StoredJob
from jobagent.eligibility import matches_any, rules_fingerprint
from jobagent.normalization import opening_key
from jobagent.regions import is_international


@dataclass(frozen=True)
class RankedJob:
    job: StoredJob
    assessment: StoredAssessment
    ranking_score: float
    freshness_score: float
    role_score: float
    location_score: float
    exceptional: bool
    early_career_score: float = 0.0
    recent_score: float = 0.0


def freshness_score(posted_at: datetime | None, *, now: datetime) -> float:
    """Use explainable bands rather than pretending post age is precisely predictive."""
    if posted_at is None:
        return 0.4
    days = max(0, (now - posted_at.astimezone(UTC)).days)
    if days <= 1:
        return 1.0
    if days <= 3:
        return 0.8
    if days <= 7:
        return 0.6
    if days <= 14:
        return 0.35
    return 0.1


def is_recent(posted_at: datetime | None, *, now: datetime, hours: int) -> bool:
    """Posted within the last ``hours``. Boards give dates only (midnight UTC), so this is date-granular."""
    return posted_at is not None and (now - posted_at.astimezone(UTC)).total_seconds() <= hours * 3600


def seen_at(job: StoredJob) -> datetime:
    """Posting date, or first sighting for undated jobs (LinkedIn often omits the date).

    ponytail: an old posting seen for the first time counts as fresh; parse dates from the
    description if that bites.
    """
    return job.posted_at or job.first_seen_at.replace(tzinfo=job.first_seen_at.tzinfo or UTC)


def is_early_career(title: str, terms: list[str]) -> bool:
    """Whole-word title match, so "Security Analyst I" counts but "Identity" does not."""
    lowered = title.lower()
    return any(re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", lowered) for term in terms)


def rank_jobs(
    pairs: list[tuple[StoredJob, StoredAssessment]], candidate: CandidateTruth, *, now: datetime | None = None
) -> list[RankedJob]:
    """Rank assessment-qualified jobs with stable ties based on their canonical ID."""
    now = now or datetime.now(UTC)
    settings = candidate.preferences.scoring
    primary = [role.lower() for role in candidate.preferences.primary_roles]
    ranked: list[RankedJob] = []
    for job, assessment in pairs:
        fresh = freshness_score(seen_at(job), now=now)
        role = 1.0 if any(value in job.title.lower() for value in primary) else 0.0
        location = 1.0 if matches_any(job.location, candidate.preferences.preferred_locations) else 0.0
        early = 1.0 if is_early_career(job.title, candidate.preferences.early_career_terms) else 0.0
        recent = 1.0 if is_recent(seen_at(job), now=now, hours=settings.recent_posting_hours) else 0.0
        score = 100 * (
            (assessment.overall_score / 100) * settings.semantic_score_weight
            + fresh * settings.freshness_weight
            + role * settings.role_priority_weight
            + location * settings.location_weight
            + early * settings.early_career_weight
            + recent * settings.recent_posting_weight
        )
        exceptional = assessment.overall_score >= settings.exceptional_score and fresh >= 0.8
        ranked.append(RankedJob(job, assessment, round(score, 2), fresh, role, location, exceptional, early, recent))
    return sorted(ranked, key=lambda item: (-item.ranking_score, -item.assessment.overall_score, item.job.id))


@dataclass(frozen=True)
class QueueBuild:
    considered: int
    above_threshold: int
    entries: list[QueueEntry]


def build_queue(
    repository: JobRepository, candidate: CandidateTruth, queue_date: str,
    is_closed: Callable[[StoredJob], bool] | None = None,
) -> QueueBuild:
    """Queue today's best new jobs up to the daily maximum; queued-before or actioned jobs are skipped.

    ``is_closed`` is asked only about jobs about to be queued; a closed one is marked "expired"
    (so it is never checked again) and the next-best job takes its place.
    """
    fingerprint = rules_fingerprint(candidate)
    tracked = repository.tracked_job_ids()
    minimum = candidate.preferences.scoring.minimum_ai_score
    within = candidate.preferences.daily_queue.posted_within_hours
    now = datetime.now(UTC)
    jobs = repository.list_jobs()
    # One opening = one company + title: per-city LinkedIn listings and LinkedIn-vs-ATS links of the
    # same job never reach the queue twice. ponytail: a company with two truly different openings
    # under the identical title gets only the first; compare descriptions if that ever matters.
    taken = {_opening(job) for job in jobs if job.id in tracked}
    pairs = []
    for job in jobs:
        if job.id in tracked or job.has_active_observation is False or _opening(job) in taken:
            continue
        if within and not is_recent(seen_at(job), now=now, hours=within):
            continue
        eligibility = repository.get_eligibility(job.id, fingerprint)
        assessment = repository.get_latest_assessment(job.id)
        if eligibility and eligibility.status != "ineligible" and assessment and assessment.overall_score >= minimum:
            pairs.append((job, assessment))
    ranked = rank_jobs(pairs, candidate)
    # A later run tops each region up to its own maximum; one region never backfills the other.
    today = repository.list_queue(queue_date)
    already = len(today)
    split = bool(candidate.preferences.daily_queue.international_maximum)
    slots = _slots(repository, candidate, today)
    selected: list[RankedJob] = []
    for item in ranked:
        if not any(slots.values()):
            break
        region = _pool(item.job, split)
        if slots[region] <= 0 or _opening(item.job) in taken:  # a lower-ranked listing of an opening already picked today
            continue
        if is_closed and is_closed(item.job):
            repository.set_application_status(item.job.id, "expired", source="liveness")
            continue
        taken.add(_opening(item.job))
        slots[region] -= 1
        selected.append(item)
    entries = repository.add_queue_entries(
        queue_date,
        [
            {
                "job_id": item.job.id,
                "rank": index,
                "ranking_score": item.ranking_score,
                "exceptional": item.exceptional,
                "reason": (
                    f"semantic={item.assessment.overall_score}; freshness={item.freshness_score:.2f}"
                    + ("; early_career" if item.early_career_score else "")
                    + ("; posted_24h" if item.recent_score else "")
                ),
            }
            for index, item in enumerate(selected, start=already + 1)
        ],
    )
    return QueueBuild(len(pairs), len(ranked), entries)


def _pool(job: StoredJob, split: bool) -> str:
    return ("international" if is_international(job) else "us") if split else "all"


def _slots(repository: JobRepository, candidate: CandidateTruth, today: list[QueueEntry]) -> dict[str, int]:
    """Open queue slots per pool: US and international when a quota is set, otherwise one shared pool."""
    queue = candidate.preferences.daily_queue
    if not queue.international_maximum:
        return {"all": max(0, queue.maximum_target - len(today))}
    abroad = sum(1 for entry in today if (job := repository.get_job(entry.job_id)) and is_international(job))
    return {
        "us": max(0, queue.maximum_target - queue.international_maximum - (len(today) - abroad)),
        "international": max(0, queue.international_maximum - abroad),
    }


def _opening(job: StoredJob) -> tuple[str, str]:
    return opening_key(job.company, job.title)
