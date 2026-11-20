"""Minimal local `.env` loading without a dependency or secret logging."""

from __future__ import annotations

import os
from pathlib import Path


def load_local_env(path: str | Path = ".env") -> None:
    """Load simple KEY=VALUE lines only when the shell has not set that key."""
    file_path = Path(path)
    if not file_path.is_file():
        return
    for raw_line in file_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")
