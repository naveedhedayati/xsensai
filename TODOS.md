# TODOS

Project work organized by skill/component, then by priority (P0 = blocker through P4 = idea), then a Completed section at the bottom. Format reference: `~/.claude/skills/gstack/review/TODOS-format.md`.

---

## /xpaste

### `/xpaste recover` end-to-end UX polish

**Priority:** P1
**Origin:** Slice 2 v0 limitation noted during /ship (2026-04-26).
**Description:** The recovery flow works at the MCP layer (`list_recoverable_pastes`, `get_aborted_paste`, `clear_paste_snapshot` all wired), but the slash command markdown describes manual steps (user picks an entry by number from a list). Smoothing this into a single "press y to promote the most recent" flow would close the discoverability gap the DX subagent flagged.
**Files:** [commands/xpaste.md](commands/xpaste.md), [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py)

### `/xpaste` step-3 tentative snapshot timing edge cases

**Priority:** P2
**Origin:** Slice 2 v0 limitation. The slash command markdown says "call `write_paste_snapshot` after step 1", but Claude Code's exact send semantics for multi-paragraph pastes mean the snapshot may be written for a partial paste if the user's first message is only the opener.
**Description:** Verify with the manual gauntlet (G6) that multi-paragraph pastes get the FULL content into the tentative snapshot, not just the first message. If they don't, add a "wait for content stability" pattern.
**Files:** [commands/xpaste.md](commands/xpaste.md), [tests/manual/SLICE_2_GAUNTLET.md](tests/manual/SLICE_2_GAUNTLET.md)

---

## /xnote

### Review walk cursor — `s` skip should NOT advance the cursor

**Priority:** P1
**Origin:** Slice 2 v0 review-walk semantics. Slash command markdown says `s` (skip) advances the cursor so the card "doesn't re-appear next walk." But for genuinely-undecided skips, the user probably wants the card to re-surface — `s` should mean "I'm not deciding right now," not "permanently skip."
**Description:** Add a 4th option `n` (next without state change) that advances within the current walk only. Repurpose `s` as "ephemeral-skip" (mark `next_review_at = now + 30 days`).
**Files:** [commands/xnote.md](commands/xnote.md), [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py)

### Journal-based review walk enrichment (deferred from autoplan)

**Priority:** P3
**Origin:** Slice 2 EFFECTIVE plan deferred this from spec ("recently-active project at capture time" mining from journal entries within ±1 day). Defer to a post-Slice-3 polish micro-slice or fold into Slice 4 with sync.
**Description:** The DX subagent's empathy narrative noted that without the journal context, the review walk is "just data entry." This is the enrichment that makes review *cheaper than* in-the-moment annotation.
**Files:** [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py) (`due_cards_for_review`)

---

## locks/

### ~~Slice 4: add `index_rebuild` + `transcribe_queue` lock domains~~ — CLOSED

**Status:** Done in Slice 4 (v0.5.0.0). `index_rebuild` lock + heartbeat thread shipped per /autoplan E-2 fix (heartbeat is diagnostics-only; flock is the truth). `transcribe_queue` deliberately NOT added per S-4 fix (YAGNI — add when `/xtranscribe` actually ships).

### Slice 5: cron context detection in service.run()

**Priority:** P1 (unblocks Slice 5 cron)
**Origin:** /autoplan F8 (DX subagent finding) — XSENSAI_VAULT_DIRTY_PROCEED env var is wrong default for cron (cron's vault is a fresh checkout, never "dirty" in the user's sense). Slice 5 needs an explicit "I am cron, ignore that env var" detection.
**Description:** Add `mode: SyncMode = "headless"` detection at service.run start; in headless mode, automatically set proceed_dirty=True since the cron vault is a clone. Document in CLAUDE.md.
**Files:** [src/xsensai/sync/service.py](src/xsensai/sync/service.py), CLAUDE.md.

---

## storage/

### Stale sidecar GC for the v1-card-touched corner case

