---
description: Restore a soft-deleted x-sensai card
---

You are running `/xrestore` for the x-sensai bookmark corpus.

## What this does

Restores a card that was previously soft-deleted via `delete_bookmark`. The
card stays on disk while deleted (frontmatter `deleted: true`); restoring
clears the flag so the card returns to search, list ops, and dedup.

Soft-delete in Slice 6 is via the `delete_bookmark` MCP tool (no `/xdelete`
slash command yet — ships in Slice 7+ once delete semantics stabilize).

## Mode

Single mode. Ask the user which deleted card to restore, then confirm.

### 1. List recently-deleted cards

Call `list_deleted(limit=10)` (read-only, no user_confirmed needed).

If `count == 0`: show `No deleted cards in the corpus.` and exit.

Otherwise show a numbered list:

```
Recently deleted:
1. {id} — {author_or_domain} — deleted {deleted_at}
2. ...
```

### 2. Resolve target

Ask: "Which card to restore? (number from list above, or id, or keyword)"

- A digit (1, 2, ...) within the list size → use that entry's id
- An id matching `^[A-Za-z0-9][A-Za-z0-9._-]*$` → use directly
- Otherwise treat as keyword and run `search_bookmarks` (which excludes
  deleted by default), ask user to pick — but this only finds non-deleted
  cards, so prefer the numbered-list path

If none of these resolve, respond `Could not resolve target.` and exit.

### 3. Confirm + write

Ask: "Restore `{id}`? (y to restore, anything else cancels)"

Strict accept token: ONLY `y` (case-insensitive, alone on a line).
Otherwise respond `Cancelled; not restored.` and exit.

On `y`: call `restore_bookmark(id=..., user_confirmed=True)`.

### 4. Show result

If `ok: true` and `restored: true`: show `rendered_message` (e.g.,
"Restored `{id}`.").

If already active (`already_active: true`): show the no-op message.

If error: show `rendered_message` verbatim.

## Notes for Claude (do not show user)

- `list_deleted` is read-only; no confirmation needed.
- `restore_bookmark` requires `user_confirmed=True`. Set it after the y
  confirm — never speculate.
- The card body and raw_bytes are unchanged by restore — only the
  `deleted` flag and `deleted_at` timestamp are cleared.
- After restore, the card is back in `search_bookmarks` results and
  `list_pinned` if it was pinned before deletion.
