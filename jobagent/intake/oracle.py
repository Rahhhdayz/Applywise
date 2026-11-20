"""Oracle Cloud HCM ("CandidateExperience") job pages render with JavaScript; their public
requisition API carries the description the static page lacks."""

from __future__ import annotations

import re
from contextlib import nullcontext
from urllib.parse import quote, urlsplit

import httpx

from jobagent.intake.errors import IntakeError
from jobagent.intake.extraction import sanitize_html_description
from jobagent.intake.urls import validate_public_url
from jobagent.models import JobPosting

_JOB_PATH = re.compile(r"/hcmUI/CandidateExperience/[^/]+/sites/([^/]+)/job/(\d+)")


def enrich_oracle_posting(posting: JobPosting, url: str, client: httpx.Client | None = None) -> JobPosting:
    """Fill description/location from the requisition API; any failure keeps the page's data."""
    parts = urlsplit(url)
    match = _JOB_PATH.search(parts.path)
    if not match or not (parts.hostname or "").endswith(".oraclecloud.com") or posting.description_text:
        return posting
    site, job_id = match.groups()
    finder = quote(f'ById;Id="{job_id}",siteNumber={site}', safe=";=,")
    api = f"https://{parts.hostname}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails?expand=all&onlyData=true&finder={finder}"
    try:
        validate_public_url(api)
        with nullcontext(client) if client else httpx.Client(timeout=20, follow_redirects=False) as http:  # never close a caller's client
            items = http.get(api).raise_for_status().json().get("items") or []
    except (httpx.HTTPError, ValueError, IntakeError):
        return posting
    if not items:
        return posting
    item = items[0]
    sections = [item.get(key) or "" for key in ("ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr")]
    description = sanitize_html_description("\n".join(section for section in sections if section))
    return posting.model_copy(update={
        "description_text": description or posting.description_text,
        "location": posting.location or item.get("PrimaryLocation"),
    })
