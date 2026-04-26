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

## Sync-status banner (Slice 4 — auto-prepended)

BEFORE running the search, check the vault for `_sync-status.md`. If the
file exists AND `consecutive_failures >= 2` OR `last_success > 5 days ago`,
prepend ONE line above results:

> ⚠ Sync is stale — last successful sync was {N} days ago / {N} consecutive failures. Run `/xsync` when convenient.

Do NOT block the search. The banner is informational only.

## Conversational flow

1. **If the user provided a query inline** with `/xfind <query>`, use that
   as the query. Otherwise ask: "What are you looking for?"

2. **Detect inline overrides** in the query and strip them before searching:
   - "no decay" or "no recency" → set `no_decay=true`
   - "skip pins" or "no pins" → set `include_pinned=false`
   Otherwise use defaults: `no_decay=false`, `include_pinned=true`.

3. **Detect URL-as-query.** If the answer matches a URL pattern
   (`https?://...`), pass it through as the query — `search_bookmarks` will
   match it against `source` and `source_url` fields.

4. **Empty answer:** if the user says nothing, "nothing", "nevermind", or
   sends an empty message, respond: "ok, nothing to search; pass." and exit.

5. **Run `search_bookmarks`** via the MCP tool with `query`, `limit=5`,
   `no_decay`, `include_pinned`.

## Showing results

The MCP tool returns a structured payload. Show the `rendered_markdown`
field verbatim — it is already formatted with `[B]` (bookmarks) and `[P]`
(pastes) prefixes per spec. Do NOT summarize, editorialize, or add commentary.

If the response contains an `error` field, show the `rendered_markdown`
(which is the formatted error) and stop.

## Footer

After showing results (or error), append exactly this line:

> _Tip: append "no decay" or "skip pins" to override defaults._

## Override fuzzy match

If the user's query contained words like "decay", "recency", "pin", or
"pinned" but did NOT match the canonical phrases ("no decay" / "skip
pins"), prepend a one-liner to the result: "Did you mean `no decay` or
`skip pins`? Running with defaults."
