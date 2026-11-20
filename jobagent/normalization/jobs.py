"""Stable, deliberately simple fields used to identify equivalent jobs."""

from __future__ import annotations

import hashlib
import re
import unicodedata

from jobagent.models.job import JobPosting

_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")


def _normalize_text(value: str | None) -> str:
    """Normalize casing, whitespace, and basic punctuation deterministically."""
    if value is None:
        return ""
    value = unicodedata.normalize("NFKC", value).casefold().strip()
    value = _PUNCTUATION.sub(" ", value)
    return _WHITESPACE.sub(" ", value).strip()


def normalize_company(value: str) -> str:
    """Return the company form used by canonical deduplication."""
    return _normalize_text(value)


def normalize_title(value: str) -> str:
    """Return the title form used by canonical deduplication."""
    return _normalize_text(value)


def opening_key(company: str, title: str) -> tuple[str, str]:
    """One opening = one company + title: LinkedIn's per-city listings and the ATS link share it."""
    return normalize_company(company), normalize_title(title)


def normalize_location(value: str | None) -> str:
    """Return the location form used by canonical deduplication."""
    return _normalize_text(value)


def canonical_dedupe_key(job: JobPosting) -> str:
    """Hash canonical job fields behind a stable function boundary.

    Keeping key construction here means a later strategy can evolve without changing
    repository or caller interfaces.
    """
    normalized_fields = (
        normalize_company(job.company),
        normalize_title(job.title),
        normalize_location(job.location),
    )
    payload = "\x1f".join(normalized_fields).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

