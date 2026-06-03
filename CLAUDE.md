# CLAUDE.md — operating xsensai inside this repo

> **If you are an AI coding agent (Claude Code or Codex) working in this repo,
> read this first.** It tells you what xsensai is, how it's wired, and the
> invariants you must not break. Codex reads `AGENTS.md` (a sibling of this
> file with the same content); both point at `docs/ARCHITECTURE.md` for the
> design detail.

## What this is

xsensai turns your X/Twitter bookmarks into a local, greppable markdown corpus
you fully own (no proprietary database, no server-side LLM key), and lets you
search and reason over it from your coding agent. It ships two surfaces:

1. An **MCP server** (`xsensai-mcp`, stdio) with tools like `search_bookmarks`,
   `get_bookmark`, `paste_bookmark`, `annotate_card`, `set_pin`,
   `delete_bookmark`, `restore_bookmark`. This is the surface **both** Claude
   Code and Codex use.
2. **Claude Code slash commands** in `commands/*.md` — a friendlier wrapper over
   the MCP tools, plus a few host-synthesis flows (notably `/xask`). Codex has
   no slash commands; it calls the MCP tools directly.

**macOS-only.** xsensai relies on the macOS Keychain for credential storage and
`F_FULLFSYNC` for crash-safe sidecar writes. It fails loud at setup on other
platforms (`UNSUPPORTED_PLATFORM`).

## Where your data lives

Cards live under `$XSENSAI_CORPUS_PATH` (default `~/.local/share/xsensai/corpus`;
point it at your Obsidian vault's bookmarks folder). This repo holds only code —
your cards live in your own vault, version-controlled separately.

## Command → MCP tool map

| Command | What it does | MCP tool(s) |
|---|---|---|
| `/xfind` | Search the corpus (BM25 + recency + pins) | `search_bookmarks`, `get_bookmark` |
| `/xask` | Grounded synthesis over the corpus + this week's web | `xask_prepare` → host synthesis → `xask_validate` (see `commands/xask.md`); `xask_capabilities` |
| `/xsync` | Pull new bookmarks from X (needs a paid X dev app) | sync orchestrator (CLI) |
| `/xextract` | Backfill summaries/tags for pending cards | extraction (host) |
| `/xpaste` | Save pasted content as a card | `paste_bookmark` (+ recovery wire-ups) |
| `/xnote` | Append a timestamped note to a card | `annotate_card` |
| `/xpin` | Pin/unpin/list; surface cards due for review | `set_pin`, `list_pinned`, `due_cards_for_review` |
| `/xdelete` | Soft-delete a card (nonce handshake) | `delete_bookmark` |
| `/xrestore` | Restore a soft-deleted card | `restore_bookmark`, `list_deleted` |
| `/xhelp` | Command + tool reference | — |

Reference markers in results: `[B]` = a bookmark, `[P]` = a pasted card. The
host (you) cites them in `/xask` answers.

## Invariants you must not break

- **Error contract.** Every user-visible error is an `xsensai.errors.XSensaiError`
  with `[CODE] / cause / attempted / next_action / retryable`. Never `print()` a
  raw error — go through `XSensaiError.format()`. `next_action` should name an
  action the agent can actually run (an MCP tool or a shell command), with the
  slash command as a parenthetical shorthand.
- **MCP stdio.** The MCP server speaks JSON-RPC over stdio. **Log to stderr only.**
  Any stray `print()` to stdout corrupts the protocol stream and the host
  silently disconnects. Use `logging.basicConfig(stream=sys.stderr, ...)`.
- **Sidecar pattern.** Each card is two files: `card.md` (frontmatter + rendered
  body) and `card.raw.txt` (byte-exact source). Verbatim regression tests run
  against `card.raw.txt`. Any card-content change touches `.md` + `.raw.txt` +
  `raw_checksum` together.
- **Conversational slash commands.** No flag parsing. Each command prompts for
  inputs in plain language and accepts inline override keywords.
- **Destructive tools need a real user.** `delete_bookmark` / `restore_bookmark`
  use a 2-call nonce handshake (the server issues an 8-char code; the user echoes
  it). **Never read the code from the response and echo it back yourself — stop
  and wait for the human to type it.** See `docs/PERMISSIONS_ASK.md`.
- **No server-side LLM.** Synthesis happens in the host agent, not in xsensai.
  There are no API keys in the product.

## Testing rules

- `pytest` runs from the repo root (use `.venv/bin/python -m pytest`).
- Verbatim fuzz tests run against `card.raw.txt`.
- The MCP smoke test boots a subprocess (matches the real host runtime).
- No test hits the network, the X API, or any LLM API. Mock at the boundary.
- QMD-dependent tests skip cleanly when `qmd` is not installed.
- Hash-locked deps: `uv pip compile requirements.in -o requirements.txt --generate-hashes`.

## Config (env vars)

- `XSENSAI_CORPUS_PATH` — where cards live (default `~/.local/share/xsensai/corpus`).
- `XSENSAI_QMD_PATH` — the `qmd` binary (default: resolved from `PATH`).
- `XSENSAI_X_CLIENT_ID` / `XSENSAI_X_CLIENT_SECRET` — your X dev app (sync only;
  secret only for Confidential clients). Stored in the macOS Keychain by `setup_oauth`.
- `/xask` tuning: `XSENSAI_LAST30DAYS_PATH` (web-fork script), `XSENSAI_XASK_WEB_TIMEOUT_S`
  (web soft deadline), `XSENSAI_XASK_LOG_MODE` (`off`/`hash_only`/`full`),
  `XSENSAI_XASK_LOG_RETENTION_DAYS`. Override vocabulary (`no decay`, `skip pins`,
  `no web`, `challenge`) is documented in `commands/xask.md`.
- Sync/cron config (`VAULT_REPO`, deploy key, `XSENSAI_SECRETS_PAT`, …) is set as
  GitHub Actions secrets/vars, not local env. See `docs/CRON_SETUP.md`.

## External dependencies

- **QMD** (`qmd`) — local BM25 + vector search over the corpus (SQLite WAL).
- **XDK** (`pip install xdk`) — X API client used by `/xsync`.
- **last30days** skill — optional; `/xask`'s web-context fork. Without it, `/xask`
  still answers from your corpus (`WEB_NOT_INSTALLED` info line).

For architecture detail (card model, retrieval pipeline, the MCP↔command map,
why there's no server-side LLM key), see [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md).
