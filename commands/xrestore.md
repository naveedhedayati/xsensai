---
description: Restore a soft-deleted x-sensai card
---

You are running `/xrestore` for the x-sensai bookmark corpus.

## What this does

Restores a card that was previously soft-deleted via `delete_bookmark`. The
card stays on disk while deleted (frontmatter `deleted: true`); restoring
clears the flag so the card returns to search, list ops, and dedup.

Soft-delete via `/xdelete` (or the `delete_bookmark` MCP tool directly).

**Slice 7 update — confirmation handshake.** Restore now uses a two-call
nonce/handshake instead of `user_confirmed: True`. The first call to
`restore_bookmark` returns a one-time confirmation code; the user types
the code and the second call redeems it. Closes the Slice 6 known
limitation that `user_confirmed: bool` was host-attestable (the LLM set
the flag, not the user).

## Mode

Single mode. Ask the user which deleted card to restore, request the
confirmation code, and apply.

### 1. List recently-deleted cards

Call `list_deleted(limit=10)` (read-only, no nonce needed).

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

### 3. Issue confirmation code (first call)

Call `restore_bookmark(id=<resolved_id>)` with NO `confirmation_nonce`
argument. The server will respond with a `[NONCE_REQUIRED]` envelope. The
`rendered_message` contains a delimited block in this exact shape:

```
<<<NONCE: ABCD-EFGH>>>
```

(The 8 characters are random per request, formatted as 4-4 with a hyphen.)

Show `rendered_message` to the user **verbatim**. The host LLM MUST NOT
paraphrase, truncate, or re-format the delimited block — the user expects
to see the literal `<<<NONCE: ` and `>>>` markers around an 8-character
code.

### 4. Ask the user to echo the code

Prompt: **"Type the 8 characters between the `<<<NONCE: ` and `>>>` markers
above. Hyphens are optional; case-insensitive. Anything else cancels."**

Strict accept: input parsed case-insensitively with hyphens stripped,
must equal the code returned in step 3. Anything else → respond
`Cancelled; not restored.` and exit.

The code expires 90 seconds after step 3 (server-enforced). If the user
takes too long and you re-run, you'll get a fresh code.

### 5. Redeem the code (second call)

Call `restore_bookmark(id=<resolved_id>, confirmation_nonce=<echoed_code>)`.
Strip hyphens and uppercase the echoed string before passing.

### 6. Show result

If `ok: true` and `restored: true`: show `rendered_message` (e.g.,
"Restored `{id}`.").

If already active (`already_active: true`): show the no-op message.

If error: show `rendered_message` verbatim. Common envelopes:
- `[NONCE_INVALID]` — the code didn't match anything (typo or expired record)
- `[NONCE_EXPIRED]` — the 90s window passed; re-run /xrestore for a new code
- `[NONCE_OPERATION_MISMATCH]` — the code was issued for a different op or card
- `[NONCE_ALREADY_REDEEMED]` — the code was already used; re-run for a fresh one

## Notes for Claude (do not show user)

- `list_deleted` is read-only; no confirmation needed.
- `restore_bookmark` two-call flow:
  1. First call with `id` only → returns `[NONCE_REQUIRED]` with the code embedded
  2. Second call with `id` + `confirmation_nonce` → redeems and applies
- NEVER call `restore_bookmark` twice without a user-echoed code in between.
  The host LLM passing the code to itself defeats the purpose.
- The card body and raw_bytes are unchanged by restore — only the
  `deleted` flag and `deleted_at` timestamp are cleared.
- After restore, the card is back in `search_bookmarks` results and
  `list_pinned` if it was pinned before deletion.
- DO NOT pass `user_confirmed=True` — that's the deprecated Slice 6 kwarg.
  If you supply it, the server returns a `[NONCE_REQUIRED]` envelope
  pointing at this flow as a one-release migration aid.
- If the first call returns `[USER_CONFIRMATION_REQUIRED]` instead of
  `[NONCE_REQUIRED]`, the MCP server is running pre-Slice-7 code; ask
  the user to restart Claude Code to pick up the new server.
