# CLAUDE.md — x-sensai project routing

## Source of truth

The locked product spec lives at:

`/Users/naveedhedayati/Documents/Vault/02_projects/x-sensai/v2-build-spec.md`

Read it first for any non-trivial change. It went through CEO + autoplan reviews; design decisions are intentional.

## Active slice

See `SLICE_0_PLAN.md` (and successive `SLICE_N_PLAN.md` files). The Slice 1 plan is `~/.claude/plans/zippy-crafting-wreath.md` (with full /autoplan review report appended); the consolidated implementation spec lives in its "EFFECTIVE SLICE 1" section.

## Build sequence

Shipped releases per version: see [CHANGELOG.md](./CHANGELOG.md).

1. **Slice 0** — spikes + skeleton + `ping` smoke + `errors.py`. **Shipped (v0.1.0).**
2. **Slice 1** — card model + retrieval + `search_bookmarks` + `get_bookmark` + `/xfind` + `/xhelp` + v1 read adapter. **Shipped (v0.2.0.0).**
3. **Slice 2** — locks + sidecar atomic write + `/xpaste` + `/xnote` + `/xpin`. **Shipped (v0.3.0.0).**
4. **Slice 3** — `/xask` + last30days web fork + grounded synthesis (in host Claude Code session, no server-side LLM dep). **Shipped (v0.4.0.0).**
5. **Slice 4** — XDK sync + `/xsync`.
6. **Slice 5** — GitHub Actions cron.
7. **Slice 6** — v1→v2 migration + setup wizard. (v1 read adapter from Slice 1 deleted then.)

## Slice 1 — what works

- `/xfind` (Claude Code) and `search_bookmarks` (MCP) search the corpus via QMD (BM25), apply recency weighting + pin bypass + adaptive fallback, render `[B]`/`[P]` references.
- `get_bookmark(id)` (MCP) fetches full card detail by id; ids are returned by `search_bookmarks` (= filename without `.md`).
- `/xhelp` lists current + planned surface; static "Sync ships in Slice 4" footer (no `_sync-status.md` parsing yet).
- v1 read adapter loads existing v1-shape cards (no `raw_path`/`raw_checksum`) in-memory. No write-back. Deleted in Slice 6.

## Slice 2 — what works

- `/xpaste` (Claude Code) and `paste_bookmark` (MCP) save a pasted tweet/thread as a v2 card via the conversational flow. Empty/abort spills to `00_inbox/quick.md`; partial state is recoverable via `recover_aborted_paste` (+ wire-ups: `write_paste_snapshot`, `clear_paste_snapshot`, `list_recoverable_pastes`, `get_aborted_paste`).
- `/xnote` (Claude Code) and `annotate_card` (MCP) append a timestamped block to an existing card's `## Notes` section. v1 cards reject the mutation with `[V1_MUTATION_BLOCKED]` until Slice 6 migrates them.
- `/xpin` (Claude Code) and `set_pin` / `list_pinned` / `due_cards_for_review` (MCP) toggle/list pins and surface cards due for re-review (+ wire-ups: `get_review_cursor`, `set_review_cursor`).
- Concurrency: `xsensai.locks.filelock` provides a `card_write` lock via `fcntl.flock` with a UUID4 fencing token. Atomic write helper `durable_replace` uses `F_FULLFSYNC` on macOS for crash-safe sidecar writes.
- Read-side reindex trigger: a `/xpaste` followed immediately by `/xfind` round-trips in one session (QMD `update` is invoked on the read path, coalesced).
- `XSensaiError` codes added: `V1_MUTATION_BLOCKED`, `USER_CONFIRMATION_REQUIRED`.
- New helper script: `scripts/dev_refresh.sh` (re-installs commands + restarts MCP for fast local iteration).

## Slice 3 — what works

- `/xask` (Claude Code) is the thinking-session command. Single prompt: "What's your question?" plus inline override keywords. Calls a thin Python orchestrator (`xsensai.xask.service`) that pulls top-20 candidates from `search_bookmarks`, optionally forks `last30days` for this-week web context (with 20s soft deadline), deterministically re-ranks to top-3, and assembles a synthesis prompt with `<DATA_TO_ANALYZE>` injection-defense wrap + locked output template + hard rules. The host Claude Code session does the synthesis (no server-side LLM, no API keys, no cost accounting). Output template enforced via `xsensai.synthesis.template.validate()` with one re-prompt on failure.
- `xask_capabilities()` (MCP, read-only) exposes deploy-status info: `{ok, version, prompt_template_version, web_fork_available, web_fork_path, log_path, log_mode}`. Used by `/xhelp` and post-merge health checks.
- Privacy-aware question log at `~/.cache/xsensai/xask-log.jsonl` (mode 0600, dir 0700). Default `hash_only` mode logs `q_hash` + meta but NOT raw question text. `XSENSAI_XASK_LOG_MODE=full` to log text. `python -m xsensai.xask.log purge` honors `XSENSAI_XASK_LOG_RETENTION_DAYS`.
- `XSensaiInfo` envelope (sibling of `XSensaiError`) for non-error status lines (web miss / empty / parse / no_corpus_match / challenge_no_dissent). Renders as `[INFO/CODE] {cause}\n{action_or_note}\nSource: {source}` — same contract discipline as errors.
- 5 prompt-injection adversarial fixtures at `tests/fixtures/prompt_injection/` with canary strings; live integration test (`tests/test_xask_injection_live.py`, gated on `XSENSAI_RUN_INTEGRATION=1`) asserts canaries stay inside `<DATA_TO_ANALYZE>` boundaries.

