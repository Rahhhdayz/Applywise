"""All HTTP is mocked; all companies and job identities are synthetic."""

import json

import httpx
import pytest

from jobagent.intake import resolve
from jobagent.intake.resolve import resolve_application_url, resolve_destination
from jobagent.intake.urls import detect_source

LINKEDIN = "https://www.linkedin.com/jobs/view/1"
CAREER = "https://careers.acme.example/job/1"
WORKDAY = "https://acme.wd1.myworkdayjobs.com/External/job/Boston/Engineer_R1"
GREENHOUSE = "https://job-boards.greenhouse.io/acme/jobs/11"


def identity(company="Acme", title="Security Engineer"):
    return '<script type="application/ld+json">' + json.dumps({
        "@type": "JobPosting", "title": title, "hiringOrganization": {"name": company},
    }) + "</script>"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(resolve, "validate_public_url", lambda url: url)
    monkeypatch.setattr(resolve.time, "sleep", lambda _: None)


def run(url=LINKEDIN, routes=None, seen=None, **kwargs):
    def handler(request):
        if seen is not None:
            seen.append(request)
        response = (routes or {}).get(str(request.url))
        return response if response is not None else httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True,
                      cookies={"private": "secret"}, auth=("user", "password")) as client:
        return resolve_destination(url, "Acme", "Security Engineer", client=client, request_interval=0, **kwargs)


def test_linkedin_offsite_apply():
    result = run(routes={
        LINKEDIN: httpx.Response(200, text=identity() + f'<a href="{WORKDAY}">Apply</a>'),
        WORKDAY: httpx.Response(200, text=identity()),
    })
    assert result.destination_url == WORKDAY
    assert result.ats == "workday"
    assert result.evidence == "linkedin_offsite;title+company"


@pytest.mark.parametrize("marker", ['<button>Easy Apply</button>', '<script>{"isEasyApply":true}</script>'])
def test_easy_apply_is_classified_and_never_automated(marker):
    seen = []
    result = run(routes={LINKEDIN: httpx.Response(200, text=identity() + marker)}, seen=seen)
    assert result.ats == "linkedin_easy_apply"
    assert result.destination_url is None
    assert len(seen) == 1


def test_easy_apply_requires_matching_identity():
    assert run(routes={LINKEDIN: httpx.Response(200, text=identity("Other") + '<button>Easy Apply</button>')}) is None


@pytest.mark.parametrize("tag, rule", [
    (f'<a href="{WORKDAY}">Apply</a>', "link"),
    (f'<iframe src="{WORKDAY}"></iframe>', "iframe"),
    ('<script>var config = {"apply_url": "' + WORKDAY.replace('/', r'\/') + '"}</script>', "script_config"),
])
def test_company_career_page_workday(tag, rule):
    result = run(CAREER, {CAREER: httpx.Response(200, text=tag), WORKDAY: httpx.Response(200, text=identity())})
    assert result.destination_url == WORKDAY
    assert result.evidence == rule + ";title+company"


def test_iframe_greenhouse():
    result = run(CAREER, {
        CAREER: httpx.Response(200, text=f'<iframe src="{GREENHOUSE}"></iframe>'),
        GREENHOUSE: httpx.Response(200, text=identity()),
    })
    assert result.ats == "greenhouse"
    assert result.destination_url == "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=11"
    assert result.evidence.startswith("iframe;")


@pytest.mark.parametrize("company,title", [("Other", "Security Engineer"), ("Acme", "Senior Security Engineer"),
                                           ("Acme", "Security Engineer II"), ("Acme", "Accountant")])
def test_identity_mismatch_rejected(company, title):
    assert run(CAREER, {
        CAREER: httpx.Response(200, text=identity() + f'<a href="{WORKDAY}">Apply</a>'),
        WORKDAY: httpx.Response(200, text=identity(company, title)),
    }) is None


def test_identity_missing_is_unresolved():
    assert run(CAREER, {CAREER: httpx.Response(200, text=f'<a href="{WORKDAY}">Apply</a>'),
                        WORKDAY: httpx.Response(200, text="JavaScript required")}) is None


def test_safe_redirect_verified_at_destination():
    result = run(CAREER, {CAREER: httpx.Response(302, headers={"location": WORKDAY}),
                         WORKDAY: httpx.Response(200, text=identity())})
    assert result.destination_url == WORKDAY
    assert "redirect" in result.evidence


