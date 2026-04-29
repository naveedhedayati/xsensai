# Slice 7.5 manual gauntlet — `/xdelete` + `permissions.ask` wiring

Run after a fresh `./scripts/install_commands.sh` to verify v0.9.0.0
end-to-end in a real Claude Code session against the user's vault.

Prerequisites:
- Slice 7.5 merged (v0.9.0.0) and the MCP server reinstalled (`pip install -e .`).
- Claude Code restarted to pick up the new server + commands.
- Pick one v2 paste card you don't mind cycling through delete/restore.

If you used the Slice 7 manual gauntlet (`SLICE_7_GAUNTLET.md`), the v0.9.0.0
delta is: a polished slash command (`/xdelete`) replaces the
"ask Claude to call the MCP tool directly" step, AND the cryptographic gate
(`permissions.ask`) is now auto-installed and stacks with the in-band nonce.

---

## G1 — Install + announce-on-mutation observation

Before running the install, verify the gate file's current state:

```bash
cat ~/.claude/settings.json | python -m json.tool 2>/dev/null | grep -A5 permissions
```

Run install:

```bash
cd /path/to/xsensai
./scripts/install_commands.sh
```

**Expected output (in install stdout — NOT stderr):**

```
Configuring Claude Code permission prompts for destructive tools...
Created /Users/<you>/.claude/settings.json with permissions.ask entries: mcp__xsensai__delete_bookmark, mcp__xsensai__restore_bookmark
```

(or, if the file already existed:)

```
Added permissions.ask entries to /Users/<you>/.claude/settings.json: mcp__xsensai__delete_bookmark, mcp__xsensai__restore_bookmark (backup: ...bak.<ts>). See docs/PERMISSIONS_ASK.md.
```

(or, on idempotent re-run:)

```
permissions.ask entries already present in /Users/<you>/.claude/settings.json (no changes).
```

**Expected MCP version line:**

```
MCP server version: 0.9.0.0 (compatible with shipped commands).
```

If you see `WARN: xsensai MCP server is version <X> but commands target v0.8.0.0+`, run `pip install -e .` and retry — that's the AD2 mismatch warning.

Verify the file content:

```bash
cat ~/.claude/settings.json | python -m json.tool | grep -A4 ask
```

You should see both entries.

## G2 — Idempotent re-run

Run `./scripts/install_commands.sh` a second time without changing anything.

**Expected:** stdout includes `permissions.ask entries already present (no changes).` No new `.bak.` files in `~/.claude/`.

## G3 — `/xdelete` happy path with permissions.ask modal

In Claude Code, type:

```
/xdelete
```

**Expected flow:**

1. Prompt: *"Which card to delete? Paste an id, URL, or keyword."*
2. Type a keyword that matches one card you don't mind deleting.
3. The picker shows numbered results. Pick `1` (or whatever index matches).
4. **Claude Code surfaces a native modal**: *"MCP server xsensai wants to call delete_bookmark. Allow?"* with options including **Allow once**, **Allow for this session**, **Always allow**, **Deny**.
5. Click **Allow once**.
6. Server returns `[NONCE_REQUIRED]`. The host shows the rendered_message verbatim, including the `<<<NONCE: ABCD-EFGH>>>` block.
7. Type the 8 characters between the markers (hyphens optional).
8. **Claude Code modal fires AGAIN** for the second `delete_bookmark` call (with the nonce). Click Allow once.
9. Server redeems and returns `Deleted '<id>'. Undo within 90s: /xrestore (this card listed first).`

**Failure modes to watch:**
- Step 4 doesn't fire → the gate is bypassed. Check `permissions.allow` in your settings file (see G6 below).
- Step 6's `<<<NONCE: ` markers missing → the host is paraphrasing rendered_message. Check `commands/xdelete.md` is the v0.9.0.0 version.
- Step 8 fails with `[NONCE_INVALID]` → typo, or 90s expired between step 6 and step 7.

## G4 — Cancel path with recovery hint

Re-run `/xdelete`, pick a card, see the nonce, but type the wrong code (e.g., `ZZZZZZZZ`).

**Expected:** Host responds with `Cancelled; not deleted. (Re-run /xdelete to start over.)` — the recovery hint is the AD9 fix.

(If the host echoes the displayed code instead of typing it itself: that's a host-LLM bug, file as a regression — the nonce echo MUST come from the user's typed input.)

