"""Headless-browser fallback for job pages that block plain HTTP or render with JavaScript."""

from __future__ import annotations

from functools import cache
from urllib.parse import urlsplit

from jobagent.intake.errors import IntakeError
from jobagent.intake.fetch import MAX_RESPONSE_BYTES, FetchedPage
from jobagent.intake.urls import validate_public_url

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
_SKIPPED_RESOURCES = {"image", "media", "font"}


@cache
def _public_origin(scheme: str, host: str) -> bool:
    try:
        validate_public_url(f"{scheme}://{host}/")
    except IntakeError:
        return False
    return True


class BrowserFetcher:
    """Render a page in a throwaway headless Chromium; never the persistent pre-fill profile."""

    def __init__(self, *, timeout_ms: int = 45_000, settle_ms: int = 5_000) -> None:
        self._timeout_ms = timeout_ms
        self._settle_ms = settle_ms

    def fetch(self, submitted_url: str) -> FetchedPage:
        """Return the rendered HTML; the response status is ignored (WAFs send 403 with real content)."""
        from playwright.sync_api import Error, Route, sync_playwright

        url = validate_public_url(submitted_url)

        def guard(route: Route) -> None:  # page JavaScript must not reach private or local hosts
            parts = urlsplit(route.request.url)
            blocked = route.request.resource_type in _SKIPPED_RESOURCES or (
                parts.scheme in {"http", "https"} and not _public_origin(parts.scheme, parts.netloc)
            )
            route.abort() if blocked else route.continue_()

        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    context = browser.new_context(user_agent=BROWSER_USER_AGENT, viewport={"width": 1366, "height": 900})
                    context.route("**/*", guard)
                    page = context.new_page()
                    page.goto(url, wait_until="domcontentloaded", timeout=self._timeout_ms)
                    page.wait_for_timeout(self._settle_ms)
                    html, final_url = page.content(), page.url
                finally:
                    browser.close()
        except Error as error:
            raise IntakeError(f"Browser fetch failed: {str(error).splitlines()[0][:200]}") from error
        if len(html.encode()) > MAX_RESPONSE_BYTES:
            raise IntakeError("HTML response exceeded the maximum allowed size")
        return FetchedPage(submitted_url=submitted_url, final_url=validate_public_url(final_url), html=html)