def test_redirect_cap():
    seen = []
    routes = {CAREER: httpx.Response(302, headers={"location": "/step/0"})}
    routes.update({f"https://careers.acme.example/step/{i}": httpx.Response(302, headers={"location": f"/step/{i+1}"})
                   for i in range(10)})
    assert run(CAREER, routes, seen) is None
    assert len([r for r in seen if r.url.host == "careers.acme.example"]) == resolve.MAX_REDIRECTS + 1


@pytest.mark.parametrize("target", ["https://evil.example/job", "https://myworkdayjobs.com.evil.example/job",
                                     "https://evilmyworkdayjobs.com/job", "file:///secret", "http://127.0.0.1/",
                                     "https://user:pass@jobs.lever.co/acme/1", "https://jobs.lever.co:8080/acme/1"])
def test_non_allowlisted_destinations_never_requested(target):
    seen = []
    assert run(CAREER, {CAREER: httpx.Response(302, headers={"location": target})}, seen) is None
    assert not any(str(r.url) == target for r in seen)


def test_size_limit_and_no_cookie_or_authorization(monkeypatch):
    monkeypatch.setattr(resolve, "MAX_RESPONSE_BYTES", 100)
    seen = []
    assert run(CAREER, {CAREER: httpx.Response(200, text="x" * 101)}, seen) is None
    assert all("cookie" not in r.headers and "authorization" not in r.headers for r in seen)


def test_board_search_requires_company_and_title_not_guessed_slug():
    board = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"
    embed = "https://job-boards.greenhouse.io/embed/job_app?for=acme&token=11"
    routes = {board: httpx.Response(200, json={"jobs": [{"id": 11, "title": "Security Engineer"}]}),
              embed: httpx.Response(200, text=identity("Other"))}
    assert run(routes=routes) is None
    routes[embed] = httpx.Response(200, text=identity())
    result = run(routes=routes)
    assert result.evidence == "board_search;title+company"


def test_direct_ats_and_wrapper_compatibility():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(404))) as client:
        assert resolve_application_url(WORKDAY, "Acme", "Security Engineer", client=client) == WORKDAY
        assert resolve_application_url("https://evil.example", "Acme", "Security Engineer", client=client) is None


@pytest.mark.parametrize("host,ats", [("jobs.adp.com", "adp"), ("careers.dayforcehcm.com", "dayforce"),
    ("acme.applicantpro.com", "applicantpro"), ("acme.oraclecloud.com", "oracle"),
    ("career.successfactors.eu", "successfactors"), ("jobs.smartrecruiters.com", "smartrecruiters"),
    ("acme.icims.com", "icims"), ("jobs.ashbyhq.com", "ashby"), ("jobs.lever.co", "lever")])
def test_ats_host_detection(host, ats):
    assert detect_source("https://" + host + "/jobs/1") == ats
    assert detect_source("https://" + host + ".evil.example/jobs/1") == "manual_url"


def test_public_validation_applies_to_redirects(monkeypatch):
    def validate(url):
        if "myworkdayjobs.com" in url:
            raise resolve.IntakeError("private DNS")
        return url
    monkeypatch.setattr(resolve, "validate_public_url", validate)
    seen = []
    assert run(CAREER, {CAREER: httpx.Response(302, headers={"location": WORKDAY})}, seen) is None
    assert not any(str(r.url) == WORKDAY for r in seen)


def test_same_company_hop_and_linkedin_safety_wrapper():
    from urllib.parse import quote
    target = "https://acme.example/job/1"
    wrapper = "https://www.linkedin.com/safety/go/?url=" + quote(target, safe="")
    result = run(routes={LINKEDIN: httpx.Response(200, text=f'<a href="{wrapper}">Apply</a>'),
                         target: httpx.Response(200, text=f'<a href="{WORKDAY}">Apply</a>'),
                         WORKDAY: httpx.Response(200, text=identity())})
    assert result.destination_url == WORKDAY


def test_invalid_link_does_not_hide_valid_ats_link():
    result = run(CAREER, {CAREER: httpx.Response(200, text=f'<a href="javascript:evil()">bad</a><a href="{WORKDAY}">Apply</a>'),
                         WORKDAY: httpx.Response(200, text=identity())})
    assert result.destination_url == WORKDAY
