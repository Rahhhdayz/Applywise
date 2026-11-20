"""Deterministic outcome analytics: which applications have gone quiet, and the application funnel."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime

from jobagent.db import JobRepository
from jobagent.lifecycle import AWAITING_REPLY

FOLLOWUP_AFTER_DAYS = 10
MAX_FOLLOWUPS = 2
GHOSTED_AFTER_DAYS = 45
# Funnel stages, in order; a job counts toward every stage it ever reached.
FUNNEL = ("applied", "recruiter_screen", "interview", "offer")


@dataclass(frozen=True)
class QuietApplication:
    job_id: int
    status: str
    quiet_days: int
    followups_sent: int
    unsent_draft: bool


def quiet_applications(repository: JobRepository, *, days: int, now: datetime | None = None) -> list[QuietApplication]:
    """Applications still awaiting a reply with no news for ``days``, quietest first.

    The quiet clock restarts at the latest of: the current status, or the last follow-up sent.
    """
    now = now or datetime.now(UTC)
    latest_change: dict[int, datetime] = {}
    for job_id, _status, when in repository.status_history():
        latest_change[job_id] = when
    sent: dict[int, list[datetime]] = defaultdict(list)
    unsent: set[int] = set()
    for followup in repository.list_followups():
        if followup.sent_at:
            sent[followup.job_id].append(followup.sent_at)
        else:
            unsent.add(followup.job_id)
    found = []
    for job_id, status in repository.latest_statuses().items():
        if status not in AWAITING_REPLY:
            continue
        since = max([latest_change[job_id], *sent[job_id]])
        quiet = (now - since).days
        if quiet >= days:
            found.append(QuietApplication(job_id, status, quiet, len(sent[job_id]), job_id in unsent))
    return sorted(found, key=lambda item: (-item.quiet_days, item.job_id))


def due_for_followup(repository: JobRepository, *, days: int = FOLLOWUP_AFTER_DAYS, now: datetime | None = None) -> list[QuietApplication]:
    """Quiet applications that may get another follow-up: under the cap, and no draft waiting to be sent."""
    return [item for item in quiet_applications(repository, days=days, now=now)
            if item.followups_sent < MAX_FOLLOWUPS and not item.unsent_draft]


@dataclass(frozen=True)
class Funnel:
    reached: dict[str, int]  # stage -> jobs that ever reached it
    by_week: dict[str, dict[str, int]]  # ISO apply week -> stage -> count
    closed: dict[str, int]  # rejected / ghosted after applying

    def rate(self, stage: str) -> float:
        applied = self.reached.get("applied", 0)
        return self.reached.get(stage, 0) / applied if applied else 0.0


def funnel(repository: JobRepository) -> Funnel:
    """Applied -> recruiter screen -> interview -> offer, overall and by the week each job was applied to."""
    reached: dict[int, set[str]] = defaultdict(set)
    applied_on: dict[int, datetime] = {}
    for job_id, status, when in repository.status_history():
        if status in FUNNEL or status in {"rejected", "ghosted"}:
            reached[job_id].add(status)
        if status in FUNNEL:
            applied_on.setdefault(job_id, when)  # first funnel step = when it was submitted
    stages = {stage: 0 for stage in FUNNEL}
    weeks: dict[str, dict[str, int]] = defaultdict(lambda: {stage: 0 for stage in FUNNEL})
    closed = {"rejected": 0, "ghosted": 0}
    for job_id, statuses in reached.items():
        if job_id not in applied_on:
            continue  # rejected/ghosted without ever applying (not a funnel entry)
        statuses = statuses | {"applied"}  # a screen or interview implies it was submitted
        top = max(FUNNEL.index(status) for status in statuses if status in FUNNEL)
        year, week, _ = applied_on[job_id].isocalendar()
        for stage in FUNNEL[: top + 1]:
            stages[stage] += 1
            weeks[f"{year}-W{week:02d}"][stage] += 1
        for outcome in closed:
            closed[outcome] += outcome in statuses
    return Funnel(stages, dict(sorted(weeks.items())), closed)

