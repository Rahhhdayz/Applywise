"""Small typed settings for bounded document-preparation runs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class WritingSettings:
    """Cost limits for subscription-authenticated document writing."""

    max_packets_per_run: int = 20


def load_writing_settings() -> WritingSettings:
    """Read optional local settings without treating example configuration as runtime state."""
    path = Path(os.getenv("JOBAGENT_SETTINGS", "config/settings.yaml"))
    values: object = {}
    if path.is_file():
        try:
            values = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError):
            values = {}
    section = values.get("writing", {}) if isinstance(values, dict) else {}
    if not isinstance(section, dict):
        section = {}
    maximum = _environment_int("JOBAGENT_WRITING_MAX_PACKETS", section.get("max_packets_per_run", 20), 20)
    return WritingSettings(maximum)


def _environment_int(name: str, value: object, fallback: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(value))))
    except ValueError:
        return fallback
