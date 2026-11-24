#!/usr/bin/env bash
# Local CI gate: lint + full test suite. Nonzero exit on any failure.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
uv run ruff check .
uv run pytest -q
