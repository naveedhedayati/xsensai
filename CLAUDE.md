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
6. **Slice 5** — GitHub Actions cron + git push + cost ceiling + cross-host conflict resolution + lazy-extract on read in `/xfind` (Spike #10 promoted from polish to load-bearing) + heartbeat instrumentation + `/xhelp` cron banner. **Shipped (v0.6.0.0).**
7. **Slice 6** — v1→v2 migration with byte-exact rollback + tombstone schema (`deleted` + `deleted_at` + invariant validator) + MCP-only `delete_bookmark`/`restore_bookmark` + `/xrestore` slash command + shadow-mode union-frontmatter merge driver (logs candidate; fail-loud stays primary) + guided setup wizard. v1 adapter retained 1 release as soft-landing. **Shipped (v0.7.0.0).**

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

## Slice 5 — what works

- **Scheduled sync via GitHub Actions** (`/.github/workflows/sync.yml`).
  Cron `0 7 */2 * *` (every 2 days at 07:00 UTC) + manual
  `workflow_dispatch`. Concurrency group `xsensai-sync` prevents
  overlapping runs. Uses an SSH deploy key (write access) to push back
  to the user's vault repo.
- **`xsensai.entrypoints.headless`** (NEW): orchestrator. Reads env
  (XSENSAI_X_REFRESH_TOKEN, XSENSAI_X_CLIENT_ID, XSENSAI_X_CLIENT_SECRET
  if Confidential), builds `EnvSecretTokenProvider` (Slice 4 seam) +
  `DeferredExtractor` (Slice 4 default for headless) + `BudgetTracker`,
  calls `service.run(mode="headless")`, on success calls
  `git_push.commit_and_push`. Exit codes: 0 full / 0 no-new / 1 partial /
  2 fatal. CLI: `--check` (preflight) + `--emit-secrets-stdin`
  (DX D1 helper that prints ready-to-paste `gh secret set` commands
  reading from Keychain).
- **`xsensai.sync.git_push`** (NEW): commit + pull-rebase + push with
  retry up to 3. Stage allowlist (`*.md`, `*.raw.txt`,
  `_sync-status.md`, `_conflicts/<run-id>/*`, `_conflicts.md`); never
  `git add -A`. Excludes `*.rej` / `*.local` / `*.remote` outside
  `_conflicts/`. After max retries: writes static-template
  `SYNC_PUSH_REJECTED.md` flag (no secret interpolation per autoplan E7).
- **`xsensai.sync.git_merge`** (NEW): cross-host conflict resolver.
  Two paths:
  - **Heartbeat fast-path** (autoplan E1 / CRITICAL): on conflict in
    `_sync-status.md`, regenerates from in-memory `SyncStatus`
    (max-merge counters/timestamps), restages, continues rebase.
    Without this, every cron-after-manual cycle would livelock.
  - **Card fail-loud sidecar** (autoplan E2 + Spike #8): on conflict
    in `*.md` / `*.raw.txt`, captures `:2:` (remote) and `:3:` (local)
    blobs from rebase index BEFORE abort, then `git rebase --abort` →
    `git reset --hard origin/main` → write
    `_conflicts/<run-id>/<card>.local|.remote` → log to `_conflicts.md`
    → commit marker → push. Exit 2 with `[CRON_CONFLICT_UNRESOLVED]`.
    User resolves manually per `docs/CONFLICT_RESOLUTION.md`.
  - Porcelain v2 NUL-delimited parsing (autoplan E6); every parsed
    path validated through `_assert_inside_corpus` at the boundary.
- **`xsensai.sync.cost_ceiling.BudgetTracker`** (NEW): per-attempt X
  API call cap, default 200, env override `XSENSAI_CRON_API_CAP`.
  Per-attempt semantics, not per-day (autoplan E9 — documented
  limitation; GH Actions retry policy must be 0 to prevent
  multiplicative cost amplification under failure).
- **`xsensai.sync.lazy_extract`** (NEW): claim/release coordination
  for `/xfind` lazy-extract-on-read (autoplan + Spike #10 / 26.7pp recall
  finding). Two-`/xfind`-at-once race protection via
  `lazy_extract_in_progress` flag under `card_write` lock; 60s stale
  reclaim. Run-id prefix `lazy-extract-{uuid}`; `service.apply_extraction`'s
  `is_extraction_owner_path` check accepts it.
- **Heartbeat extension** (`heartbeat.py`): 5 new fields —
  `last_cron_run`, `last_cron_success`, `consecutive_cron_failures`,
  `last_cron_runner`, `oldest_pending_age_days`. Cron-only counters are
  NEVER reset by manual `/xsync` (autoplan E5 — prevents healthy manual
  sync from masking dead cron). New banner methods:
  `should_show_cron_stale_banner()`, `should_show_extraction_backlog_banner()`,
  `cron_never_fired()`. Pre-Slice-5 status files read with defaults
  (backwards compatible).
- **`/xfind` lazy-extract** (`commands/xfind.md`): when search surfaces
  a top-3 result with `extraction_pending: true`, calls
  `lazy_extract.claim_for_lazy_extract` → host LLM extraction →
  `service.apply_extraction` (with `lazy-extract-{uuid}` run_id), then
  re-renders the result with summary+tags. Concurrency: second `/xfind`
  on same card sees claim flag, prints `(another session is extracting;
  body-only)`. Failure path: `release_lazy_claim` + render body-only
  with footnote. Hard cap: skip lazy pass if >3 results need extraction
  (banner instead). Override: `no lazy` keyword.
- **Banner integration**: `/xfind`, `/xask`, `/xhelp` surface ONE-LINE
  banners for cron-stale / extraction-backlog-growing / cron-never-fired.
  Once-per-session via `~/.cache/xsensai/banner-state.json` (4-hour
  cooldown per banner kind). `/xpaste`, `/xnote`, `/xpin` are
  flow-protected — NO banner.
- **Auth redaction helper**: `auth.redact_token_strings()` for any
  text persisted to non-committed logs (autoplan E7 defense in depth).
  Committed flag files use static templates only — verified by
  `test_no_secrets_in_flags`.
- **`SCRIPT.md` updates**: `commands/xfind.md` lazy-extract pass +
  banner; `commands/xask.md` banner integration; `commands/xextract.md`
  repositioned as "backlog drain / repair command" (lazy-extract makes
  it optional in steady state); `commands/xhelp.md` Slice 5 section.
- **Docs**: `docs/CRON_SETUP.md` (45-90 min one-time setup runbook);
  `docs/CONFLICT_RESOLUTION.md` (manual `_conflicts/<run-id>/`
  resolution workflow); `TROUBLESHOOTING.md` extended with all 8 new
  envelopes.
- **Decisions deferred to Slice 6** (per autoplan):
  - Tombstone schema (`deleted: true` field on CardFrontmatter +
    retrieval exclusion + dedup respect) — Slice 5 scope was too tight.
    Replay-write of deleted-on-Mac cards is rare in practice.
  - Union-frontmatter merge driver replacing fail-loud sidecars —
    deferred until multi-stream conflict surface is clearer.

## Slice 6 — what works

Slice 6 ships v1→v2 migration with byte-exact rollback, tombstone schema
+ MCP-only delete/restore + `/xrestore` slash command, shadow-mode
union-frontmatter merge driver, and a guided setup wizard. Single PR.
Plan + decision audit at
`~/.claude/plans/immutable-waddling-quokka.md` (with full /autoplan
review report appended).

- **v1→v2 migration script** (`scripts/migrate_v1_to_v2.py`) — three
  exclusive modes (`--dry-run` / `--apply` / `--rollback`) via argparse
  mutually exclusive group; requires interactive `Type APPLY/ROLLBACK`
  confirmation unless `--yes` is passed. Per-card byte-exact rollback
  journal at `{corpus}/migrate_v1_to_v2.rollback.jsonl` — full original
  `.md` bytes (base64) + sha256, fsync'd BEFORE the corresponding
  `write_card` mutation so rollback restores byte-exact even after a
  mid-flight crash. `--rollback` reads the journal in reverse, atomically
  replaces each migrated `.md`, unlinks the new sidecar, archives the
  journal on success.
- **v1 read adapter retained** (`src/xsensai/storage/v1_adapter.py`) —
  per /autoplan eng-review premise gate, NOT deleted in Slice 6. Stays
  alive for one release as soft-landing if migration corrupts a card
  discovered later. Promote for deletion when 0 v1 cards observed in
  corpus for 14 consecutive days.
- **Tombstone schema** (`src/xsensai/model/card.py`):
  `deleted: bool = False` + `deleted_at: Optional[datetime] = None`
  + `@model_validator` enforcing the invariant (deleted=True ↔
  deleted_at set; deleted=False ↔ deleted_at None). Defaults preserve
  backward compat with existing v2 cards on disk.
- **`include_deleted=False` filter** added to `iter_cards`,
  `iter_cards_metadata`, and `load_card_by_id` ([src/xsensai/storage/corpus.py](src/xsensai/storage/corpus.py)).
  Default-False means every existing call site inherits exclusion.
  Internal paths (sync dedup, lazy-extract, /xrestore) pass
  `include_deleted=True` explicitly.
- **Retrieval-layer filter** at [src/xsensai/retrieval/engine.py](src/xsensai/retrieval/engine.py):66-75 —
  per Codex eng-review (retrieval calls QMD → `load_card` directly,
  not `iter_cards`, so corpus-level filter doesn't apply). CANDIDATE_LIMIT
  over-fetches enough to absorb tombstone exclusion at top_k.
- **`paste_bookmark` fingerprint dedup** sees tombstones
  ([src/xsensai/storage/corpus.py:593](src/xsensai/storage/corpus.py))
  via `iter_cards_metadata(include_deleted=True)` so within-24h paste
  dedup still fires after a delete.
- **MCP tools** ([src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py)):
  - `delete_bookmark(id, user_confirmed)` — soft-delete; lock-first-then-load
    pattern (per Codex eng-review) to prevent the lost-update race
    where concurrent annotate/pin resurrects a deleted card from a
    stale snapshot.
  - `restore_bookmark(id, user_confirmed)` — un-tombstone.
  - `list_deleted(limit?)` — list recently-deleted (read-only).
  - `annotate_card`/`set_pin` now load with `include_deleted=True` and
    raise `[TOMBSTONE_BLOCKED]` if the target is deleted.
  - `[V1_MUTATION_BLOCKED]` `next_action` updated to point at
    `./scripts/setup.sh --migrate` (post-DX-review fix — was the stale
    "wait for Slice 6" string).
- **Slash command**: new `commands/xrestore.md` mirroring `/xpin`'s
  conversational shape. Lists recently-deleted, picks by number,
  confirms, calls `restore_bookmark`. NO `/xdelete` slash command this
  slice (deferred to Slice 7+ per /autoplan premise gate); use the
  MCP tool directly.
- **Tombstone-aware sync dedup** ([src/xsensai/sync/dedup.py](src/xsensai/sync/dedup.py)):
  new `existing_source_ids_with_tombstones() → Tuple[Set[str], Dict[str, bool]]`
  helper; legacy `existing_source_ids()` keeps `Set[str]` signature
  for backward compat (Codex caught: signature change would break
  `service.py:620, 643` callers). `service.run` threads the dict to
  the per-card write loop; tombstoned source_ids are skipped with a
  log line and counted into `n_skipped_tombstoned`. Cron honors sticky
  deletion: a deleted-on-Mac card stays deleted even when its source_id
  is still in the user's X bookmarks.
- **Shadow-mode union merge driver**
  ([src/xsensai/sync/git_merge.py](src/xsensai/sync/git_merge.py)):
  new `compute_union_candidate(local, remote, base) → (bytes, diff)` —
  spec-literal rules (frontmatter union with prefer-local on collision;
  list union for `tags`/`applicability`/`media.external_urls`;
  prefer-local body). NO clever per-key policy (`pinned: true wins`,
  etc. were flagged by Codex as new-policy-not-spec-locked; revisited
  at promotion). New `append_shadow_union_log()` writes to
  `_conflicts.md` with `(run_id, card_path)` idempotency to prevent
  3x retry-loop duplication. Wired into `git_push.commit_and_push()`
  BEFORE the existing fail-loud sequence (which destroys index access
  to the conflicted blobs); shadow does NOT change rebase outcome —
  fail-loud stays primary in Slice 6.
- **Setup wizard**
  ([src/xsensai/entrypoints/setup_wizard.py](src/xsensai/entrypoints/setup_wizard.py)):
  guided full-flow mirror of `setup_oauth.py`'s structure. 8 mutually
  exclusive flags (`--preflight` / `--oauth` / `--deploy-key` /
  `--gh-secrets` / `--gh-vars` / `--first-run` / `--migrate` /
  `--all` / `--resume`). State at `~/.cache/xsensai/setup-state.json`
  enables `--resume` (skip-completed semantics). Each step idempotent:
  `--deploy-key` queries existing keys via `gh api repos/X/keys` and
  skips on title match; `--gh-vars` upserts; `--first-run` checks
  recent successful runs. `scripts/setup.sh` is now a thin wrapper.
- **`install_commands.sh` v1 detection** — per DX-review fix: after
  install, count v1 cards in the corpus and print
  *"Detected N v1 cards. Run `./scripts/setup.sh --migrate` ..."* if any
  exist. Prevents the onboarding regression both /autoplan voices flagged.
- **Error envelopes** ([src/xsensai/errors.py](src/xsensai/errors.py)):
  - `TOMBSTONE_BLOCKED` — canonical envelope referenced from all sites,
    with a test asserting NO `(Slice 7)` substring anywhere
    (codification of the dual-voice convergent finding).
  - `NO_ROLLBACK_JOURNAL` — migration `--rollback` with no journal.
  - `SETUP_GH_AUTH_REQUIRED`, `SETUP_DEPLOY_KEY_REJECTED`,
    `SETUP_FIRST_RUN_FAILED` — full XSensaiError envelopes per the
    contract (cause / attempted / next_action / retryable).
- **Tests** (+55 new, 683 total): `test_tombstone.py` (29),
  `test_v1_migration.py` (10), `test_git_merge_union_shadow.py` (7),
  `test_setup_wizard.py` (9). Covers schema invariants, backward compat,
  delete/restore flow, mutation guards, retrieval/list filtering,
  byte-exact rollback (happy + corrupt-line skip), shadow log
  retry-idempotency, mutual-exclusion CLI contract, error envelope
  format.
- **Known limitation (Slice 6)**: `user_confirmed: bool` on
  `delete_bookmark` and `restore_bookmark` is host-attestable, not
  user-attestable — the host LLM sets it. Prompt-injection from card
  body could trick the host into invoking with `user_confirmed=True`
  without explicit user authorization. Slice 7 will add a confirmation
  nonce/handshake. Documented in TROUBLESHOOTING.md.

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
- **`XSENSAI_VAULT_DIRTY_PROCEED`** (default unset) — set `1` / `true` / `yes` to permanently opt in to "sync over uncommitted xsync output" without typing `proceed dirty` each time. **Slice 5: ignored in `mode="headless"`** — cron's vault clone is always "dirty"-OK by design (autoplan F8 / TODOS P1 fix); the env var stays for manual /xsync.
- **macOS Keychain entries** (not env vars, but config-shaped): service `x-sensai`, accounts `x-api-refresh-token` + `x-api-client-id` + `x-api-client-secret` (last only for Confidential clients). Written by `setup_oauth.py`, read by `KeychainTokenProvider` + `get_stored_client_id` + `get_stored_client_secret`. Backed by the `keyring` library which uses Security.framework via PyObjC (no `security` CLI subprocess — keeps the token off `ps -ef`).

## Slice 5 — config

- **`XSENSAI_CRON_API_CAP`** (default `200`) — per-attempt X API call cap
  for cron. Cron bails with `[COST_LIMIT_REACHED]` when exceeded;
  next scheduled run resumes from checkpoint. Per-attempt semantics
  (autoplan E9): GH Actions retry policy must be 0 (set in `sync.yml`)
  to prevent multiplicative amplification under failure.
- **GitHub Actions secrets** (NOT env vars on the user's Mac): set on
  the xsensai repo via `gh secret set`. Required:
  `XSENSAI_X_REFRESH_TOKEN`, `XSENSAI_X_CLIENT_ID`, `VAULT_DEPLOY_KEY`
  (private half of the deploy key). Optional:
  `XSENSAI_X_CLIENT_SECRET` (Confidential clients only).
- **GitHub Actions variables** (NOT secrets — slug isn't sensitive):
  `VAULT_REPO` (e.g., `naveedhedayati/obsidian-vault`),
  `VAULT_CORPUS_SUBPATH` (defaults to `04_areas/x-bookmarks`).
- **Cron schedule**: `0 7 */2 * *` (every 2 days at 07:00 UTC). Manual
  `workflow_dispatch` always available.
- **Banner state file**: `~/.cache/xsensai/banner-state.json` —
  per-machine, 4-hour cooldown per banner kind. Auto-created by /xfind
  on first banner suppression. Safe to delete.
- **`card_write` lock is per-host** (per-checkout). Cross-host
  coordination is git-only via pull-rebase + `--force-with-lease` +
  fail-loud sidecar (autoplan E11 — documented to prevent
  implementer surprise).

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
