"""Which country a job is in: decides the queue quota and the Notion database it goes to."""

from __future__ import annotations

import re
from typing import Any

US = "USA"
_US_NAMES = {"usa", "us", "united states", "united states of america"}
_COUNTRY_CODES = {"JP": "Japan", "FR": "France", "AU": "Australia", "US": US}
_TRAILING_CODE = re.compile(r",\s*([A-Z]{2})\s*$")  # jobspy style: "Paris, A8, FR", "Tokyo, JP"
_US_STATE = re.compile(r",\s*[A-Z]{2}\b")  # "Melbourne, FL"; Australian states are spelled out or 3 letters
# ponytail: the three countries Master targets plus their main cities; add a row when he adds a country.
_COUNTRY_TERMS = {
    "Japan": ["japan", "tokyo", "osaka", "yokohama", "kyoto", "fukuoka", "nagoya"],
    "France": ["france", "paris", "lyon", "marseille", "toulouse", "lille", "nantes"],
    "Australia": ["australia", "sydney", "melbourne", "brisbane", "perth", "adelaide", "canberra"],
}


def matches_any(text: str | None, terms: list[str]) -> bool:
    """Whole-word, case-insensitive (same rule as eligibility; kept here to avoid an import cycle)."""
    lowered = (text or "").lower()
    return any(re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", lowered) for term in terms)


def job_country(job: Any) -> str:
    """Scrape-time country from the search profile wins; otherwise read the location; default USA."""
    country = str((getattr(job, "source_metadata", None) or {}).get("country") or "").strip()
    if country:
        return US if country.casefold() in _US_NAMES else country.title()
    location = getattr(job, "location", None)
    code = _TRAILING_CODE.search(location or "")
    if code and code.group(1) in _COUNTRY_CODES:
        return _COUNTRY_CODES[code.group(1)]
    named = next((name for name in _COUNTRY_TERMS if matches_any(location, [name])), None)
    if named:  # "Sydney, New South Wales, Australia"
        return named
    if matches_any(location, sorted(_US_NAMES)) or _US_STATE.search(location or ""):  # "Paris, TX"
        return US
    return next((name for name, terms in _COUNTRY_TERMS.items() if matches_any(location, terms)), US)


def is_international(job: Any) -> bool:
    return job_country(job) != US

