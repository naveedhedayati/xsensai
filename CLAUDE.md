# CLAUDE.md — x-sensai project routing

## Source of truth

The locked product spec lives at:

`/Users/naveedhedayati/Documents/Vault/02_projects/x-sensai/v2-build-spec.md`

Read it first for any non-trivial change. It went through CEO + autoplan reviews; design decisions are intentional.

## Active slice

See `SLICE_0_PLAN.md` (and successive `SLICE_N_PLAN.md` files). Each slice has a CEO + Eng review completion summary appended to its plan file.

## Build sequence

1. **Slice 0** — spikes + skeleton + `ping` smoke + `errors.py`. **Current.**
2. **Slice 1** — card model + retrieval + `search_bookmarks` + `/xfind` + `/xhelp`.
3. **Slice 2** — locks + sidecar write + `/xpaste` + `/xnote` + `/xpin`.
4. **Slice 3** — `/xask` + last30days web fork + synthesis.
5. **Slice 4** — XDK sync + `/xsync`.
6. **Slice 5** — GitHub Actions cron.
7. **Slice 6** — v1→v2 migration + setup wizard.

## Rules of the road

- **Spec is locked.** Don't relitigate decisions captured in the autoplan review unless the user explicitly asks.
- **Error contract.** Every user-visible error uses `xsensai.errors.XSensaiError` (Slice 0 ships the module). Format: `[CODE] / cause / attempted / next_action / retryable`. Never `print()` an error message — always go through `XSensaiError.format()` and emit on the right channel.
- **MCP stdio gotcha.** The MCP server uses stdio for protocol traffic. **Log to stderr only.** Any stray `print()` to stdout corrupts the JSON-RPC stream and Claude Desktop silently disconnects. `import logging; logging.basicConfig(stream=sys.stderr, ...)`.
- **Sidecar pattern.** Each card is two files: `card.md` (frontmatter + rendered body) + `card.raw.txt` (byte-exact source). Verbatim regression tests run against `card.raw.txt`. See spec section "Card data model".
- **Conversational slash commands.** No flag parsing. Each command prompts for inputs in plain language. Empty/abort flows save partial state to `00_inbox/quick.md` (paste) or write nothing (other commands).
- **Hash-locked deps.** Use `uv pip compile requirements.in -o requirements.txt --generate-hashes`. Adding a dep means updating `requirements.in` and re-compiling.

## Testing rules

- `pytest` runs from the repo root.
- Verbatim fuzz corpus tests (Slice 1+) run against `card.raw.txt`. Use the corpus from `tests/fixtures/verbatim_fuzz/`.
- MCP server smoke test boots a subprocess (matches Claude Desktop runtime).
- No test should hit the network, X API, or LLM APIs. Mock at the boundary.

## External dependencies

- **QMD** (`/Users/naveedhedayati/.bun/bin/qmd`) — full-text + vector search over the corpus. SQLite WAL mode at `~/.cache/qmd/index.sqlite`. WAL gives us safe concurrent reads + 1 writer.
- **last30days** skill (`~/.claude/skills/last30days/`) — runtime dep for `/xask` web fork.
- **XDK** (`pip install xdk`) — X API client. `users.get_bookmarks()` for bookmarks; `posts.search_recent(query="conversation_id:{id}")` for thread fetching. Added in Slice 4.

## Vault layout (off-repo)

The corpus lives in `/Users/naveedhedayati/Documents/Vault/04_areas/x-bookmarks/` (private GitHub repo, git-synced). This project repo only contains code; cards live in the vault repo.
