# CLAUDE.md — x-sensai project routing

## Source of truth

The locked product spec lives at:

`/Users/naveedhedayati/Documents/Vault/02_projects/x-sensai/v2-build-spec.md`

Read it first for any non-trivial change. It went through CEO + autoplan reviews; design decisions are intentional.

## Active slice

See `SLICE_0_PLAN.md` (and successive `SLICE_N_PLAN.md` files). The Slice 1 plan is `~/.claude/plans/zippy-crafting-wreath.md` (with full /autoplan review report appended); the consolidated implementation spec lives in its "EFFECTIVE SLICE 1" section.

## Build sequence

Shipped releases per version: see [CHANGELOG.md](./CHANGELOG.md). Open
follow-up work and deferred polish: see [TODOS.md](./TODOS.md) (organized
by component, then priority).

1. **Slice 0** — spikes + skeleton + `ping` smoke + `errors.py`. **Shipped (v0.1.0).**
2. **Slice 1** — card model + retrieval + `search_bookmarks` + `get_bookmark` + `/xfind` + `/xhelp` + v1 read adapter. **Shipped (v0.2.0.0).**
3. **Slice 2** — locks + sidecar atomic write + `/xpaste` + `/xnote` + `/xpin`. **Shipped (v0.3.0.0).**
4. **Slice 3** — `/xask` + last30days web fork + grounded synthesis (in host Claude Code session, no server-side LLM dep). **Shipped (v0.4.0.0).**
5. **Slice 4** — XDK sync + `/xsync` + `/xextract` + setup_oauth + smart-default extraction + git plumbing. **Shipped (v0.5.0.0).**
6. **Slice 5** — GitHub Actions cron + git push + cost ceiling + cross-host conflict resolution. (Engine is already headless-runnable per Slice 4 UC-1=C; Slice 5 = wire up the schedule.)
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

## Slice 4 — what works

- `/xsync` (Claude Code) ingests new bookmarks from X via XDK. One-prompt conversational flow (no flags per CLAUDE.md:90). Modes: `since-last-run` (default) / `backlog` / `single` (stubbed) / `preview`. Inline modifiers: `inline`, `defer`, `commit`, `proceed dirty`. Smart-default extraction (UC-2=C): inline if N≤5 new cards, deferred (cards land with `extraction_pending: true`) if N>5.
- `/xextract` (Claude Code, NEW) drains `extraction_pending: true` cards. Same one-prompt flow per Slice 1+3 precedent. Modes: `backlog` (all pending) / `single` (one card-id) / numeric-limit / `retry-failed`.
- `xsensai.sync` package: 8 modules — `service` (single-process orchestrator per E-1 fix), `client` (XDK wrapper with auth refresh + rate-limit backoff + Spike #6b graceful degradation in `get_thread()`), `auth` (TokenProvider seam — Keychain + Env), `extraction` (HostExtractor + DeferredExtractor sharing a single `extract_batch()` protocol), `card_writer`, `dedup` (S-7 race recheck under lock), `checkpoint` (E-4 ordering, archives to `~/.cache/xsensai/sync-checkpoints/`), `heartbeat` (`_sync-status.md` committed per D-S3).
- `xsensai.sync.git_check`: vault cleanliness check + opt-in `commit` keyword (UC-3=C). All subprocess calls use argv list + `--` separator; paths validated via `_assert_inside_corpus` (E-5 defense).
- `xsensai.sync.setup_oauth`: minimal one-shot PKCE flow. 127.0.0.1 ephemeral port, state-parameter CSRF defense (E-5). `--check`, `--dry-run`, `--copy-url` modes. 4 dedicated error envelopes for the OAuth lifecycle (port collision, browser, grant refused, Keychain blocked).
- `xsensai.locks` extension: `LockDomain` enum + `with_index_rebuild_lock()` with optional heartbeat thread (E-2 fix: heartbeat is diagnostics-only; flock is the truth; `threading.excepthook` installed globally). Reindex now serialized cross-process between `/xsync` finalize and `/xfind`/`/xask` read-side reindex (S-9 fix).
- Schema: 2 new fields on `CardFrontmatter` — `thread_fetch_status`, `xsync_run_id` (S-8 fix; `model/card.py` was missing from original Modify list and would have failed strict validation).
- Privacy-aware sync log at `~/.cache/xsensai/xsync-log.jsonl` (same chmod 600 + flock pattern as xask). `XSENSAI_XSYNC_LOG_MODE=full` to opt in to full text.
- 17 new error/info codes; full list in `commands/xhelp.md`. Notable: `[INFO/THREAD_OUTSIDE_7DAY_WINDOW]` + `[INFO/SEARCH_ALL_UNAVAILABLE]` (Spike #6b graceful-degradation outcomes).
- Pre-flight: `python -m xsensai.sync.setup_oauth --check` verifies preconditions (X dev app client_id, macOS Keychain availability via `keyring`, 127.0.0.1 port binding, xdk import) without burning a real token.

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

## Slice 4 — config

- **`XSENSAI_X_CLIENT_ID`** — your X dev app's client_id. Required for `setup_oauth.py` AND for `/xsync` (the orchestrator builds the XDK client with it). After running `setup_oauth`, the value is also persisted in Keychain so /xsync from a fresh Claude Code session works without re-exporting.
- **`XSENSAI_X_CLIENT_SECRET`** (optional) — required ONLY if your X dev app is a Confidential Client (the dev portal's "Web App" type). Public Clients (Native App / Single Page App) don't need it. `setup_oauth` accepts it via `--client-secret` and persists to Keychain.
- **`XSENSAI_XSYNC_LOG_MODE`** (default `hash_only`) — `off` | `hash_only` | `full`. Same privacy convention as xask log.
- **`XSENSAI_XSYNC_LOG_RETENTION_DAYS`** (default `90`) — purge threshold for `python -m xsensai.sync.log purge`.
- **`XSENSAI_VAULT_DIRTY_PROCEED`** (default unset) — set `1` / `true` / `yes` to permanently opt in to "sync over uncommitted xsync output" without typing `proceed dirty` each time. Mostly relevant for cron later (Slice 5 will detect "headless context" and override).
- **macOS Keychain entries** (not env vars, but config-shaped): service `x-sensai`, accounts `x-api-refresh-token` + `x-api-client-id` + `x-api-client-secret` (last only for Confidential clients). Written by `setup_oauth.py`, read by `KeychainTokenProvider` + `get_stored_client_id` + `get_stored_client_secret`. Backed by the `keyring` library which uses Security.framework via PyObjC (no `security` CLI subprocess — keeps the token off `ps -ef`).

## /xsync override vocabulary

Append to your input (anywhere on the line — fully conversational, NO flags):
- empty / `latest` / `since` / `new` — sync since last run (default)
- `backlog` / `full` / `everything` / `all` — fetch all bookmarks
- 19-digit numeric or `x.com/.../status/<id>` URL — single-tweet mode (stubbed in Slice 4)
- `preview` / `dry-run` — fetch list, write nothing
- `inline` / `force inline` — force inline extraction regardless of N
- `defer` / `defer all` — force deferred regardless of N
- `commit` / `auto commit` — `git add` + `git commit` after sync (no push)
- `proceed dirty` / `dirty ok` — sync anyway when prior xsync left uncommitted output

## /xextract override vocabulary

- empty / `all` / `backlog` — process every pending card
- 19-digit numeric (card_id stem) — single-card mode
- a small number (`5`) — process at most that many
- `retry` / `failed` — synonym for backlog

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
