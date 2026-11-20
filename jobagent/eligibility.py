"""Conservative, explainable deterministic eligibility checks."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from jobagent.candidate.models import CandidateTruth
from jobagent.db import JobRepository, StoredJob
from jobagent.regions import is_international, matches_any

RULES_VERSION = "m6-eligibility-v6"
_YEAR_PATTERNS = (
    (re.compile(r"(?:preferred|ideally)\s+(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+)?\s+years?", re.IGNORECASE), "preferred"),
    (re.compile(r"(?:minimum|at least|must have|require[sd]?)\s+(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+)?\s+years?", re.IGNORECASE), "required"),
    (re.compile(r"(\d+(?:\.\d+)?)\s*(?:\+|[-–]\s*\d+)?\s+years?\s+(?:of )?(?:experience|professional experience)", re.IGNORECASE), "required"),
)
_CLEARANCE_REQUIRED = re.compile(
    r"(?:active\s+)?(?:top secret|ts/sci|secret)\s+(?:security\s+)?clearance\s+(?:is\s+)?required|"
    r"security\s+clearance\s+(?:is\s+)?required|must\s+(?:hold|have|possess)\s+(?:an?\s+)?(?:active\s+)?(?:secret|top secret|ts/sci)|"
    r"active\s+(?:top secret|ts/sci|secret)(?:/sci)?\s+(?:security\s+)?clearance",
    re.IGNORECASE,
)
_CLEARANCE_AMBIGUOUS = re.compile(r"(?:ability to obtain|eligible to obtain|clearance preferred)", re.IGNORECASE)
_CITIZENSHIP = re.compile(
    r"(?:u\.?s\.?\s+citizens?\s+only|must be (?:a )?u\.?s\.?\s+citizen|citizenship required|"
    r"no sponsorship available|will not sponsor)",
    re.IGNORECASE,
)

# International jobs only: a US-only work authorization counts for nothing abroad, so a role open only to people
# who already hold local citizenship or work rights (or that won't sponsor) is out. US jobs keep the
# _CITIZENSHIP rule above.
_LOCAL_WORK_RIGHTS = re.compile(
    r"(?:australian\s+citizens?(?:hip)?(?:\s+(?:or|and|/)\s+(?:permanent\s+residen(?:t|cy|ts)|pr))?\s+(?:is\s+|are\s+)?(?:only|required|essential|mandatory)|"
    r"must\s+be\s+an?\s+australian\s+citizen|"
    r"(?:citizens?|permanent\s+residents?)\s+(?:only|of\s+australia\s+only)|"
    r"must\s+(?:have|hold|possess)\s+(?:full\s+|unrestricted\s+|existing\s+|current\s+|valid\s+)*"
    r"(?:australian\s+)?(?:working\s+rights|work(?:ing)?\s+rights|right\s+to\s+work)"
    r"(?:\s+in\s+(?:australia|france|the\s+eu|europe))?|"
    r"(?:eu|european)\s+work\s+(?:permit|authori[sz]ation)\s+(?:is\s+)?required|"
    r"no\s+visa\s+sponsorship|"
    r"nationalit[ée]\s+fran[çc]aise\s+(?:exig[ée]e|requise|obligatoire)|"
    r"(?:autorisation|permis)\s+de\s+travail\s+(?:en\s+france\s+|europ[ée]en\s+)?(?:exig[ée]e?|requis[e]?|obligatoire))",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class EligibilityResult:
    job_id: int
    status: str
    reasons: list[str]
    flags: list[str]
    detected_requirements: dict[str, object]
    rules_version: str
    rules_fingerprint: str
    evaluated_at: datetime


def rules_fingerprint(candidate: CandidateTruth) -> str:
    """Fingerprint only deterministic rules and candidate preferences that affect them."""
    payload = json.dumps(candidate.preferences.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{RULES_VERSION}:{payload}".encode()).hexdigest()


def parse_years_requirement(text: str) -> tuple[float, str] | None:
    """Return a conservative minimum and mandatory/preferred classification."""
    for pattern, kind in _YEAR_PATTERNS:
        match = pattern.search(text)
        if match:
            # A later explicit preferred qualifier must not become a hard requirement.
            preceding = text[max(0, match.start() - 30) : match.end() + 30].lower()
            if "preferred" in preceding or "nice to have" in preceding:
                return float(match.group(1)), "preferred"
            return float(match.group(1)), kind
    return None


class EligibilityEngine:
    """Run deterministic checks without guessing from incomplete source data."""

    def evaluate(self, job: StoredJob, candidate: CandidateTruth, *, now: datetime | None = None) -> EligibilityResult:
        now = now or datetime.now(UTC)
        text = f"{job.title}\n{job.description_text or ''}"
        reasons: list[str] = []
        flags: list[str] = []
        detected: dict[str, object] = {}
        hard_reject = False

        allowed = {value.lower().replace("-", "_") for value in candidate.preferences.employment_types}
        if job.employment_type:
            employment = job.employment_type.lower().replace("-", "_")
            detected["employment_type"] = employment
            if allowed and employment not in allowed:
                hard_reject = True
                reasons.append(f"Employment type '{job.employment_type}' conflicts with preferences")
        else:
            flags.append("employment_type_unknown")

        # A URL Master submitted himself (add-url) is a deliberate pick: the discovery-pruning filters
        # (required title words, location) don't apply; legal and seniority checks still do.
        hand_picked = bool((job.source_metadata or {}).get("submitted_url"))
        required_terms = candidate.preferences.required_title_terms
        if required_terms and not hand_picked and not any(
            re.search(rf"\b{re.escape(term.lower())}\b", job.title.lower()) for term in required_terms
        ):
            hard_reject = True
            reasons.append("Title contains none of the required title terms")

        lower_title = job.title.lower()
        blocked = [term for term in candidate.preferences.blocked_title_terms if term.lower() in lower_title]
        if blocked:
            hard_reject = True
            detected["blocked_title_terms"] = blocked
            reasons.append(f"Blocked seniority term in title: {blocked[0]}")

        blocked_company = [name for name in candidate.preferences.blocked_companies if name.lower() in job.company.lower()]
        if blocked_company:
            hard_reject = True
            reasons.append(f"Blocked company: {blocked_company[0]}")

        preferred = candidate.preferences.preferred_locations
        if preferred and not hand_picked:
            location = (job.location or "").strip()
            if matches_any(location, ["remote"]):
                if not candidate.preferences.remote_us:
                    hard_reject = True
                    reasons.append("Remote job but remote_us is off")
            elif location.lower() in {"", "us", "usa", "united states"}:
                flags.append("location_unknown")
            elif not matches_any(location, preferred):
                hard_reject = True
                reasons.append(f"Location '{location}' is not a preferred location")

        years = parse_years_requirement(text)
        if years:
            minimum, requirement_kind = years
            detected["minimum_years_detected"] = minimum
            detected["years_requirement_kind"] = requirement_kind
            if requirement_kind == "required" and minimum > candidate.preferences.eligibility.max_required_years:
                hard_reject = True
                reasons.append(f"Requires {minimum:g}+ years; configured maximum is {candidate.preferences.eligibility.max_required_years:g}")
            elif requirement_kind == "preferred":
                flags.append("preferred_years_above_target")

        clearance = _CLEARANCE_REQUIRED.search(text)
        if clearance:
            detected["clearance_text"] = clearance.group(0)
            if candidate.preferences.eligibility.reject_clearance_required:
                hard_reject = True
                reasons.append("Explicit security clearance requirement")
            else:
                flags.append("clearance_required")
        elif _CLEARANCE_AMBIGUOUS.search(text):
            flags.append("clearance_ambiguous")

        citizenship = _CITIZENSHIP.search(text)
        if citizenship:
            detected["work_authorization_text"] = citizenship.group(0)
            lowered = citizenship.group(0).lower()
            reject = (
                ("sponsor" in lowered and candidate.preferences.eligibility.reject_no_sponsorship)
                or ("sponsor" not in lowered and candidate.preferences.eligibility.reject_explicit_citizenship_requirement)
            )
            if reject:
                hard_reject = True
                reasons.append("Explicit work-authorization requirement conflicts with configuration")
            else:
                flags.append("work_authorization_review")

        local = _LOCAL_WORK_RIGHTS.search(text) if is_international(job) else None
        if local:
            detected["local_work_rights_text"] = local.group(0)
            if candidate.preferences.eligibility.reject_explicit_citizenship_requirement:
                hard_reject = True
                reasons.append("Explicit local citizenship or work-rights requirement")
            else:
                flags.append("work_authorization_review")

        if job.posted_at:
            age_days = max(0, (now - job.posted_at.astimezone(UTC)).days)
            detected["age_days"] = age_days
        if job.has_active_observation is False:
            hard_reject = True
            reasons.append("Every trusted source observation is inactive")
        elif job.has_active_observation is None:
            flags.append("source_activity_unknown")

        if hard_reject:
            status = "ineligible"
        elif flags:
            status = "review"
        else:
            status = "eligible"
        if not reasons:
            reasons.append("No deterministic conflict detected")
        return EligibilityResult(
            job_id=job.id,
            status=status,
            reasons=reasons,
            flags=flags,
            detected_requirements=detected,
            rules_version=RULES_VERSION,
            rules_fingerprint=rules_fingerprint(candidate),
            evaluated_at=now,
        )


def refresh_eligibility(
    repository: JobRepository, candidate: CandidateTruth, jobs: list[StoredJob]
) -> list[EligibilityResult]:
    """Evaluate and persist eligibility for each job under the current rules."""
    engine = EligibilityEngine()
    results = []
    for job in jobs:
        result = engine.evaluate(job, candidate)
        repository.upsert_eligibility(
            job_id=result.job_id,
            status=result.status,
            reasons=result.reasons,
            flags=result.flags,
            detected_requirements=result.detected_requirements,
            rules_version=result.rules_version,
            rules_fingerprint=result.rules_fingerprint,
        )
        results.append(result)
    return results
