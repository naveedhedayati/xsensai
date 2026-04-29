# x-sensai

Personal X bookmark retrieval skill for Claude. MCP server + 9 conversational slash commands that let Claude draw on a curated taste corpus when thinking with the user.

**Spec / source of truth:** `~/Documents/Vault/02_projects/x-sensai/v2-build-spec.md`

**Current slice:** Slice 7.5.1 + v0.9.1.1 hot-fix — cron `headless.run()` now treats `status="empty"` as success per spec (no-new-bookmarks runs no longer false-fail with exit 2 + bogus heartbeat error). Surfaced during a manual QA pass; verified live. The Slice 7.5.1 contract change still stands: stale `delete_bookmark`/`restore_bookmark` calls passing `user_confirmed=` raise `TypeError`; only the 2-call confirmation-nonce flow (or `XSENSAI_DESTRUCTIVE_BYPASS=1` for scripted maintenance) is accepted. See [CHANGELOG.md](./CHANGELOG.md) for what shipped in each release.

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
  xask/              /xask orchestration (Slice 3): service.py + log.py + version.py
  synthesis/         Output template + validator + injection-fixture helpers (Slice 3)
  web_fork/          last30days subprocess wrapper, env-scrubbed (Slice 3)
  entrypoints/       Headless cron orchestrator (Slice 5)

commands/            Slash command source files (xfind.md, xhelp.md, xpaste.md, xnote.md, xpin.md, xask.md, xsync.md, xextract.md, xrestore.md)
                     Installed to ~/.claude/commands/ via scripts/install_commands.sh

tests/               pytest suite (~683 tests; ~13 gated on XSENSAI_RUN_INTEGRATION=1)
  fixtures/cards/    10 hand-curated v2 cards + 1 v1 card for adapter coverage
  fixtures/verbatim_fuzz/   3 critical adversarial inputs (triple-dash, backticks, ## Content)
  fixtures/prompt_injection/   5 adversarial fixtures with INJECTED_<n> canaries (Slice 3)
  fixtures/qmd_query_output.json   QMD JSON-output schema contract fixture
  eval/golden_set.py F1 quality gate (15 queries, target top-3 ≥ 80%)

scripts/             bootstrap_qmd.sh, install_commands.sh, setup.sh (Slice 6 guided wizard), migrate_v1_to_v2.py (Slice 6 migration with byte-exact rollback)
spikes/              Verification spike results
docs/                Slice 5: CRON_SETUP.md (one-time setup runbook), CONFLICT_RESOLUTION.md (manual `_conflicts/<run-id>/` workflow)
.github/workflows/   CI (pytest on push) + sync.yml (Slice 5 cron, every 2 days at 07:00 UTC)
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

## What works (Slices 1–6)

| Surface | Where | What |
|---|---|---|
| `/xfind` | Claude Code slash command | Fast lookup against your corpus, [B]/[P]-formatted refs |
| `/xhelp` | Claude Code slash command | Reference of available + planned commands and tools |
| `/xpaste` | Claude Code slash command | Conversational paste: drop a tweet/thread, save as a card with abort recovery (Slice 2) |
| `/xnote` | Claude Code slash command | Annotate an existing card with `## Notes` block (Slice 2) |
| `/xpin` | Claude Code slash command | Pin / unpin / list pinned cards; due-for-review surfacing (Slice 2) |
| `/xask` | Claude Code slash command | Thinking session: corpus + last30days web fork + grounded synthesis with `[B]`/`[P]` refs (Slice 3) |
| `/xsync` | Claude Code slash command | Ingest new bookmarks from X via XDK; smart-default extraction (inline ≤5, deferred >5) (Slice 4) |
| `/xextract` | Claude Code slash command | Backfill extraction for cards left as `extraction_pending: true` (Slice 4) |
| `/xrestore` | Claude Code slash command | Restore a tombstoned card (clears `deleted` + `deleted_at`); pairs with `/xdelete` via the Slice 7 nonce/handshake |
| `/xdelete` | Claude Code slash command | Soft-delete a card via the 2-call nonce/handshake (Slice 7.5). Auto-installed `permissions.ask` gate prompts per call. See [docs/PERMISSIONS_ASK.md](docs/PERMISSIONS_ASK.md) |
| `search_bookmarks` | MCP tool (any Claude conversation) | Structured response: `{hits, meta, rendered_markdown}` (tombstone-aware via `include_deleted=False` default) |
| `get_bookmark` | MCP tool | Full card detail by id (returned by search_bookmarks) |
| `paste_bookmark` / `recover_aborted_paste` | MCP tools | Powers `/xpaste` (Slice 2) |
| `annotate_card` | MCP tool | Powers `/xnote` (Slice 2) — raises `[TOMBSTONE_BLOCKED]` on tombstoned targets (Slice 6) |
| `set_pin` / `list_pinned` / `due_cards_for_review` | MCP tools | Powers `/xpin` (Slice 2) — `set_pin` raises `[TOMBSTONE_BLOCKED]` on tombstoned targets (Slice 6) |
| `delete_bookmark` / `restore_bookmark` / `list_deleted` | MCP tools | Tombstone lifecycle; lock-first-then-load to prevent stale-snapshot resurrection (Slice 6) |
| `xask_capabilities` | MCP tool | Read-only deploy-status helper for `/xask` (Slice 3) |
| `ping` | MCP tool | Smoke test (Slice 0) |

## Slice 4 — quick start (sync setup)

Realistic TTHW: ~25-45 min first time (dominated by external X dev portal),
~5-8 min on a new machine with the dev app already registered.

```bash
# 1. Register an X dev app at https://developer.x.com (~5-15 min, browser).
# 2. Buy ~$10 of API credits at https://console.x.com (one-time).
# 3. Export your client_id:
export XSENSAI_X_CLIENT_ID=<your-client-id>

# 4. Verify preconditions (no browser, no token write):
python -m xsensai.sync.setup_oauth --check

# 5. Run the OAuth flow (opens browser, captures redirect, stores in Keychain):
python -m xsensai.sync.setup_oauth

# 6. In Claude Code, smoke test:
#    /xsync since
```

Steady-state cost: **~$1.18/month** for ~50 bookmarks/month with ~10 threaded.

## Scheduled sync (Slice 5, optional, one-time setup)

Slice 5 ships an unattended cron via GitHub Actions. After Slice 4's
local OAuth setup, you can optionally enable scheduled sync that fires
every 2 days at 07:00 UTC, fetches new bookmarks, commits them to your
vault repo, and pushes — no Mac needed for the actual sync.

Realistic TTHW: **45-90 min for first-time** GH Actions secrets setup
(deploy key + 4 secrets), **5-10 min on a new machine** if secrets
already exist. The `--emit-secrets-stdin` helper cuts the most
error-prone step (piping refresh token from Keychain through to
`gh secret set`).

```bash
# Helper that prints ready-to-paste `gh secret set` commands:
python -m xsensai.entrypoints.headless --emit-secrets-stdin

# Verify env + xdk readiness without burning a token:
python -m xsensai.entrypoints.headless --check

# Manually trigger a cron run (after secrets configured):
gh workflow run sync.yml
```

Full setup runbook: [docs/CRON_SETUP.md](docs/CRON_SETUP.md).

When cron lands new cards on the vault, your next `/xfind` lazy-extracts
the top-3 results' summaries+tags inline (DX surface; details in
`commands/xfind.md`). For bulk drain or non-queried cards, run
`/xextract backlog`.

