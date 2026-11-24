#!/usr/bin/env bash
# Symlink the tracked hooks in scripts/hooks/ into this repo's git hooks directory.
set -euo pipefail
root="$(git rev-parse --show-toplevel)"
hooks="$(git rev-parse --git-common-dir)/hooks"
mkdir -p "$hooks"
for hook in "$root"/scripts/hooks/*; do
  ln -sf "$hook" "$hooks/$(basename "$hook")"
  echo "installed $(basename "$hook")"
done
