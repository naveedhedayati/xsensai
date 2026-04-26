#!/usr/bin/env bash
# install_commands.sh — install x-sensai slash commands into ~/.claude/commands/.
#
# COPIES (does not symlink — autoplan T3 decision) so branch switching
# doesn't silently change /xfind in unrelated Claude Code sessions.
# Re-run after editing any commands/*.md.
#
# Per /review#15: idempotent + content-aware. Skips files that already
# match. If the destination differs (user customized), writes a timestamped
# backup before overwriting so customizations are not silently destroyed.
#
# Also calls bootstrap_qmd.sh first to ensure the QMD collection exists.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
COMMANDS_SRC="$REPO_ROOT/commands"
COMMANDS_DST="$HOME/.claude/commands"

# Step 1: bootstrap QMD collection (idempotent).
"$REPO_ROOT/scripts/bootstrap_qmd.sh"

# Step 2: copy command files (with content-aware skip + backup).
mkdir -p "$COMMANDS_DST"

ts=$(date +%Y%m%d-%H%M%S)
installed=0
skipped=0
backed_up=0

for src in "$COMMANDS_SRC"/*.md; do
  [ -e "$src" ] || continue
  name=$(basename "$src")
  dst="$COMMANDS_DST/$name"

  if [ -e "$dst" ]; then
    if cmp -s "$src" "$dst"; then
      skipped=$((skipped + 1))
      continue
    fi
    backup="$dst.bak.$ts"
    cp "$dst" "$backup"
    backed_up=$((backed_up + 1))
    echo "Backed up $name -> $backup"
  fi

  cp "$src" "$dst"
  installed=$((installed + 1))
  echo "Installed $name -> $dst"
done

echo ""
echo "x-sensai slash commands: $installed installed, $skipped already up-to-date, $backed_up backups created."

# Slice 4 D-7: data-driven Available list — derive from commands/*.md instead
# of a hand-maintained string that goes stale every slice.
if compgen -G "$COMMANDS_SRC/*.md" > /dev/null; then
  available=$(ls "$COMMANDS_SRC"/*.md | xargs -I{} basename {} .md | awk '{printf "/%s ", $0}')
  echo "Available: $available"
fi
echo ""
echo "If your Claude Code is already running, restart it to pick up the new commands."
