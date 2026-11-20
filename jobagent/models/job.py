"""The validated, source-independent job-posting model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


def utc_now() -> datetime:
    """Return the current timezone-aware UTC timestamp."""
    return datetime.now(UTC)


class JobPosting(BaseModel):
    """A normalized job received from a source adapter.

    A source must supply either its durable job identifier or a canonical URL, so the
    repository can derive a stable source identity.
    """

    model_config = ConfigDict(extra="forbid")

    source: str
    source_job_id: str | None = None
    company: str
    title: str
    location: str | None = None
    employment_type: str | None = None
    description_text: str | None = None
    apply_url: str | None = None
    canonical_url: str | None = None
    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=utc_now)
    source_metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("source", "company", "title")
    @classmethod
    def required_text_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator(
        "source_job_id",
        "location",
        "employment_type",
        "description_text",
        "apply_url",
        "canonical_url",
    )
    @classmethod
    def optional_text_is_not_blank_when_given(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        return value or None

    @field_validator("posted_at", "discovered_at")
    @classmethod
    def timestamps_are_aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def has_source_identity_component(self) -> JobPosting:
        if self.source_job_id is None and self.canonical_url is None:
            raise ValueError("source_job_id or canonical_url is required")
        return self

    @property
    def source_identity(self) -> str:
        """Return an unambiguous, stable identity within the originating source."""
        if self.source_job_id is not None:
            return f"{self.source}:id:{self.source_job_id}"
        return f"{self.source}:url:{self.canonical_url}"

    def source_metadata_dict(self) -> dict[str, Any]:
        """Return JSON-compatible metadata for persistence."""
        return self.source_metadata
