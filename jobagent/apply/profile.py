"""Validated applicant answers used to pre-fill ATS application forms.

This is factual configuration, not application code, mirroring ``jobagent.candidate``:
a local ``candidate/application.yaml`` overrides the checked-in synthetic example.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from jobagent.normalization import normalize_title


class ApplicationConfigurationError(ValueError):
    """An actionable application-profile configuration problem."""


class ProfileModel(BaseModel):
    """Reject undeclared configuration so configuration mistakes are visible."""

    model_config = ConfigDict(extra="forbid")


class ContactInfo(ProfileModel):
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    phone_device_type: str = ""  # Workday "Phone Device Type" (Master, 2026-11-19: Mobile)
    city: str = ""
    state: str = ""
    postal_code: str = ""
    county: str = ""
    country: str = ""

    @field_validator("first_name", "last_name", "email")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class Links(ProfileModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class WorkAuthorization(ProfileModel):
    """Exact-text answers for common yes/no work-authorization questions."""

    authorized_us: str = "Yes"
    requires_sponsorship: str = "No"
    # Follow-up "which answer most accurately fits your situation" (Master, 2026-11-19, Northwind)
    status: str = ""


class Screening(ProfileModel):
    """Screening answers Master gave on Northwind #52001 (2026-11-19), matched by meaning, not wording."""

    clearance: str = "None | Not Required | No Clearance | N/A"  # never claim a clearance
    travel_max_percent: int = 50  # "Up to 50%", or the nearest option at or below it
    former_government: str = "No"  # former/current US federal civilian or military employment (not EEO self-ID)


class EeoAnswers(ProfileModel):
    """Voluntary demographic questions; ``decline`` matches a "prefer not" option."""

    gender: str = "decline"
    hispanic: str = "decline"
    race: str = "decline"
    veteran: str = "decline"
    disability: str = "decline"


class CannedAnswer(ProfileModel):
    """One exact-intent answer applied when every phrase in ``match`` appears in a label."""

    match: list[str] = Field(min_length=1)
    value: str
    company: str = ""  # only on this company's forms (e.g. a "why us" answer); blank = every form

    @field_validator("match")
    @classmethod
    def phrases_are_lowercase_and_non_blank(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip().casefold() for value in values if value.strip()]
        if not cleaned:
            raise ValueError("match must contain non-blank phrases")
        return cleaned


class EmploymentRef(ProfileModel):
    """Which experience.yaml entry is a job, plus the one fact the truth files lack: where."""

    id: str
    location: str = ""


class EducationEntry(ProfileModel):
    school: str
    school_search: str = ""  # Workday school-list names to try first: "Riverside University -Boston | ..."
    degree: str  # may list alternatives: "Master's Degree | Masters"
    field_of_study: str = ""  # alternatives in preference order: "Cybersecurity | Information Security"
    gpa: str = ""
    start_year: str = ""  # YYYY; blank when not on file
    end_year: str = ""  # YYYY (actual or expected)


class CertificationEntry(ProfileModel):
    """One certification (Master, 2026-11-19). Dates are MM/DD/YYYY; blank fields are left blank on forms."""

    name: str
    number: str = ""
    exam_date: str = ""
    issued_date: str = ""
    expiration_date: str = ""
    score: str = ""  # left blank on forms unless set
    file: str = ""  # certificate PDF, attached at most once where a form asks for it


class WorkEntry(ProfileModel):
    """One job resolved from candidate/experience.yaml; never typed by hand."""

    title: str
    company: str
    location: str
    start: str  # YYYY-MM
    end: str | None
    description: str


