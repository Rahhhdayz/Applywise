"""Run expanded search queries through jobspy and persist canonical jobs."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from sqlalchemy.exc import SQLAlchemyError

from jobagent.db import JobRepository
from jobagent.scrape.jobspy_source import ScrapeFn, ScrapeSettings, scrape_query
from jobagent.scrape.profiles import SearchProfile, expand_queries

logger = logging.getLogger(__name__)


@dataclass
class ScrapeSummary:
    received: int = 0
    new: int = 0
    existing: int = 0
    updated: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


class ScrapeService:
    """One jobspy call per query; a failed query is recorded and the next one still runs."""

    def __init__(
        self,
        repository: JobRepository,
        *,
        settings: ScrapeSettings | None = None,
        scrape_fn: ScrapeFn | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.repository, self.settings, self.scrape_fn, self.sleep = (
            repository, settings or ScrapeSettings(), scrape_fn, sleep
        )

    def run(self, profiles: list[SearchProfile]) -> ScrapeSummary:
        summary = ScrapeSummary()
        for index, query in enumerate(expand_queries(profiles)):
            if index:
                self.sleep(self.settings.delay_seconds)
            try:
                postings, skipped = scrape_query(query, self.settings, scrape_fn=self.scrape_fn)
            except Exception as error:  # noqa: BLE001 - a blocked site must not stop other queries
                message = f"{query.profile_name} / {query.keyword}: {error}"[:300]
                logger.warning("scrape query failed: %s", message)
                summary.errors.append(message)
                continue
            summary.received += len(postings)
            summary.skipped += skipped
            for posting in postings:
                try:
                    result = self.repository.add_or_get_job(posting)
                except SQLAlchemyError as error:  # one bad row must not abort the whole scrape
                    logger.warning("could not store %s job %s: %s", posting.source, posting.source_job_id, error)
                    summary.errors.append(f"{posting.source}:{posting.source_job_id}: {type(error).__name__}")
                    continue
                summary.new += int(result.created)
                summary.existing += int(not result.created)
                summary.updated += int(result.updated)
        return summary
