# Changelog

All notable changes to x-sensai are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), 4-digit semver `MAJOR.MINOR.PATCH.MICRO`.

## [0.5.0.0] - 2026-04-26

Slice 4 — `/xsync` + `/xextract` ship. After this release, you can pull
new bookmarks from X into your corpus on demand. Cron automation lands in
Slice 5; the orchestrator was designed headless-runnable from day one
(/autoplan UC-1=C) so Slice 5 will be "wire up the schedule," not "rewrite
the orchestrator."

Smart-default extraction (UC-2=C): if `/xsync` finds ≤5 new cards, your
host Claude Code session writes the `retrieval_summary` + `retrieval_tags`
inline (snappy). If >5, the cards land with `extraction_pending: true` and
you run `/xextract` later to backfill (no grinding wait on big backfills).

### Added

- **`/xsync` slash command**: one-prompt conversational flow, no flags
  (per CLAUDE.md:90). Modes: since-last-run / backlog / single / preview.
  Inline modifiers: `inline`, `defer`, `commit`, `proceed dirty`. Smart
  default per N≤5/N>5. Per-5-card progress emits during long runs (DX5
  carve-out from /xask's DX8 silence).
- **`/xextract` slash command** (NEW): drains cards with
  `extraction_pending: true`. One-prompt flow per Slice 1+3 precedent.
  Modes: backlog / single / numeric-limit / retry-failed.
- **`xsensai.sync` package** (8 new modules):
  - `service`: single-process orchestrator (E-1 fix). 4 Python entry
    points (`run`, `apply_extraction`, `finalize_run`, `extract_pending`)
    + matching CLI subcommands.
  - `client`: XClient wrapping XDK with auth refresh + rate-limit backoff
    + Spike #6b graceful degradation in `get_thread()` (search_recent →
    search_all on >7-day-old bookmarks → outside_window envelope on 403).
  - `auth`: TokenProviderProtocol + KeychainTokenProvider (manual mode) +
    EnvSecretTokenProvider (Slice 5 cron-ready).
  - `extraction`: `Extractor` protocol with `extract_batch()` (E-1 shape
    fix). HostExtractor produces per-card prompts; DeferredExtractor
    no-ops with pending=True for all.
  - `card_writer`: XDK dict → v2 LoadedCard with `extraction_pending=True`
    invariant at write (E-4 ordering). Author handle sanitized via
    `_safe_handle()` (E-5 defense).
  - `dedup`: `existing_source_ids()` unions parsed-card source_ids +
    filename regex (catches malformed-on-disk cards).
    `source_id_exists_under_lock()` is the S-7 race fix.
  - `checkpoint`: append-on-success JSONL; archives to
    `~/.cache/xsensai/sync-checkpoints/` with 30-day retention (S-5 fix).
  - `heartbeat`: `_sync-status.md` write/read; banner threshold logic;
    `threads_permanently_unfetched` cumulative metric (auto-decision #6).
- **`xsensai.sync.git_check`**: vault cleanliness check + opt-in
  `commit` keyword. Cleans `[INFO/VAULT_DIRTY_FIRST_RUN]` on first run +
  `--proceed-dirty` escape hatch (S-10 fix). All subprocess calls use
  argv list + `--` separator; paths validated via `_assert_inside_corpus`
  (E-5 defense).
- **`xsensai.sync.setup_oauth`**: minimal one-shot PKCE flow.
  127.0.0.1 ephemeral port, state-parameter CSRF defense (E-5).
  `--check` (preconditions only), `--dry-run` (no token write),
  `--copy-url` (manual browser fallback). 4 dedicated error envelopes.
- **`xsensai.sync.log`**: privacy-aware JSONL run log mirroring xask.log.
  Default mode `hash_only`; `XSENSAI_XSYNC_LOG_MODE=full` to opt in.
  `python -m xsensai.sync.log purge` honors retention env.
- **`xsensai.locks` extension**: `LockDomain` enum (`card_write`,
  `index_rebuild`); `with_index_rebuild_lock()` with optional heartbeat
  thread (E-2 fix: heartbeat is diagnostics-only; flock is the truth;
  `threading.excepthook` installed globally to surface daemon-thread failures).
- **Schema additions** (S-8 fix — was missing from original Modify list):
  `thread_fetch_status`, `xsync_run_id` on `CardFrontmatter`. Forward-
  compat: old cards without these fields still load.
- **17 new error/info codes**: 6 OAuth-lifecycle errors (`OAUTH_*`),
  `X_API_RATE_LIMITED`, `X_API_NETWORK_ERROR`, `SYNC_LOCK_HELD`,
  `CORPUS_UNREACHABLE`, `INVALID_FLAGS`; 18 INFO codes covering sync
  lifecycle, thread-fetch outcomes, git surface.
- **Live integration smoke** (gated on `XSENSAI_RUN_LIVE_X_API=1`): manual
  end-to-end test against the user's real X account. See
  `tests/manual/SLICE_4_GAUNTLET.md` (~30 items).
- **Tests**: +110 across 13 new test files. All 465+ tests pass.

### Changed

- `model/card.py`: 2 new fields (`thread_fetch_status`, `xsync_run_id`).
  `populate_by_name=True` on `CardFrontmatter` so the alias `_xsync_run_id`
  works both ways.
- `errors.py`: 11 new ErrorCode entries + 18 new InfoCode entries.
- `locks/filelock.py`: `LockDomain` enum + new `with_index_rebuild_lock()`
  context manager. `WriterKind` adds `xextract`. Existing `card_write`
  helpers unchanged (Slice 2 callers untouched).
- `commands/xhelp.md`: `/xsync` + `/xextract` listed; full inline-override
  vocab section; sync-status banner integration documented.
- `commands/xfind.md`: cross-references the sync-status banner from
  `_sync-status.md` (read post-Slice-4).
- `requirements.in`: `xdk>=0.1.0,<1.0`. `requirements.txt` recompiled
  with hashes via `uv pip compile --generate-hashes`.
- `scripts/install_commands.sh`: data-driven from `commands/*.md` glob
  (D-7 fix). No more hand-maintained "Available:" footer.
- `scripts/dev_refresh.sh`: pre-Slice-4 micro-PR fixed uv-venv vs
  python-venv handling + made the NEXT STEPS message slice-agnostic
  (commit `77d0316` on main).

### Spec deviations (acknowledged + load-bearing)

1. **Manual `/xsync` only this slice**; cron is Slice 5 (UC-1=C answered C
   in /autoplan: design extraction.py + lock semantics for headless-runnable
   so Slice 5 = "wire up the schedule," not "rewrite the orchestrator").
2. **Smart-default extraction** instead of always-inline (UC-2=C). Mirrors
   the spec's existing `defer if N>5` pattern from `/xnote review`.
3. **Cleanliness check + opt-in `commit`** instead of always-auto-commit
   (UC-3=C). Respects existing manual git workflow; warns + opt-in for
   the laptop→phone→laptop cross-machine scenario.
4. **`_sync-status.md` is committed, not gitignored** (D-S3 fix —
   promoted from taste decision to auto-decided): cron's heartbeat must
   be readable on the user's laptop after `git pull`.
5. **No `posts.search_all` tier verification at slice time**; graceful
   degradation in `client.get_thread()` handles 403 by emitting
   `[INFO/SEARCH_ALL_UNAVAILABLE]` once per session.

### Known limitations

- **`/xsync single`** is stubbed in this slice — XDK's bookmark endpoint
  doesn't expose single-bookmark fetch; supporting single mode would
  need a separate `/2/tweets/{id}` integration. Slice 4.5 candidate.
- **Threads for bookmarks >7 days old** can't always be back-fetched.
  `search_recent` returns empty for old conversations. The graceful-
  degradation path tries `search_all` once but it may 403 on tier
  restrictions. The card is still saved with the bookmarked tweet's
  text; only the OP reply chain may be missing. `thread_fetch_status:
  outside_window` records this on the card.
- **Git push** is NOT invoked by `/xsync --commit`. The user pushes
  manually (or Slice 5 cron will). Cross-host conflict resolution
  (`_conflicts.md`, pull-rebase) is Slice 5 work.

## [0.4.0.0] - 2026-04-26

Slice 3 — `/xask` ships. After this release, you can ask grounded questions
of your bookmark corpus, optionally cross-checked against this week's web,
with deterministic citations back to the cards. Synthesis happens in the
host Claude Code session (no server-side LLM dep, no API keys, no cost
accounting needed for /xask itself).

### Added

- **`/xask` slash command**: one-prompt thinking session. "What's your
  question?" + inline overrides (`no decay`, `skip pins`, `no web`,
  `challenge`). Output uses the locked template (`## From your corpus`,
  optional `## Internal tension`, optional `## Web this week`,
  `## Synthesis`, `## References` with `[B]`/`[P]` citations).
