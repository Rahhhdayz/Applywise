import httpx

from jobagent.intake.greenhouse import greenhouse_posting

JOB = {
    "absolute_url": "https://zenith.to/job-posts/81/?gh_jid=81",
    "title": "Security Software Engineer",
    "company_name": "Zenith",
    "content": "&lt;p&gt;Secure &lt;b&gt;things&lt;/b&gt;&lt;/p&gt;",
    "offices": [{"name": "San Francisco / New York"}],
    "metadata": [{"name": "Employment Type", "value": "Full-time"}],
    "first_published": "2026-09-08T18:11:34-04:00",
}


def _client(job: dict) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        if "/boards/zenith/jobs/81" in str(request.url):
            return httpx.Response(200, json=job)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_custom_domain_gh_jid_uses_job_api(monkeypatch) -> None:
    monkeypatch.setattr("jobagent.intake.greenhouse.validate_public_url", lambda url: url)
    posting = greenhouse_posting("https://zenith.to/job-posts/81?gh_jid=81", _client(JOB))
    assert posting is not None
    assert (posting.company, posting.title, posting.location) == ("Zenith", "Security Software Engineer", "San Francisco / New York")
    assert "Secure" in (posting.description_text or "") and "<" not in (posting.description_text or "")
    assert posting.source_metadata["submitted_url"] == "https://zenith.to/job-posts/81?gh_jid=81"  # hand-picked
    assert posting.apply_url == "https://job-boards.greenhouse.io/embed/job_app?for=zenith&token=81"


def test_non_matching_urls_fall_through(monkeypatch) -> None:
    monkeypatch.setattr("jobagent.intake.greenhouse.validate_public_url", lambda url: url)
    # a board whose job lives on another host is not this company's job
    assert greenhouse_posting("https://zenith.to/job-posts/81?gh_jid=81", _client({**JOB, "absolute_url": "https://x.test/81"})) is None
    assert greenhouse_posting("https://zenith.to/job-posts/81", _client(JOB)) is None
    assert greenhouse_posting("https://job-boards.greenhouse.io/embed/job_app?for=zenith", _client(JOB)) is None


def test_greenhouse_hosted_pages_use_the_job_api_not_the_page_title(monkeypatch) -> None:
    # The embed page's own title reads "Job Application for ... at Zenith Job Board".
    monkeypatch.setattr("jobagent.intake.greenhouse.validate_public_url", lambda url: url)
    for url in ("https://job-boards.greenhouse.io/embed/job_app?for=zenith&token=81",
                "https://job-boards.greenhouse.io/zenith/jobs/81"):
        posting = greenhouse_posting(url, _client(JOB))
        assert posting is not None
        assert (posting.company, posting.title) == ("Zenith", "Security Software Engineer")
        assert posting.apply_url == "https://job-boards.greenhouse.io/embed/job_app?for=zenith&token=81"


def test_caller_client_stays_open(monkeypatch) -> None:
    # resolve_application_url reuses its client after a gh_jid miss; closing it crashed `apply url`
    monkeypatch.setattr("jobagent.intake.greenhouse.validate_public_url", lambda url: url)
    client = _client({**JOB, "absolute_url": "https://x.test/81"})
    assert greenhouse_posting("https://zenith.to/job-posts/81?gh_jid=81", client) is None
    assert greenhouse_posting("https://job-boards.greenhouse.io/zenith/jobs/81", client) is not None
    assert not client.is_closed


def test_absolute_url_on_own_greenhouse_board_matches(monkeypatch) -> None:
    monkeypatch.setattr("jobagent.intake.greenhouse.validate_public_url", lambda url: url)
    job = {**JOB, "absolute_url": "https://boards.greenhouse.io/zenith/jobs/81?gh_jid=81"}
    posting = greenhouse_posting("https://zenith.to/careers/apply/?gh_jid=81", _client(job))
    assert posting is not None and posting.title == "Security Software Engineer"
    other_board = {**JOB, "absolute_url": "https://boards.greenhouse.io/someoneelse/jobs/81"}
    assert greenhouse_posting("https://zenith.to/careers/apply/?gh_jid=81", _client(other_board)) is None
