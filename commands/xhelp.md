---
description: x-sensai command and tool reference
---

You are running `/xhelp` for x-sensai.

Print exactly this content (no editorializing, no header changes):

---

# x-sensai

Personal X bookmark retrieval skill — MCP server + slash commands for Claude.

## Slash commands

### Available now

| Command | What it does |
|---|---|
| `/xfind` | Fast lookup against your bookmark corpus (uses `search_bookmarks` MCP tool) |
| `/xhelp` | This help |
| `/xpaste` | Save pasted content as a paste card. First prompt accepts `recover` to restore an aborted paste from the inbox. |
| `/xnote` | Annotate a card (single mode) or walk pending cards (`review` mode). V1 cards refused until Slice 6 migration. |
| `/xpin` | Pin / unpin / list pinned cards. V1 cards refused until Slice 6 migration. |
| `/xask` | Thinking session, live — corpus + last30days web fork + grounded synthesis with `[B]`/`[P]` refs. |
| `/xsync` | Ingest new bookmarks from X via XDK. Smart-default extraction (inline ≤5, deferred >5). |
| `/xextract` | Backlog drain: bulk-extract `retrieval_summary` + `retrieval_tags` for cards still `extraction_pending: true` (Slice 5 lazy-extract handles top-3 on /xfind, but bulk drain is faster + ensures non-queried cards get extracted). |
| `/xrestore` | Restore a soft-deleted card. Lists recently-deleted, pick by number, then confirm via the Slice 7 nonce/handshake (server prints an 8-character code, you echo it). For delete, see `/xdelete`. |
| `/xdelete` | Soft-delete a card. Search → pick → confirm via nonce/handshake (8-char code). Recoverable via `/xrestore`. (Claude Code asks permission per call — see `docs/PERMISSIONS_ASK.md`.) |

> **Destructive tools (`/xdelete` + `/xrestore`) require `permissions.ask` in
> `~/.claude/settings.json` — auto-configured by `scripts/install_commands.sh`.**

### Planned

| Command | Slice | What it will do |
|---|---|---|
| `/xtranscribe` | Future | Process queued video transcriptions |

## MCP tools (callable from any Claude conversation with MCP configured)

### Available now

| Tool | What it does |
|---|---|
| `search_bookmarks(query, limit, no_decay, include_pinned)` | Returns top-N matches with `[B]`/`[P]` references |
| `get_bookmark(id)` | Fetch full card detail by id |
| `ping(echo)` | Smoke test (Slice 0) |
| `paste_bookmark(content, user_confirmed, why_saved?, source_url?, tags?, clear_snapshot_id?)` | Write a paste card. `user_confirmed` REQUIRED. 24h dedup against `content_fingerprint`. |
| `write_paste_snapshot(content, snapshot_id, why_saved_attempt?, source_url?)` | Write a tentative paste snapshot to the inbox (UC11). snapshot_id MUST be uuid4. |
| `clear_paste_snapshot(snapshot_id)` | Clear a tentative snapshot from the inbox (UC9). Idempotent. |
| `list_recoverable_pastes()` | List recoverable inbox entries (newest-first). Read-only. |
| `get_aborted_paste(snapshot_id)` | Fetch a single recoverable inbox entry. Read-only. |
| `recover_aborted_paste(snapshot_id?)` | DEPRECATED — kept for back-compat. Use list_recoverable_pastes / get_aborted_paste. |
| `annotate_card(id, user_confirmed, why_saved?, applicability?, pinned?, next_review_at?)` | Mutate frontmatter on a v2 card. V1 cards return `V1_MUTATION_BLOCKED`. |
| `set_pin(id, pinned, user_confirmed)` | Pin/unpin a v2 card. Idempotent. V1 cards refused. |
| `list_pinned(limit?)` | List pinned cards (read-only). |
| `due_cards_for_review(limit?)` | List cards needing /xnote review (read-only). |
| `get_review_cursor()` | Read the /xnote review walk cursor (UC10). |
| `set_review_cursor(last_card_id?)` | Update or clear the /xnote review walk cursor (UC10). |
| `xask_capabilities()` | Read-only deploy-status helper for `/xask` (Slice 3). |
| `delete_bookmark(id, confirmation_nonce?)` | **Slice 7** — soft-delete a card via 2-call nonce/handshake. First call (no nonce) returns `[NONCE_REQUIRED]` with a one-time 8-char code; user echoes; second call redeems. Tombstoned cards stay on disk but are excluded from search/list/dedup. Cron skips replay-write of deleted cards (sticky deletion). Legacy `user_confirmed: bool` accepted for one release with migration message; **removed in v0.9.1.0** (calls TypeError after that). Bypass: `XSENSAI_DESTRUCTIVE_BYPASS=1` in the parent shell skips the handshake for scripted maintenance. |
| `restore_bookmark(id, confirmation_nonce?)` | **Slice 7** — un-delete a tombstoned card; same 2-call nonce/handshake as `delete_bookmark`. |
| `list_deleted(limit?)` | **Slice 6** — list recently-deleted cards, newest-first (read-only). |