- **`xsensai.xask.service`**: thin Python orchestrator. Deterministic
  top-3 re-rank via stable sort `(combined_score DESC, captured DESC, id
  ASC)`. Real asyncio parallelism — web fork + retrieval overlap, end-to-
  end latency is `max(retrieval, web)` not sum. Branch table covers
  empty corpus, NO_CORPUS_MATCH, web miss/empty/parse/timeout, challenge
  no-real-dissent.
- **`xsensai.web_fork.last30days_runner`**: subprocess wrapper for the
  external `last30days` Claude Code skill. Env-scrubbed (no Anthropic /
  X tokens leak), executable-path validated (rejects executables not
  owned by the user), 20s soft deadline, well-formed status outcomes.
- **`xsensai.synthesis.template`**: locked output-template constant +
  structural validator. CLI: `python -m xsensai.synthesis.template
  validate`. The slash command pipes its draft through this before emit;
  invalid drafts get one stricter re-prompt, then emit raw with banner.
- **`xsensai.xask.log`**: privacy-aware JSONL question log
  (`~/.cache/xsensai/xask-log.jsonl`). Default mode `hash_only` strips
  question text — only `q_hash` + meta logged. `XSENSAI_XASK_LOG_MODE=full`
  opts in to text logging. `python -m xsensai.xask.log purge` honors
  `XSENSAI_XASK_LOG_RETENTION_DAYS` (default 90). File mode 0600, dir 0700.
  Schema includes `prompt_template_version` + `service_version` +
  `output_sha256` for bisect.
