#!/usr/bin/env bash
# x-sensai setup wizard — Slice 6.
#
# Thin wrapper around `python -m xsensai.entrypoints.setup_wizard`.
# All flags pass through. Common usage:
#
#   ./scripts/setup.sh --preflight       # just check prereqs
#   ./scripts/setup.sh --oauth           # X OAuth PKCE flow (Keychain stored)
#   ./scripts/setup.sh --migrate         # v1→v2 corpus migration
#   ./scripts/setup.sh --all             # full guided setup (idempotent)
#   ./scripts/setup.sh --resume          # synonym for --all (skips done steps)
#
# State lives at ~/.cache/xsensai/setup-state.json. Re-running --all after
# a partial failure resumes from the failed step. Each step is idempotent
# (deploy-key skips if title-matched key exists; gh-vars upserts; etc.).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# Honor an existing virtualenv if one is active; otherwise prefer the
# project venv at .venv/. Pure shell-out — no sourcing magic.
if [ -n "${VIRTUAL_ENV:-}" ]; then
  PY="$VIRTUAL_ENV/bin/python"
elif [ -x "$REPO_ROOT/.venv/bin/python" ]; then
  PY="$REPO_ROOT/.venv/bin/python"
else
  PY="$(command -v python3 || command -v python)"
fi

exec "$PY" -m xsensai.entrypoints.setup_wizard "$@"