**Priority:** P3
**Origin:** Slice 2 v0 known-debt: the `write_card` sidecar GC unlinks the OLD sidecar after a successful new commit. But the FIRST mutation of a v1 card (refused with `V1_MUTATION_BLOCKED`) doesn't trigger any GC — and v1 cards have no sidecar to GC anyway. Once Slice 6 migration runs, every former-v1 card gets a sidecar and the regular GC kicks in. Until then, no leaks.
**Description:** Verify after Slice 6 ships that the GC interaction with newly-migrated cards behaves correctly. Add a regression test if needed.
**Files:** [src/xsensai/storage/corpus.py](src/xsensai/storage/corpus.py) (`write_card`)

### Optional: `_pinned.json` / `_due.json` index files for O(1) list operations

**Priority:** P4
**Origin:** /review F4 — the Performance specialist's first-cut suggestion was caching. Slice 2 shipped the cheaper fix (`iter_cards_metadata` skip-verify), which is sufficient at current corpus sizes. Cache files become worth it past ~1000 cards.
**Description:** When you cross 500-1000 cards, add `_pinned.json` (updated by `set_pin`) and `_due.json` (updated by `annotate_card`) so `list_pinned` and `due_cards_for_review` are O(1) reads instead of O(N) scans.
**Files:** [src/xsensai/storage/corpus.py](src/xsensai/storage/corpus.py), [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py)

---

## /xfind

### QMD underscore tokenization

**Priority:** P3
**Origin:** Slice 2 manual gauntlet (G30) + Codex adversarial review. `search_bookmarks` returns NO_RESULTS for queries containing underscores (e.g., `TEST_GAUNTLET deep work`) because QMD's BM25 doesn't split on `_`. Workaround: query without underscores. Real fix: query-side normalization or QMD config.
**Description:** Either pre-process queries (split on `_` before passing to QMD) or look into QMD's tokenizer options. Affects discoverability of cards whose title/content has snake_case identifiers. Slice 3 considered fixing this incidentally inside `xask.service.parse_overrides` query normalization but deferred — this is a retrieval-layer concern, not /xask-specific.
**Files:** [src/xsensai/retrieval/qmd.py](src/xsensai/retrieval/qmd.py), [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py)

---

## /xask (Slice 3 — shipped, future work)

### Slice 3.5: server-side `ask_bookmarks` MCP tool for non-Claude-Code surfaces

**Priority:** P3 (only if it becomes load-bearing)
**Origin:** Slice 3 CEO autoplan reshape — synthesis-as-host-Claude-session ships now. The locked spec lists `ask_bookmarks(question, challenge, no_decay)` as a server-side MCP tool with synthesized output. Slice 3 deferred this because (a) single-user-on-Mac, (b) Claude Desktop access to /xask is "nice to have" per spec, (c) mobile is bonus, not committed. If the user starts using Claude Desktop or mobile MCP for /xask-style questions, this tool needs to ship.
**Description:** Lift the synthesis prompt + DATA_TO_ANALYZE wrap + HARD RULES from `commands/xask.md` into a server-side MCP tool that calls Anthropic SDK directly with API key from macOS Keychain. Restores the original v1 plan's server-side surface but as Slice 3.5 instead of Slice 3.
**Files:** would add `src/xsensai/llm/`, `src/xsensai/cache/`, extend `src/xsensai/mcp_server/server.py`.

### `/xask` decision-brief output template variant

**Priority:** P4 (taste decision deferred from Slice 3 CEO review)
**Origin:** Slice 3 Codex CEO challenge (TD-CEO-1) — argued for a `claim, supporting cards, counter-card, next action, confidence` output template instead of the spec's generic synthesis. Spec is locked; output template stays. Revisit after 2 weeks of real /xask use if the generic template feels too soft.
**Description:** Add an opt-in template variant; switch via inline `decision-brief` keyword (matching the inline override pattern). Single source of truth in `src/xsensai/synthesis/template.py`.
**Files:** [src/xsensai/synthesis/template.py](src/xsensai/synthesis/template.py), [commands/xask.md](commands/xask.md)