- **`xsensai.errors.XSensaiInfo`** (sibling of `XSensaiError`): structured
  envelope for non-error status lines (web miss, no_results, challenge
  dup) so branch outcomes stay contract-compliant. Renders as
  `[INFO/CODE] {cause}\n{action_or_note}\nSource: {source}`.
- **3 new error codes**: `WEB_FORK_FAILED`, `EMPTY_CORPUS`,
  `TEMPLATE_VALIDATION_FAILED`. **5 new info codes**: `NO_CORPUS_MATCH`,
  `WEB_NO_FRESH`, `WEB_TIMEOUT`, `WEB_PARSE`, `CHALLENGE_NO_DISSENT`.
- **`xask_capabilities()` MCP tool** (read-only): deploy-status helper
  exposing `{ok, version, prompt_template_version, web_fork_available,
  web_fork_path, log_path, log_mode}`. Restores the MCP `tools/list`
  health signal that the Slice 3 reshape would otherwise have eliminated.
- **5 prompt-injection adversarial fixtures**:
  `tests/fixtures/prompt_injection/injection_in_{body,author,why_saved,source_url,tags}`
  with canary strings `INJECTED_<n>`. Each renders cleanly via
  `corpus.load_card` + `format_reference`.
- **`tests/test_xask_injection_live.py`** (gated on
  `XSENSAI_RUN_INTEGRATION=1`): boots a temp QMD-indexed corpus with the
  fixtures + asserts `INJECTED_<n>` strings appear ONLY inside
  `<DATA_TO_ANALYZE>` wraps in the assembled synthesis prompt.
- **`tests/manual/SLICE_3_GAUNTLET.md`** (~22 items): post-merge human
  walkthrough covering happy path, web fork, challenge mode, fuzzy
  override, branch table, injection canaries, log + privacy, concurrency.
- **`tests/test_docs.py`** (DX9 doc-CI grep): asserts `/xask` is
  documented in CLAUDE.md, README.md, TROUBLESHOOTING.md,
  commands/xhelp.md, CHANGELOG.md — and that all 4 override tokens +
  4 new env vars + `xask_capabilities` are documented.
- **2 new env vars**: `XSENSAI_LAST30DAYS_PATH` (default
  `~/.claude/skills/last30days/scripts/last30days.py`),
  `XSENSAI_XASK_WEB_TIMEOUT_S` (default `20`). **2 privacy env vars**:
  `XSENSAI_XASK_LOG_MODE` (default `hash_only`),
  `XSENSAI_XASK_LOG_RETENTION_DAYS` (default `90`).

### Changed

- `commands/xhelp.md` updated: `/xask | live`, removed deferred
  `ask_bookmarks` MCP tool entry, added `/xask` override vocabulary section.
- `CLAUDE.md` updated: new `## Slice 3 — what works` section,
  `/xask override vocabulary` section parallel to `/xfind`'s,
  `xask_capabilities` added to deploy-status enumeration, 4 new env vars
  documented.