## Inline `/xfind` overrides

Append to your query:
- `no decay` — disable recency weighting
- `skip pins` — exclude pinned cards from results
- `no lazy` — skip lazy-extract on results that have `extraction_pending: true` (faster, but lower retrieval quality on those cards)

## Inline `/xask` overrides

Append to your question:
- `no decay` — disable recency weighting on retrieval
- `skip pins` — exclude pinned cards from retrieval
- `no web` — skip the `last30days` web fork entirely
- `challenge` — run an extra retrieval pass that hunts for a dissenting card

Override fuzzy match: if you say `dissent`, `recency`, `web off`, etc., `/xask`
will detect the canonical phrase, apply the override, AND prepend a one-line
note to your output telling you the canonical token to use next time.

## Inline `/xsync` overrides (Slice 4 — conversational, NOT flags per CLAUDE.md:90)

Append to your input (anywhere on the line):
- empty / `latest` / `since` / `new` — sync since last run (default)
- `backlog` / `full` / `everything` / `all` — fetch all bookmarks (paginate to end)
- 19-digit numeric or `x.com/<user>/status/<id>` URL — single-tweet mode (stubbed in Slice 4)
- `preview` / `dry-run` — fetch the bookmark list but write nothing
- `inline` / `force inline` — extract every new card inline regardless of N
- `defer` / `defer all` — write cards with `extraction_pending=true`; run `/xextract` later
- `commit` / `auto commit` — `git add` + `git commit` the new cards (no push)
- `proceed dirty` / `dirty ok` — sync anyway when prior xsync left uncommitted output

## Inline `/xextract` overrides

Append to your input:
- empty / `all` / `backlog` — process every pending card
- 19-digit numeric (card_id stem) — single-card mode
- a small number (e.g., `5`) — process at most that many
- `retry` / `failed` — synonym for backlog (drains all extraction_pending=true)

## Sync setup (one-time, ~25-45 min first run, ~5-8 min on a new machine)

Before `/xsync` works the first time:

1. Register an X dev app at https://developer.x.com (~5-15 min, browser + dev portal approval).
2. Buy ~$10 of API credits at https://console.x.com (one-time, lasts years at personal volume).
3. Export your client_id: `export XSENSAI_X_CLIENT_ID=<your-client-id>`
4. Verify preconditions: `python -m xsensai.sync.setup_oauth --check`
5. Run the OAuth flow: `python -m xsensai.sync.setup_oauth` (opens browser, captures redirect, stores refresh token in macOS Keychain).
6. Smoke test: in Claude Code, type `/xsync since` (defaults to since-last-run).

If the OAuth flow misbehaves: `--copy-url` prints the URL instead of opening
the browser; `--dry-run` runs the full PKCE flow without actually exchanging
the code (useful for debugging port collisions / browser issues).

