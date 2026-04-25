# Slice 1 — Runbook

From clean clone to a working `/xfind` in **~5 minutes**.

## Prerequisites (one-time)

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for dep install (or pip)
- [`qmd`](https://github.com/tobi/qmd) installed via `bun install -g qmd`
- Claude Desktop (for MCP tools) and/or Claude Code (for slash commands)

## Quickstart

```bash
# 1. Clone (or git pull) and cd in
cd ~/Documents/Claude/Projects/xsensai

# 2. Install deps + the package itself (editable)
python3.11 -m venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e .

# 3. Set the corpus path env var (or accept the default)
export XSENSAI_CORPUS_PATH=~/Documents/Vault/04_areas/x-bookmarks

# 4. Bootstrap the QMD index + install the slash commands
./scripts/install_commands.sh

# 5. Restart Claude Desktop (so it re-reads the MCP server registration)

# 6. Try it
#    From any Claude conversation:  "search my bookmarks for startups"
#    From Claude Code:               /xfind
```

## What success looks like

After step 6:

- **/xfind** prompts "What are you looking for?" → you type a query → ranked references appear with `[B]` (bookmark) / `[P]` (paste) prefixes.
- **search_bookmarks** (the MCP tool) returns structured hits + a rendered_markdown payload that Claude shows you.
- **/xhelp** lists what's available now and what's planned.

## Expected first-run states

The corpus path is `$XSENSAI_CORPUS_PATH` (defaults to `~/Documents/Vault/04_areas/x-bookmarks/`).

- **You have v1 cards already** (the typical case): the v1 read adapter (UC1) loads them in-memory; `/xfind` returns real results immediately.
- **Empty corpus**: `/xfind` returns `[CORPUS_UNAVAILABLE]` with a pointer to add cards or wait for Slice 6 migration.
- **Path doesn't exist**: same `[CORPUS_UNAVAILABLE]` with a pointer to fix `XSENSAI_CORPUS_PATH`.

## Verifying

```bash
# All unit tests
.venv/bin/pytest

# Integration tests (require qmd + a corpus)
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest

# F1 quality gate (15-query golden set; requires qmd)
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest tests/eval/golden_set.py -v -s
```

Quality gate target: top-3 hit rate ≥ 80% on the fixture corpus. As of Slice 1 ship: **top1=93%, top3=100%** (validates D1 — QMD's BM25 is sufficient for /xfind without LLM re-rank).

## Eval history

Trend over time: `xsensai-eval-history` (or `~/.cache/xsensai/eval-history.jsonl`).

## Re-installing slash commands after edits

```bash
./scripts/install_commands.sh
```

Commands are **copied** (not symlinked) per autoplan T3 — re-run after editing `commands/*.md` so changes propagate.
