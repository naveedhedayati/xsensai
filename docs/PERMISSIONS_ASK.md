# `permissions.ask` — the cryptographic gate for `/xdelete` + `/xrestore`

This doc explains the `permissions.ask` setting that `scripts/install_commands.sh`
auto-writes into your user-global `~/.claude/settings.json`. It's the **true
user-attestation gate** for x-sensai's destructive MCP tools (`delete_bookmark`,
`restore_bookmark`); the in-band 8-character nonce handshake raises
social-engineering effort but is NOT a cryptographic boundary on its own (the
host LLM can mint and redeem in one tool-use chain). For real attestation,
`permissions.ask` is the answer.

## 1. What `permissions.ask` does

Claude Code reads `permissions.ask` from your settings file (project-local
`.claude/settings.json` first, then user-global `~/.claude/settings.json`).
Each entry is a string matching an MCP tool name. Before invoking a matching
tool, Claude Code surfaces a native modal:

> MCP server `xsensai` wants to call `delete_bookmark`.
> Allow?
> [ Allow once ] [ Allow for this session ] [ Always allow ] [ Deny ]

The user clicks. Claude Code only calls the tool after explicit approval.
This is a Claude Code feature — not something the MCP server can implement
or bypass from inside.

## 2. Why x-sensai uses it for delete/restore

Slice 7 (v0.8.0.0) introduced the 2-call confirmation nonce/handshake on
`delete_bookmark` and `restore_bookmark`. The honest framing in
[TROUBLESHOOTING.md](../TROUBLESHOOTING.md) called out: *"The handshake raises
social-engineering effort by ~1 step; the user remains the only true
boundary. The same host LLM can mint and redeem in one tool-use chain — for
genuine cryptographic gating, configure Claude Code's per-tool permission
prompt."*

Slice 7.5 (v0.9.0.0) auto-installs that prompt as part of `./scripts/install_commands.sh`.
The two gates stack:

1. **Cryptographic gate** (Claude Code's native modal): host can't even call
   `delete_bookmark` until the user clicks Allow. This is the boundary.
2. **User-attestation gate** (the 8-character nonce echo): user types the
   exact code displayed by the server, server-bound to the specific
   (operation, target) pair. Even if a malicious card snippet flips the
   host's pick step, the nonce displayed is for that wrong target — the
   user echoing it consents to that delete.

Both intentional. They protect against different failure modes:
- Cryptographic gate → host-LLM compromise (prompt injection, jailbreak).
- Nonce gate → user attestation that the right target is being acted on.

## 3. Precedence — `permissions.allow` supersedes `permissions.ask`

If your `~/.claude/settings.json` already has a `permissions.allow` entry
that subsumes a tool name, the `ask` gate is **silently bypassed**. The
install helper detects two patterns and warns:

- Literal match: `"permissions.allow": ["mcp__xsensai__delete_bookmark"]` —
  the literal string subsumes our `ask` entry.
- Wildcard suffix: `"permissions.allow": ["mcp__*"]` or
  `["mcp__xsensai__*"]` — the wildcard subsumes our `ask` entry.

When the install helper sees this, it prints to stdout:

```
[PERMISSIONS_WILDCARD_OVERRIDE] WARNING: ~/.claude/settings.json has
`permissions.allow` entries that subsume the new `ask` entries: ['mcp__xsensai__delete_bookmark']
```

To inspect:

```bash
cat ~/.claude/settings.json | python -m json.tool | grep -A5 permissions
```

To re-enable the gate, narrow or remove the matching `allow` entry, then
re-run `./scripts/install_commands.sh`.

## 4. Three options when the modal fires

| Option | When to pick it | Cost |
|---|---|---|
| **Allow once** | Default for `/xdelete` and `/xrestore`. Modal fires every call. | Friction every delete (intentional). |
| **Allow for this session** | Reasonable for genuine garbage-day cleanup where you'll delete 5+ cards in one session. Modal stops firing until you restart Claude Code. | One-session weakening; auto-resets on restart. |
| **Always allow** | **Trap.** Defeats the gate permanently. Only do this if you've decided the nonce handshake is sufficient by itself — it's a defensible choice but you're explicitly opting out of the cryptographic gate. | Permanent gate removal. Defensible only if you understand the threat model. |

If you click "Always allow" by mistake: edit `~/.claude/settings.json`,
remove the matching entry from `permissions.allow`, re-run
`./scripts/install_commands.sh`, and restart Claude Code.

## 5. Where the file lives

The install helper writes to **user-global `~/.claude/settings.json`**,
matching `install_commands.sh`'s per-user precedent (commands go to
`~/.claude/commands/`, the gate goes to `~/.claude/settings.json`). This
choice was made at the Slice 7.5 /autoplan final gate (TD-ENG-1).

Trade-off: the gate fires for every Claude Code project on this machine
that connects to an MCP server named `xsensai`. In practice, only one
project does. If you want project-scoped behavior, manually copy the
relevant block to `<repo>/.claude/settings.json` and remove from the
user-global file — Claude Code prefers project-local entries when both
exist.

The exact JSON shape:

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

## ADR-001 (Single-mode `/xdelete`)

`/xdelete` supports only single-card delete. No batch / multi-card / review-walk
mode. Per the Slice 7.5 /autoplan dual-voice review: bounded batch mode would
either nonce-habituate the user (one-confirm-per-id-set) or break the per-id
attestation invariant (a code that says "delete X" shouldn't authorize "delete
X, Y, Z"). For genuine cleanup workflows (>5 cards in a session), use the
`XSENSAI_DESTRUCTIVE_BYPASS=1` env var path with a Python maintenance script.

## ADR-002 (Slice 2 mutation guards stay `user_confirmed: bool`)

`paste_bookmark`, `annotate_card`, and `set_pin` keep their **required**
`user_confirmed: bool` kwarg unchanged. The asymmetry is intentional, not
legacy:

- Slice 2 mutations are **reversible**. Annotate edits frontmatter you can
  edit again; set_pin toggles a flag; paste creates rather than destroys.
  Re-run cost is minutes.
- Slice 7 nonce handshake exists because deletion is the only mutation that's
  destructive in a way that searching can no longer recover — tombstoned
  cards are excluded from search/list/dedup until explicitly restored.

A two-system surface acknowledged: both can coexist because they protect
against different failure modes and have different blast radii. If a Slice 2
mutation grows a destructive variant (e.g., `/xnote --replace-tags` that drops
history without backup), the nonce/handshake pattern should be reused.

## See also

- [TROUBLESHOOTING.md](../TROUBLESHOOTING.md) — error envelopes and recovery
  for `[NONCE_*]` codes and the new `[PERMISSIONS_WILDCARD_OVERRIDE]` /
  `[SETTINGS_MALFORMED]` warnings.
- [`commands/xdelete.md`](../commands/xdelete.md) — the slash command flow.
- [`commands/xrestore.md`](../commands/xrestore.md) — restore companion flow.
- [`/xhelp`](../commands/xhelp.md) — "Guard levels" table summarizing
  soft vs. strong guards.
