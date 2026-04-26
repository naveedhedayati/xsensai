#!/usr/bin/env bash
# dev_refresh.sh — one-shot post-merge refresh for the local install.
#
# After a merge: (a) git pull, (b) re-install the package, (c) copy slash
# commands to ~/.claude/commands/, and (d) restart both Claude Desktop (for
# new MCP tools) and Claude Code (for new slash commands). This script
# handles a-c idempotently and prints the restart checklist.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> dev_refresh: refreshing x-sensai local install"
echo

# Step 1: git pull (if on a tracking branch)
if git -C "$REPO_ROOT" rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  echo "==> git pull (tracking remote)"
  git -C "$REPO_ROOT" pull --ff-only
else
  echo "==> skipping git pull (no upstream tracking branch — local-only branch?)"
fi
echo

# Step 2: install -e . (picks up src/ changes). Detect pip vs uv-venv —
# uv-created venvs don't ship pip by default, so fall back to `uv pip` when
# the in-venv pip is missing.
if [ -d "$REPO_ROOT/.venv" ]; then
  echo "==> install -e . (refreshing package)"
  if [ -x "$REPO_ROOT/.venv/bin/pip" ]; then
    "$REPO_ROOT/.venv/bin/pip" install -e "$REPO_ROOT" --quiet
  elif command -v uv >/dev/null 2>&1; then
    VIRTUAL_ENV="$REPO_ROOT/.venv" uv pip install -e "$REPO_ROOT" --quiet
  else
    echo "    ERROR: .venv has no pip and uv is not installed."
    echo "    Either: $REPO_ROOT/.venv/bin/python -m ensurepip"
    echo "    Or:     install uv (https://docs.astral.sh/uv/)"
    exit 1
  fi
else
  echo "==> WARN: no .venv found; skipping install. Set up venv with one of:"
  echo "    python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e ."
  echo "    uv venv && VIRTUAL_ENV=.venv uv pip install -r requirements.txt && VIRTUAL_ENV=.venv uv pip install -e ."
fi
echo

# Step 3: install_commands.sh (copies commands/*.md → ~/.claude/commands/)
echo "==> installing slash commands"
"$REPO_ROOT/scripts/install_commands.sh"
echo

# Step 4: smoke check the MCP entry point (ping)
if [ -x "$REPO_ROOT/.venv/bin/xsensai-mcp" ]; then
  echo "==> MCP smoke check (xsensai-mcp --help)"
  "$REPO_ROOT/.venv/bin/xsensai-mcp" --help >/dev/null 2>&1 || true
  echo "    OK"
fi
echo

# Step 5: print the restart checklist (slice-agnostic — won't rot per slice)
cat <<'EOF'
==> NEXT STEPS

You must restart these two apps for new functionality to surface:

  1. Claude Desktop  — picks up new MCP tools.
                       Quit + relaunch from /Applications.
                       Verify: ask Claude "list your MCP tools" and check
                       that xsensai tools appear (any of search_bookmarks,
                       paste_bookmark, set_pin, xask_capabilities, etc.).

  2. Claude Code     — picks up new slash commands.
                       Restart your terminal session, OR reload via the
                       /commands UI inside Claude Code.

After restart, smoke test in Claude Code:

  /xhelp             → lists the current command surface for THIS slice.
                       (If /xhelp is missing, restart didn't take.)
  /xfind <keyword>   → fast retrieval; verifies the read path is healthy.

If something doesn't work, check TROUBLESHOOTING.md (project root) keyed
by error code.

EOF

echo "==> dev_refresh: done"
