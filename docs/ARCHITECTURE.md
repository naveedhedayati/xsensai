# Architecture

The design invariants behind xsensai, written once here so `CLAUDE.md` and
`AGENTS.md` can stay thin and point at this file.

## The card data model (sidecar pattern)

Each saved item is **two files** that travel together:

- `card.md` — YAML frontmatter (`source_type`, `source`, `source_id`, `author`,
  `tags`, `pinned`, `deleted`, …) plus a rendered markdown body.
- `card.raw.txt` — the byte-exact original source text.

`raw_checksum` (in the frontmatter) is a sha256 over `card.raw.txt`. Verbatim
regression tests assert against `card.raw.txt`, so **any content change touches
`card.md` + `card.raw.txt` + `raw_checksum` together** — treat it as one edit or
checksum validation fails on load.

`source_type` is `bookmark` (someone else's post you saved — rendered `[B]`) or
`paste` (content you pasted yourself — rendered `[P]`, `author="self"`, no
`source_id`). The invariant is enforced in `model/card.py`.

## Retrieval pipeline

```
query ──> qmd (BM25 over the corpus) ──> recency weighting ──> pin bypass
      ──> adaptive fallback ──> top-k ──> [B]/[P] reference rendering
```

- **QMD** (`qmd`) is the local index — SQLite in WAL mode (safe concurrent reads,
  one writer). Resolved from `$XSENSAI_QMD_PATH` or `PATH`; a fresh corpus is
  reindexed best-effort on the read path.
- The corpus lives under `$XSENSAI_CORPUS_PATH` (default
  `~/.local/share/xsensai/corpus`). A missing-but-env-unset corpus is created on
  demand; a set-but-missing path fails loud (`CORPUS_UNAVAILABLE`).
- Tombstoned (`deleted: true`) cards are excluded from retrieval by default.

## Why there is no server-side LLM key

Synthesis (`/xask`) is done by the **host agent** (Claude Code or Codex), not by
xsensai. The Python orchestrator assembles a prompt (retrieved cards wrapped in
`<DATA_TO_ANALYZE>` for injection defense, plus a locked output template and hard
rules); the host completes it. This means: no API keys in the product, no
usage-based AI billing inside the tool, and your data never leaves your machine
for a third-party inference endpoint. It is a deliberate ownership/privacy choice.

## MCP server (the host-agnostic surface)

`xsensai-mcp` speaks JSON-RPC over **stdio** — so it **logs to stderr only**; any
stray stdout write corrupts the protocol and the host disconnects. The tools are
the contract both agents share (see the command→tool map in `AGENTS.md`).
Destructive tools (`delete_bookmark` / `restore_bookmark`) require a 2-call nonce
handshake so a human, not the agent, authorizes the action.

## Error contract

Every user-visible failure is an `xsensai.errors.XSensaiError`:
`[CODE] / cause / attempted / next_action / retryable`. `next_action` names a
concrete recovery the agent can run (an MCP tool or shell command). Non-error
status lines use the sibling `XSensaiInfo` envelope.

## Platform

macOS-only by design: the macOS Keychain holds credentials (never argv/disk), and
`F_FULLFSYNC` gives crash-safe atomic sidecar writes. Non-Darwin fails loud at
setup step 0 (`UNSUPPORTED_PLATFORM`); there is no Linux/Windows shim.