---

## CI / infra

### Re-evaluate concurrency tests on default CI lane

**Priority:** P3
**Origin:** /review UC4 + Codex adversarial. Currently `test_concurrency_paste.py` is gated on `XSENSAI_RUN_INTEGRATION=1` and only runs on the nightly CI lane. After 2-3 weeks of nightly runs prove stability (no flakes), promote to the default lane.
**Description:** Watch the nightly job for 2-3 weeks. If green, move the integration tests to the default CI lane.
**Files:** [.github/workflows/ci.yml](.github/workflows/ci.yml)

### Unify `VERSION` and `pyproject.toml` as single source of truth

**Priority:** P3
**Origin:** Slice 7.5 /review F7 (caught `pyproject.toml` stale at 0.7.0.0 — pre-existing drift since Slice 7) + /plan-eng-review on v0.9.1.0 (caught it would drift again every release without a structural fix).
**Description:** `pyproject.toml` currently hardcodes `version = "..."` independently from the `VERSION` file. Each release someone has to remember to bump both. Standard fix: `pyproject.toml` reads version dynamically — `dynamic = ["version"]` + `[tool.setuptools.dynamic] version = {file = "VERSION"}` is the simpler change (no new build-time dep). Add 1 doc-consistency test asserting the two stay in sync. ~1 hr work.
**Files:** [pyproject.toml](pyproject.toml), [VERSION](VERSION), [tests/test_doc_consistency.py](tests/test_doc_consistency.py)

---

## Completed

(Slice 2 — see [CHANGELOG.md](CHANGELOG.md) v0.3.0.0 for the full ship log. Plan: `~/.claude/plans/slice-2-draft.md` EFFECTIVE SLICE 2 section. All 25 contract items DONE.)

---

## Slice 4 (shipped — see CHANGELOG v0.5.0.0). Future work spec'd:

### Slice 4.5: `/xsync single <tweet-id>` real implementation

**Priority:** P3 (only if user uses it)
**Origin:** Slice 4 stubbed single-tweet mode because XDK's bookmark endpoint doesn't expose single-bookmark fetch. Adding it requires `/2/tweets/{id}` integration (separate XDK method, separate auth scope check).
**Description:** Implement `XClient.get_tweet(id)` via XDK's `posts.get_post(id)`. Plumb through `_gather_bookmarks(mode="single", target=...)` so `/xsync 2028162355511583052` actually fetches and writes that one tweet.
**Files:** [src/xsensai/sync/client.py](src/xsensai/sync/client.py), [src/xsensai/sync/service.py](src/xsensai/sync/service.py), test_sync_service.py.

### ~~Slice 5: GitHub Actions cron + git push + cost ceiling~~ — CLOSED

**Status:** Done in Slice 5 (v0.6.0.0). Architectural decision per
/autoplan premise gate: lazy-extract on read in `/xfind` instead of
server-side LLM in cron (preserves "no LLM in CI" reshape; closes the
spec-promise gap empirically validated by Spike #10's 26.7pp recall
finding). 95 new tests; all 5 sub-items shipped; 4 spikes ran.

### Future: bounded-async backlog fetch (T-2 from /autoplan, deferred per recommendation)

**Priority:** P3 (post-launch, only if user reports backfill latency complaint)
**Origin:** /autoplan Phase 3 T-2 — Codex argued for bounded async (3-5 concurrent XDK calls) for backlog mode; subagent argued YAGNI (sequential is right since wall-clock is dominated by extraction at 50× the network cost). Shipped sequential per recommendation.
**Description:** Wrap `XClient.iter_bookmarks()` with `asyncio.gather` + dynamic-throttle on 429. ~50 LoC + 2 tests. Adds the asyncio surface to XClient.
**Files:** [src/xsensai/sync/client.py](src/xsensai/sync/client.py), test_sync_client.py.

