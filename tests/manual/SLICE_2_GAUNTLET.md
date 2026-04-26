# Slice 2 Manual Gauntlet

**MUST-RUN before /ship.** Pytest covers the MCP layer (215+ tests as of
Slice 2 ship), but conversational slash command flow drift requires a
human pass. Slice 1 had the `/xfind` override fuzzy-match issue; this
checklist ensures Slice 2 doesn't ship the equivalent for `/xpaste`,
`/xnote`, `/xpin`.

Run after `./scripts/dev_refresh.sh` + restart Claude Desktop + restart
Claude Code. Each item is a single Claude Code session. Mark ☑ when
the observed behavior matches the expected behavior. Any ✗ blocks merge.

---

## /xpaste — paste mode

- [ ] **G1: One-shot happy path.** `/xpaste` → paste 2-3 sentences →
      provide why_saved → empty source_url → tags `test, gauntlet` →
      `y` to confirm. Expected: `Saved card 'paste-YYYY-MM-DD-...'.`
      message; card exists at `$XSENSAI_CORPUS_PATH/paste-...md`;
      `_index-dirty` marker exists in corpus dir.

- [ ] **G2: Empty content rejection.** `/xpaste` → at content prompt,
      send empty / "nevermind" / "cancel". Expected: `ok, nothing
      pasted; pass.` and exit. NO inbox write.

- [ ] **G3: Empty why_saved → pending.** `/xpaste` → paste content →
      hit enter (empty) at why_saved prompt → empty url → empty tags →
      `y`. Expected: `Saved card 'paste-...' (why_saved_pending —
      auto-queued for /xnote review)`. Verify in `/xnote review` step
      G7 below.

- [ ] **G4: y/n confirm — typed `n` cancels with abort recovery.**
      `/xpaste` → paste content → fill prompts → `n` at confirm.
      Expected: `Paste cancelled. To save your content for later: ...`
      message naming an inbox path. Open the path; verify content is
      there with marker `<!-- xsensai-abort-begin -->`.

- [ ] **G5: y/n confirm — typed `yes` is treated as cancel.** `/xpaste`
      → paste content → fill prompts → `yes` at confirm. Expected:
      since strict accept is ONLY `y`, `yes` is treated as "anything
      else" and abort path fires. (If Claude interprets `yes` as `y`,
      flag this as a flow-drift issue and tighten the prompt copy.)

- [ ] **G6: Multi-paragraph paste.** `/xpaste` → paste 3+ paragraphs
      separated by blank lines (e.g., copy from a long article). Hit
      send. Expected: ALL paragraphs appear in the saved card's `##
      Content` section. (Slice 2 NOTE: if any paragraph is truncated,
      re-validate the prompt copy and Claude Code's send-on-blank
      behavior.)

## /xpaste — recover mode

- [ ] **G7: List recoverable.** Run G4 first to plant an abort entry.
      `/xpaste` → first prompt → type `recover`. Expected: shows the
      most recent abort entry's content (first 200 chars), captured
      timestamp, why_saved attempt, source_url. Asks "Promote? (y / l
      to list all / anything else cancels)".

- [ ] **G8: Promote recovered → card lands.** Continue from G7 → `y`.
      Expected: paste flow resumes from why_saved prompt with the
      recovered content. Walk through, confirm. Card lands.
      (LIMITATION: inbox entry NOT auto-cleared in Slice 2 v0; you'll
      see the same entry on next `recover`. Manually trim quick.md if
      desired.)

## /xnote — single mode

- [ ] **G9: Annotate v2 card by id.** From G1 above, copy the card id.
      `/xnote` → paste id at first prompt. Expected: shows `Selected:
      paste-...` plus snippet. Walks 3 prompts (why_saved,
      applicability, pin). Empty answers leave fields unchanged.

- [ ] **G10: Update why_saved.** Continue G9 → at why_saved prompt
      type "updated reason". Empty applicability. Skip pin. Expected:
      `Annotated paste-....` Re-load via `get_bookmark` (or check
      `.md` on disk) — `why_saved: updated reason` and
      `why_saved_pending: false`.

- [ ] **G11: Mutate v1 card refused with clear error.** Find a v1
      card id (anything in the vault that ISN'T a Slice 2 paste). Run
      `/xnote` → paste v1 id. Expected: `[V1_MUTATION_BLOCKED]` error
      with Slice 6 migration hint. Card on disk is unchanged.
      `{corpus}/_v1-upgraded.jsonl` has a new line with the card id
      and `attempted_op: annotate`.

- [ ] **G12: Keyword resolution disambiguation.** `/xnote` → keyword
      that matches 2-3 cards. Expected: numbered list, asks "Which
      one? (1-N)". Pick `1`. Annotation flow proceeds against that
      card.

- [ ] **G13: URL resolution.** `/xnote` → paste `https://x.com/...`
      URL of an existing bookmark. Expected: resolves to one card; if
      0 results, message says so. NOT the same as keyword match.