Cost steady-state: ~$1.18/month for ~50 bookmarks/month with ~10 threaded
(per Spike #6 documentation analysis). Owned-read pricing on bookmarks
($0.001/Post) + search-recent on threads ($0.005-$0.010/Post).

## /xpaste / /xsync → /xfind round-trip

Slice 2 + 4 ship a read-side reindex trigger: when you `/xpaste` or
`/xsync` lands cards, an `_index-dirty` marker is written. The next
`/xfind` query consumes the marker by running `qmd update -c xsensai-cards`
under the cross-process `index_rebuild` lock (Slice 4 addition — prevents
concurrent QMD updates from racing). Typically ~5s reindex on a small
corpus.

In practice: any write → `/xfind` works in one session. First query after
a write is ~5s slower than usual; subsequent queries are normal speed.

## Sync status banner (Slice 4 + Slice 5)

`_sync-status.md` (committed to the vault) records the last `/xsync` and
cron run. `/xhelp`, `/xfind`, and `/xask` surface a one-line banner when
ANY of:

- `consecutive_cron_failures >= 2` — cron has failed twice in a row
- `last_cron_run > 5 days ago` — cron is silently broken
- `consecutive_failures >= 2` — combined manual + cron failures
- `last_success > 5 days ago` — corpus is stale
- `extraction_pending_count >= 50` OR `oldest_pending_age_days >= 30` — extraction backlog growing
- `last_cron_run is null` — cron is set up but has never fired (informational, points at CRON_SETUP.md)

Cron-only counters (`last_cron_run`, `last_cron_success`,
`consecutive_cron_failures`, `last_cron_runner`) are NEVER reset by manual
`/xsync` (autoplan E5). A healthy manual sync can no longer mask a dead
cron pipeline.

Banner cadence is once-per-session via
`~/.cache/xsensai/banner-state.json` (4-hour cooldown per banner kind) so
it doesn't nag every command call.

Clear cron-stale by running a successful manual `/xsync` AND fixing the
underlying cron failure (see `docs/CRON_SETUP.md` token-rotation runbook
for the most common cause). Clear extraction-backlog by running
`/xextract backlog`.

## Scheduled sync (Slice 5 — optional, one-time setup)

Slice 5 ships `.github/workflows/sync.yml`: an unattended cron that runs
every 2 days at 07:00 UTC, fetches new bookmarks, commits + pushes them
to your vault repo. Cards land with `extraction_pending: true`; `/xfind`
lazy-extracts top-3 on read; bulk drain via `/xextract`.

**One-time setup**: see `docs/CRON_SETUP.md` (45-90 min for
first-time setup of GH Actions secrets + deploy key).

**Quick commands**:

```bash
# Generate the four `gh secret set` commands for cron setup:
python -m xsensai.entrypoints.headless --emit-secrets-stdin

# Verify env + xdk readiness without burning a token:
python -m xsensai.entrypoints.headless --check

# Manually trigger a cron run (after secrets configured):
gh workflow run sync.yml
```

**Recovery flags** that may appear in the vault:

| Flag file | Meaning | Recovery |
|---|---|---|
| `SYNC_AUTH_FAILED.md` | Refresh token rotated/expired | Run `python -m xsensai.sync.setup_oauth --reauth` on Mac, then `gh secret set XSENSAI_X_REFRESH_TOKEN`. Then `git rm SYNC_AUTH_FAILED.md`. |
| `SYNC_PUSH_REJECTED.md` | Cron's push lost the race after 3 retries | `git pull --rebase` on Mac, resolve, push, re-trigger workflow. |
| `_conflicts/<run-id>/` | Cross-host conflict — cards manually merged | See `docs/CONFLICT_RESOLUTION.md`. |

## Mutation safety

`/xpaste`, `/xnote`, `/xpin`, `/xsync`, `/xextract` all hold the
`card_write` lock (via `fcntl.flock` + UUID fencing token) for the duration
of each individual write. `/xsync` and `/xextract` acquire/release per
card to keep `/xpaste` from blocking on long backlogs.

### Guard levels (Slice 7)

x-sensai has two confirmation patterns. Same `card_write` lock either way;
the difference is what gates the host LLM from invoking the tool.

| Tool | Guard | Reason |
|---|---|---|
| `paste_bookmark` (`/xpaste`) | **Soft** — `user_confirmed: bool` | Recoverable: re-paste or edit |
| `annotate_card` (`/xnote`) | **Soft** — `user_confirmed: bool` | Recoverable: edit `## Notes` block |
| `set_pin` (`/xpin`) | **Soft** — `user_confirmed: bool` | Recoverable: toggle back |
| `delete_bookmark` | **Strong** — confirmation nonce/handshake | Destructive transition; requires explicit user-typed code |
| `restore_bookmark` (`/xrestore`) | **Strong** — confirmation nonce/handshake | Destructive transition; requires explicit user-typed code |

**Soft guards** are accident-guards — they protect against the host LLM
firing a tool in the wrong context (e.g., reading a card body that
mentions paste). They are NOT a security boundary; the host sets the bool.

**Strong guards** are the 2-call handshake. The host calls `delete_bookmark(id)`
without a nonce; server returns `[NONCE_REQUIRED]` with a one-time
8-character code in `<<<NONCE: ABCD-EFGH>>>` markers. User echoes the
code; host calls `delete_bookmark(id, confirmation_nonce=...)` to redeem.
Honest framing: this raises social-engineering effort by one step; the
user remains the only true boundary. For cryptographic gating, configure
Claude Code's per-tool permission prompt in `.claude/settings.json`.

**Why two patterns?** Card mutations are reversible — you can re-edit a
note or unpin a card with one slash command. Deletes aren't reversible
without an explicit `/xrestore`, and `/xrestore` itself moves state.
Different blast radius, different gate. See TROUBLESHOOTING.md
"Resolved (Slice 7)" section for full envelope details.

**Power-user bypass.** `XSENSAI_DESTRUCTIVE_BYPASS=1` in the MCP server's
parent shell skips the handshake. For scripted maintenance only — the
env var is read at call time so the host LLM cannot inject it.

Reindex (`qmd update`) holds the `index_rebuild` lock cross-process —
`/xsync` finalize and `/xfind`/`/xask` read-side reindex serialize on it.

Two terminals running `/xpaste` simultaneously: one wins, the other gets
`[LOCK_HELD]` with the holder's PID + manual escape hint.

Mid-write crash: orphan `.tmp` files are detected and discarded on the
next `iter_cards` walk. The sidecar contract uses immutable per-version
`.raw.txt` files, so existing-card mutation under crash NEVER leaves a
`.md` referencing torn sidecar bytes.

## V1 card mutation policy

Cards still in v1 shape (no `raw_path` / `raw_checksum`) are REFUSED by
`/xnote` and `/xpin` with `[V1_MUTATION_BLOCKED]`. Slice 6 ships v1→v2
migration via XDK re-fetch. Until then: `/xfind` reads them (v1 read
adapter), but mutations are refused.

Slice 4 `/xsync` writes v2 cards from new bookmarks; existing v1 cards
are skipped (their tweet ids are detected in dedup, so `/xsync` won't
re-fetch them).

## Eval history

Quality-gate history (top-3 hit rate over time) lives at
`~/.cache/xsensai/eval-history.jsonl`. Run `xsensai-eval-history` to view
the last 10 runs.

## Logs

- `~/.cache/xsensai/xask-log.jsonl` — `/xask` runs (privacy-aware,
  default mode `hash_only`). `python -m xsensai.xask.log purge` to GC.
- `~/.cache/xsensai/xsync-log.jsonl` — `/xsync` runs (same privacy
  pattern). `python -m xsensai.sync.log purge` to GC.

## Troubleshooting

See `TROUBLESHOOTING.md` in the project root, keyed by error code.
