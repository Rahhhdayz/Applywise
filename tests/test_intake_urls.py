import socket

import pytest

from jobagent.intake import IntakeError
from jobagent.intake.urls import canonicalize_url, detect_source, validate_public_url


def public_resolver(host: str, port: int) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def private_resolver(host: str, port: int) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]


def test_canonicalize_url_removes_fragments_and_tracking_parameters() -> None:
    result = canonicalize_url("HTTPS://Jobs.Example.com/path?role=7&utm_source=x&gclid=y#part")

    assert result == "https://jobs.example.com/path?role=7"


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/job.html", "ftp://example.com/job", "http://localhost/job", "http://[bad/job"],
)
def test_validate_public_url_rejects_unsupported_or_local_targets(url: str) -> None:
    with pytest.raises(IntakeError):
        validate_public_url(url, resolver=public_resolver)


def test_validate_public_url_rejects_private_network_resolution() -> None:
    with pytest.raises(IntakeError, match="private or local"):
        validate_public_url("https://jobs.example.com/opening", resolver=private_resolver)


def test_validate_public_url_accepts_public_resolution() -> None:
    assert validate_public_url("https://jobs.example.com/opening", resolver=public_resolver) == (
        "https://jobs.example.com/opening"
    )


def test_source_detection_is_limited_to_recognized_ats_hosts() -> None:
    assert detect_source("https://boards.greenhouse.io/example/jobs/123") == "greenhouse"
    assert detect_source("https://x.munisselfservice.com/ess/EmploymentOpportunities/JobDetail.aspx") == "munis"
    assert detect_source("https://acme.hire.trakstar.com/jobs/fk0z8d2/") == "trakstar"
    assert detect_source("https://apply.workable.com/j/3DAD137619") == "workable"
    assert detect_source("https://careers.example.com/talentcommunity/apply/1442063933/?locale=en_US") == "successfactors"
    assert detect_source("https://careers-heart.icims.com/jobs/17814/cybersecurity-engineer/login") == "icims"
    assert detect_source("https://jobs.example.com/opening") == "manual_url"
