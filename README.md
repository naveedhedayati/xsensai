# x-sensai

Personal X bookmark retrieval skill for Claude. MCP server + 8 conversational slash commands that let Claude draw on a curated taste corpus when thinking with the user.

**Spec / source of truth:** `~/Documents/Vault/02_projects/x-sensai/v2-build-spec.md`

**Current slice:** Slice 2 — locks + sidecar atomic write + `/xpaste` + `/xnote` + `/xpin` (12 new MCP tools). See [CHANGELOG.md](./CHANGELOG.md) for what shipped in each release.

## Layout

```
src/xsensai/         Python package (importable as `xsensai`)
  errors.py          Error contract: [CODE]/cause/attempted/next/retryable
  model/             Card data model (CardFrontmatter + LoadedCard)
  storage/           Corpus iteration + sidecar I/O + v1 read adapter
  retrieval/         QMD wrapper + scoring + format ([B]/[P])
  mcp_server/        MCP server (ping, search_bookmarks, get_bookmark)
  cli/               Console scripts (xsensai-eval-history)
  locks/             Concurrency (Slice 2)
  sync/              XDK + sync (Slice 4)
  commands/          (Reserved for Slice 3+ command handlers)

commands/            Slash command source files (xfind.md, xhelp.md, xpaste.md, xnote.md, xpin.md)
                     Installed to ~/.claude/commands/ via scripts/install_commands.sh

tests/               pytest suite (77 tests: 75 always-on + 2 integration-gated)
  fixtures/cards/    10 hand-curated v2 cards + 1 v1 card for adapter coverage
  fixtures/verbatim_fuzz/   3 critical adversarial inputs (triple-dash, backticks, ## Content)
  fixtures/qmd_query_output.json   QMD JSON-output schema contract fixture
  eval/golden_set.py F1 quality gate (15 queries, target top-3 ≥ 80%)

scripts/             bootstrap_qmd.sh, install_commands.sh, setup.sh (Slice 6 wizard stub)
spikes/              Verification spike results
.github/workflows/   CI (pytest on push)
```

## Slice 1 — quick start

See [SLICE_1_RUNBOOK.md](./SLICE_1_RUNBOOK.md) for the complete walkthrough. TL;DR:

```bash
# One-time setup
python3.11 -m venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python -e .
export XSENSAI_CORPUS_PATH=~/Documents/Vault/04_areas/x-bookmarks
./scripts/install_commands.sh   # bootstraps QMD + installs /xfind, /xhelp

# Restart Claude Desktop. Then:
#   From any conversation: "search my bookmarks for X"
#   From Claude Code:       /xfind
```

## What works (Slice 1 + Slice 2)

| Surface | Where | What |
|---|---|---|
| `/xfind` | Claude Code slash command | Fast lookup against your corpus, [B]/[P]-formatted refs |
| `/xhelp` | Claude Code slash command | Reference of available + planned commands and tools |
| `/xpaste` | Claude Code slash command | Conversational paste: drop a tweet/thread, save as a card with abort recovery (Slice 2) |
| `/xnote` | Claude Code slash command | Annotate an existing card with `## Notes` block (Slice 2) |
| `/xpin` | Claude Code slash command | Pin / unpin / list pinned cards; due-for-review surfacing (Slice 2) |
| `search_bookmarks` | MCP tool (any Claude conversation) | Structured response: `{hits, meta, rendered_markdown}` |
| `get_bookmark` | MCP tool | Full card detail by id (returned by search_bookmarks) |
| `paste_bookmark` / `recover_aborted_paste` | MCP tools | Powers `/xpaste` (Slice 2) |
| `annotate_card` | MCP tool | Powers `/xnote` (Slice 2) |
| `set_pin` / `list_pinned` / `due_cards_for_review` | MCP tools | Powers `/xpin` (Slice 2) |
| `ping` | MCP tool | Smoke test (Slice 0) |

## Quality gate (F1)

Slice 1 ships with a 15-query golden-set evaluation against the fixture corpus. Current results:

- **top-1 hit rate: 93%** (14/15 queries return the expected card as #1)
- **top-3 hit rate: 100%** (target was ≥ 80%)

Run yourself: `XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest tests/eval/golden_set.py -v -s`

This validates the autoplan D1 decision — QMD's BM25 ranking is sufficient for `/xfind`'s "fast lookup" purpose; Claude/GPT re-rank stays deferred to Slice 3 where it powers `/xask` synthesis.

## Build slicing

- **Slice 0** (shipped): spikes + skeleton + `ping` smoke test + `errors.py`.
- **Slice 1** (shipped): card model + sidecar storage + v1 read adapter + retrieval (QMD wrapper, scoring, [B]/[P] format) + `search_bookmarks` + `get_bookmark` + `/xfind` + `/xhelp`.
- **Slice 2** (shipped): locks (`fcntl.flock` + UUID fencing) + atomic sidecar write (`durable_replace` with macOS `F_FULLFSYNC`) + `/xpaste` + `/xnote` + `/xpin` + read-side reindex trigger so paste→find round-trips in one session.
- **Slice 3** (current): `/xask` + last30days web fork + synthesis + LLM re-rank.
- **Slice 4**: XDK sync + `/xsync` + checkpoint resume.
- **Slice 5**: GitHub Actions cron.
- **Slice 6**: v1→v2 migration script + setup wizard. (v1 read adapter from Slice 1 deleted at this point.)

## Troubleshooting

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md), keyed by error code.

## Tests

```bash
.venv/bin/pytest                                      # unit tests (75 fast)
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest            # + 7 integration tests (need QMD)
xsensai-eval-history                                  # quality-gate trend over time
```