## G5 — Restore round-trip with `/xrestore`

After successfully deleting a card via G3:

```
/xrestore
```

**Expected:** `list_deleted(limit=10)` shows the card from G3 at #1. Picker → nonce flow → `Restored '<id>'.`

The `permissions.ask` modal fires twice for `restore_bookmark` (same as G3 for `delete_bookmark`).

## G6 — Wildcard override warning

Edit `~/.claude/settings.json` and add a wildcard subsuming the gate:

```json
{
  "permissions": {
    "allow": ["mcp__*"],
    "ask": ["mcp__xsensai__delete_bookmark", "mcp__xsensai__restore_bookmark"]
  }
}
```

Re-run `./scripts/install_commands.sh`.

**Expected stdout:**

```
[PERMISSIONS_WILDCARD_OVERRIDE] WARNING: ~/.claude/settings.json has `permissions.allow` entries that subsume the new `ask` entries: ['mcp__xsensai__delete_bookmark', 'mcp__xsensai__restore_bookmark']. The Claude Code permission prompt will NOT fire for these tools. To enable the gate, remove or narrow the matching `allow` entries. (See docs/PERMISSIONS_ASK.md.)
```

Restart Claude Code, run `/xdelete`. **Expected:** the `permissions.ask` modal does NOT fire (the wildcard bypasses it). The in-band nonce flow still works — the user-attestation gate is intact, but the cryptographic gate is now bypassed.

Recovery: edit `~/.claude/settings.json`, remove `mcp__*` from `allow`, restart Claude Code. The modal should fire again.

## G7 — Malformed JSON safe-skip

Make `~/.claude/settings.json` invalid JSON:

```bash
echo '{ this is broken' > ~/.claude/settings.json
```

Run `./scripts/install_commands.sh`.

**Expected:**

- Install does NOT abort (slash commands still install).
- stdout includes:
  ```
  [SETTINGS_MALFORMED] /Users/<you>/.claude/settings.json is not valid JSON (...). Backed up to .../settings.json.bak.<ts>. Permissions wiring SKIPPED — fix the JSON and re-run ./scripts/install_commands.sh.
  ```
- The `.bak.<ts>` file contains the broken original.
- The original file is untouched (the helper does NOT overwrite a malformed file with guessed JSON).

Recovery: restore from backup or hand-edit the broken file, then re-run install.

## G8 — V1 card pre-flight short-circuit (id-resolve path)

If your corpus has any v1 cards remaining (cards without `raw_path`/`raw_checksum`), pick one:

```
/xdelete
> <v1-card-id>
```

**Expected:** show the formatted `[V1_MUTATION_BLOCKED]` envelope BEFORE the nonce flow starts (no `permissions.ask` modal fires, no nonce is issued, no nonce is consumed). The next_action points at `./scripts/setup.sh --migrate`.

This is the AE5 short-circuit. If the keyword/URL path picks a v1 card instead, the nonce gets issued and consumed before the V1 refusal — that's intentional (single-rule contract from Slice 7 AE10).

## G9 — Atomic markdown gate test

```bash
cd /path/to/xsensai
source .venv/bin/activate
pytest tests/test_destructive_token_flow.py::TestAtomicMarkdownGate -v
```

**Expected:** all 4 tests pass:
- `test_xrestore_md_has_no_legacy_kwarg`
- `test_xrestore_md_mentions_nonce_flow`
- `test_xdelete_md_has_no_legacy_kwarg`
- `test_xdelete_md_mentions_nonce_flow`

The latter two are new in Slice 7.5.

---

## Notes (do not skip)

- The `permissions.ask` modal is the **cryptographic gate**. The in-band nonce is the **user-attestation gate**. Both stack. See `docs/PERMISSIONS_ASK.md`.
- "Always allow" defeats the cryptographic gate permanently. Don't click it unless you understand the trade-off.
- The 90-second window in the success message refers to the card being LISTED FIRST in `/xrestore`'s `list_deleted` output for ~90s after deletion (so an immediate undo finds it at #1). The card itself is recoverable indefinitely until the file is manually deleted from the corpus.
- Slice 7 G6 (legacy `user_confirmed=True` returns `[NONCE_REQUIRED]` with deprecation text) APPLIED in v0.9.0.0 only — the shim was one-release. v0.9.1.0+ raises `TypeError` on that kwarg; running G6 against a current MCP server will now hit the TypeError path, not the deprecation envelope.
