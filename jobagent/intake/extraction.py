"""Static HTML and JSON-LD extraction into validated job postings."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from jobagent.intake.errors import IntakeError
from jobagent.intake.fetch import FetchedPage
from jobagent.intake.urls import canonicalize_url, detect_source, same_origin
from jobagent.models import JobPosting

MAX_DESCRIPTION_CHARS = 50_000
_WHITESPACE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES = re.compile(r"\n{3,}")


def extract_job_posting(page: FetchedPage) -> JobPosting:
    """Extract one conservatively validated job posting from static page content."""
    soup = BeautifulSoup(page.html, "html.parser")
    canonical_url = _canonical_url(soup, page.final_url)
    structured = _first_job_posting(soup)
    metadata = _metadata(soup)

    title = _text(structured.get("title")) if structured else None
    title = title or metadata.get("title") or _page_title(soup)
    company = _organization_name(structured.get("hiringOrganization")) if structured else None
    company = company or metadata.get("company") or _embedded_company(page.html) or metadata.get("site_name")
    location = _location(structured) if structured else None
    location = location or ", ".join(metadata[k] for k in ("addresslocality", "addressregion") if k in metadata) or None
    employment_type = _employment_type(structured.get("employmentType")) if structured else None
    description = _description(structured.get("description")) if structured else None
    description = description or _fallback_description(soup)
    source = detect_source(canonical_url)
    source_job_id = _source_job_id(structured, source, canonical_url) if structured else None
    apply_url = _structured_url(structured.get("url"), page.final_url) if structured else None
    apply_url = apply_url or canonical_url

    missing = [name for name, value in (("company", company), ("title", title), ("apply/canonical URL", apply_url)) if not value]
    if missing:
        raise IntakeError(f"Missing required field: {missing[0]}")

    metadata_value: dict[str, Any] = {
        "submitted_url": page.submitted_url,
        "fetched_url": page.final_url,
    }
    if structured and isinstance(structured.get("validThrough"), str):
        metadata_value["valid_through"] = structured["validThrough"]
    return JobPosting(
        source=source,
        source_job_id=source_job_id,
        company=company,
        title=title,
        location=location,
        employment_type=employment_type,
        description_text=description,
        apply_url=apply_url,
        canonical_url=canonical_url,
        posted_at=_date(structured.get("datePosted")) if structured else None,
        source_metadata=metadata_value,
    )


_EMBEDDED_COMPANY = re.compile(r'"company(?:_name|Name)":("(?:[^"\\]|\\.){1,200}")')


def _embedded_company(html: str) -> str | None:
    """Greenhouse job-boards and Rippling pages carry no JSON-LD; the company is in embedded page data."""
    match = _EMBEDDED_COMPANY.search(html)
    if not match:
        return None
    try:
        return _text(json.loads(match.group(1)))
    except ValueError:
        return None


def _first_job_posting(soup: BeautifulSoup) -> dict[str, Any] | None:
    for script in soup.find_all("script", attrs={"type": lambda value: value and value.lower() == "application/ld+json"}):
        raw = script.string or script.get_text()
        try:
            decoded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        for candidate in _json_ld_objects(decoded):
            if _has_type(candidate, "JobPosting"):
                return candidate
    return None


def _json_ld_objects(value: Any) -> list[dict[str, Any]]:
    """Collect JSON-LD objects from root, arrays, and graph containers."""
    if isinstance(value, list):
        return [item for entry in value for item in _json_ld_objects(entry)]
    if not isinstance(value, dict):
        return []
    objects = [value]
    graph = value.get("@graph")
    if isinstance(graph, list):
        objects.extend(item for entry in graph for item in _json_ld_objects(entry))
    return objects


def _has_type(value: dict[str, Any], expected: str) -> bool:
    type_value = value.get("@type")
    if isinstance(type_value, str):
        return type_value.casefold() == expected.casefold()
    if isinstance(type_value, list):
        return any(isinstance(item, str) and item.casefold() == expected.casefold() for item in type_value)
    return False


def _metadata(soup: BeautifulSoup) -> dict[str, str]:
    values: dict[str, str] = {}
    for tag in soup.find_all("meta"):
        key = (tag.get("property") or tag.get("name") or tag.get("itemprop") or "").casefold()
        content = _text(tag.get("content"))
        if not content:
            continue
        if key in {"og:title", "twitter:title"} and "title" not in values:
            values["title"] = content
        if key in {"company", "job:company", "job:company_name", "hiringorganization"} and "company" not in values:
            values["company"] = content
        if key in {"addresslocality", "addressregion"}:  # schema.org microdata job address
            values.setdefault(key, content)
        if key == "og:site_name":  # a company's own careers page: "ATI | Application Development, ..."
            values["site_name"] = re.split(r"\s+[|\u2013\u2014-]\s+", content)[0].strip()
    return values


def _canonical_url(soup: BeautifulSoup, final_url: str) -> str:
    canonical = soup.find("link", rel=lambda value: value and "canonical" in value.lower())
    if canonical and canonical.get("href"):
        candidate = canonicalize_url(urljoin(final_url, str(canonical["href"])))
        if same_origin(final_url, candidate):
            return candidate
    return canonicalize_url(final_url)


def _structured_url(value: Any, base_url: str) -> str | None:
    text = _text(value)
    if not text:
        return None
    try:
        return canonicalize_url(urljoin(base_url, text))
    except IntakeError:
        return None


def _organization_name(value: Any) -> str | None:
    if isinstance(value, dict):
        return _text(value.get("name"))
    return _text(value)


def _location(job: dict[str, Any]) -> str | None:
    if (_text(job.get("jobLocationType")) or "").casefold() == "telecommute":
        return "Remote"
    locations = job.get("jobLocation")
    if locations is None:
        locations = job.get("applicantLocationRequirements")
    if not isinstance(locations, list):
        locations = [locations]
    readable = [_readable_location(location) for location in locations]
    readable = [location for location in readable if location]
    if not readable:
        return None
    if len(set(readable)) > 1:
        return "Multiple Locations"
    return readable[0]


def _readable_location(value: Any) -> str | None:
    if isinstance(value, str):
        return _text(value)
    if not isinstance(value, dict):
        return None
    address = value.get("address", value)
    if isinstance(address, str):
        return _text(address)
    if not isinstance(address, dict):
        return _text(value.get("name"))
    city = _text(address.get("addressLocality"))
    region = _text(address.get("addressRegion"))
    country = _text(address.get("addressCountry"))
    city_region = ", ".join(part for part in (city, region) if part)
    return city_region or country or _text(value.get("name"))


def _employment_type(value: Any) -> str | None:
    if isinstance(value, list):
        values = [_employment_type(item) for item in value]
        return " / ".join(item for item in values if item) or None
    text = _text(value)
    if not text:
        return None
    normalized = text.casefold().replace("-", "_").replace(" ", "_")
    mapping = {
        "full_time": "full-time",
        "part_time": "part-time",
        "contractor": "contract",
        "temporary": "temporary",
        "intern": "internship",
        "per_diem": "per-diem",
        "other": "other",
    }
    return mapping.get(normalized, text)


def _source_job_id(job: dict[str, Any], source: str, canonical_url: str) -> str | None:
    identifier = job.get("identifier")
    if isinstance(identifier, dict):
        value = _text(identifier.get("value") or identifier.get("@value"))
        if value:
            return value
    if isinstance(identifier, str):
        return _text(identifier)
    patterns = {
        "greenhouse": r"/jobs/(\d+)(?:/|$)",
        "lever": r"/(?:apply|jobs)/[^/]+/([0-9a-f-]{16,})(?:/|$)",
    }
    pattern = patterns.get(source)
    if pattern:
        match = re.search(pattern, canonical_url, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def _description(value: Any) -> str | None:
    return sanitize_html_description(_text(value))


def sanitize_html_description(value: str | None) -> str | None:
    """Convert untrusted HTML to bounded plain text for every ingestion path."""
    text = _text_from_html(value)
    return text[:MAX_DESCRIPTION_CHARS] or None


def _fallback_description(soup: BeautifulSoup) -> str | None:
    # schema.org microdata (SuccessFactors career sites) marks the posting body itself
    content = soup.select_one("[itemprop=description]:not(meta)") or soup.select_one("main, article, [role=main], body")
    if content is None:
        return None
    return _text_from_html(str(content))[:MAX_DESCRIPTION_CHARS] or None


def _text_from_html(value: str | None) -> str:
    if not value:
        return ""
    soup = BeautifulSoup(value, "html.parser")
    for tag in soup.find_all(["script", "style", "noscript", "nav", "header", "footer"]):
        tag.decompose()
    lines = [_WHITESPACE.sub(" ", line).strip() for line in soup.get_text("\n").splitlines()]
    return _BLANK_LINES.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def _page_title(soup: BeautifulSoup) -> str | None:
    return _text(soup.title.string) if soup.title and soup.title.string else None


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = _WHITESPACE.sub(" ", value).strip()
    return value or None


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
