"""Shared fixtures. Tests must never read Master's private candidate files."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def example_candidate_dir(tmp_path: Path) -> Path:
    """A candidate directory containing only the checked-in synthetic *.example.yaml files."""
    directory = tmp_path / "candidate"
    directory.mkdir()
    for path in Path("candidate").glob("*.example.yaml"):
        shutil.copy(path, directory / path.name)
    return directory
