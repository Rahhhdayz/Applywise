"""python-jobspy adapter: the only place jobspy/pandas data shapes are visible."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jobagent.intake.errors import IntakeError
from jobagent.intake.extraction import MAX_DESCRIPTION_CHARS
from jobagent.intake.urls import canonicalize_url
from jobagent.models import JobPosting
from jobagent.normalization import normalize_company, normalize_location, normalize_title
from jobagent.scrape.profiles import ScrapeError, SearchQuery

SITES = ("linkedin", "indeed", "glassdoor", "google", "zip_recruiter")
_JOB_TYPES = {"fulltime": "full-time", "parttime": "part-time", "internship": "internship", "contract": "contract"}

ScrapeFn = Callable[..., Any]
# Boards that only cover some countries; any board not listed here is asked everywhere.
_SITE_COUNTRIES = {"zip_recruiter": {"usa", "us", "united states", "canada"}}
# jobspy's Country enum has no Glassdoor domain for these, so a Glassdoor call would raise.
_NO_GLASSDOOR = {"japan"}


def sites_for(country: str, sites: tuple[str, ...]) -> list[str]:
    """The configured boards that actually serve ``country``."""
    key = country.casefold().strip()
    return [
        site for site in sites
        if key in _SITE_COUNTRIES.get(site, {key}) and not (site == "glassdoor" and key in _NO_GLASSDOOR)
    ]


@dataclass(frozen=True)
class ScrapeSettings:
    """Politeness limits for board scraping; no proxies and no logins."""

    sites: tuple[str, ...] = SITES
    results_wanted: int = 25
    delay_seconds: float = 5.0
    country_indeed: str = "USA"
    linkedin_fetch_description: bool = True


def load_scrape_settings() -> ScrapeSettings:
    """Read the optional ``scrape:`` section of the local settings file."""
    path = Path(os.getenv("JOBAGENT_SETTINGS", "config/settings.yaml"))
    values: object = {}
    if path.is_file():
        try:
            values = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            values = {}
    section = values.get("scrape", {}) if isinstance(values, dict) else {}
    if not isinstance(section, dict):
        section = {}
    defaults = ScrapeSettings()
    sites = tuple(str(site).strip().lower() for site in section.get("sites", defaults.sites))
    unknown = set(sites) - set(SITES)
    if unknown or not sites:
        raise ScrapeError(f"scrape.sites must be a non-empty subset of {', '.join(SITES)}")
    try:
        return ScrapeSettings(
            sites=sites,
            results_wanted=max(1, int(section.get("results_wanted", defaults.results_wanted))),
            delay_seconds=max(0.0, float(section.get("delay_seconds", defaults.delay_seconds))),
            country_indeed=str(section.get("country_indeed", defaults.country_indeed)),
            linkedin_fetch_description=bool(
                section.get("linkedin_fetch_description", defaults.linkedin_fetch_description)
            ),
        )
    except (TypeError, ValueError) as error:
        raise ScrapeError("scrape settings are invalid") from error


def _jobspy_scrape(**kwargs: Any) -> Any:
    from jobspy import scrape_jobs  # heavy import (pandas); only when really scraping

    return scrape_jobs(**kwargs)


def scrape_query(
    query: SearchQuery, settings: ScrapeSettings, *, scrape_fn: ScrapeFn | None = None
) -> tuple[list[JobPosting], int]:
    """Run one jobspy search across all configured sites; return postings and skipped rows."""
    location = query.location
    country = query.country or settings.country_indeed
    sites = sites_for(country, settings.sites)
    if not sites:
        return [], 0
    frame = (scrape_fn or _jobspy_scrape)(
        site_name=sites,
        search_term=query.keyword,
        google_search_term=f"{query.keyword} jobs in {location}" if location else f"{query.keyword} jobs",
        location=location,
        is_remote=query.remote == "only",
        results_wanted=settings.results_wanted,
        hours_old=query.freshness_days * 24 if query.freshness_days else None,
        country_indeed=country,
        linkedin_fetch_description=settings.linkedin_fetch_description,
        description_format="markdown",
    )
    rows: list[dict[str, Any]] = frame.to_dict("records")
    postings: dict[object, JobPosting] = {}
    skipped = 0
    for row in rows:
        posting = _to_posting(row, query, country)
        if posting is None:
            skipped += 1
            continue
        # ponytail: in-scrape collapse by direct URL or normalized company/title/location;
        # two genuinely different requisitions with identical fields in one scrape merge.
        # Across runs the repository only merges exact canonical URLs.
        key: object = (
            posting.canonical_url
            if _url(row.get("job_url_direct"))
            else (
                normalize_company(posting.company),
                normalize_title(posting.title),
                normalize_location(posting.location),
            )
        )
        if key in postings:
            first = postings[key]
            via = list(first.source_metadata["discovered_via"])  # type: ignore[arg-type]
            if posting.source not in via:
                postings[key] = first.model_copy(
                    update={"source_metadata": {**first.source_metadata, "discovered_via": [*via, posting.source]}}
                )
        else:
            postings[key] = posting
    return list(postings.values()), skipped


def _to_posting(row: dict[str, Any], query: SearchQuery, country: str) -> JobPosting | None:
    site = _clean(row.get("site"))
    company, title = _clean(row.get("company")), _clean(row.get("title"))
    if not site or not company or not title:
        return None
    board_url = _url(row.get("job_url"))
    canonical = _url(row.get("job_url_direct")) or board_url
    is_remote = bool(_clean(row.get("is_remote")))
    description = _clean(row.get("description"))
    identifier = _clean(row.get("id"))
    try:
        return JobPosting(
            source=str(site),
            source_job_id=str(identifier) if identifier is not None else None,
            company=str(company),
            title=str(title),
            location=_clean(row.get("location")) or ("Remote" if is_remote else None),
            employment_type=_employment_type(row.get("job_type")),
            description_text=str(description)[:MAX_DESCRIPTION_CHARS] if description else None,
            apply_url=canonical,
            canonical_url=canonical,
            posted_at=_posted_at(row.get("date_posted")),
            source_metadata={
                "board_url": board_url,
                "company_url": _url(row.get("company_url_direct")),  # the employer's own site, for outreach
                "salary_min": _number(row.get("min_amount")),
                "salary_max": _number(row.get("max_amount")),
                "salary_interval": _clean(row.get("interval")),
                "salary_currency": _clean(row.get("currency")),
                "is_remote": is_remote,
                "job_level": _clean(row.get("job_level")),
                "search_profile": query.profile_name,
                "country": country,
                "discovered_via": [str(site)],
            },
        )
    except (ValidationError, TypeError):
        return None


def _clean(value: object) -> Any:
    """Map pandas NaN/NaT/blank strings to None; pass everything else through."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    try:
        if value != value:  # noqa: PLR0124 - NaN/NaT are the only values unequal to themselves
            return None
    except (TypeError, ValueError):
        pass
    return value


def _url(value: object) -> str | None:
    value = _clean(value)
    if not isinstance(value, str):
        return None
    try:
        return canonicalize_url(value)
    except IntakeError:
        return None


def _number(value: object) -> float | None:
    value = _clean(value)
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _employment_type(value: object) -> str | None:
    """Map one jobspy type; several types become None so eligibility says 'review'."""
    value = _clean(value)
    types = [item.strip() for item in str(value).split(",") if item.strip()] if value else []
    return _JOB_TYPES.get(types[0]) if len(types) == 1 else None


def _posted_at(value: object) -> datetime | None:
    value = _clean(value)
    if isinstance(value, datetime):
        value = value.date()
    elif value is not None and not isinstance(value, date):
        try:
            value = date.fromisoformat(str(value)[:10])
        except ValueError:
            return None
    return datetime(value.year, value.month, value.day, tzinfo=UTC) if value else None
