# AGENTS.md — driving xsensai from a coding agent

> **Read this first.** This is the canonical guide for an AI coding agent
> (Codex *or* Claude Code) operating xsensai. Claude Code also reads `CLAUDE.md`
> (same content); the shared design detail lives in `docs/ARCHITECTURE.md`.

## What xsensai is

A local, **macOS-only** tool that turns your X/Twitter bookmarks into a
greppable markdown corpus you fully own (no proprietary DB, no server-side LLM
key), searchable and reasoned-over from your agent. It exposes an **MCP server**
(`xsensai-mcp`, stdio) — that is the surface you use. Claude Code adds slash
commands on top; **Codex has no slash commands and drives the MCP tools directly.**

## First step: confirm the server is registered and current

Register the MCP server, then confirm it exposes the tools below before relying
on it:

- **Codex** — add to `~/.codex/config.toml`:
  ```toml
  [mcp_servers.xsensai]
  command = "/abs/path/to/xsensai/.venv/bin/xsensai-mcp"
  # env = { XSENSAI_CORPUS_PATH = "/abs/path/to/your/vault/x-bookmarks" }
  ```
- **Claude Code** — `claude mcp add xsensai -- /abs/path/.venv/bin/xsensai-mcp`,
  or rely on the committed project `.mcp.json` (auto-discovered when you open the
  repo).

Then list tools (`tools/list`) and confirm **`xask_prepare`** is present (it's the
newest flagship tool — if it's missing, your installed server predates this guide;
run `pip install -e .` and restart).

## Command → MCP tool map (the tool-only path for each flow)

| Flow | Slash command (Claude Code) | MCP tool(s) — Codex uses these directly |
|---|---|---|
| Search | `/xfind` | `search_bookmarks(query=...)` → hits + `rendered_markdown`; `get_bookmark(id)` for detail |
| Ask (grounded synthesis) | `/xask` | `xask_prepare(question=...)` → **you** synthesize per the returned driving loop → `xask_validate(draft=..., candidate_card_ids=meta["rerank_winners"], ...)`. No server-side LLM; the prompt is for you to answer. Pass `candidate_card_ids` to turn on the groundedness gate: the answer must abstain, or cite ≥2 distinct returned cards with every `## Synthesis` line carrying an inline `[B]/[P]` ref (or a `(no corpus support — general knowledge)` hedge). |
| Sync from X | `/xsync` | sync orchestrator (CLI; needs a paid X dev app) |
| Backfill extraction | `/xextract` | extraction (host) |
| Paste a post | `/xpaste` | `paste_bookmark(...)` |
| Annotate | `/xnote` | `annotate_card(...)` |
| Pin / review | `/xpin` | `set_pin`, `list_pinned`, `due_cards_for_review` |
| Delete | `/xdelete` | `delete_bookmark` (2-call nonce — see below) |
| Restore | `/xrestore` | `restore_bookmark`, `list_deleted` |

Results carry `[B]` (bookmark) and `[P]` (pasted) reference markers — cite them
in answers.

## ⚠️ Destructive-tool rule (do not skip)

`delete_bookmark` / `restore_bookmark` use a 2-call handshake: the first call
returns an 8-character code; the **user** echoes it on the second call.

> **NEVER read the code out of the response and pass it back yourself.** Stop,
> show the code to the human, and wait for them to type it. The whole point of
> the handshake is that a human — not you — authorizes the destructive action.

## Invariants

- **Errors** are `XSensaiError` envelopes: `[CODE] / cause / attempted /
  next_action / retryable`. The `next_action` names something you can run.
- **MCP is stdio.** The server logs to stderr; never write to its stdout.
- **Sidecar:** each card is `card.md` + `card.raw.txt` (byte-exact source).
- **No server-side LLM:** synthesis is *you* reading the retrieved cards. There
  are no API keys in the product.

## Setup steps a human must do (you cannot)

Creating a **paid X developer app**, completing **browser OAuth**, and **paying**
are human-only. When setup reaches them, print the numbered checklist and stop —
do not attempt them yourself (it can burn a single-use token).

See `docs/ARCHITECTURE.md` for the card model + retrieval pipeline, and
`docs/CRON_SETUP.md` for scheduled sync.
