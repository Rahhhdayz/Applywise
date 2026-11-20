from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from jobagent.models import JobPosting


def test_job_posting_accepts_serializable_data_and_converts_to_utc() -> None:
    job = JobPosting(
        source="example",
        source_job_id="123",
        company="Example Co",
        title="Engineer",
        discovered_at=datetime(2026, 1, 1, 8, tzinfo=timezone(timedelta(hours=-5))),
        source_metadata={"page": 1, "tags": ["engineering"]},
    )

    assert job.discovered_at == datetime(2026, 1, 1, 13, tzinfo=UTC)
    assert job.source_identity == "example:id:123"


def test_job_posting_requires_source_identity_and_aware_timestamps() -> None:
    with pytest.raises(ValidationError, match="source_job_id or canonical_url"):
        JobPosting(source="example", company="Example Co", title="Engineer")

    with pytest.raises(ValidationError, match="timestamp must include a timezone"):
        JobPosting(
            source="example",
            source_job_id="123",
            company="Example Co",
            title="Engineer",
            discovered_at=datetime(2026, 1, 1),  # noqa: DTZ001 - validation target
        )
