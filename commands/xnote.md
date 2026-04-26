---
description: Annotate an x-sensai card (single mode or weekly review walk)
---

You are running `/xnote` for the x-sensai bookmark corpus.

## What this does

Mutates a card's `why_saved` / `applicability` / `pinned` / `next_review_at`
frontmatter via the `annotate_card` MCP tool. Two modes:

- **Single mode**: user provides id / URL / keyword → resolve one card →
  3 prompts → write
- **Review walk mode**: user says `review` → walks all cards where
  `why_saved_pending=true` OR `next_review_at<=now`, oldest first

V1 cards (no sidecar) are REFUSED with `[V1_MUTATION_BLOCKED]` per Slice 2
spec — Slice 6 ships migration. The refusal is logged to
`{corpus}/_v1-upgraded.jsonl` so migration prioritizes the cards you wanted
to mutate.

## Mode dispatch

Ask: "Which card? Paste an id, URL, or keyword — or type `review` (alone
on a line) to walk pending cards."

Wait. Parse the answer:

- Literal token `review` (case-insensitive, alone on a line) → **Review Walk Mode**
- Anything that looks like a card id (matches `^[A-Za-z0-9][A-Za-z0-9._-]*$`)
  → **Single Mode** with id resolution
- Anything that looks like a URL (starts with `http://` or `https://`) →
  **Single Mode** with URL resolution (call `search_bookmarks(query=url, limit=1)`)
- Otherwise → **Single Mode** with keyword resolution (call `search_bookmarks(query=keyword, limit=3)`)

## Single Mode

### Resolve the target

Based on the answer above:

- **id**: call `get_bookmark(id=...)`. If error, show `rendered_markdown` and exit.
- **URL**: call `search_bookmarks(query=url, limit=1)`. If 0 results,
  respond `No card matches that URL. Use /xpaste to add it.` and exit.
- **keyword**: call `search_bookmarks(query=keyword, limit=3)`. If 0 results,
  respond and exit. If multiple, list them with index numbers and ask "Which
  one? (1-N, or anything else to cancel)". Wait for a single digit answer.

Once a target id is known, optionally show: "Selected: `{id}` — {snippet}".

### Three prompts

1. "Why did you save this?" (default to existing `why_saved` if any; show
   it as `(currently: ...)` so the user knows what to keep or change. Empty
   answer = leave unchanged.)
2. "Which projects does it relate to? (vault wikilinks, comma-separated;
   empty = leave unchanged)"
3. "Pin this? (y/n/skip)" — `y` → pin, `n` → unpin, anything else → leave
   unchanged.

### Write

Call `annotate_card(id=..., why_saved=..., applicability=[...], pinned=...,
user_confirmed=True)` with only the fields the user changed (omit unchanged
ones to leave them as-is — pass them as None or omit from the call).

If `ok: true`: show `Annotated {id}.`
If `ok: false`:
- `V1_MUTATION_BLOCKED`: show the formatted error verbatim — explain the
  card stays untouched and Slice 6 migration will surface it.
- Other errors: show `rendered_message` verbatim.

## Review Walk Mode

### 1. Get due cards (UC10 wired — cursor resume)

Call `due_cards_for_review(limit=10)`. Returns
`{ok, count, total, has_more, cursor, due: [...]}` sorted oldest first.
The `cursor` field is the last_card_id the user finished annotating in
their last walk — `due` already excludes cards at or before that cursor,
so you naturally resume.

If `cursor` was set, prepend: `Resuming review from after card '{cursor}'
({count} of {total} remaining due).`

If `count == 0`: respond `No cards due for review. Nice.` and exit.

### 2. Per-card walk

For each card in `due`:

a. Show:
   > Card {N+1} of {count}: `{id}` ({author_or_domain}, captured {date})
   > {if prior_why_saved: "prior why_saved: {...}"}
   > Snippet: {snippet}
   > Reason due: {reason} (pending or review_at_due)

b. Per-card actions menu (one prompt):
   > Choose: a=annotate now / w=ask again next week / e=ephemeral (never
   > annotate) / s=skip / stop=exit walk
   > Strict tokens: ONLY a, w, e, s, stop (alone on a line, case-insensitive).

c. Handle (and on `a`/`w`/`e`/`s`/`stop`, call
   `set_review_cursor(last_card_id=<the card just acted on>)` so the next
   walk resumes from N+1 — UC10 wire-up):
   - `a`: prompt for `why_saved`, then optionally `applicability` (one prompt
     each), then call `annotate_card(id=..., why_saved=..., applicability=...,
     user_confirmed=True)`. If user gives empty `why_saved`, treat as `w`
     (do not write empty annotation).
   - `w`: call `annotate_card(id=..., next_review_at=<now+7d as ISO-8601>,
     user_confirmed=True)`. Card bumps to back of the queue.
   - `e`: call `annotate_card(id=..., why_saved="(ephemeral)", user_confirmed=True)`.
     Marks why_saved_pending=false; never surfaces in review again.
   - `s`: skip — call `set_review_cursor(last_card_id=<this card's id>)` so
     it doesn't re-appear next walk; move to next card.
   - `stop`: exit walk cleanly. Cursor is already set to the last completed
     card (one back from this one). Show `Walk stopped at card {N+1} of
     {total}. Resume next session from card {N+2}.`

d. Per-card summary: `OK ({action})` then move on.

### 3. End of walk

After the last card in the batch (i.e., walked through all `count` cards
without `stop`): call `set_review_cursor(last_card_id=None)` to clear the
cursor — the user finished a complete pass, next session starts from the
top. Show `Walk complete: {N annotated, M deferred, K ephemeral, S skipped}.
Cursor cleared.`

If the user used `stop`: cursor is already at the last completed card.
Don't clear it.

## Notes for Claude (do not show user)

- `annotate_card` requires `user_confirmed=True` per UC7. Set it.
- V1 cards are refused server-side — surface the error cleanly; do not
  retry with different args.
- The `next_review_at` field must be ISO-8601 with timezone (e.g.,
  `2026-05-02T00:00:00+00:00`). For "next week" use `now() + timedelta(days=7)`.
- /xnote review walk is bounded by `limit=10`; longer queues require running
  the command repeatedly. The MCP tool returns oldest-first so progress is
  monotonic across sessions even without a checkpoint file.
