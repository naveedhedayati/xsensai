---
description: Pin / unpin / list pinned x-sensai cards
---

You are running `/xpin` for the x-sensai bookmark corpus.

## What this does

Pins, unpins, or lists pinned cards. Pinned cards bypass recency decay in
`/xfind` (still must score on relevance — pin dominance is bounded). Pins
are stored in card frontmatter as `pinned: true` and persist atomically via
`set_pin` (MCP tool, requires `user_confirmed=True`).

V1 cards (no sidecar) are REFUSED with `[V1_MUTATION_BLOCKED]` per Slice 2
spec — Slice 6 ships migration.

## Mode dispatch

Ask: "Pin a card, unpin a card, or list pinned? (`pin`, `unpin`, or
`list` — alone on a line)"

Strict accept tokens: ONLY `pin`, `unpin`, or `list` (case-insensitive,
alone on a line). Anything else: respond `Unknown action; expected pin /
unpin / list.` and exit.

## Pin Mode

### 1. Resolve target

Ask: "Which card? (id, URL, or keyword)"

Use the same resolution flow as `/xnote` single mode:
- id matching `^[A-Za-z0-9][A-Za-z0-9._-]*$` → `get_bookmark(id=...)` for verification
- URL → `search_bookmarks(query=url, limit=1)`, error if 0 results
- keyword → `search_bookmarks(query=keyword, limit=3)`, ask user to pick by
  number if multiple

Once a target id is known, optionally show: "Selected: `{id}` —
{author_or_domain}, {snippet}".

### 2. Confirm + write

Ask: "Pin `{id}`? (y to pin, anything else cancels)"

Strict accept token: ONLY `y` (case-insensitive, alone on a line). Otherwise
respond `Cancelled; not pinned.` and exit.

On `y`: call `set_pin(id=..., pinned=True, user_confirmed=True)`.

### 3. Show result

If `ok: true`: show `rendered_message` (e.g., "Pinned `{id}`.")
If already pinned (no-op): show the no-op message verbatim.
If error: show `rendered_message` verbatim. For V1_MUTATION_BLOCKED,
explain the card stays untouched and Slice 6 will surface it.

## Unpin Mode

Same flow as Pin Mode but call `set_pin(id=..., pinned=False, user_confirmed=True)`.

## List Mode

Call `list_pinned()` (read-only, no user_confirmed needed).

If `count == 0`: show `No pinned cards yet.`

Otherwise show a markdown table (or simple list if 1-3 entries):

```
| # | id | author/domain | captured | why_saved |
|---|----|---------------|----------|-----------|
| 1 | {id} | {author_or_domain} | {captured} | {why_saved or "(no annotation)"} |
| ... | ... | ... | ... | ... |
```

After the list, show:
`{count} pinned. /xfind weights these above unpinned baseline (with pin-dominance
bound: pinned cards must still score relevantly).`

## Notes for Claude (do not show user)

- `set_pin` requires `user_confirmed=True`. Set it after the y confirm.
- `list_pinned` is read-only and does NOT mutate; safe to call without
  confirmation.
- V1 cards refused server-side; do not retry.
- Pin idempotency is server-side: pinning an already-pinned card returns
  ok=true with a "no-op" message.
