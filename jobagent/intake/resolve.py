"""Find the real application form behind a pasted or scraped job link.

LinkedIn listings, company careers pages and redirect wrappers (LinkedIn safety links, eQuest)
hide the ATS form the filler needs. Rules run in order and the first hit wins; None means
"keep the link and fill by hand". Page and API text is untrusted data: it is only parsed as
JSON or regex-matched against known ATS hosts, never interpreted. Every request goes through
``validate_public_url``; board lookups only call three fixed public API hosts.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlsplit

import httpx

from jobagent.intake.errors import IntakeError
from jobagent.intake.fetch import MAX_REDIRECTS, MAX_RESPONSE_BYTES, USER_AGENT
from jobagent.intake.greenhouse import greenhouse_posting
from jobagent.intake.urls import detect_source, validate_public_url
from jobagent.normalization import normalize_location, normalize_title

_GREENHOUSE_BOARD_PAGE = re.compile(r"^/([\w-]+)/jobs/(\d+)")
_EMBEDDED_APPLY_URL = re.compile(r'"apply_url"\s*:\s*"([^"]+)"')  # Jibe career sites (Cyberdyne -> iCIMS)
_COMPANY_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "the", "plc", "gmbh"}


def resolve_application_url(
    url: str,
    company: str,
    title: str,
    location: str | None = None,
    *,
    client: httpx.Client | None = None,
) -> str | None:
    """Return the ATS application URL for one job, or None when no supported form was found."""
    http = client or httpx.Client(timeout=20, follow_redirects=False, trust_env=False)
    url = _unwrap(url)
    source = detect_source(url)
    if source == "greenhouse":
        return _greenhouse_embed(url)
    if source != "manual_url":
        return url
    query = parse_qs(urlsplit(url).query)
    if query.get("gh_jid"):
        posting = greenhouse_posting(url, http)
        if posting is not None:
            return posting.apply_url
    if ashby_id := (query.get("ashby_jid") or [""])[0]:
        found = _board_lookup(company, title, location, http, ashby_id=ashby_id)
        if found:
            return found
    host = urlsplit(url).hostname or ""
    if not host.endswith("linkedin.com"):  # LinkedIn hides the offsite link behind sign-in
        found = _from_page(url, company, title, location, http)
        if found:
            return found
    return _board_lookup(company, title, location, http)


def company_hint(url: str) -> str:
    """A company name guessed from a careers link's domain (careers.cyberdyne.com -> "cyberdyne"), for board
    lookups before the posting itself has been read."""
    labels = (urlsplit(_unwrap(url)).hostname or "").split(".")[:-1]
    return next((label for label in labels if label not in {"www", "careers", "jobs", "apply", "boards"}), "")


def _unwrap(url: str) -> str:
    """LinkedIn's "leaving LinkedIn" interstitial carries the real link in ``url=``."""
    parts = urlsplit(url)
    if (parts.hostname or "").endswith("linkedin.com") and parts.path.startswith("/safety/go"):
        return (parse_qs(parts.query).get("url") or [url])[0]
    return url


def _greenhouse_embed(url: str) -> str:
    """Greenhouse board pages on custom-domain boards redirect to the company site; the embed never does."""
    parts = urlsplit(url)
    match = _GREENHOUSE_BOARD_PAGE.match(parts.path)
    if parts.hostname in {"job-boards.greenhouse.io", "boards.greenhouse.io"} and match:
        return f"https://job-boards.greenhouse.io/embed/job_app?for={match[1]}&token={match[2]}"
    return url


def _from_page(url: str, company: str, title: str, location: str | None, http: httpx.Client) -> str | None:
    """Follow redirects hop by hop (each re-validated) and stop at the first supported ATS URL, before
    fetching it: ATS pages often refuse bots, and the URL alone is the answer. Otherwise look in the
    page for an embedded ATS apply link."""
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(current)
            response = http.get(current, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
            if not response.is_redirect:
                break
            current = str(response.url.join(response.headers.get("location", "")))
            if detect_source(current) != "manual_url":  # eQuest -> Lever/Greenhouse/...
                return resolve_application_url(current, company, title, location, client=http)
        else:
            return None
    except (httpx.HTTPError, IntakeError):
        return None
    for candidate in _EMBEDDED_APPLY_URL.findall(response.text[:MAX_RESPONSE_BYTES]):
        candidate = candidate.replace("\\/", "/")
        if detect_source(candidate) != "manual_url":
            return candidate
    return None


def _slugs(company: str) -> list[str]:
    words = [word for word in re.findall(r"[a-z0-9]+", company.casefold()) if word not in _COMPANY_SUFFIXES]
    if not words:
        return []
    return list(dict.fromkeys(["".join(words), "-".join(words), words[0]]))


def _board_lookup(
    company: str, title: str, location: str | None, http: httpx.Client, *, ashby_id: str = ""
) -> str | None:
    """Search the company's public Greenhouse, Ashby and Lever boards for this exact title."""
    # ponytail: board slug guessed from the company name (STARK.AI -> starkai/stark-ai/stark);
    # a board named unlike the company is missed, and the job keeps its link for manual filling.
    wanted = normalize_title(title)
    providers = (("ashby", _ashby_jobs),) if ashby_id else (
        ("greenhouse", _greenhouse_jobs), ("ashby", _ashby_jobs), ("lever", _lever_jobs),
    )
    for slug in _slugs(company):
        for _, jobs_of in providers:
            try:
                jobs = jobs_of(slug, http)
            except (httpx.HTTPError, IntakeError, ValueError):
                continue
            if ashby_id:
                matches = [form for job_id, _, _, form in jobs if job_id == ashby_id]
                if matches:
                    return matches[0]
                continue
            matches = [(where, form) for _, name, where, form in jobs if normalize_title(name) == wanted]
            if matches:
                return _nearest(matches, location)
    return None


def _nearest(matches: list[tuple[str, str]], location: str | None) -> str:
    """Several listings of one title (one per city): prefer the job's own city."""
    city = normalize_location((location or "").split(",")[0])
    return next((form for where, form in matches if city and city in normalize_location(where)), matches[0][1])


Board = list[tuple[str, str, str, str]]  # (job id, title, location, application form URL)


def _get_json(http: httpx.Client, url: str) -> object:
    validate_public_url(url)
    response = http.get(url, headers={"Accept": "application/json"})
    if response.status_code != 200:
        raise IntakeError(f"board returned {response.status_code}")
    return json.loads(response.text)


def _greenhouse_jobs(slug: str, http: httpx.Client) -> Board:
    data = _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [
        (str(job["id"]), job.get("title", ""), (job.get("location") or {}).get("name", ""),
         f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job['id']}")
        for job in data.get("jobs", [])  # type: ignore[union-attr]
    ]


def _ashby_jobs(slug: str, http: httpx.Client) -> Board:
    data = _get_json(http, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [
        (job["id"], job.get("title", ""), job.get("location") or "", f"https://jobs.ashbyhq.com/{slug}/{job['id']}")
        for job in data.get("jobs", [])  # type: ignore[union-attr]
    ]


def _lever_jobs(slug: str, http: httpx.Client) -> Board:
    data = _get_json(http, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [
        (job["id"], job.get("text", ""), (job.get("categories") or {}).get("location") or "",
         job.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{job['id']}")
        for job in data if isinstance(job, dict)  # type: ignore[union-attr]
    ]
