"""YAML search profiles and deterministic query expansion for scraping."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


class ScrapeError(Exception):
    """A bounded, user-reportable scrape configuration or source failure."""


class SearchProfile(BaseModel):
    """One YAML search profile."""

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True
    keywords: list[str]
    locations: list[str] = Field(default_factory=list)
    remote: str = "include"
    freshness_days: int | None = None
    country: str | None = None  # jobspy country (Indeed/Glassdoor domain); None = scrape.country_indeed

    @field_validator("name")
    @classmethod
    def profile_name_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("keywords", "locations")
    @classmethod
    def clean_text_list(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if values and not cleaned:
            raise ValueError("must contain non-blank values")
        return cleaned

    @field_validator("remote")
    @classmethod
    def validate_remote(cls, value: str) -> str:
        value = value.strip().lower()
        if value not in {"include", "only", "exclude"}:
            raise ValueError("must be include, only, or exclude")
        return value

    @field_validator("freshness_days")
    @classmethod
    def validate_freshness(cls, value: int | None) -> int | None:
        if value is not None and value <= 0:
            raise ValueError("must be positive")
        return value


class SearchQuery(BaseModel):
    """One deduplicated query expanded from a profile."""

    profile_name: str
    keyword: str
    location: str | None = None
    remote: str = "include"
    freshness_days: int | None = None
    country: str | None = None

    @property
    def identity(self) -> tuple[str, str, str, int | None, str]:
        """Return cross-profile query fields used to avoid repeat scrapes per run."""
        return (
            self.keyword.casefold().strip(),
            (self.location or "").casefold().strip(),
            self.remote,
            self.freshness_days,
            (self.country or "").casefold().strip(),
        )


def load_search_profiles(path: str | Path) -> list[SearchProfile]:
    """Load a complete profile file without partial acceptance."""
    try:
        loaded = yaml.safe_load(Path(path).read_text())
    except (OSError, yaml.YAMLError) as error:
        raise ScrapeError("Could not load search profile configuration") from error
    if not isinstance(loaded, dict) or not isinstance(loaded.get("profiles"), list):
        raise ScrapeError("Search profile configuration must contain a profiles list")
    try:
        profiles = [SearchProfile.model_validate(value) for value in loaded["profiles"]]
    except ValidationError as error:
        raise ScrapeError("Search profile configuration is invalid") from error
    names = [profile.name for profile in profiles]
    if len(names) != len(set(names)):
        raise ScrapeError("Search profile names must be unique")
    return profiles


def expand_queries(profiles: list[SearchProfile]) -> list[SearchQuery]:
    """Expand enabled keyword/location combinations once per run, removing duplicates."""
    queries: list[SearchQuery] = []
    seen: set[tuple[str, str, str, int | None, str]] = set()
    for profile in profiles:
        if not profile.enabled:
            continue
        for keyword in profile.keywords:
            for location in profile.locations or [None]:
                query = SearchQuery(
                    profile_name=profile.name,
                    keyword=keyword,
                    location=location,
                    remote=profile.remote,
                    freshness_days=profile.freshness_days,
                    country=profile.country,
                )
                if query.identity not in seen:
                    seen.add(query.identity)
                    queries.append(query)
    return queries
