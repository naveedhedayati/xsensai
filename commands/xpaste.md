---
description: Save pasted content into your x-sensai corpus (with abort recovery)
---

You are running `/xpaste` for the x-sensai bookmark corpus.

## What this does

Captures pasted content (article excerpt, quote, observation, code snippet)
as a v2 paste card with frontmatter. NOT a bookmark — bookmarks come from
X via `/xsync` (Slice 4). The card lands on disk via `paste_bookmark`
(MCP), which holds the `card_write` lock + writes atomically + flips an
`_index-dirty` marker that `/xfind` consumes on the next query.

> **Why `/xpaste` uses `user_confirmed=True` (a soft guard) and not the
> nonce/handshake** that `/xdelete` and `/xrestore` use: paste creates
> rather than destroys, and the result is fully editable + deletable. The
> nonce/handshake exists for destructive transitions where the user
> can't recover with a slash command. See ADR-002 in
> `docs/PERMISSIONS_ASK.md` and the "Guard levels" table in `/xhelp`.

## Two modes

If the user's first input is the literal token `recover` (alone on a line,
case-insensitive), enter **recover mode** (jump to "Recover Mode" below).
Otherwise enter **paste mode**.

## Paste Mode

### 1. Ask for content

Prompt: "Paste the content. (Multi-paragraph is fine — send when done.)"

Wait for the user. The user's next message IS the content.

### 2. Empty content guard

If the user sends nothing, an empty string, or the word "nevermind" /
"cancel" alone: respond `ok, nothing pasted; pass.` and exit. NO card
written. NO inbox write (there's nothing to recover).

### 3. Tentative snapshot (PASTE_CRASHED defense — UC11 wired)

Generate a fresh snapshot id (`uuid4()` string). Call the MCP tool
`write_paste_snapshot(content=..., snapshot_id=...)` to write a tentative
recovery entry to the inbox. If /xpaste reaches step 7 successfully, we
pass the snapshot_id to `paste_bookmark`'s `clear_snapshot_id` arg to
auto-clear the snapshot. If /xpaste crashes (Ctrl-C, network drop, or
Claude Code dies), the snapshot survives and `/xpaste recover` finds it.

Remember the snapshot_id for steps 7 and 8.

### 4. Ask why_saved

Prompt: "Why are you saving this? (One line, or hit enter to figure out
later — the card auto-queues for /xnote review.)"

Wait. Empty or whitespace-only answer = leave why_saved unset (the MCP tool
will flip why_saved_pending=true).

### 5. Ask source_url

Prompt: "Source URL? (Optional — paste a URL or hit enter.)"

Wait. Empty answer = no source_url.

### 6. Ask tags

Prompt: "Tags? (Comma-separated, optional.)"

Wait. Parse comma-separated, strip whitespace, drop empties.

### 7. Confirm + write

Show a one-line summary:

> Saving: {first 60 chars of content}... | why: {why_saved or 'pending'} |
> tags: {tags or 'none'}.
> Proceed? (y to write, anything else cancels and saves to inbox)

**Strict accept token**: ONLY `y` (case-insensitive, no leading/trailing
whitespace) → call `paste_bookmark(content=..., why_saved=..., source_url=...,
tags=..., user_confirmed=True, clear_snapshot_id=<the snapshot_id from
step 3>)`. The clear_snapshot_id arg ensures the tentative snapshot is
cleared from the inbox now that the card committed. ANYTHING ELSE →
abort path (step 8).

If paste_bookmark returns `duplicate_of` (same content already saved within
24h), surface: "Duplicate of recent paste {duplicate_of} — no new card
written." NOT an error. Per /review F10 idempotency.

### 8. Abort path (UC9 wired)

The tentative snapshot from step 3 already preserved the content. Tell the
user: `Paste cancelled. Your content is in the recovery queue — run
/xpaste with first prompt 'recover' to restore.`

If you want to be extra safe, also call `clear_paste_snapshot` is NOT
appropriate here — leave the snapshot for the user to recover.

### 9. Show result

If `paste_bookmark` returned `ok: true`:

> Saved card `{id}` at `{path}`.
> {if why_saved_pending: "auto-queued for /xnote review (run /xnote review when ready)."}
> {if why_saved set: ""}
> Run /xfind to verify; first /xfind after a paste runs a quick reindex (~5s).

If `paste_bookmark` returned `ok: false`:

Show the formatted error verbatim (`rendered_message` field). For
LOCK_HELD: include the manual escape hint (`rm {corpus}/.locks/card_write.lock`
if the holder is known dead). For DISK_WRITE_FAILED: tell the user "Your
content was not saved — re-run /xpaste with the content still in your
scrollback."

## Recover Mode

User said `recover` at step 1. Walk:

### 1. List recoverable

Call `list_recoverable_pastes()` (no args). It returns
`{ok: true, count: N, entries: [...]}`. Each entry has timestamp, kind
(tentative or abort), content, optional why_saved_attempt, optional
source_url, optional snapshot_id.

If `count == 0`: respond `No recoverable inbox entries.` and exit.

### 2. Show the most recent

Show the user the newest entry (entries[0]):

> Most recent recoverable paste:
> Captured: {timestamp}
> {if why_saved_attempt: "why_saved (attempt): {...}"}
> {if source_url: "source_url: {...}"}
> Content (first 200 chars): {snippet}
>
> Promote this to a card? (y to write, l to see all N entries, anything
> else cancels)

### 3. Handle answer

- `y` (case-insensitive, alone): re-feed content + metadata into paste mode
  starting at step 4 (skip the content prompt — content is the recovered
  one). When you call `paste_bookmark` in step 7, pass
  `clear_snapshot_id=<the recovered entry's snapshot_id>`. The MCP tool
  auto-clears the inbox entry after a successful card write (UC9 wired).
- `l` (case-insensitive): show all N entries with timestamps; ask which
  one to promote by number. If user picks one by number, fetch it via
  `get_aborted_paste(snapshot_id=...)` and proceed as if `y` was chosen.
- Anything else: cancel without modification.

## Notes for Claude (do not show user)

- `paste_bookmark` REQUIRES `user_confirmed=True` per the runtime guard.
  Set this flag ONLY in step 7 after the user explicitly typed `y`.
- The `_index-dirty` marker is written automatically by the MCP tool;
  `/xfind` consumes it on the next query (read-side reindex trigger).
- /xpaste recover mode is SLICE 2 NEW — not in /xfind, /xhelp; users
  discover it via /xpaste's first-prompt option or by reading /xhelp's
  Slice 2 entry.
