"""ScrapeService: isolation of failing queries, politeness delay, and repeat runs."""

from __future__ import annotations

import pandas as pd

from jobagent.db import JobRepository
from jobagent.scrape.jobspy_source import ScrapeSettings
from jobagent.scrape.profiles import SearchProfile
from jobagent.scrape.service import ScrapeService
from tests.test_scrape_jobspy import row

PROFILE = SearchProfile(name="security", keywords=["Blocked Query", "Security Engineer"], locations=["United States"])


def fake(**kwargs: object) -> pd.DataFrame:
    if kwargs["search_term"] == "Blocked Query":
        raise RuntimeError("429 Too Many Requests")
    return pd.DataFrame([row(), row(id="li-2", title="Detection Engineer", job_url="https://www.linkedin.com/jobs/view/2")])


def test_failed_query_is_isolated_and_second_run_reports_existing() -> None:
    repository = JobRepository(":memory:")
    repository.initialize()
    sleeps: list[float] = []
    service = ScrapeService(repository, settings=ScrapeSettings(delay_seconds=3), scrape_fn=fake, sleep=sleeps.append)

    first = service.run([PROFILE])
    assert (first.received, first.new, first.existing) == (2, 2, 0)
    assert len(first.errors) == 1 and "429" in first.errors[0]
    assert sleeps == [3]

    second = service.run([PROFILE])
    assert (second.new, second.existing) == (0, 2)
    assert repository.count_jobs() == 2
