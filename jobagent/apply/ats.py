"""A tiny per-provider table mapping a canonical job to its application form.

Deliberately not a class hierarchy: each supported ATS needs only a URL rule and
two CSS selectors, so a plain dataclass table is the whole abstraction this needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from jobagent.db import StoredJob
from jobagent.intake.urls import detect_source


@dataclass(frozen=True)
class ApplicationForm:
    """Where to find one job's application form and its key elements."""

    provider: str
    form_url: str
    form_selector: str
    resume_input_selector: str
    # Workday-style wizards: the human signs in and clicks Next; each new step is filled as it appears.
    multi_page: bool = False


WORKDAY_DOMAINS = ("myworkdayjobs.com", "myworkday.com")


def is_workday_url(url: str) -> bool:
    """https only, and the host is a Workday domain or a subdomain of one ("evilmyworkday.com" is not)."""
    parts = urlsplit(url)
    host = (parts.hostname or "").casefold()
    return parts.scheme == "https" and any(host == domain or host.endswith("." + domain) for domain in WORKDAY_DOMAINS)


_FORM_ROOT_SELECTOR = "form"
_RESUME_INPUT_SELECTOR = "input[type=file]"


def _greenhouse_url(job: StoredJob) -> str:
    return job.resolved_destination_url or job.apply_url or job.canonical_url or ""


def _icims_url(job: StoredJob) -> str:
    """iCIMS renders its forms in an iframe; open the iframe's own page so the form is the top frame."""
    parts = urlsplit(job.resolved_destination_url or job.apply_url or job.canonical_url or "")
    query = "&".join(item for item in parts.query.split("&") if item and not item.startswith("in_iframe="))
    return urlunsplit(parts._replace(query=f"{query}&in_iframe=1" if query else "in_iframe=1"))


def _lever_url(job: StoredJob) -> str:
    parts = urlsplit(job.resolved_destination_url or job.apply_url or job.canonical_url or "")
    path = parts.path.rstrip("/")
    return urlunsplit(parts._replace(path=path if path.endswith("/apply") else f"{path}/apply"))


def _ashby_url(job: StoredJob) -> str:
    parts = urlsplit(job.resolved_destination_url or job.apply_url or job.canonical_url or "")
    path = parts.path.rstrip("/")
    return urlunsplit(parts._replace(path=path if path.endswith("/application") else f"{path}/application"))


def _workable_url(job: StoredJob) -> str:
    """Workable's posting page links to a single-page form at <posting>/apply/."""
    parts = urlsplit(job.resolved_destination_url or job.apply_url or job.canonical_url or "")
    path = parts.path.rstrip("/")
    return urlunsplit(parts._replace(path=f"{path if path.endswith('/apply') else path + '/apply'}/", query=""))


def _rippling_url(job: StoredJob) -> str:
    base = (job.resolved_destination_url or job.apply_url or job.canonical_url or "").split("?")[0].rstrip("/")
    base = base if base.endswith("/apply") else f"{base}/apply"
    return f"{base}?step=application"


_URL_BUILDERS = {
    "greenhouse": _greenhouse_url,
    "lever": _lever_url,
    "ashby": _ashby_url,
    "workday": _greenhouse_url,
    "jazzhr": _greenhouse_url,  # the posting URL is the application form
    "rippling": _rippling_url,
    "successfactors": _greenhouse_url,  # the SuccessFactors job_application URL (sign-in first)
    "eightfold": _greenhouse_url,  # posting page; Master clicks "Apply Now" and the form fills as it opens
    "oracle": _greenhouse_url,  # Oracle Cloud posting; Master clicks Apply and enters the emailed code
    "trakstar": _greenhouse_url,  # Trakstar Hire "?apply=true" page is a plain single-page form
    "icims": _icims_url,  # Master signs in (hCaptcha is his); each step fills as it appears
    "munis": _greenhouse_url,  # Tyler Munis posting; Master clicks Apply and signs in/up first
    "workable": _workable_url,  # React single-page form; fields render after load
}


def application_form(job: StoredJob) -> ApplicationForm | None:
    """Return the application form for one job, or None for an unsupported ATS."""
    if job.resolved_ats == "linkedin_easy_apply":
        return None
    provider = detect_source(job.resolved_destination_url or job.apply_url or job.canonical_url or "")
    builder = _URL_BUILDERS.get(provider)
    if builder is None:
        return None
    return ApplicationForm(
        provider=provider,
        form_url=builder(job),
        form_selector="body" if provider in {"workday", "ashby", "workable", "successfactors", "eightfold", "oracle", "munis", "icims"}
        else "#job_application_form" if provider == "trakstar"  # its first <form> is a hidden Indeed-apply stub
        else _FORM_ROOT_SELECTOR,
        resume_input_selector=_RESUME_INPUT_SELECTOR,
        multi_page=provider in {"workday", "successfactors", "eightfold", "oracle", "munis", "icims"},
    )
