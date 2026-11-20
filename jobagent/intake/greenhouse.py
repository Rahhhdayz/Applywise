"""Greenhouse boards hosted on a company's own domain (``?gh_jid=``) render client-side, so the
static page has no company or description; Greenhouse's public job API has both."""

from __future__ import annotations

import html
import re
from contextlib import nullcontext
from datetime import datetime
from urllib.parse import parse_qs, urlsplit

import httpx

from jobagent.intake.errors import IntakeError
from jobagent.intake.extraction import sanitize_html_description
from jobagent.intake.urls import validate_public_url
from jobagent.models import JobPosting

_GENERIC_LABELS = {"www", "careers", "jobs", "boards", "apply"}


def greenhouse_posting(url: str, client: httpx.Client | None = None) -> JobPosting | None:
    """Build the posting for a custom-domain Greenhouse job, or None when it is not one."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host.endswith("greenhouse.io"):
        return _hosted_posting(url, client)
    job_id = (parse_qs(parts.query).get("gh_jid") or [""])[0]
    if not job_id.isdigit():
        return None
    # ponytail: board token guessed from the domain's labels (zenith.to -> "zenith"); a board
    # named unlike its domain is missed and falls back to scraping the page as submitted.
    boards = [label for label in host.split(".")[:-1] if label not in _GENERIC_LABELS]
    try:
        with nullcontext(client) if client else httpx.Client(timeout=20, follow_redirects=False) as http:  # never close a caller's client
            for board in boards:
                api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
                validate_public_url(api)
                response = http.get(api)
                job = response.json() if response.status_code == 200 else {}
                absolute = urlsplit(job.get("absolute_url", ""))
                # Some boards (Tyrell) point absolute_url at their own greenhouse.io board, not the domain.
                own_board = (absolute.hostname or "").endswith("greenhouse.io") and absolute.path.startswith(f"/{board}/")
                if absolute.hostname == host or own_board:
                    return _posting(job, board, job_id, url)
    except (httpx.HTTPError, ValueError, IntakeError):
        return None
    return None


def _hosted_posting(url: str, client: httpx.Client | None) -> JobPosting | None:
    """Greenhouse's own pages (embed form or board page): the page title reads "Job Application for
    ... at ... Job Board", so take company and title from the job API instead."""
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    board, job_id = (query.get("for") or [""])[0], (query.get("token") or [""])[0]
    if not (board and job_id):
        match = re.match(r"^/([\w-]+)/jobs/(\d+)", parts.path)
        board, job_id = (match[1], match[2]) if match else ("", "")
    if not (board and job_id.isdigit()):
        return None
    api = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{job_id}"
    try:
        validate_public_url(api)
        with nullcontext(client) if client else httpx.Client(timeout=20, follow_redirects=False) as http:  # never close a caller's client
            response = http.get(api)
        return _posting(response.json(), board, job_id, url) if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError, KeyError, IntakeError):
        return None


def _posting(job: dict, board: str, job_id: str, submitted_url: str) -> JobPosting:
    # The embed page is Greenhouse's own application form for this job.
    form = f"https://job-boards.greenhouse.io/embed/job_app?for={board}&token={job_id}"
    offices = " / ".join(office["name"] for office in job.get("offices") or [] if office.get("name"))
    employment = next((m.get("value") for m in job.get("metadata") or [] if m.get("name") == "Employment Type"), None)
    published = job.get("first_published")
    return JobPosting(
        source="greenhouse",
        source_job_id=job_id,
        company=job.get("company_name") or board,
        title=job["title"],
        location=offices or None,
        employment_type=employment if isinstance(employment, str) else None,
        description_text=sanitize_html_description(html.unescape(job.get("content") or "")),
        apply_url=form,
        canonical_url=form,
        posted_at=datetime.fromisoformat(published) if published else None,
        source_metadata={"submitted_url": submitted_url, "board": board, "absolute_url": job.get("absolute_url")},
    )
