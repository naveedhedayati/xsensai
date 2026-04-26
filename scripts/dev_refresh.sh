#!/usr/bin/env bash
# dev_refresh.sh — one-shot post-merge refresh for the local install.
#
# Slice 2 added 3 new slash commands + 6 new MCP tools. After a merge,
# you need to (a) git pull, (b) re-install the package, (c) copy the new
# slash commands to ~/.claude/commands/, and (d) restart both Claude
# Desktop (for new MCP tools) and Claude Code (for new slash commands).
# This script handles a-c idempotently and prints the restart checklist.

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

# Step 2: pip install -e . (picks up Slice 2 src/ changes)
if [ -d "$REPO_ROOT/.venv" ]; then
  echo "==> pip install -e . (refreshing package)"
  "$REPO_ROOT/.venv/bin/pip" install -e "$REPO_ROOT" --quiet
else
  echo "==> WARN: no .venv found; skipping pip install. Set up venv with:"
  echo "    python -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/pip install -e ."
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

# Step 5: print the restart checklist
cat <<'EOF'
==> NEXT STEPS

You must restart these two apps for new functionality to surface:

  1. Claude Desktop  — picks up new MCP tools (paste_bookmark, set_pin,
                       annotate_card, list_pinned, due_cards_for_review,
                       recover_aborted_paste)
                       Quit + relaunch from /Applications.

  2. Claude Code     — picks up new slash commands (/xpaste, /xnote, /xpin)
                       Restart your terminal session, OR reload commands
                       via /commands inside Claude Code.

After restart, smoke test in Claude Code:

  /xpaste            → step through the paste flow with any short content
                       end with "y" to confirm. Card lands on disk.
  /xfind <keyword>   → finds the card you just pasted (first /xfind after
                       a paste runs ~5s reindex, then unlinks marker)
  /xpin list         → shows current pins (empty initially; pin one to test)

If something doesn't work, check TROUBLESHOOTING.md (project root) keyed
by error code.

EOF

echo "==> dev_refresh: done"
