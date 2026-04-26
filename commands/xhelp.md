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
| `/xask` | Thinking session — live: corpus + last30days web fork + grounded synthesis with `[B]`/`[P]` refs (synthesis runs in your Claude Code session). |

### Planned

| Command | Slice | What it will do |
|---|---|---|
| `/xsync` | Slice 4 | Ingest new bookmarks from X |
| `/xtranscribe` | Slice 4 | Process queued video transcriptions |

## MCP tools (callable from any Claude conversation with MCP configured)

### Available now

| Tool | What it does |
|---|---|
| `search_bookmarks(query, limit, no_decay, include_pinned)` | Returns top-N matches with `[B]`/`[P]` references |
| `get_bookmark(id)` | Fetch full card detail by id |
| `ping(echo)` | Smoke test (Slice 0) |
| `paste_bookmark(content, user_confirmed, why_saved?, source_url?, tags?, clear_snapshot_id?)` | Write a paste card. `user_confirmed` REQUIRED (slash command sets True after y/n prompt). 24h dedup against `content_fingerprint`. `clear_snapshot_id` clears a tentative snapshot from the inbox after success. |
| `write_paste_snapshot(content, snapshot_id, why_saved_attempt?, source_url?)` | Write a tentative paste snapshot to the inbox (UC11). snapshot_id MUST be uuid4. /xpaste calls this after step 1. |
| `clear_paste_snapshot(snapshot_id)` | Clear a tentative snapshot from the inbox (UC9). Idempotent. |
| `list_recoverable_pastes()` | List recoverable inbox entries (newest-first). Read-only. |
| `get_aborted_paste(snapshot_id)` | Fetch a single recoverable inbox entry. Read-only. |
| `recover_aborted_paste(snapshot_id?)` | DEPRECATED — kept for back-compat. Use list_recoverable_pastes / get_aborted_paste. |
| `annotate_card(id, user_confirmed, why_saved?, applicability?, pinned?, next_review_at?)` | Mutate frontmatter on a v2 card. V1 cards return `V1_MUTATION_BLOCKED`. `user_confirmed` REQUIRED. |
| `set_pin(id, pinned, user_confirmed)` | Pin/unpin a v2 card. Idempotent. V1 cards refused. `user_confirmed` REQUIRED. |
| `list_pinned(limit?)` | List pinned cards (read-only). Returns `{count, total, has_more, pinned}`. |
| `due_cards_for_review(limit?)` | List cards needing /xnote review (read-only). Returns `{count, total, has_more, cursor, due}`. Skips past `_review-cursor.json` (UC10). |
| `get_review_cursor()` | Read the /xnote review walk cursor (UC10). |
| `set_review_cursor(last_card_id?)` | Update or clear the /xnote review walk cursor (UC10). |
| `xask_capabilities()` | Read-only deploy-status helper for `/xask`: `{ok, version, prompt_template_version, web_fork_available, web_fork_path, log_path, log_mode}` (Slice 3). |

### Planned

| Tool | Slice | What it will do |
|---|---|---|
| (none currently planned for the next slice) | — | — |

## Inline `/xfind` overrides

Append to your query:
- `no decay` — disable recency weighting
- `skip pins` — exclude pinned cards from results

## Inline `/xask` overrides

Append to your question:
- `no decay` — disable recency weighting on retrieval
- `skip pins` — exclude pinned cards from retrieval
- `no web` — skip the `last30days` web fork entirely
- `challenge` — run an extra retrieval pass that hunts for a dissenting card

Override fuzzy match: if you say `dissent`, `recency`, `web off`, etc., `/xask`
will detect the canonical phrase, apply the override, AND prepend a one-line
note to your output telling you the canonical token to use next time.

## /xpaste → /xfind round-trip

Slice 2 ships a read-side reindex trigger: when you `/xpaste` a card,
an `_index-dirty` marker is written. The next `/xfind` query consumes
the marker by running `qmd update -c xsensai-cards` before searching
(typically ~5s on a small corpus, then unlinks the marker).

In practice: `/xpaste` → immediately `/xfind` works in one session.
First query after a paste is ~5s slower than usual; subsequent queries
are normal speed.

## Mutation safety

`/xpaste`, `/xnote`, `/xpin` all hold the `card_write` lock (via
`fcntl.flock` + UUID fencing token) for the duration of the write.
Two terminals running `/xpaste` simultaneously: one wins, the other
gets `[LOCK_HELD]` with the holder's PID + manual escape hint.

Mid-write crash: orphan `.tmp` files are detected and discarded on
the next `iter_cards` walk (logged as `[MID_WRITE_DETECTED]`). The
sidecar contract uses immutable per-version `.raw.txt` files (filename
includes a checksum prefix), so existing-card mutation under crash
NEVER leaves a `.md` referencing torn sidecar bytes.

## V1 card mutation policy

Cards still in v1 shape (no `raw_path` / `raw_checksum` — about 26 of
your existing vault cards) are REFUSED by `/xnote` and `/xpin` with
`[V1_MUTATION_BLOCKED]`. The refusal is logged to
`{corpus}/_v1-upgraded.jsonl` so Slice 6 migration knows to prioritize
those cards. Slice 6 ships proper v1→v2 migration via XDK re-fetch.

Until then: pin/annotate works on v2-shape cards (anything you
`/xpaste` is automatically v2). Existing v1 cards stay readable via
`/xfind` (the v1 read adapter loads them) but immutable.

## Sync status

Sync is configured in **Slice 4** (GitHub Actions cron + XDK ingestion).
Until then, the corpus is whatever you put in `$XSENSAI_CORPUS_PATH`
(defaults to `~/Documents/Vault/04_areas/x-bookmarks/`).

## Eval history

Quality-gate history (top-3 hit rate over time) lives at
`~/.cache/xsensai/eval-history.jsonl`. Run `xsensai-eval-history` to view
the last 10 runs.

## Troubleshooting

See `TROUBLESHOOTING.md` in the project root, keyed by error code.
