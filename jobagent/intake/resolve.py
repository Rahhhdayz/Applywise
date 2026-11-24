"""Deterministic, bounded ATS resolution. Untrusted HTML is parsed, never executed."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

import httpx

from jobagent.intake.errors import IntakeError
from jobagent.intake.fetch import MAX_REDIRECTS, MAX_RESPONSE_BYTES, USER_AGENT
from jobagent.intake.resolve_html import COMPANY_SUFFIXES, company_key, parse_page
from jobagent.intake.urls import canonicalize_url, detect_ats_host, validate_public_url
from jobagent.normalization import normalize_location, normalize_title

_GREENHOUSE_BOARD_PAGE = re.compile(r"^/([\w-]+)/jobs/(\d+)")
MAX_REQUESTS = 32


@dataclass(frozen=True)
class DestinationResolution:
    destination_url: str | None
    ats: str
    evidence: str
    confidence: float


def _linkedin(url: str) -> bool:
    host = urlsplit(url).hostname or ""
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def _unwrap(url: str) -> str:
    parts = urlsplit(url)
    if _linkedin(url) and parts.path in {"/safety/go", "/safety/go/", "/jobs/view/externalApply/"}:
        return (parse_qs(parts.query).get("url") or [url])[0]
    return url


def company_hint(url: str) -> str:
    labels = (urlsplit(_unwrap(url)).hostname or "").split(".")[:-1]
    return next((label for label in labels if label not in {"www", "careers", "jobs", "apply", "boards"}), "")


def _greenhouse_embed(url: str) -> str:
    parts = urlsplit(url)
    match = _GREENHOUSE_BOARD_PAGE.match(parts.path)
    if parts.hostname in {"job-boards.greenhouse.io", "boards.greenhouse.io"} and match:
        return f"https://job-boards.greenhouse.io/embed/job_app?for={match[1]}&token={match[2]}"
    return url


class _Fetcher:
    """One resolution's request budget, allowlist, rate limit, and bounded streaming reader."""

    def __init__(self, http: httpx.Client, origin: str, company: str, interval: float) -> None:
        self.http = http
        self.origin = urlsplit(origin).hostname or ""
        self.company_domain = self.origin
        while self.company_domain.split(".")[0] in {"www", "careers", "jobs", "apply"}:
            self.company_domain = self.company_domain.split(".", 1)[1]
        self.company = company_key(company)
        self.interval = interval
        self.last_request: float | None = None
        self.requests = 0

    def allowed(self, url: str) -> bool:
        url = canonicalize_url(url)
        parts = urlsplit(url)
        if parts.port not in {None, 80, 443}:
            return False
        host = parts.hostname or ""
        if detect_ats_host(url) != "manual_url" or host == self.origin:
            return True
        if not _linkedin(f"https://{self.origin}") and detect_ats_host(f"https://{self.origin}") == "manual_url":
            return host == self.company_domain or host.endswith("." + self.company_domain)
        # A LinkedIn offsite link may point at the named employer's public career site.
        labels = host.split(".")
        suffix_size = 2 if host.endswith((".co.uk", ".com.au", ".co.in")) else 1
        return len(labels) > suffix_size and company_key(labels[-suffix_size - 1]) == self.company

    def fetch(self, url: str) -> tuple[str, str]:
        for hop in range(MAX_REDIRECTS + 1):
            if not self.allowed(url):
                raise IntakeError("Destination host is not allowlisted")
            url = validate_public_url(url)
            if self.requests >= MAX_REQUESTS:
                raise IntakeError("Resolution request budget exhausted")
            if self.last_request is not None:
                time.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            request = self.http.build_request("GET", url, timeout=httpx.Timeout(15, connect=5),
                                              headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"})
            # Even injected clients must not supply session credentials or automatically follow redirects.
            for header in ("cookie", "authorization", "proxy-authorization"):
                request.headers.pop(header, None)
            self.requests += 1
            self.last_request = time.monotonic()
            response = self.http.send(request, stream=True, follow_redirects=False, auth=None)
            try:
                if response.is_redirect:
                    if hop == MAX_REDIRECTS or not response.headers.get("location"):
                        raise IntakeError("Redirect limit exceeded or missing location")
                    url = str(response.url.join(response.headers["location"]))
                    continue
                response.raise_for_status()
                chunks: list[bytes] = []
                size = 0
                for chunk in response.iter_bytes(chunk_size=16_384):
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise IntakeError("Response size limit exceeded")
                    chunks.append(chunk)
                return url, b"".join(chunks).decode("utf-8", errors="replace")
            finally:
                response.close()
        raise IntakeError("Redirect limit exceeded")


def resolve_application_url(
    url: str, company: str, title: str, location: str | None = None, *,
    client: httpx.Client | None = None,
) -> str | None:
    """Compatibility API for callers that only need the verified URL."""
    result = resolve_destination(url, company, title, location, client=client)
    return result.destination_url if result else None


def resolve_destination(
    url: str, company: str, title: str, location: str | None = None, *,
    client: httpx.Client | None = None, request_interval: float = 1.0,
) -> DestinationResolution | None:
    """Resolve a job or classify Easy Apply. Missing/conflicting identity stays unresolved."""
    if client is None:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as http:
            return resolve_destination(url, company, title, location, client=http,
                                       request_interval=request_interval)
    try:
        url = canonicalize_url(url)
        source = detect_ats_host(url)
        if source != "manual_url":
            # This is already the supplied ATS job, not a match against another listing.
            return DestinationResolution(_greenhouse_embed(url), source, "original_ats_url", 1.0)
        fetcher = _Fetcher(client, url, company, request_interval)
        pending = [(_unwrap(url), "page")]
        visited: set[str] = set()
        while pending and len(visited) < 10:
            candidate, rule = pending.pop(0)
            if candidate in visited:
                continue
            visited.add(candidate)
            try:
                final, html = fetcher.fetch(candidate)
                page = parse_page(html, final)
                matches = page.matches(company, title)
                if _linkedin(final) and page.easy_apply:
                    return (DestinationResolution(None, "linkedin_easy_apply", "linkedin_easy_apply;title+company", 1.0)
                            if matches else None)
                if page.identities and not matches:
                    continue
                ats = detect_ats_host(final)
                if ats != "manual_url" and matches:
                    evidence = rule + (";redirect" if final != candidate else "") + ";title+company"
                    return DestinationResolution(_greenhouse_embed(final), ats, evidence, 0.95)
                links = []
                for link, link_rule in page.links[:100]:
                    try:
                        link = _unwrap(link)
                        if fetcher.allowed(link) and link not in visited:
                            links.append((link, "linkedin_offsite" if _linkedin(final) else link_rule))
                    except (IntakeError, ValueError):
                        continue
                links.sort(key=lambda item: detect_ats_host(item[0]) == "manual_url")
                pending.extend(links)
            except (httpx.HTTPError, IntakeError, ValueError, RecursionError):
                continue
        return _board_lookup(company, title, location, fetcher)
    except (httpx.HTTPError, IntakeError, ValueError):
        return None


def _board_lookup(company: str, title: str, location: str | None, http: _Fetcher) -> DestinationResolution | None:
    """A guessed board slug is never proof of company identity: verify the actual posting."""
    if not title or not company:
        return None
    for slug in _slugs(company):
        for provider, jobs_of in (("greenhouse", _greenhouse_jobs), ("ashby", _ashby_jobs), ("lever", _lever_jobs)):
            try:
                jobs = jobs_of(slug, http)
                matches = [(where, form) for _, name, where, form in jobs if normalize_title(name) == normalize_title(title)]
                city = normalize_location((location or "").split(",")[0])
                matches.sort(key=lambda item: not (city and city in normalize_location(item[0])))
                for _, form in matches[:5]:
                    final, body = http.fetch(form)
                    if detect_ats_host(final) == provider and parse_page(body, final).matches(company, title):
                        return DestinationResolution(_greenhouse_embed(final), provider, "board_search;title+company", 0.9)
            except (httpx.HTTPError, IntakeError, ValueError, KeyError, TypeError, AttributeError):
                continue
    return None


def _slugs(company: str) -> list[str]:
    words = [word for word in re.findall(r"[a-z0-9]+", company.casefold()) if word not in COMPANY_SUFFIXES]
    if not words:
        return []
    return list(dict.fromkeys(["".join(words), "-".join(words), words[0]]))


Board = list[tuple[str, str, str, str]]  # (job id, title, location, application form URL)


def _get_json(http: _Fetcher, url: str) -> object:
    _, body = http.fetch(url)
    return json.loads(body)


def _greenhouse_jobs(slug: str, http: _Fetcher) -> Board:
    data = _get_json(http, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
    return [
        (str(job["id"]), job.get("title", ""), (job.get("location") or {}).get("name", ""),
         f"https://job-boards.greenhouse.io/embed/job_app?for={slug}&token={job['id']}")
        for job in data.get("jobs", [])  # type: ignore[union-attr]
    ]


def _ashby_jobs(slug: str, http: _Fetcher) -> Board:
    data = _get_json(http, f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    return [
        (job["id"], job.get("title", ""), job.get("location") or "", f"https://jobs.ashbyhq.com/{slug}/{job['id']}")
        for job in data.get("jobs", [])  # type: ignore[union-attr]
    ]


def _lever_jobs(slug: str, http: _Fetcher) -> Board:
    data = _get_json(http, f"https://api.lever.co/v0/postings/{slug}?mode=json")
    return [
        (job["id"], job.get("text", ""), (job.get("categories") or {}).get("location") or "",
         job.get("hostedUrl") or f"https://jobs.lever.co/{slug}/{job['id']}")
        for job in data if isinstance(job, dict)  # type: ignore[union-attr]
    ]
