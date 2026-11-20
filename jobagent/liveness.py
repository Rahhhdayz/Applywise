"""Is a posting still open? Checked just before a job enters the daily queue."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from jobagent.db import StoredJob
from jobagent.intake.fetch import USER_AGENT


def linkedin_closed(url: str | None, get: Callable[..., httpx.Response] = httpx.get) -> bool:
    """A LinkedIn job page that renders its title card but no Apply button no longer takes applications.

    Any other host, a network error, or an unexpected page (login wall) counts as open: an unknown
    state never drops a job. ponytail: LinkedIn only; add Greenhouse/Lever/Ashby 404 checks if
    closed ATS postings start reaching the queue.
    """
    if not url or "linkedin.com/jobs/view/" not in url:
        return False
    try:
        page = get(url, headers={"User-Agent": USER_AGENT}, timeout=15, follow_redirects=True).text
    except httpx.HTTPError:
        return False
    return "top-card-layout__title" in page and "apply-button" not in page


def job_closed(job: StoredJob) -> bool:
    return linkedin_closed(job.apply_url or job.canonical_url)
