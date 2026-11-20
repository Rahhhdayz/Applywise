"""Application service that connects URL intake to persistence."""

from __future__ import annotations

from typing import Protocol

from jobagent.db import JobRepository, UpsertJobResult
from jobagent.intake.errors import IntakeError
from jobagent.intake.extraction import extract_job_posting
from jobagent.intake.fetch import FetchedPage, HttpFetcher
from jobagent.intake.greenhouse import greenhouse_posting
from jobagent.intake.oracle import enrich_oracle_posting
from jobagent.intake.render import BrowserFetcher
from jobagent.models import JobPosting


class PageFetcher(Protocol):
    def fetch(self, submitted_url: str) -> FetchedPage: ...


class UrlIntakeService:
    """Fetch, extract, validate, and persist one public job URL."""

    def __init__(
        self,
        repository: JobRepository,
        fetcher: PageFetcher | None = None,
        renderer: PageFetcher | None = None,
    ) -> None:
        self._repository = repository
        self._fetcher = fetcher or HttpFetcher()
        self._renderer = renderer or BrowserFetcher()

    def import_url(self, url: str) -> UpsertJobResult:
        """Persist a job extracted from a single submitted URL."""
        posting = greenhouse_posting(url) or enrich_oracle_posting(self._extract(url), url)
        return self._repository.add_or_get_job(posting)

    def _extract(self, url: str) -> JobPosting:
        """Plain HTTP first; on a 4xx/5xx or failed extraction, retry once on browser-rendered HTML."""
        try:
            return extract_job_posting(self._fetcher.fetch(url))
        except IntakeError as error:
            try:
                return extract_job_posting(self._renderer.fetch(url))
            except IntakeError:
                raise error from None
