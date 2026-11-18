#!/usr/bin/env bash
#
# Point Git at the hooks in scripts/githooks.
#
# Usage: ./scripts/install-githooks.sh        (or: make hooks)
# Undo:  git config --unset core.hooksPath

set -euo pipefail

if ! root="$(git rev-parse --show-toplevel 2>/dev/null)"; then
    echo "install-githooks: not inside a Git repository. Run \`git init\` first." >&2
    exit 1
fi
cd "$root"

chmod +x scripts/githooks/*
git config core.hooksPath scripts/githooks

echo "Git hooks installed from scripts/githooks:"
for hook in scripts/githooks/*; do
    echo "  - $(basename "$hook")"
done
