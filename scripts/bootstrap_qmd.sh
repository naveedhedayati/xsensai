#!/usr/bin/env bash
# bootstrap_qmd.sh — idempotent QMD collection setup for x-sensai.
#
# Creates the 'xsensai-cards' collection pointing at $XSENSAI_CORPUS_PATH
# (default: ~/.local/share/xsensai/corpus) so /xfind has an
# index to search. Safe to re-run; if the collection already exists, no-op.
#
# Used by:
#   - scripts/install_commands.sh (called automatically on first install)
#   - manual setup ("[CORPUS_UNAVAILABLE] Run scripts/bootstrap_qmd.sh")

set -euo pipefail

QMD_BIN="${XSENSAI_QMD_PATH:-$(command -v qmd || true)}"
CORPUS_PATH="${XSENSAI_CORPUS_PATH:-$HOME/.local/share/xsensai/corpus}"
COLLECTION="xsensai-cards"

if [ ! -x "$QMD_BIN" ]; then
  echo "ERROR: qmd binary not found at $QMD_BIN" >&2
  echo "Install via: bun install -g qmd" >&2
  echo "Or set XSENSAI_QMD_PATH to your installed binary." >&2
  exit 2
fi

# Mirror corpus.resolve_corpus_path's asymmetric-create rule: when
# XSENSAI_CORPUS_PATH is UNSET we use (and create) the neutral default; when it
# is SET but missing we fail loud rather than indexing a typo'd empty path.
if [ -z "${XSENSAI_CORPUS_PATH:-}" ]; then
  mkdir -p "$CORPUS_PATH"
elif [ ! -d "$CORPUS_PATH" ]; then
  echo "ERROR: XSENSAI_CORPUS_PATH is set but does not exist: $CORPUS_PATH" >&2
  echo "Create the directory or fix the path, then re-run." >&2
  exit 2
fi

# Check if collection already exists. Per /review#16: avoid `\b` (GNU-grep
# only) and brittle stdout parsing. Use `qmd ls <collection>` exit code as
# the canonical existence check; fall back to an awk word-match on
# `qmd collection list`.
if "$QMD_BIN" ls "$COLLECTION" >/dev/null 2>&1; then
  echo "QMD collection '$COLLECTION' already exists; no action."
  exit 0
fi
if "$QMD_BIN" collection list 2>/dev/null | awk -v c="$COLLECTION" '$1 == c {found=1} END {exit !found}'; then
  echo "QMD collection '$COLLECTION' already exists; no action."
  exit 0
fi

echo "Creating QMD collection '$COLLECTION' indexing $CORPUS_PATH ..."
"$QMD_BIN" collection add "$CORPUS_PATH" --name "$COLLECTION" --mask "*.md"
echo "Done."
