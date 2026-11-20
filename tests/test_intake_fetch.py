import socket

import httpx
import pytest

from jobagent.intake import IntakeError
from jobagent.intake.fetch import HttpFetcher


def public_resolver(host: str, port: int) -> list[tuple[object, ...]]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def fetcher_for(handler, **kwargs):  # type: ignore[no-untyped-def]
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return HttpFetcher(client=client, resolver=public_resolver, **kwargs)


def test_fetcher_accepts_bounded_html_and_sets_user_agent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["user-agent"].startswith("JobAgent/")
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<h1>Job</h1>")

    page = fetcher_for(handler).fetch("https://jobs.example.com/opening")

    assert page.html == "<h1>Job</h1>"


def test_fetcher_rejects_non_html_and_oversized_responses() -> None:
    def pdf_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf")

    with pytest.raises(IntakeError, match="HTML"):
        fetcher_for(pdf_handler).fetch("https://jobs.example.com/opening")

    def large_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html"}, content=b"too large")

    with pytest.raises(IntakeError, match="maximum"):
        fetcher_for(large_handler, max_response_bytes=2).fetch("https://jobs.example.com/opening")


def test_fetcher_validates_redirect_destinations() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://localhost/private"})

    with pytest.raises(IntakeError, match="Local hostnames"):
        fetcher_for(handler).fetch("https://jobs.example.com/opening")


def test_fetcher_converts_http_errors_to_safe_messages() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "text/html"})

    with pytest.raises(IntakeError, match="status 404"):
        fetcher_for(handler).fetch("https://jobs.example.com/missing")