class ApplicationProfile(ProfileModel):
    contact: ContactInfo
    links: Links = Field(default_factory=Links)
    work_authorization: WorkAuthorization = Field(default_factory=WorkAuthorization)
    screening: Screening = Field(default_factory=Screening)
    eeo: EeoAnswers = Field(default_factory=EeoAnswers)
    answers: list[CannedAnswer] = Field(default_factory=list)
    employment: list[EmploymentRef] = Field(default_factory=list)  # newest first
    education: list[EducationEntry] = Field(default_factory=list)
    certifications: list[CertificationEntry] = Field(default_factory=list)
    work_history: list[WorkEntry] = Field(default_factory=list, exclude=True)
    # ATS candidate-account password (Workday). Secret: only ever from $WORKDAY_PASSWORD, never YAML.
    account_password: str = Field(default="", repr=False, exclude=True)

    def for_company(self, company: str) -> ApplicationProfile:
        """Drop canned answers written for a different company."""
        keep = [a for a in self.answers if not a.company or a.company.casefold() == company.strip().casefold()]
        return self.model_copy(update={"answers": keep})
    # Organizations from candidate/experience.yaml, for "Have you previously been employed by X?"
    past_employers: list[str] = Field(default_factory=list, exclude=True)
    # Every skill in candidate/skills.yaml: canonical name first, then its aliases (search fallbacks).
    skills: list[list[str]] = Field(default_factory=list, exclude=True)


def load_application_profile(directory: str | Path = "candidate") -> ApplicationProfile:
    """Load the local profile, falling back to the checked-in synthetic example."""
    root = Path(directory)
    path = root / "application.yaml"
    if not path.exists():
        path = root / "application.example.yaml"
    if not path.exists():
        raise ApplicationConfigurationError(f"Missing application configuration: {root / 'application.yaml'}")
    try:
        document = yaml.safe_load(path.read_text()) or {}
    except (OSError, yaml.YAMLError) as error:
        raise ApplicationConfigurationError(f"Could not read {path}: {error}") from error
    try:
        document.pop("account_password", None)
        for derived in ("past_employers", "work_history", "skills"):
            document.pop(derived, None)
        profile = ApplicationProfile.model_validate(document)
        employers, history, skills = _from_truth(root, profile.employment)
        return profile.model_copy(
            update={
                "account_password": os.environ.get("WORKDAY_PASSWORD", ""),
                "past_employers": employers,
                "work_history": history,
                "skills": skills,
            }
        )
    except ValidationError as error:
        raise ApplicationConfigurationError(f"Invalid application configuration: {error}") from error


def _from_truth(root: Path, employment: list[EmploymentRef]) -> tuple[list[str], list[WorkEntry], list[list[str]]]:
    """Past employers, job entries, and skills, straight from the candidate truth files."""
    from jobagent.candidate import CandidateConfigurationError, load_candidate_truth

    try:
        truth = load_candidate_truth(root)
    except (CandidateConfigurationError, ValueError, OSError):
        return [], [], []
    employers = [item.organization for item in truth.experiences
                 if item.kind == "employment" and not item.organization.lower().startswith("personal")]
    by_id = {item.id: item for item in truth.experiences}
    history = []
    for ref in employment:
        item = by_id.get(ref.id)
        if item is None:
            raise ApplicationConfigurationError(f"employment id {ref.id!r} is not in experience.yaml")
        if item.kind != "employment":
            raise ApplicationConfigurationError(f"employment id {ref.id!r} is a {item.kind}, not a job")
        history.append(
            WorkEntry(
                title=item.title,
                company=item.organization,
                location=ref.location,
                start=item.start_date,
                end=item.end_date,
                description="\n".join(f"• {fact.text}" for fact in item.facts),
            )
        )
    skills, seen = [], set()
    for group in truth.skills.values():  # one entry per skill: a name listed twice must never be added twice
        for skill in group:
            forms = {normalize_title(name): name for name in reversed([skill.canonical_name, *skill.aliases])}
            names = [name for form, name in reversed(forms.items()) if form not in seen]
            if names and names[0] == skill.canonical_name:
                skills.append(names)
            seen.update(normalize_title(name) for name in names)
    return employers, history, skills
