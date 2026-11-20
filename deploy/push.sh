#!/usr/bin/env bash
# Push code (never personal data, DB, packets or secrets) from the Mac to the VPS, then sync deps.
# Usage: deploy/push.sh jobagent@<vps-ip>
set -euo pipefail
host="${1:?usage: deploy/push.sh jobagent@<vps-ip>}"
cd "$(dirname "$0")/.."
rsync -az --delete \
  --exclude .venv --exclude .git --exclude '__pycache__' --exclude '.pytest_cache' --exclude '.ruff_cache' \
  --exclude data/ --exclude out/ --exclude logs/ --exclude backups/ --exclude candidate/ \
  --exclude .env --exclude config/search_profiles.yaml --exclude config/settings.yaml --exclude .DS_Store \
  ./ "$host:jobagent/"
ssh "$host" 'cd ~/jobagent && ~/.local/bin/uv sync --frozen && ~/.local/bin/uv run jobagent doctor --offline'