## Slice 6 — guided setup wizard + v1→v2 migration

Slice 6 ships a guided setup wizard (replaces the previous stub) plus a
byte-exact v1→v2 migration script. New users get a checked, resumable
install flow; existing v1-corpus users get a safe one-shot migration
with a per-card rollback journal.

```bash
# Guided first-run setup (idempotent, --resume if interrupted):
./scripts/setup.sh

# Migrate existing v1 cards to v2 (dry-run first, then apply):
python scripts/migrate_v1_to_v2.py --dry-run
python scripts/migrate_v1_to_v2.py --apply        # prompts: type APPLY
python scripts/migrate_v1_to_v2.py --rollback    # restores byte-exact originals
```

`install_commands.sh` prints a v1-card count on every run so you know
when to migrate. Tombstones (`deleted: true` + `deleted_at`) are now
first-class: deleted cards are excluded from `/xfind`, `/xask`, and sync
dedup; `/xrestore` brings them back.

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
- **Slice 3** (shipped, v0.4.0.0): `/xask` + last30days web fork + grounded synthesis in the host Claude Code session (no server-side LLM dep).
- **Slice 4** (shipped, v0.5.0.0): XDK sync + `/xsync` + `/xextract` + setup_oauth + smart-default extraction + git plumbing + cross-process index_rebuild lock.
- **Slice 5** (shipped, v0.6.0.0): GitHub Actions cron + git push + cost ceiling + cross-host conflict resolution + lazy-extract on read in `/xfind` (Spike #10) + heartbeat instrumentation.
- **Slice 6** (shipped, v0.7.0.0): v1→v2 migration with byte-exact rollback + tombstone schema (`deleted` + `deleted_at` + invariant validator) + MCP `delete_bookmark` / `restore_bookmark` / `list_deleted` + `/xrestore` slash command + shadow-mode union-frontmatter merge driver (logs candidate; fail-loud stays primary) + guided setup wizard. v1 adapter retained 1 release; promote for deletion in Slice 7+ once 0 v1 cards observed for 14 consecutive days.
- **Slice 7** (shipped, v0.8.0.0): confirmation nonce/handshake on destructive MCP tools (`delete_bookmark` + `restore_bookmark` 2-call flow replacing Slice 6 host-attestable `user_confirmed: bool`). One-release legacy-kwarg shim + `XSENSAI_DESTRUCTIVE_BYPASS` env var for scripted maintenance.
- **Slice 7.5** (shipped, v0.9.0.0): `/xdelete` slash command + auto-installed `.claude/settings.json` `permissions.ask` cryptographic gate via `scripts/_settings_merge.py`. Closes Slice 7's honest-framing gap. Two locked ADRs: ADR-001 (single-mode `/xdelete`) and ADR-002 (Slice 2 mutation guards stay `user_confirmed: bool` because annotate/pin/paste are reversible). v0.9.1.0 follow-up removes the legacy shim after >=7 day soak.

## Troubleshooting

See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md), keyed by error code.

## Tests

```bash
.venv/bin/pytest                                      # unit tests (always-on)
XSENSAI_RUN_INTEGRATION=1 .venv/bin/pytest            # + integration tests (need QMD + last30days)
xsensai-eval-history                                  # quality-gate trend over time
```
