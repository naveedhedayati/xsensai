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

### Slice 5: GitHub Actions cron + git push + cost ceiling

**Priority:** P1 (next slice in build sequence)
**Origin:** Slice 4 deliberately scoped to manual /xsync per /autoplan UC-1=C answered C. Engine is headless-runnable; Slice 5 just needs the cron yaml + secret rotation + push automation.
**Description:**
1. `.github/workflows/sync.yml` — cron schedule (every 2-3 days per spec), env secrets for X client_id + refresh token (use `EnvSecretTokenProvider`).
2. Add `entrypoints.headless.run()` that wraps `service.run` + auto-handles extraction (subagent OR server-side LLM — Slice 5 architectural decision).
3. Git push + pull-rebase + `SYNC_PUSH_REJECTED.md` flag handling per spec section "Sync automation" step 9.
4. Cost ceiling: hard cap on per-run X API spend; bail with `[COST_LIMIT_REACHED]` envelope. (Not needed for manual /xsync but cron should not blow up the user's budget on a runaway loop.)
5. Cross-host conflict resolution per spec line 213-214.

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

