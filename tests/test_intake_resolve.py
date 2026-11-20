import httpx
import pytest

from jobagent.intake.resolve import resolve_application_url

GH_BOARD = {"jobs": [
    {"id": 11, "title": "Security Engineer", "location": {"name": "Austin, TX"}},
    {"id": 12, "title": "Security Engineer", "location": {"name": "Boston, MA"}},
    {"id": 13, "title": "Senior Security Engineer", "location": {"name": "Boston, MA"}},
]}
ASHBY_BOARD = {"jobs": [{"id": "abc-123", "title": "Automation Engineer", "location": "San Jose, CA"}]}
JIBE_PAGE = '<html><script>var job = {"apply_url":"https:\\/\\/us-careers-acme.icims.com\\/jobs\\/31\\/login"};</script></html>'


@pytest.fixture(autouse=True)
def _offline_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jobagent.intake.resolve.validate_public_url", lambda url: url)


def _client(routes: dict[str, httpx.Response], seen: list[str] | None = None) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if seen is not None:
            seen.append(url)
        path = url.split("?")[0]  # routes match a whole path, so ".../stark" never answers ".../starkai"
        return routes.get(path) or routes.get(url) or httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def _resolve(url: str, company: str, title: str, location: str | None = None, routes=None, seen=None):  # type: ignore[no-untyped-def]
    return resolve_application_url(url, company, title, location, client=_client(routes or {}, seen))


def test_linkedin_listing_resolves_through_the_company_greenhouse_board_preferring_its_city() -> None:
    routes = {"https://boards-api.greenhouse.io/v1/boards/acme/jobs": httpx.Response(200, json=GH_BOARD)}
    found = _resolve("https://www.linkedin.com/jobs/view/1", "Acme, Inc.", "Security Engineer", "Boston, MA", routes)
    assert found == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=12"


def test_a_near_miss_title_is_never_accepted() -> None:
    routes = {"https://boards-api.greenhouse.io/v1/boards/acme/jobs": httpx.Response(200, json=GH_BOARD)}
    assert _resolve("https://www.linkedin.com/jobs/view/1", "Acme", "Security Engineer II", None, routes) is None


def test_linkedin_pages_are_never_fetched() -> None:
    seen: list[str] = []
    _resolve("https://www.linkedin.com/jobs/view/1", "Acme", "Security Engineer", seen=seen)
    assert not any("linkedin.com" in url for url in seen)


def test_greenhouse_board_page_becomes_the_embed_form() -> None:
    found = _resolve("https://job-boards.greenhouse.io/wonka/jobs/8243737", "Wonka", "x")
    assert found == "https://job-boards.greenhouse.io/embed/job_app?for=wonka&token=8243737"


def test_supported_ats_links_pass_through_unchanged() -> None:
    url = "https://acme.wd5.myworkdayjobs.com/Acme/job/Boston/Engineer_JR-1"
    assert _resolve(url, "Acme", "Engineer") == url


def test_ashby_jid_on_a_careers_page_finds_the_ashby_job_by_id() -> None:
    routes = {"https://api.ashbyhq.com/posting-api/job-board/stark": httpx.Response(200, json=ASHBY_BOARD)}
    found = _resolve("https://stark.ai/careers/?ashby_jid=abc-123", "STARK.AI", "Anything", None, routes)
    assert found == "https://jobs.ashbyhq.com/stark/abc-123"


def test_jibe_career_page_yields_its_embedded_icims_link() -> None:
    routes = {"https://careers.acme.com/careers-home/jobs/31": httpx.Response(200, text=JIBE_PAGE, headers={"content-type": "text/html"})}
    found = _resolve("https://careers.acme.com/careers-home/jobs/31", "Acme", "Engineer", None, routes)
    assert found == "https://us-careers-acme.icims.com/jobs/31/login"


def test_linkedin_safety_link_and_redirect_to_an_ats_are_followed_without_fetching_the_ats() -> None:
    # The Lever page itself is not routed (404): the redirect target alone is the answer.
    routes = {"https://ars2.equest.com/": httpx.Response(302, headers={"location": "https://jobs.lever.co/acme/9"})}
    wrapped = "https://www.linkedin.com/safety/go/?url=https%3A%2F%2Fars2.equest.com%2F%3Fresponse_id%3D1"
    assert _resolve(wrapped, "Acme", "Engineer", None, routes) == "https://jobs.lever.co/acme/9"


def test_nothing_found_returns_none() -> None:
    assert _resolve("https://careers.example.com/job/1", "Nobody", "Engineer") is None