## Slice 1 — config

- **`XSENSAI_CORPUS_PATH`** (default `~/Documents/Vault/04_areas/x-bookmarks/`) — where cards live.
- **`XSENSAI_QMD_PATH`** (default `/Users/naveedhedayati/.bun/bin/qmd`) — QMD binary.
- **`XSENSAI_RUN_INTEGRATION=1`** — enables QMD-dependent integration + golden-eval tests + live injection regression test.
- **QMD collection name:** `xsensai-cards` (created by `scripts/bootstrap_qmd.sh`).

## Slice 3 — config

- **`XSENSAI_LAST30DAYS_PATH`** (default `~/.claude/skills/last30days/scripts/last30days.py`) — path to the `last30days` skill script that `/xask` shells out to for web context.
- **`XSENSAI_XASK_WEB_TIMEOUT_S`** (default `20`) — soft deadline for the web fork. On timeout, output renders `## (web context unavailable this run — timeout)`.
- **`XSENSAI_XASK_LOG_MODE`** (default `hash_only`) — `off` | `hash_only` | `full`. Privacy default strips question text; `full` logs raw text for empirical steering.
- **`XSENSAI_XASK_LOG_RETENTION_DAYS`** (default `90`) — purge threshold for `python -m xsensai.xask.log purge`.

## /xfind override vocabulary

Append to your query:
- `no decay` — disable recency weighting
- `skip pins` — exclude pinned cards from results

Override fuzzy match: if you say "no recency" or "no pins", `/xfind` will note the canonical phrasing and run with defaults.

## /xask override vocabulary

Append to your question:
- `no decay` — disable recency weighting on retrieval
- `skip pins` — exclude pinned cards from retrieval
- `no web` — skip the `last30days` web fork entirely
- `challenge` — run an extra retrieval pass that hunts for a dissenting card

Override fuzzy match: if you say `dissent`, `recency`, `web off`, etc., `/xask`'s service detects the canonical phrase, applies the override, and prepends a one-line note to your output mapping the fuzzy phrase to the canonical token.

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

## Deploy Configuration (configured by /setup-deploy)

- **Platform:** local-install (Python package + MCP server + Claude Code slash commands)
- **Production URL:** N/A — runs in user's local Claude Desktop / Claude Code
- **Deploy workflow:** none (manual install post-merge)
- **Deploy trigger:** manual — run `./scripts/install_commands.sh` after merging
  (bootstraps QMD `xsensai-cards` collection + copies `commands/*.md` to `~/.claude/commands/`)
- **Deploy status command:** `pytest` (CI-verified) + manual `/xfind` smoke in Claude Code
- **Merge method:** squash (default)
- **Project type:** Python library + MCP server + Claude Code slash commands

### Custom deploy hooks

- **Pre-merge:** `pytest` (CI runs on every push; gated on PR before merge)
- **Deploy trigger:** `./scripts/install_commands.sh` (manual, post-merge, per-machine)
- **Deploy status:** MCP `tools/list` returns `search_bookmarks` + `get_bookmark` + `ping` + Slice 2 tools (`paste_bookmark`, `recover_aborted_paste`, `annotate_card`, `set_pin`, `list_pinned`, `due_cards_for_review`, plus 6 wire-ups) + Slice 3 tool `xask_capabilities`
- **Health check:** `XSENSAI_RUN_INTEGRATION=1 pytest tests/eval/golden_set.py` (top-3 ≥ 80%)
- **Post-deploy verification:** in Claude Code, type `/xfind` and confirm a query returns hits

### Future deploy work (informational; not yet active)

- **Slice 5:** GitHub Actions cron (`.github/workflows/sync.yml`) syncs new bookmarks every 2-3 days. When this lands, this section moves up to active config and the cron's last-success timestamp becomes a real deploy-status signal.
- **Slice 6:** `scripts/setup.sh` setup wizard automates first-time install (currently a stub).
- **Post-Slice-6:** optional PyPI publish workflow on git tag (`pip install xsensai` distribution).
