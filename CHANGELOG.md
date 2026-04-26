# Changelog

All notable changes to x-sensai are recorded here. Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), 4-digit semver `MAJOR.MINOR.PATCH.MICRO`.

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
