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

# Slice 5 — surface the cron setup hint after install so the user knows
# scheduled sync is available but unconfigured (DX D4).
echo ""
echo "Scheduled sync (Slice 5, optional): see docs/CRON_SETUP.md for setup."
echo "  python -m xsensai.entrypoints.headless --emit-secrets-stdin   # ready-to-paste setup"
echo "  python -m xsensai.entrypoints.headless --check                # verify env + xdk"

# Slice 6 — detect v1 cards and direct user to migration. Prevents the
# onboarding regression both /autoplan dual voices flagged: "Slice 6
# shipped but my v1 cards still error on annotate/pin."
echo ""
v1_count=$(python -c "
import os, sys
try:
    from xsensai.storage import corpus, v1_adapter
    import frontmatter
    p = corpus.resolve_corpus_path()
    n = 0
    for md in p.glob('*.md'):
        if md.name.startswith('_') or md.name in ('CLAUDE.md', 'README.md'):
            continue
        try:
            post = frontmatter.load(md)
            if v1_adapter.is_v1_shape(dict(post.metadata)):
                n += 1
        except Exception:
            continue
    print(n)
except Exception as e:
    print(0, file=sys.stderr)
    print(0)
" 2>/dev/null)
if [ "${v1_count:-0}" -gt 0 ]; then
  echo "Detected $v1_count v1 card(s). Run \`./scripts/setup.sh --migrate\` to upgrade them to v2"
  echo "(mutations on v1 cards are blocked until migrated). Preview first with:"
  echo "  python scripts/migrate_v1_to_v2.py --dry-run"
fi