- Spec deviation: the locked spec lists `ask_bookmarks(question, ...)` as a
  server-side MCP synthesis tool. Slice 3 deliberately ships
  synthesis-as-host-Claude-session instead — slash command works in Claude
  Code only, not Claude Desktop / mobile via raw MCP. Trade accepted per
  CEO autoplan reshape; revisit as Slice 3.5 if non-CC surfaces become
  load-bearing.

### Tests

- 55 new unit tests (template validator, last30days runner with
  env-scrub assertions, log with concurrency + privacy + retention,
  injection fixture corpus integrity + verbatim regression on `.raw.txt`,
  service orchestration with branch-table + deterministic re-rank +
  parallel-overlap timing).
- All Slice 1+2's 257 tests still pass; 0 regressions.

### Removed (vs Slice 3 v1 draft plan, dropped per CEO reshape)

- Server-side LLM stack: no Anthropic SDK dep, no `src/xsensai/llm/`, no
  `src/xsensai/cache/`, no API key plumbing, no cost accounting.
- `ask_bookmarks` MCP tool (deferred to hypothetical Slice 3.5).
- `list_pending_xask` / `get_pending_xask` MCP tools.
- Pending queue + 7-day GC for late web results.
- 4-prompt conversational flow (collapsed to 1 prompt + inline overrides).
- 5 LLM-related error codes (`LLM_API_FAILED`, `LLM_RATE_LIMITED`,
  `LLM_KEY_MISSING`, `LLM_BUDGET_WARN`, `SYNTHESIS_TEMPLATE_VIOLATION` —
  the last is now `TEMPLATE_VALIDATION_FAILED` without LLM-specific
  semantics).
- 5 LLM env vars (`XSENSAI_LLM_PROVIDER`, `XSENSAI_LLM_RERANK_MODEL`,
  `XSENSAI_LLM_SYNTHESIS_MODEL`, `XSENSAI_LLM_API_KEY`,
  `XSENSAI_LLM_BUDGET_WARN_USD`).

## [0.3.0.0] - 2026-04-26

Slice 2 — first writes, concurrency primitives, and the three slash commands that exercise them. After this release, you can `/xpaste` content into your corpus, `/xnote` annotate cards (single or weekly review walk), and `/xpin` to bias retrieval. Mid-write crashes never corrupt cards. Two simultaneous writers serialize cleanly. Aborted pastes are recoverable.

### Added

