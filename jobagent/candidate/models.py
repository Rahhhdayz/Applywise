"""Pydantic models for factual candidate data; these are the truth boundary."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TruthModel(BaseModel):
    """Reject undeclared configuration so configuration mistakes are visible."""

    model_config = ConfigDict(extra="forbid")


class CandidateProfile(TruthModel):
    candidate_id: str
    display_name: str
    education_level: str | None = None
    professional_years: float = Field(ge=0, le=80)
    internship_years: float = Field(default=0, ge=0, le=40)
    work_authorization: str | None = None
    remote_preference: Literal["required", "preferred", "open"] = "open"
    relocation_willing: bool = False
    preferred_locations: list[str] = Field(default_factory=list)
    master_resume_path: str | None = None
    reference_paths: list[str] = Field(default_factory=list)

    @field_validator("candidate_id", "display_name")
    @classmethod
    def non_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class Skill(TruthModel):
    canonical_name: str
    aliases: list[str] = Field(default_factory=list)
    confidence: Literal["familiar", "working", "strong"] | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @field_validator("canonical_name")
    @classmethod
    def skill_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("skill name must not be blank")
        return value.strip()


class ExperienceFact(TruthModel):
    id: str
    text: str
    skills: list[str] = Field(default_factory=list)

    @field_validator("id", "text")
    @classmethod
    def fact_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class Experience(TruthModel):
    id: str
    organization: str
    title: str
    start_date: str
    end_date: str | None = None
    # employment | competition | project. Only employment may go into an application's work history.
    kind: Literal["employment", "competition", "project"] = Field(default="employment", exclude=True)  # not in fingerprint
    facts: list[ExperienceFact] = Field(default_factory=list)

    @field_validator("id", "organization", "title", "start_date")
    @classmethod
    def experience_text_not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()


class EligibilityPreferences(TruthModel):
    max_required_years: float = Field(default=3, ge=0, le=80)
    reject_clearance_required: bool = True
    reject_explicit_citizenship_requirement: bool = False
    reject_no_sponsorship: bool = False


class DailyQueueSettings(TruthModel):
    minimum_target: int = Field(default=15, ge=1, le=100)
    target: int = Field(default=18, ge=1, le=100)
    maximum_target: int = Field(default=20, ge=1, le=100)
    # Slots of maximum_target reserved for jobs outside the US; the US gets the rest. 0 = one shared queue.
    international_maximum: int = Field(default=0, ge=0, le=100)
    posted_within_hours: int | None = Field(default=None, ge=1)  # set: queue only jobs posted this recently

    @model_validator(mode="after")
    def target_order_is_valid(self) -> DailyQueueSettings:
        if not self.minimum_target <= self.target <= self.maximum_target:
            raise ValueError("daily queue requires minimum_target <= target <= maximum_target")
        if self.international_maximum > self.maximum_target:
            raise ValueError("daily queue requires international_maximum <= maximum_target")
        return self


class RankingSettings(TruthModel):
    semantic_score_weight: float = Field(default=0.8, ge=0, le=1)
    freshness_weight: float = Field(default=0.1, ge=0, le=1)
    role_priority_weight: float = Field(default=0.05, ge=0, le=1)
    location_weight: float = Field(default=0.05, ge=0, le=1)
    early_career_weight: float = Field(default=0.0, ge=0, le=1)
    recent_posting_weight: float = Field(default=0.0, ge=0, le=1)
    recent_posting_hours: int = Field(default=24, ge=1, le=720)
    minimum_ai_score: int = Field(default=75, ge=0, le=100)
    exceptional_score: int = Field(default=95, ge=0, le=100)
    max_jobs_per_run: int = Field(default=100, ge=1, le=1000)

    @model_validator(mode="after")
    def weights_are_sane(self) -> RankingSettings:
        total = sum(
            (
                self.semantic_score_weight,
                self.freshness_weight,
                self.role_priority_weight,
                self.location_weight,
                self.early_career_weight,
                self.recent_posting_weight,
            )
        )
        if not 0.99 <= total <= 1.01:
            raise ValueError("ranking weights must sum to 1.0")
        return self


class Preferences(TruthModel):
    primary_roles: list[str] = Field(default_factory=list)
    secondary_roles: list[str] = Field(default_factory=list)
    blocked_title_terms: list[str] = Field(
        default_factory=lambda: ["Senior", "Staff", "Principal", "Lead", "Manager", "Director", "VP"]
    )
    employment_types: list[str] = Field(default_factory=lambda: ["full_time"])
    # A job is only worth scoring if its title contains one of these whole words
    # (case-insensitive). Empty means no title filter.
    required_title_terms: list[str] = Field(default_factory=list)
    # Whole words/phrases in a title that mark an early-career role; boosts ranking only.
    early_career_terms: list[str] = Field(default_factory=list)
    remote_us: bool = True
    relocation: bool = False
    # Whole words/phrases (e.g. "Austin, TX", "CA"). Non-empty makes it a hard filter: jobs
    # elsewhere are ineligible; remote jobs pass when remote_us is set.
    preferred_locations: list[str] = Field(default_factory=list)
    # Companies never suggested, matched case-insensitively against the company name.
    blocked_companies: list[str] = Field(default_factory=list)
    eligibility: EligibilityPreferences = Field(default_factory=EligibilityPreferences)
    daily_queue: DailyQueueSettings = Field(default_factory=DailyQueueSettings)
    scoring: RankingSettings = Field(default_factory=RankingSettings)


AI_PREFERENCE_FIELDS = frozenset({"primary_roles", "secondary_roles", "remote_us", "relocation", "preferred_locations"})


class CandidateTruth(TruthModel):
    profile: CandidateProfile
    experiences: list[Experience]
    skills: dict[str, list[Skill]]
    preferences: Preferences

    @field_validator("skills", mode="before")
    @classmethod
    def allow_simple_skill_lists(cls, value: object) -> object:
        """Accept the compact YAML form (`- Python`) as well as rich skill objects."""
        if not isinstance(value, dict):
            return value
        converted: dict[str, object] = {}
        for category, skills in value.items():
            if isinstance(skills, list):
                converted[category] = [
                    {"canonical_name": skill} if isinstance(skill, str) else skill for skill in skills
                ]
            else:
                converted[category] = skills
        return converted

    @model_validator(mode="after")
    def identifiers_are_unique(self) -> CandidateTruth:
        experience_ids = [item.id for item in self.experiences]
        if len(experience_ids) != len(set(experience_ids)):
            raise ValueError("experience IDs must be unique")
        fact_ids = [fact.id for item in self.experiences for fact in item.facts]
        if len(fact_ids) != len(set(fact_ids)):
            raise ValueError("evidence fact IDs must be unique")
        return self

    @property
    def evidence_ids(self) -> set[str]:
        return {fact.id for item in self.experiences for fact in item.facts}

    @property
    def skill_count(self) -> int:
        return sum(len(items) for items in self.skills.values())

    def ai_payload(self) -> dict[str, object]:
        """Candidate data an AI call may see. Other preferences (blocklists, queue sizes, weights,
        filter toggles) are enforced in Python, so editing them must not invalidate AI caches."""
        data = self.model_dump(mode="json")
        data["preferences"] = {k: v for k, v in data["preferences"].items() if k in AI_PREFERENCE_FIELDS}
        return data

    @property
    def fingerprint(self) -> str:
        payload = json.dumps(self.ai_payload(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()
