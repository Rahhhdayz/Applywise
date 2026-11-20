"""Offline contract tests for the subscription-authenticated Codex scoring CLI."""
from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jobagent.candidate.models import (
    CandidateProfile,
    CandidateTruth,
    Experience,
    ExperienceFact,
    Preferences,
    Skill,
)
from jobagent.db.repository import StoredJob
from jobagent.scoring import CodexCliJobScorer, JobMatchAssessment, ScoringConfigurationError


def truth() -> CandidateTruth:
    return CandidateTruth(profile=CandidateProfile(candidate_id="x", display_name="Example", professional_years=1), experiences=[Experience(id="exp", organization="Example", title="Intern", start_date="2025-01", facts=[ExperienceFact(id="fact", text="Used Python")])], skills={"programming": [Skill(canonical_name="Python")]}, preferences=Preferences())


def job() -> StoredJob:
    return StoredJob(1, "key", "test", "Example", "Security Engineer", "Remote", "id", "https://example.test", datetime.now(UTC), datetime.now(UTC), description_text="Untrusted text")


def assessment() -> dict[str, object]:
    return {"overall_score": 80, "role_alignment_score": 80, "skills_score": 80, "experience_score": 70, "career_direction_score": 80, "matched_skills": ["Python"], "transferable_skills": [], "meaningful_gaps": [], "evidence_ids_used": ["fact"], "strengths": [], "concerns": [], "summary_reason": "Grounded", "recommendation": "match"}


def test_codex_cli_scoring_forwards_model_and_validates_json(monkeypatch) -> None:
    captured: list[str] = []
    def run(command, **kwargs):
        captured.extend(command); Path(command[command.index("--output-last-message") + 1]).write_text(JobMatchAssessment(**assessment()).model_dump_json()); return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(subprocess, "run", run)
    result = CodexCliJobScorer(executable="codex", model_identifier="chosen").score(truth(), job())
    assert result.overall_score == 80 and "--model" in captured and "chosen" in captured


def test_codex_cli_malformed_or_missing_executable(monkeypatch) -> None:
    def malformed(command, **kwargs): Path(command[command.index("--output-last-message") + 1]).write_text("not json"); return subprocess.CompletedProcess(command, 0, "", "")
    monkeypatch.setattr(subprocess, "run", malformed)
    with pytest.raises(ValueError): CodexCliJobScorer().score(truth(), job())
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(FileNotFoundError()))
    with pytest.raises(ScoringConfigurationError, match="executable not found"): CodexCliJobScorer().score(truth(), job())

