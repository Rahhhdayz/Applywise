"""Bounded static HTTP fetching for untrusted public job pages."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from jobagent.intake.errors import IntakeError
from jobagent.intake.urls import Resolver, validate_public_url

MAX_RESPONSE_BYTES = 2_000_000
MAX_REDIRECTS = 5
USER_AGENT = "JobAgent/0.2 (+https://example.invalid/jobagent)"


@dataclass(frozen=True)
class FetchedPage:
    """A safely bounded static HTML response."""

    submitted_url: str
    final_url: str
    html: str
    redirect_history: tuple[str, ...] = ()


class HttpFetcher:
    """Fetch static HTML while validating every redirect destination."""

    def __init__(
        self,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        self._client = client or httpx.Client(
            follow_redirects=False,
            timeout=httpx.Timeout(15.0, connect=5.0),
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
            trust_env=False,
        )
        self._resolver = resolver
        self._max_response_bytes = max_response_bytes

    def fetch(self, submitted_url: str) -> FetchedPage:
        """Fetch a public HTML document without JavaScript execution."""
        current_url = self._validate(submitted_url)
        history: list[str] = []
        for _ in range(MAX_REDIRECTS + 1):
            try:
                with self._client.stream(
                    "GET",
                    current_url,
                    headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"},
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise IntakeError("Redirect response did not include a location")
                        history.append(current_url)
                        current_url = self._validate(str(response.url.join(location)))
                        continue
                    try:
                        response.raise_for_status()
                    except httpx.HTTPStatusError as error:
                        raise IntakeError(f"HTTP request failed with status {error.response.status_code}") from error
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type and "html" not in content_type:
                        raise IntakeError("URL did not return an HTML document")
                    content = self._read_limited(response)
                    encoding = response.encoding or "utf-8"
                    return FetchedPage(
                        submitted_url=submitted_url,
                        final_url=self._validate(str(response.url)),
                        html=content.decode(encoding, errors="replace"),
                        redirect_history=tuple(history),
                    )
            except IntakeError:
                raise
            except httpx.TimeoutException as error:
                raise IntakeError("HTTP request timed out") from error
            except httpx.RequestError as error:
                raise IntakeError("HTTP request failed") from error
        raise IntakeError("Too many redirects")

    def _validate(self, url: str) -> str:
        if self._resolver is None:
            return validate_public_url(url)
        return validate_public_url(url, resolver=self._resolver)

    def _read_limited(self, response: httpx.Response) -> bytes:
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > self._max_response_bytes:
                raise IntakeError("HTML response exceeded the maximum allowed size")
            chunks.append(chunk)
        return b"".join(chunks)
