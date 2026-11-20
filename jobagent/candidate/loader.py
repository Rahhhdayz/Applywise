"""Load the four human-editable candidate truth YAML files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from jobagent.candidate.models import CandidateTruth


class CandidateConfigurationError(ValueError):
    """An actionable candidate configuration problem."""


def load_candidate_truth(directory: str | Path = "candidate") -> CandidateTruth:
    """Load regular files, falling back to checked-in synthetic examples for local demos."""
    root = Path(directory)
    names = ("profile", "experience", "skills", "preferences")
    documents: dict[str, Any] = {}
    for name in names:
        path = root / f"{name}.yaml"
        if not path.exists():
            path = root / f"{name}.example.yaml"
        if not path.exists():
            raise CandidateConfigurationError(f"Missing candidate configuration: {root / (name + '.yaml')}")
        try:
            documents[name] = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as error:
            raise CandidateConfigurationError(f"Could not read {path}: {error}") from error
    profile = documents["profile"].get("candidate", documents["profile"])
    preferences = documents["preferences"].get("preferences", documents["preferences"])
    # Support the intentionally human-friendly nested example shape while retaining a
    # compact internal schema for deterministic rules.
    if isinstance(preferences, dict):
        target_roles = preferences.get("target_roles")
        if isinstance(target_roles, dict):
            preferences = {
                **preferences,
                "primary_roles": preferences.get("primary_roles", target_roles.get("primary", [])),
                "secondary_roles": preferences.get("secondary_roles", target_roles.get("secondary", [])),
            }
        employment = preferences.get("employment_types")
        if isinstance(employment, dict):
            preferences = {**preferences, "employment_types": employment.get("preferred", [])}
    try:
        return CandidateTruth(
            profile=profile,
            experiences=documents["experience"].get("experiences", []),
            skills=documents["skills"].get("skills", {}),
            preferences=preferences,
        )
    except ValidationError as error:
        raise CandidateConfigurationError(f"Invalid candidate configuration: {error}") from error