- **`/xpaste` slash command**: 6-step conversational paste flow with strict-`y` confirmation. Empty `why_saved` flips `why_saved_pending: true` so the card auto-queues for review. 24h content-fingerprint dedup surfaces "duplicate of {id}" instead of writing a second card. Multi-paragraph content supported.
- **`/xpaste recover` mode**: type `recover` at the first prompt to list and promote previously-aborted pastes from the inbox. The promote flow auto-clears the inbox entry on successful write.
- **`/xnote` slash command**: single-card mode (id / URL / keyword resolution) + review walk mode. Walk per-card actions: `a` annotate, `w` defer one week, `e` mark ephemeral, `s` skip, `stop` exit. Mid-walk `stop` records a `_review-cursor.json` checkpoint so the next session resumes from where you left off.
- **`/xpin` slash command**: pin / unpin / list. Strict accept tokens. List mode shows author, captured date, and `why_saved` for every pinned card.
- **6 new MCP write tools**: `paste_bookmark`, `annotate_card`, `set_pin`, `list_pinned`, `due_cards_for_review`, `recover_aborted_paste` (back-compat). Mutation tools require explicit `user_confirmed: true` (the slash commands set this after a y/n prompt).
- **6 new MCP wire-up tools**: `write_paste_snapshot`, `clear_paste_snapshot`, `list_recoverable_pastes`, `get_aborted_paste`, `get_review_cursor`, `set_review_cursor`. Used by the slash commands; safe for direct invocation.
- **Lock module** (`src/xsensai/locks/`): `card_write` lock via `fcntl.flock(LOCK_EX|LOCK_NB)` + UUID4 fencing token. Concurrent `/xpaste` from two terminals: one wins, the other gets `[LOCK_HELD]` with the holder's PID + manual escape hint. OS auto-releases on process death.
- **Atomic write helper** (`storage/sidecar.durable_replace`): `.tmp` + `os.fsync` + `os.replace` + parent-directory fsync, with macOS `fcntl.F_FULLFSYNC` for APFS power-loss durability. Two durability tiers (`full` for cards, `metadata` for lock JSON). Cross-device rename detected and surfaced as `DISK_WRITE_FAILED`.
- **Immutable per-version sidecars**: `raw_path` includes a 12-char checksum prefix so a crash mid-mutation never leaves a `.md` referencing torn sidecar bytes. Old versions are GC'd after the new `.md` commits successfully (bounded disk growth).
- **Read-side reindex trigger**: `engine.search()` checks `_index-dirty` marker on entry, runs `qmd update -c xsensai-cards`, then queries. Concurrent searches coalesce via in-process `asyncio.Lock` so two simultaneous `/xfind` calls don't double-spawn `qmd update`. The `/xpaste` → `/xfind` round-trip works in one session.
- **Path traversal guard**: `validate_card_id` (strict regex `^[A-Za-z0-9][A-Za-z0-9._-]*$`) + `_assert_inside_corpus` close the path-traversal class on every MCP `id` argument.
- **iCloud detection**: corpus path under `Mobile Documents/` or other iCloud-synced locations gets a one-time stderr warning at startup.
- **Crash injection**: `XSENSAI_CRASH_AFTER_STEP=N` env var (steps 1-4) for deterministic atomic-write failure tests, replacing flaky SIGKILL timing.
- **`scripts/dev_refresh.sh`**: one-shot post-merge refresh (git pull + pip install + install_commands.sh + restart checklist).
- **Manual gauntlet checklist**: `tests/manual/SLICE_2_GAUNTLET.md` (30-item human run before merge) covering conversational flow drift the unit tests can't catch.
- **CI nightly lane**: `.github/workflows/ci.yml` runs `XSENSAI_RUN_INTEGRATION=1` subprocess concurrency tests on a daily schedule.
- **TROUBLESHOOTING.md stubs**: keyed entries for `LOCK_HELD`, `MID_WRITE_DETECTED`, `PASTE_EMPTY`, `PASTE_CRASHED`, `USER_CONFIRMATION_REQUIRED`, `V1_MUTATION_BLOCKED`.
- **Audit log**: `_v1-upgraded.jsonl` records every refused v1 mutation so Slice 6 migration knows which cards to prioritize re-fetching.

### Changed

- **`paste_bookmark`, `annotate_card`, `set_pin`** now require `user_confirmed: bool` as a positional parameter (no default). Calls without it return `[USER_CONFIRMATION_REQUIRED]`. The slash commands set this after explicit y/n confirmation.
- **`get_bookmark` error envelope** now uses the slim write-tool shape (no `hits`/`meta`/`rendered_markdown` clutter on a single-card-fetch error path).
- **Error envelope `details` field** uniformly present (None when absent) across all read and write tools — generic error handlers can now read `error.details` from any of the 15 MCP tools.
- **`list_pinned` and `due_cards_for_review`** now return `{count, total, has_more}` for pagination + use the metadata-only `iter_cards_metadata` path (skips sha256 sidecar verification when only frontmatter fields are needed). `due_cards_for_review` also returns the current `_review-cursor.json` value and skips past it for resume-aware walks.
- **`paste_bookmark`** wraps its lock + write in `asyncio.to_thread` so the event loop isn't blocked during fsync.
- **iter_cards self-heal** gates orphan `.tmp` deletion on mtime > 300 seconds; younger tmps are presumed in-flight by another writer (closes a race where concurrent `/xfind` could unlink a live `.tmp` mid-write).
- **`paste_bookmark` filename selection** moved INSIDE the `card_write` lock context (closes a namespace race where two concurrent pastes could pick the same id).
- **`why_saved` whitespace handling** unified across `paste_bookmark` and `annotate_card`: whitespace-only counts as empty, flips `why_saved_pending: true` consistently.
- **`XSENSAI_VAULT_INBOX` override** validated against `$HOME` or vault-root membership; out-of-bounds paths fall back to level-2/3 (vault inbox or corpus inbox) instead of writing arbitrary files.
- **`python-frontmatter` re-emit** uses `sort_keys=False` (no key-reorder regression on v1 card mutation).

### Fixed

