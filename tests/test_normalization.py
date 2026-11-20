from jobagent.models import JobPosting
from jobagent.normalization import (
    canonical_dedupe_key,
    normalize_company,
    normalize_location,
    normalize_title,
)


def make_job(**overrides: object) -> JobPosting:
    values: dict[str, object] = {
        "source": "example",
        "source_job_id": "123",
        "company": "Example Labs, Inc.",
        "title": "Senior Software Engineer",
        "location": "New York, NY",
    }
    values.update(overrides)
    return JobPosting(**values)


def test_field_normalization_is_stable() -> None:
    assert normalize_company("  EXAMPLE Labs, Inc.  ") == "example labs inc"
    assert normalize_title("Senior   Software-Engineer") == "senior software engineer"
    assert normalize_location(" New York,  NY ") == "new york ny"


def test_equivalent_noise_produces_the_same_dedupe_key() -> None:
    first = make_job()
    equivalent = make_job(
        source_job_id="456",
        company=" example labs inc ",
        title="SENIOR   SOFTWARE-ENGINEER",
        location="new york ny",
    )

    assert canonical_dedupe_key(first) == canonical_dedupe_key(equivalent)


def test_distinct_job_produces_a_different_dedupe_key() -> None:
    first = make_job()
    different = make_job(title="Product Manager")

    assert canonical_dedupe_key(first) != canonical_dedupe_key(different)