### Future: sync_status() MCP tool for cross-Claude-surface health checks

**Priority:** P3 (only if user actually uses Claude Desktop for /xsync diagnostics — currently /xsync is Claude Code only)
**Origin:** /autoplan open-question A. Modeled after Slice 3's `xask_capabilities()`. Cheap to add (~10 LoC).
**Description:** Add MCP tool `sync_status()` that returns the parsed `_sync-status.md` heartbeat as a structured dict. Lets Claude Desktop sessions answer "when did sync last run?" without inferring from corpus state.
**Files:** [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py), test_mcp_server.py.

### Future: /xtranscribe slash command + transcribe_queue lock domain

**Priority:** P2 (when user actually wants video transcripts on cards)
**Origin:** Spec section "Sync automation" step 7. Slice 4 sync writes `media.video_transcript_status: queued` for cards with video; the queue draining is a separate slice.
**Description:**
1. New slash command `/xtranscribe` with backlog / single / retry-failed modes.
2. New service module `xsensai.sync.transcribe` — yt-dlp + whisper integration.
3. Activate the `transcribe_queue` LockDomain (currently NOT reserved per S-4 YAGNI; add when this slice spec'd).
4. Cost ceiling per-card and per-run.
5. `[INFO/TRANSCRIBE_DONE]` and `[INFO/TRANSCRIBE_PARTIAL]` envelopes.
**Files:** new `src/xsensai/sync/transcribe.py`, `commands/xtranscribe.md`.

---

## Slice 6 — what shipped (v0.7.0.0, see CHANGELOG)

### ~~Slice 6: Tombstone schema~~ — CLOSED
**Status:** Done in Slice 6 (v0.7.0.0). `deleted: bool = False` + `deleted_at: Optional[datetime] = None` on `CardFrontmatter`, `@model_validator` for invariants, `include_deleted=False` filter cascading through corpus + retrieval engine + MCP tools, `delete_bookmark`/`restore_bookmark`/`list_deleted` MCP tools (lock-first-then-load), `/xrestore` slash command, tombstone-aware sync dedup with sticky deletion. /xdelete slash command deferred to Slice 7+ (see below).

### ~~Slice 6: Union-frontmatter merge driver~~ — CLOSED (shadow mode)
**Status:** Done in Slice 6 in **shadow mode** (v0.7.0.0). `compute_union_candidate` computes spec-literal union (frontmatter union + prefer-local body, list union for `tags`/`applicability`/`media.external_urls`); `append_shadow_union_log` writes idempotent entries to `_conflicts.md`. **Does NOT change rebase outcome** — fail-loud stays primary. Promotion gate is now Slice 7+ work.

### ~~Slice 6: Setup wizard~~ — CLOSED
**Status:** Done in Slice 6 (v0.7.0.0). Full wizard at `src/xsensai/entrypoints/setup_wizard.py` with `--preflight` / `--oauth` / `--deploy-key` / `--gh-secrets` / `--gh-vars` / `--first-run` / `--migrate` / `--all` / `--resume`. Idempotent steps + `~/.cache/xsensai/setup-state.json` for resume after partial failure.

### ~~Slice 6: v1→v2 migration~~ — CLOSED
**Status:** Done in Slice 6 (v0.7.0.0). `scripts/migrate_v1_to_v2.py` with `--dry-run` / `--apply` / `--rollback` (mutually exclusive). Per-card byte-exact rollback journal at `{corpus}/migrate_v1_to_v2.rollback.jsonl`. v1 adapter retained one release as soft-landing.

---

## Slice 7+ — items deferred from Slice 6 /autoplan

### Slice 7+: Promote union merge from shadow to primary

**Priority:** P2 (when telemetry confirms zero manual overrides)
**Origin:** Slice 6 /autoplan premise-gate decision. Both Codex and Claude subagent flagged silent-corruption risk in replacing fail-loud directly. Shadow mode logs the union candidate; promotion gate requires ≥3 real-world conflicts logged with zero manual overrides AND explicit per-key policy review (pinned, deleted, extraction_pending).
**Description:** Replace the fail-loud invocation in `git_push.commit_and_push()` with `compute_union_candidate` + write merged bytes + `git add` + `git rebase --continue`. Remove `_conflicts/<run-id>/` directory writes. Update `docs/CONFLICT_RESOLUTION.md` to reflect auto-merge.
**Files:** [src/xsensai/sync/git_push.py](src/xsensai/sync/git_push.py), [src/xsensai/sync/git_merge.py](src/xsensai/sync/git_merge.py), [docs/CONFLICT_RESOLUTION.md](docs/CONFLICT_RESOLUTION.md).

### Slice 7+: Delete v1 adapter

**Priority:** P3 (after gate condition observed)
**Origin:** Slice 6 /autoplan premise-gate decision. Adapter kept as soft-landing; promote when both gates pass: 0 v1 cards observed for 14 consecutive days AND `_v1-upgraded.jsonl` shows `event: migrated` rows for all known v1 ids.
**Description:** Delete `src/xsensai/storage/v1_adapter.py`, remove `_is_v1_card` + `_v1_blocked_response` + `V1_MUTATION_BLOCKED` from `src/xsensai/mcp_server/server.py` and `src/xsensai/errors.py`, update CLAUDE.md build sequence.
**Files:** [src/xsensai/storage/v1_adapter.py](src/xsensai/storage/v1_adapter.py), [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py), [src/xsensai/errors.py](src/xsensai/errors.py).

### ~~Slice 7+: Confirmation nonce/handshake for destructive tools~~ — CLOSED

**Status:** Done in Slice 7 (v0.8.0.0). 2-call flow on `delete_bookmark` /
`restore_bookmark` (host calls without nonce → server returns
`[NONCE_REQUIRED]` envelope with `<<<NONCE: ABCD-EFGH>>>` in
`rendered_message`; user echoes; host re-calls with `confirmation_nonce`).
`xsensai.mcp_server.nonce_store` module with idempotent issue,
tombstone-on-redeem, `time.monotonic` TTL (90s), `threading.Lock`,
`reset()` for tests. 5 error envelopes (`NONCE_REQUIRED`, `INVALID`,
`EXPIRED`, `OPERATION_MISMATCH`, `ALREADY_REDEEMED`). One-release
`user_confirmed: bool` legacy shim. `XSENSAI_DESTRUCTIVE_BYPASS=1` env
var for scripted maintenance. /xdelete slash command deferred to Slice
7.5/8 (see below). Plan + dual-voice review at
`~/.claude/plans/vigilant-handshaking-magpie.md`.

### ~~Slice 7.5+: `/xdelete` slash command~~ — CLOSED

**Status:** Done in Slice 7.5 (v0.9.0.0). `commands/xdelete.md` ships
mirroring `commands/xrestore.md`'s 2-call nonce flow + `commands/xnote.md`'s
single-mode card resolution. Single-mode only by design (ADR-001 — per-id
attestation invariant; batch mode would nonce-habituate the user).
`commands/xhelp.md` Planned table entry removed; /xdelete is in Available
now with the permissions.ask integration line. Plan + dual-voice review at
`~/.claude/plans/deep-meandering-waffle.md`. **Completed:** v0.9.0.0
(2026-04-29).

### Slice 7.5+: `.claude/settings.json` permissions.ask auto-installer (NEW, also closed in v0.9.0.0)

**Status:** Done in Slice 7.5 (v0.9.0.0). New `scripts/_settings_merge.py`
helper invoked by `scripts/install_commands.sh` writes the
`mcp__xsensai__delete_bookmark` and `mcp__xsensai__restore_bookmark`
entries into the user-global `~/.claude/settings.json` `permissions.ask`
array. Idempotent; backs up `{path}.bak.{ts}` (chmod 0600, 3-most-recent
retention) on every real change; safe-skips on malformed JSON; warns on
`[PERMISSIONS_WILDCARD_OVERRIDE]` when pre-existing `permissions.allow`
wildcards subsume the gate; uses `fcntl.flock` + atomic `os.replace` for
crash-safe concurrent installs. Closes Slice 7's honest-framing gap: the
cryptographic gate (Claude Code's per-tool permission prompt) is now the
real user-attestation boundary, with the in-band 8-character nonce
handshake stacked on top. Spec at `docs/PERMISSIONS_ASK.md`.
**Completed:** v0.9.0.0 (2026-04-29).

### ~~Slice 7.5+: Remove legacy `user_confirmed` shim from delete/restore~~ — CLOSED

**Status:** Done in Slice 7.5.1 (v0.9.1.0). `user_confirmed: Optional[bool]`
parameter dropped from `delete_bookmark` and `restore_bookmark` signatures.
The legacy-kwarg branches in both functions and the `legacy: bool` parameter
on `_nonce_required_envelope` removed. `TestLegacyKwargShim` flipped to
`TestLegacyKwargRemoved` with 3 `pytest.raises(TypeError, match="user_confirmed")`
tests. F11 `test_legacy_plus_nonce_prefers_nonce` deleted (no longer
reachable). 32 `user_confirmed=True` kwargs stripped from `test_tombstone.py`
delete/restore call sites; `XSENSAI_DESTRUCTIVE_BYPASS=1` autouse fixture
stays. `annotate_card`/`set_pin` keep `user_confirmed: bool` per ADR-002.
Cutover gate met: >=7-day soak from v0.8.0.0 + ≥1 successful `/xdelete`
round-trip with `permissions.ask` active + zero unexpected `NONCE_*`
clusters in privacy-aware xask log. Plan + /plan-eng-review at
`~/.claude/plans/clever-dazzling-spark.md`.

### Slice 7.5+: Telemetry-driven AE10 revisit

**Priority:** P3 (gated on observed friction).
**Origin:** Slice 7 /autoplan eng + DX dual-voice partial pushback.
AE10 says "always consume nonce on redeem" (single rule, no special
cases). Subagent argued for refunding on infrastructure errors
(`LOCK_HELD`, `CORPUS_UNAVAILABLE`). Currently consume-on-everything;
revisit if observed friction surfaces.
**Description:** Add lightweight telemetry to count `LOCK_HELD` /
`CORPUS_UNAVAILABLE` events that consumed a nonce. If count > 3/week,
add a refund-on-infra-error path. Until then: stable single-rule
contract.
**Files:** [src/xsensai/mcp_server/nonce_store.py](src/xsensai/mcp_server/nonce_store.py).

### Slice 7+: Tri-lateral conflict resolution

**Priority:** P3 (when mobile writer ships)
**Origin:** Slice 6 shadow union is 2-way only (local + remote; 3-way base used only for list-union dedup). Tri-lateral surface emerges when mobile writer (spec line 192) ships.
**Description:** Either CRDT-shaped frontmatter (per spec line 213) or a documented manual fallback for 3+ writer divergence.
**Files:** [src/xsensai/sync/git_merge.py](src/xsensai/sync/git_merge.py).

---

## Slice 5 — deferred items (originally for Slice 6 — now Slice 7+ items above)

### Slice 6: Tombstone schema for deleted cards

**Priority:** P2 (Slice 6 work — deferred from Slice 5 Eng E4)
**Origin:** /autoplan Eng review E4. Codex flagged "tombstone deferral creates integrity issues as autonomous writers increase." Slice 5 review confirmed the schema work is genuinely Slice 6 size: `CardFrontmatter` ConfigDict forbids unknown fields per `card.py:36`, so adding `deleted: true` requires schema update + retrieval exclusion + dedup respect + mutation path + tests. Not 10 LoC.
**Description:** Add `deleted: bool = False` (or `deleted_at: Optional[datetime]`) to `CardFrontmatter`; `dedup.existing_source_ids()` should respect tombstones (cron skips replay-write of deleted cards); retrieval should exclude tombstoned cards; provide a "soft-delete" path via /xnote or a new tool. Replay-write of deleted-on-Mac cards is rare in practice — defer until tombstone friction surfaces.
**Files:** [src/xsensai/model/card.py](src/xsensai/model/card.py), [src/xsensai/sync/dedup.py](src/xsensai/sync/dedup.py), retrieval modules, mcp_server tool surface.

### Slice 6: Union-frontmatter merge driver

**Priority:** P3 (Slice 6 polish — deferred from Slice 5 Eng E4)
**Origin:** Slice 5 ships fail-loud `.local`/`.remote` sidecars instead of spec line 213-214's union-frontmatter. Acceptable Slice 5 deviation per /autoplan; replace once multi-stream conflict surface (mobile + paste + cron) is clearer.
**Description:** Replace `git_merge.resolve_card_conflict_failloud` with a deterministic union-frontmatter resolver: union frontmatter (collision rules: `pinned: true` wins, `notes` arrays union with content-hash dedup, prefer-local for everything else); body prefers local; conflict resolution logged to `_conflicts.md`. Tri-lateral support (3+ hosts) requires either a CRDT-shaped frontmatter or a manual fallback.
**Files:** [src/xsensai/sync/git_merge.py](src/xsensai/sync/git_merge.py).

### Slice 6: Setup wizard for cron secrets (`scripts/setup.sh`)

**Priority:** P2 (improves DX D1; current --emit-secrets-stdin helper is the manual half)
**Origin:** docs/CRON_SETUP.md is a 45-90 min manual runbook. A guided wizard could collapse the deploy-key + secret-set + workflow-trigger steps into a single command.
**Description:** Build out `scripts/setup.sh` (currently a stub) into a full guided wizard: detect missing prereqs, gen deploy key, prompt for vault repo slug, write secrets via `gh`, trigger first manual run, verify green. ~1-2 hr of bash + UX polish.
**Files:** [scripts/setup.sh](scripts/setup.sh), reference [docs/CRON_SETUP.md](docs/CRON_SETUP.md).

### Slice 5.1: Wire BudgetTracker into XClient (deferred from /review)

**Priority:** P2 (cap is currently advisory, not enforced)
**Origin:** /review on Slice 5 build (2026-04-28). `BudgetTracker` is
constructed in `entrypoints/headless.py` but never threaded into XClient
or service.run. `record_api_call()` and `should_bail()` are never invoked
from production code paths. The advertised cost cap is fictional;
defended only by `max_pages=10` (added during /review) and the 10-min
workflow timeout.
**Description:** Add `tracker: Optional[BudgetTracker] = None` parameter
to `XClient.iter_bookmarks` + `XClient.get_thread`, call
`tracker.record_api_call("bookmark_fetch")` / `"thread_search"` per
network call. Plumb tracker through `service.run` → `_gather_bookmarks` →
xclient calls. On `tracker.should_bail()`, raise via
`tracker.cost_limit_error()`. Estimated ~30 LoC + 3 tests.
**Files:** [src/xsensai/sync/client.py](src/xsensai/sync/client.py),
[src/xsensai/sync/service.py](src/xsensai/sync/service.py),
[src/xsensai/entrypoints/headless.py](src/xsensai/entrypoints/headless.py).

### Slice 5.2: lazy-extract reclaim race (run_id mismatch)

**Priority:** P3 (60s window, narrow blast radius)
**Origin:** /review on Slice 5 build (2026-04-28). When a /xfind claim
goes stale (>60s), the reclaim path lets the second caller take ownership.
But the first caller's host LLM call is still alive — when it returns,
`apply_extraction(run_id=<old>)` passes the `is_extraction_owner_path`
check (any `lazy-extract-` prefix) and overwrites the new claim's
eventual results.
**Description:** Add `lazy_extract_run_id: Optional[str]` to
`CardFrontmatter`. `claim_for_lazy_extract` writes it alongside the
flag + timestamp. `service.apply_extraction` rejects on the
`lazy-extract-` path when caller's run_id != stored run_id. ~15 LoC + 1
test.
**Files:** [src/xsensai/model/card.py](src/xsensai/model/card.py),
[src/xsensai/sync/lazy_extract.py](src/xsensai/sync/lazy_extract.py),
[src/xsensai/sync/service.py](src/xsensai/sync/service.py).

### Post-launch: Per-day cost ceiling persistence

**Priority:** P4 (current per-attempt is acceptable; bump to per-day if usage surfaces the limitation)
**Origin:** /autoplan Eng E9. Current `BudgetTracker` is per-process-attempt; crash + restart resets. Per-day persistence (in heartbeat with UTC-midnight reset) would survive workflow_dispatch retries but adds state machine complexity. Documented limitation; defer.
**Description:** Persist `api_calls_today` field in `_sync-status.md` heartbeat with UTC-midnight reset. On startup, load same-day counter and continue incrementing.
**Files:** [src/xsensai/sync/cost_ceiling.py](src/xsensai/sync/cost_ceiling.py), [src/xsensai/sync/heartbeat.py](src/xsensai/sync/heartbeat.py).

### Post-launch: `git_check.py` porcelain v2 hardening

**Priority:** P4 (existing v1 string-slicing works; defended by `_assert_inside_corpus` at boundary)
**Origin:** /autoplan Eng E6. New Slice 5 modules (git_merge, git_push) use porcelain v2 NUL-delimited parsing; existing modules still use v1 with string slicing. Not bug-fixing — hardening.
**Description:** Migrate `git_check.py` (and any remaining v1 callers) to `git status --porcelain=v2 -z` with NUL parsing; consolidate via shared helper in a new `xsensai.sync.git_porcelain` module.
**Files:** [src/xsensai/sync/git_check.py](src/xsensai/sync/git_check.py), possibly new `git_porcelain.py`.

### Post-launch: ApiExtractor (server-side LLM in cron)

**Priority:** P3 (revisit after 30 days of Slice 5 use; Spike #10's 26.7pp recall finding makes this load-bearing for `/xask` quality on never-queried cards)
**Origin:** /autoplan premise gate Approach B. Slice 5 picked Approach E (lazy-extract on read in `/xfind`) which closes the gap for queried cards but leaves never-queried cards extraction_pending forever. `/xask`'s top-20 retrieval pays the recall tax on those.
**Description:** New `ApiExtractor(Extractor)` class that calls Anthropic SDK directly with key from new GH Actions secret. Plumbed through `entrypoints.headless` as an alternative to `DeferredExtractor` for cron. Adds Anthropic SDK dep + `ANTHROPIC_API_KEY` secret + ~$5/month at expected volume. Or: separate weekly extraction cron job (Approach G — own concurrency group) to drain the backlog cron deposits.
**Files:** [src/xsensai/sync/extraction.py](src/xsensai/sync/extraction.py) extension, possibly `.github/workflows/extract.yml`.

### Post-launch: sync_status() MCP tool

**Priority:** P3 (only if Claude Desktop usage of /xsync diagnostics emerges)
**Origin:** /autoplan open-question A. Already in TODOS pre-Slice-5; Slice 5 didn't ship it. Modeled after `xask_capabilities()`. Cheap (~10 LoC).
**Description:** MCP tool that returns parsed `_sync-status.md` heartbeat as structured dict. Lets Claude Desktop sessions answer "when did sync last run?" without inferring from corpus state.
**Files:** [src/xsensai/mcp_server/server.py](src/xsensai/mcp_server/server.py).