## /xnote — review walk

- [ ] **G14: Empty walk.** Ensure no cards are pending (via G3 NOT
      run, or by annotating G3's card first). `/xnote` → `review`.
      Expected: `No cards due for review. Nice.`

- [ ] **G15: Walk pending cards in order.** Plant 2-3 pending cards
      via G3-style pastes (different content, different captured times
      via offset). `/xnote` → `review`. Expected: shows oldest first.
      Per-card prompts: `a / w / e / s / stop`.

- [ ] **G16: `a` annotates current card.** Mid-walk → `a`. Expected:
      prompts for why_saved + applicability. After write, moves to
      next card.

- [ ] **G17: `w` defers one week.** Mid-walk → `w`. Expected: card
      now has `next_review_at = now + 7d` (verify on disk). Moves to
      next card.

- [ ] **G18: `e` marks ephemeral.** Mid-walk → `e`. Expected: card
      now has `why_saved: "(ephemeral)"` and `why_saved_pending: false`
      (won't surface in next walk).

- [ ] **G19: `stop` exits walk cleanly.** Mid-walk → `stop`. Expected:
      `Walk stopped at card N+1 of M.` and exit. (Slice 2 LIMITATION:
      next session restarts from top of due queue. A formal
      `_review-cursor.json` would resume from N+2.)

## /xpin

- [ ] **G20: List empty.** `/xpin` → `list`. Expected: `No pinned
      cards yet.`

- [ ] **G21: Pin a card.** `/xpin` → `pin` → paste id from G1.
      Confirm with `y`. Expected: `Pinned 'paste-...'.` Card on disk
      now has `pinned: true`.

- [ ] **G22: Pin idempotent.** Re-run G21. Expected: `already pinned
      (no-op)` message; no error.

- [ ] **G23: List shows pinned.** `/xpin` → `list`. Expected: markdown
      table with the card from G21.

- [ ] **G24: Pin bypasses /xfind decay.** `/xfind` → query that the
      pinned card matches but isn't most-recent. Expected: pinned card
      surfaces above unpinned newer matches (still within
      pin-dominance bound).

- [ ] **G25: Unpin.** `/xpin` → `unpin` → paste id → `y`. Expected:
      `Unpinned 'paste-...'.` `/xpin list` no longer shows it.

- [ ] **G26: V1 pin refused.** `/xpin` → `pin` → paste a v1 id → `y`.
      Expected: `[V1_MUTATION_BLOCKED]` with Slice 6 hint. Card
      unchanged. `_v1-upgraded.jsonl` logs the attempt.

- [ ] **G27: Bad mode token.** `/xpin` → `delete` (or any non-
      pin/unpin/list token). Expected: `Unknown action; expected pin /
      unpin / list.`

## Concurrency

- [ ] **G28: Two-terminal /xpaste contention.** Open two Claude Code
      sessions in two terminals. Run `/xpaste` in both simultaneously.
      Take both to the confirm step. In terminal A, hit `y`. While A's
      write is running, in terminal B, hit `y`. Expected: A's card
      lands. B receives `[LOCK_HELD]` with A's PID and the manual
      escape hint (`rm $XSENSAI_CORPUS_PATH/.locks/card_write.lock`).

- [ ] **G29: Mid-write Ctrl-C.** Start `/xpaste` → step through to
      confirm → `y` → INSTANTLY Ctrl-C the Claude Code session.
      Restart. Run `/xfind anything`. Expected: `[MID_WRITE_DETECTED]`
      log line in stderr (or in MCP log); orphan `.tmp` files in
      corpus dir are gone after that walk; corpus is clean.

## /xpaste → /xfind round-trip

- [ ] **G30: Round-trip works in one session.** Run G1 → immediately
      `/xfind <unique keyword from G1's content>`. Expected: card
      surfaces in results. First /xfind after paste takes ~5s (the
      reindex trigger fires); subsequent /xfind queries are normal
      speed. `_index-dirty` marker is gone from corpus dir after.

---

## Pass/fail summary

| Total | Passed | Failed | Notes |
|-------|--------|--------|-------|
| 30    |        |        |       |

**Run date:** ____________
**Run by:** ____________
**git commit:** ____________

If any failed, file an issue with the test number and observed-vs-expected
behavior. Do not /ship until all pass.
