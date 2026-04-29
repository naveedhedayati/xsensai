# Slice 7 manual gauntlet — confirmation nonce/handshake

Run after a fresh `./scripts/install_commands.sh` to verify the 2-call
destructive flow works end-to-end in a real Claude Code session against
the user's vault.

Prerequisites: Slice 7 merged (v0.8.0.0) and the MCP server restarted.
Pick one v2 paste card you don't mind cycling through delete/restore.

---

## G1 — Happy path delete via the 2-call flow

In Claude Code:

```
Use the MCP tool delete_bookmark to delete card <id>.
```

**Expected:**
1. Claude calls `delete_bookmark(id="<id>")` (no nonce).
2. Response is `[NONCE_REQUIRED]`. Claude renders the message verbatim, including a line that looks like:

   ```
   <<<NONCE: ABCD-EFGH>>>
   ```

3. Claude prompts you to type the 8 characters between the markers.

You type `ABCD-EFGH` (or `abcdefgh`, or `abcd-efgh` — all should accept).

4. Claude calls `delete_bookmark(id="<id>", confirmation_nonce="ABCD-EFGH")`.
5. Response is `ok: true`, `deleted: true`, with rendered_message
   ending in `Undo within 90s: /xrestore (this card listed first).`

**Failure modes to flag:**
- The host paraphrases the rendered_message and the nonce markers go missing → tighten
  `commands/xrestore.md` instructions; refile as a follow-up.
- The host calls both `delete_bookmark` calls without prompting you to type → this is the
  documented Slice 7 limitation (host can self-mint+redeem). Not a bug per se but file as
  a permission-prompt opportunity in `.claude/settings.json`.

## G2 — Wrong code typed

After G1's restore (`/xrestore` in next test), repeat G1 but type
something other than the displayed code (`ZZZZZZZZ` or just hit enter).

**Expected:** Claude calls `delete_bookmark(id, confirmation_nonce="ZZZZZZZZ")`,
gets `[NONCE_INVALID]`, the rendered_message says
*"Re-run /xdelete to issue a fresh code"*. Card is NOT deleted.

## G3 — Expired code (90s window)

Run G1 step 1-2 to get a nonce displayed. Wait at least 95 seconds.
Then echo the (now-stale) nonce.

**Expected:** `[NONCE_EXPIRED]` envelope. Card NOT deleted. Re-running
`/xrestore` issues a fresh code and the new code works.

## G4 — Single-use enforcement

Successfully delete a card via G1. Without restoring, ask Claude to
delete the same card again, and (somehow) try to redeem the SAME nonce
string again.

**Expected:** `[NONCE_ALREADY_REDEEMED]` envelope. Cause text: "That
confirmation code was already used."

(In practice the host won't try to reuse the nonce; you may need to call
the MCP tool directly via `python -P -c '...'` to trigger this. Skip if
manual reproduction is awkward.)

## G5 — Round-trip: /xdelete then /xrestore

After G1 (card is deleted), run `/xrestore`.

**Expected:**
1. `/xrestore` lists deleted cards; the card from G1 is at position #1.
2. You pick #1.
3. Claude calls `restore_bookmark(id)` (no nonce) → `[NONCE_REQUIRED]`.
4. Claude renders the message + nonce.
5. You echo. Claude calls `restore_bookmark(id, confirmation_nonce=...)`.
6. Card is restored. `/xfind` finds it again.

## G6 — Legacy-kwarg compat shim

In Claude Code (NOT via slash command — direct MCP-tool invocation):

```
Call delete_bookmark with id="<id>" and user_confirmed=True.
```

**Expected:** Response is `[NONCE_REQUIRED]` with cause text mentioning
*"`user_confirmed: bool` is deprecated in Slice 7."* The nonce is
embedded in the rendered_message. Card is NOT deleted. Following the
nonce flow from there should complete the delete.

This shim is removed in v0.9. After v0.9, the same call returns a
`TypeError`-equivalent (FastMCP rejects unknown kwarg).

---

## Optional: Bypass env var (G7)

In a shell BEFORE launching Claude Code:

```bash
export XSENSAI_DESTRUCTIVE_BYPASS=1
```

Then launch Claude Code and run G1.

**Expected:** Claude calls `delete_bookmark(id="<id>")` (no nonce).
Response is immediately `ok: true, deleted: true` — no handshake.
The MCP server's stderr log (visible in Claude Desktop's MCP logs)
contains a warning: *"XSENSAI_DESTRUCTIVE_BYPASS=1 active; skipping
nonce handshake for id='<id>'"*.

Restart the shell without the env var to restore the handshake.

---

## Notes

If any G1-G5 step fails in a way the test suite didn't catch, that's
a regression — open an issue with the Claude Code MCP log + the
rendered_message you saw. Slice 7 known limitation: the handshake
raises social-engineering effort but doesn't cryptographically prove
user attestation. For real attestation, add to `.claude/settings.json`:

```json
{
  "permissions": {
    "ask": [
      "mcp__xsensai__delete_bookmark",
      "mcp__xsensai__restore_bookmark"
    ]
  }
}
```

This makes Claude Code prompt you per call before invoking the tool —
the host-level boundary the nonce alone cannot provide.