- New error codes `LOCK_HELD`, `STALE_LOCK_RECLAIMED`, `MID_WRITE_DETECTED`, `PASTE_EMPTY`, `PASTE_CRASHED`, `V1_MUTATION_BLOCKED`, `USER_CONFIRMATION_REQUIRED` registered in `errors.py` with TROUBLESHOOTING.md entries each.
- v1 cards (no `raw_path` / `raw_checksum`) are refused for mutation by `/xnote` and `/xpin` with `[V1_MUTATION_BLOCKED]` instead of synthesizing raw_bytes from rendered body — preserves the verbatim guarantee until Slice 6 migration ships proper XDK re-fetch.
- `_v1-upgraded.jsonl` audit log writes are atomic-append + `fsync`'d (POSIX guarantees atomic appends below PIPE_BUF; entries are ~150 bytes).
- Inbox marker injection class closed: snapshot_id strictly UUID4-validated; `_escape_html` strips CR/LF in addition to `-->`.
- 10MB content cap on `/xpaste` (configurable via `MAX_CONTENT_BYTES` constant); error message references the constant so changes don't drift.
- `slug.disambiguate_slug` capped at 1000 attempts; pathological loops surface as `[INTERNAL_ERROR]`.

### Tests

- 257 tests passing (251 unit + 6 integration). Net new since 0.2.0.0: +180 tests across 11 new files.
- Subprocess-fanout property test: 10 concurrent processes racing for the lock — exactly one acquires per round (closes the dual-acquire race class).
- Crash-injection tests at all 4 `durable_replace` steps (no flaky SIGKILL timing).
- Path-traversal regression tests: `id="../../etc/passwd"`, `id="foo/bar"`, `id=".hidden"`, NUL bytes — all rejected at `validate_card_id`.
- v1 multi-section body refusal: cards with `## Thread` / `## Video Transcript` content blocked from mutation with audit log entry.
- 10MB content cap boundary tests (exact + plus-one).

## [0.2.0.0] - 2026-04-25

### Added
- **`/xfind` slash command** for Claude Code: prompts for a query, searches your bookmark corpus, returns ranked references with `[B]` (bookmarks) / `[P]` (pastes) markers. Supports inline overrides (`no decay`, `skip pins`).
- **`/xhelp` slash command** listing every command and tool available now and planned.
- **`search_bookmarks` MCP tool** reachable from any Claude conversation: ranked top-N matches with structured payload (`hits`, `meta`, `rendered_markdown`).
- **`get_bookmark` MCP tool** to fetch full card detail by id (returned by `search_bookmarks`).
- **v1 read adapter** (`storage/v1_adapter.py`) so the existing vault works on day one without waiting for Slice 6 migration. Handles the canonical-v1, minimal-v1 (`source`+`author`), and manual-note schemas.
- **Card model** (Pydantic): strict source-type invariants, tz-aware UTC datetimes, sha256 sidecar verification, grapheme-cluster-aware reference truncation.
- **Retrieval engine**: async QMD subprocess wrapper, recency-weighted scoring (90-day half-life, future-date clamped, pinned bypass), pin-dominance bound, adaptive fallback (top-score + margin + dispersion).
- **Quality gate**: 15-query golden-set evaluation (top-1 93%, top-3 100% on fixture corpus). `xsensai-eval-history` console script tracks trend over time.
- **Bootstrap + install scripts**: `bootstrap_qmd.sh` (idempotent QMD collection setup) and `install_commands.sh` (content-aware copy with backup-on-edit).
- **Verbatim fuzz fixtures**: round-trip tests for triple-dash bodies, triple-backticks, and `## Content` literals.

### Changed
- `xsensai.errors.XSensaiError` is no longer a frozen dataclass (Python's exception machinery needs `__traceback__` mutation in async contexts).
- New error code `CORPUS_UNAVAILABLE` distinguishes "corpus path missing" from "no matches found."
- `pyproject.toml`: added `python-frontmatter`, `regex`, `pytest-asyncio` to dependencies. Hash-locked via `requirements.txt`.

### Tests
- 77 unit + integration tests, all passing.
- Coverage spans card model, sidecar, corpus iteration with dup-defense, scoring properties, adaptive fallback, format truncation, MCP subprocess round-trip.
- Real-vault smoke (via `/qa`): 26/31 cards loaded; top-3 hit rate 88% on a hand-picked golden set.

## [0.1.0] - 2026-04-25

### Added
- Slice 0 — project skeleton, error contract module (`XSensaiError`), MCP server with `ping` smoke tool.
- Verification spikes: QMD locking story, XDK availability, OAuth rotation behavior.
- CI scaffolding (`.github/workflows/ci.yml`).
