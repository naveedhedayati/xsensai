---
description: Fast lookup against Naveed's curated x-sensai bookmark corpus
---

You are running `/xfind` for the x-sensai bookmark corpus.

## What this routes to

This command searches Naveed's curated bookmark corpus (the MCP tool
`search_bookmarks`). It is NOT a general web search and NOT a search of
public X. It returns ranked references from his saved cards.

If the user's request is for general factual info or web-fresh content,
politely note that `/xfind` is corpus-only and ask whether they want to
proceed anyway.

**Slice 6 — tombstoned cards (frontmatter `deleted: true`) are excluded
from results by default.** The retrieval engine filters them out at the
QMD candidate stage and over-fetches to keep top_k stable on
tombstone-heavy corpora. If a card is missing from results that you
expect to see, it may have been deleted via `delete_bookmark`. Use
`/xrestore` to bring it back.

## Sync-status banner (Slice 4 + Slice 5 — auto-prepended)

BEFORE running the search, check the vault for `_sync-status.md`.
Surface ONE line above results when ANY of the following fire (most
specific wins):

- `consecutive_cron_failures >= 2` OR `last_cron_run > 5 days ago`
  (Slice 5 cron-only health) →
  > ⚠ Cron sync is failing — last cron run {N} days ago / {N} consecutive cron failures. Check the GH Actions UI or run `/xsync` from Mac.
- `consecutive_failures >= 2` OR `last_success > 5 days ago` (legacy
  combined health) →
  > ⚠ Sync is stale — last successful sync was {N} days ago / {N} consecutive failures. Run `/xsync` when convenient.
- `extraction_pending_count >= 50` OR `oldest_pending_age_days >= 30`
  (Slice 5 extraction backlog growing) →
  > ⓘ Extraction backlog growing — {N} cards still need summary+tags (oldest {D} days). Run `/xextract backlog` to drain.
- `_sync-status.md` exists but `last_cron_run` is null (cron has never
  fired on this corpus) →
  > ⓘ Cron is set up but has never run. See `docs/CRON_SETUP.md` to enable scheduled sync.

Banner cadence is once-per-session via `~/.cache/xsensai/banner-state.json`
(timestamps + last shown kind). Skip the banner if the same kind was shown
in the last 4 hours. If you can't read/write the state file, just print the
banner each time — better to be slightly nag-y than silently miss a
problem.

Do NOT block the search. The banner is informational only.

## Conversational flow

1. **If the user provided a query inline** with `/xfind <query>`, use that
   as the query. Otherwise ask: "What are you looking for?"

2. **Detect inline overrides** in the query and strip them before searching:
   - "no decay" or "no recency" → set `no_decay=true`
   - "skip pins" or "no pins" → set `include_pinned=false`
   - "no lazy" or "skip lazy" or "lazy off" → set `lazy_extract=false`
   Otherwise use defaults: `no_decay=false`, `include_pinned=true`,
   `lazy_extract=true`.

3. **Detect URL-as-query.** If the answer matches a URL pattern
   (`https?://...`), pass it through as the query — `search_bookmarks` will
   match it against `source` and `source_url` fields.

4. **Empty answer:** if the user says nothing, "nothing", "nevermind", or
   sends an empty message, respond: "ok, nothing to search; pass." and exit.

5. **Run `search_bookmarks`** via the MCP tool with `query`, `limit=5`,
   `no_decay`, `include_pinned`.

## Showing results

The MCP tool returns a structured payload. Before rendering, run the
**lazy-extract pass** (Slice 5; skip entirely if user passed `no lazy`).

### Lazy-extract pass (Slice 5)

For each result with `extraction_pending: true` (top-3 only — hard cap):

1. Call the Python helper to claim the card:

   ```bash
   python -P -m xsensai.sync.lazy_extract claim --card-id "$CARD_ID"
   ```

   (or, equivalently, import + call `claim_for_lazy_extract` directly).

   The helper returns one of:
   - `claimed` / `reclaimed`: you own extraction this turn — proceed.
   - `skip_active`: another `/xfind` session is mid-extraction. Render
     this card with a one-line note `(another session is extracting
     this card; rendering body-only)` and skip extraction.
   - `skip_done`: card already extracted. Use the result as-is.
   - `missing`: skip silently.

2. If `claimed` / `reclaimed`: read the card body via `get_bookmark`,
   then synthesize a 2-sentence `retrieval_summary` + 3-5
   `retrieval_tags` from the body. Then call:

   ```bash
   python -P -m xsensai.sync.service apply-extraction \
     --card-id "$CARD_ID" \
     --summary "$SUMMARY" \
     --tags "$TAG1,$TAG2,..." \
     --run-id "$LAZY_RUN_ID"
   ```

   (`$LAZY_RUN_ID` is the run_id returned by `claim_for_lazy_extract` —
   prefix `lazy-extract-`.)

3. Print one progress line BEFORE re-rendering: `(enriching N pending
   results... [LAZY_EXTRACT_TRIGGERED])`. After completion, refresh
   the result via `get_bookmark` and use the new summary+tags in the
   rendered output.

4. **On failure** (host LLM error, validation fail, or apply rejection):
   call `release_lazy_claim` so the next /xfind can retry. Render the
   card body-only with a footnote `(summary extraction failed —
   body-only)`. Don't loop — accept the degradation for this turn.

5. **Hard cap**: if more than 3 results need extraction, skip the lazy
   pass entirely and surface the extraction-backlog banner instead
   (see "Sync-status banner" above).

### Render

Show the `rendered_markdown` field verbatim — it is already formatted
with `[B]` (bookmarks) and `[P]` (pastes) prefixes per spec. Do NOT
summarize, editorialize, or add commentary.

If the response contains an `error` field, show the `rendered_markdown`
(which is the formatted error) and stop.

## Footer

After showing results (or error), append exactly this line:

> _Tip: append "no decay", "skip pins", or "no lazy" to override defaults._

## Override fuzzy match

If the user's query contained words like "decay", "recency", "pin",
"pinned", "lazy", or "extract" but did NOT match the canonical phrases
("no decay" / "skip pins" / "no lazy"), prepend a one-liner to the
result: "Did you mean `no decay`, `skip pins`, or `no lazy`? Running
with defaults."
