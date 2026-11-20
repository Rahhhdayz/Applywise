from pathlib import Path

import pytest

from jobagent.db import JobRepository
from jobagent.intake.errors import IntakeError
from jobagent.intake.fetch import FetchedPage
from jobagent.intake.service import UrlIntakeService

FIXTURES = Path(__file__).parent / "fixtures"


class FixtureFetcher:
    def __init__(self, fixture: str) -> None:
        self.fixture = fixture

    def fetch(self, url: str) -> FetchedPage:
        return FetchedPage(submitted_url=url, final_url=url, html=(FIXTURES / self.fixture).read_text())


def test_tracking_variants_import_one_canonical_job(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    service = UrlIntakeService(repository, fetcher=FixtureFetcher("standard_json_ld.html"))  # type: ignore[arg-type]

    first = service.import_url("https://jobs.example.com/openings/123?utm_source=newsletter")
    second = service.import_url("https://jobs.example.com/openings/123?gclid=abc")

    assert first.created is True
    assert second.created is False
    assert repository.count_jobs() == 1


def test_distinct_requisitions_with_same_similarity_fields_are_not_merged(tmp_path) -> None:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    first_service = UrlIntakeService(repository, fetcher=FixtureFetcher("standard_json_ld.html"))  # type: ignore[arg-type]
    second_service = UrlIntakeService(repository, fetcher=FixtureFetcher("standard_json_ld_456.html"))  # type: ignore[arg-type]

    first = first_service.import_url("https://jobs.example.com/openings/123")
    second = second_service.import_url("https://jobs.example.com/openings/456")

    assert first.created is True
    assert second.created is True
    assert repository.count_jobs() == 2


TENTH_HTML = """<html><head><title>Information Security Engineer - Example Staffing Group</title>
<meta property="og:site_name" content="Example Staffing Group"></head>
<body><main><h1>Information Security Engineer</h1><p>Protect systems.</p></main></body></html>"""


class BlockedFetcher:
    def fetch(self, url: str) -> FetchedPage:
        raise IntakeError("HTTP request failed with status 403")


class RecordingRenderer:
    def __init__(self, html: str | None = TENTH_HTML) -> None:
        self.html = html
        self.calls: list[str] = []

    def fetch(self, url: str) -> FetchedPage:
        self.calls.append(url)
        if self.html is None:
            raise IntakeError("Browser fetch failed: timeout")
        return FetchedPage(submitted_url=url, final_url=url + "?afd_azwaf_tok=abc", html=self.html)


def _repository(tmp_path) -> JobRepository:  # type: ignore[no-untyped-def]
    repository = JobRepository(tmp_path / "jobs.sqlite3")
    repository.initialize()
    return repository


def test_http_403_falls_back_to_rendered_page(tmp_path) -> None:  # type: ignore[no-untyped-def]
    renderer = RecordingRenderer()
    service = UrlIntakeService(_repository(tmp_path), fetcher=BlockedFetcher(), renderer=renderer)

    job = service.import_url("https://www.examplestaffing.com/job/information-security-engineer/").job

    assert renderer.calls == ["https://www.examplestaffing.com/job/information-security-engineer/"]
    assert job.company == "Example Staffing Group"
    assert "afd_azwaf_tok" not in job.canonical_url


def test_successful_http_fetch_does_not_launch_browser(tmp_path) -> None:  # type: ignore[no-untyped-def]
    renderer = RecordingRenderer()
    service = UrlIntakeService(_repository(tmp_path), fetcher=FixtureFetcher("standard_json_ld.html"), renderer=renderer)

    service.import_url("https://jobs.example.com/openings/123")

    assert renderer.calls == []


def test_failed_fallback_raises_original_error(tmp_path) -> None:  # type: ignore[no-untyped-def]
    service = UrlIntakeService(_repository(tmp_path), fetcher=BlockedFetcher(), renderer=RecordingRenderer(html=None))

    with pytest.raises(IntakeError, match="status 403"):
        service.import_url("https://www.examplestaffing.com/job/information-security-engineer/")


def test_unextractable_http_page_is_retried_rendered(tmp_path) -> None:  # type: ignore[no-untyped-def]
    class ShellFetcher:
        def fetch(self, url: str) -> FetchedPage:
            return FetchedPage(submitted_url=url, final_url=url, html="<html><body><div id=app></div></body></html>")

    renderer = RecordingRenderer()
    service = UrlIntakeService(_repository(tmp_path), fetcher=ShellFetcher(), renderer=renderer)

    assert service.import_url("https://careers.example.com/job/1").job.company == "Example Staffing Group"
    assert len(renderer.calls) == 1
