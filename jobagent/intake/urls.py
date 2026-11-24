"""URL validation, canonicalization, and deterministic source detection."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jobagent.intake.errors import IntakeError

Resolver = Callable[[str, int], list[tuple[object, ...]]]
_TRACKING_PARAMETERS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
    "afd_azwaf_tok",  # Azure Front Door WAF challenge token appended on browser redirect
}


def canonicalize_url(url: str) -> str:
    """Return a stable URL while preserving unknown, potentially meaningful query data."""
    try:
        parsed = urlsplit(url.strip())
    except ValueError as error:
        raise IntakeError("URL is malformed") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise IntakeError("Unsupported URL scheme; only http and https URLs are allowed")
    if not parsed.hostname:
        raise IntakeError("URL must include a hostname")
    if parsed.username or parsed.password:
        raise IntakeError("URLs with embedded credentials are not allowed")
    scheme = parsed.scheme.lower()
    hostname = parsed.hostname.lower()
    try:
        port_number = parsed.port
    except ValueError as error:
        raise IntakeError("URL has an invalid port") from error
    port = f":{port_number}" if port_number is not None else ""
    netloc = f"{hostname}{port}"
    query = urlencode(
        [(key, value) for key, value in parse_qsl(parsed.query, keep_blank_values=True) if key.lower() not in _TRACKING_PARAMETERS],
        doseq=True,
    )
    return urlunsplit((scheme, netloc, parsed.path or "/", query, ""))


def validate_public_url(url: str, *, resolver: Resolver = socket.getaddrinfo) -> str:
    """Reject non-public network targets before an HTTP request is issued."""
    canonical_url = canonicalize_url(url)
    hostname = urlsplit(canonical_url).hostname
    if hostname is None:  # Defensive; canonicalize_url already verifies this.
        raise IntakeError("URL must include a hostname")
    if hostname.lower() == "localhost" or hostname.lower().endswith(".localhost"):
        raise IntakeError("Local hostnames are not allowed")

    try:
        addresses = resolver(hostname, urlsplit(canonical_url).port or 443)
    except (OSError, ValueError, socket.gaierror) as error:
        raise IntakeError("Could not resolve URL hostname") from error
    if not addresses:
        raise IntakeError("Could not resolve URL hostname")
    for address in addresses:
        ip_text = str(address[4][0])
        try:
            ip = ipaddress.ip_address(ip_text)
        except ValueError as error:
            raise IntakeError("Hostname resolved to an invalid address") from error
        if not ip.is_global:
            raise IntakeError("URLs resolving to private or local networks are not allowed")
    return canonical_url


def same_origin(first: str, second: str) -> bool:
    """Return whether two absolute URLs have the same scheme, host, and port."""
    first_parts = urlsplit(first)
    second_parts = urlsplit(second)
    return (
        first_parts.scheme.lower(),
        first_parts.hostname.lower() if first_parts.hostname else None,
        first_parts.port,
    ) == (
        second_parts.scheme.lower(),
        second_parts.hostname.lower() if second_parts.hostname else None,
        second_parts.port,
    )


def detect_ats_host(url: str) -> str:
    """Recognize ATS hostnames without calling their APIs."""
    hostname = (urlsplit(url).hostname or "").lower()
    source_hosts = {
        "greenhouse.io": "greenhouse",
        "lever.co": "lever",
        "ashbyhq.com": "ashby",
        "myworkdayjobs.com": "workday",
        "myworkday.com": "workday",
        "successfactors.eu": "successfactors",
        "adp.com": "adp",
        "dayforcehcm.com": "dayforce",
        "dayforce.com": "dayforce",
        "applicantpro.com": "applicantpro",
        "smartrecruiters.com": "smartrecruiters",
        "applytojob.com": "jazzhr",
        "rippling.com": "rippling",
        "successfactors.com": "successfactors",
        "eightfold.ai": "eightfold",
        "oraclecloud.com": "oracle",
        "munisselfservice.com": "munis",
        "hire.trakstar.com": "trakstar",
        "icims.com": "icims",
        "apply.workable.com": "workable",
    }
    for suffix, source in source_hosts.items():
        if hostname == suffix or hostname.endswith(f".{suffix}"):
            return source
    return "manual_url"


def detect_source(url: str) -> str:
    """Detect an ATS, including known custom-domain path conventions."""
    source = detect_ats_host(url)
    if source != "manual_url":
        return source
    if urlsplit(url).path.startswith("/talentcommunity/apply/"):
        return "successfactors"  # SuccessFactors career sites on a company's own domain
    return "manual_url"
