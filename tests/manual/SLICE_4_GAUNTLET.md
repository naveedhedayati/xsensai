# Slice 4 Manual Gauntlet

Post-merge human walkthrough for `/xsync` + `/xextract` + setup_oauth + git plumbing.

**Triage** (D-7 fix):
- **P0** items must pass to declare "shipped" (block merge if any fail).
- **P1** items must pass before declaring "deployed and healthy."
- **P2** items are post-deploy nice-to-have (verify within a week).

**Time budget**: ~30-45 min if everything passes; longer on a fresh machine
(OAuth + dev portal setup is the wildcard).

**Live API gating**: items marked `[LIVE]` need a real X dev app + OAuth.
Skip these on CI; run manually after `setup_oauth` completes.

---

## P0 — must pass to merge

### G1. `/xhelp` lists `/xsync` + `/xextract`

```
/xhelp
```

**Expected**: both commands appear in the "Available now" table with their
descriptions. Inline override sections for `/xsync` and `/xextract` present.
"Sync setup" section visible.

### G2. `setup_oauth --check` works without burning a token

```bash
python -m xsensai.sync.setup_oauth --check
```

**Expected**: precondition output. Without `XSENSAI_X_CLIENT_ID`, it should
report `❌ Missing client_id` with a clear actionable next step. With
`XSENSAI_X_CLIENT_ID` set, all 4 checks should pass (`✅`).

### G3. Lock-domain extension doesn't break Slice 2 tests

```bash
.venv/bin/pytest tests/test_locks_acquire.py tests/test_concurrency_paste.py
```

**Expected**: 14 pass, 4 skipped. Same as before Slice 4.

### G4. Existing 355 tests still pass

```bash
.venv/bin/pytest tests/ --ignore=tests/test_sync_extraction.py --ignore=tests/test_sync_smart_extract.py --ignore=tests/test_sync_service.py --ignore=tests/test_sync_git_check.py --ignore=tests/test_setup_oauth.py --ignore=tests/test_sync_client.py --ignore=tests/test_sync_card_writer.py --ignore=tests/test_sync_dedup.py --ignore=tests/test_sync_checkpoint.py --ignore=tests/test_sync_heartbeat.py --ignore=tests/test_sync_auth.py --ignore=tests/test_locks_index_rebuild.py --ignore=tests/test_card_model_slice4.py
```

**Expected**: 355 pass, 9 skipped. Slice 1+2+3 surface regression-free.

### G5. New 110 tests pass

```bash
.venv/bin/pytest tests/test_sync_*.py tests/test_setup_oauth.py tests/test_locks_index_rebuild.py tests/test_card_model_slice4.py
```

**Expected**: ~110 pass.

### G6. `dev_refresh.sh` works on uv-venv (regression check from pre-Slice-4 fix)

```bash
./scripts/dev_refresh.sh
```

**Expected**: completes without error. "==> install -e ." step uses `uv pip`
fallback if the venv has no pip. NEXT STEPS message is slice-agnostic.

### G7. Schema forward-compat — old cards still load

```bash
.venv/bin/python -c "
from xsensai.storage.corpus import iter_cards_metadata
import os
os.environ['XSENSAI_CORPUS_PATH'] = os.path.expanduser('~/path/to/your/vault/x-bookmarks/')
n = sum(1 for _ in iter_cards_metadata())
print(f'Loaded {n} cards from real corpus')
"
```

**Expected**: prints "Loaded N cards" where N is at least 25 (existing v1 cards).
Zero `[YAML_PARSE_FAILED]` warnings related to the new `thread_fetch_status`
or `xsync_run_id` fields.

---

## P1 — must pass before "deployed and healthy"

### G8. [LIVE] `setup_oauth` real flow (one-time per machine)

```bash
export XSENSAI_X_CLIENT_ID=<your-client-id>
python -m xsensai.sync.setup_oauth
```

**Expected**: browser opens to `https://x.com/i/oauth2/authorize?...`. After
granting, callback fires and "✅ x-sensai OAuth setup complete" prints.
Refresh token stored in macOS Keychain (`security find-generic-password -s x-sensai`).

### G9. [LIVE] `/xsync since` end-to-end against real account

In Claude Code: `/xsync since`

**Expected**:
- One prompt asks for mode (or accepts `since` from inline arg).
- Service runs; emits `[INFO/SYNC_STARTING] Synced N new cards (extraction: <strategy>)`.
- If N≤5: inline extraction proceeds (one round-trip per card; per-5-card progress emit).
- If N>5: deferred — final emit tells user to run `/xextract` later.
- Final `[INFO/SYNC_DONE]` envelope per the locked text.

### G10. [LIVE] `/xfind <topic>` finds a freshly-synced card

After G9, in Claude Code: `/xfind <a topic from a synced bookmark>`

**Expected**: the new card appears in results. First /xfind after sync is
~5s slower (reindex via `_index-dirty` marker, now under cross-process
`index_rebuild` lock per S-9 fix).

### G11. `/xextract` empty path (no pending cards)

```
/xextract
```

(With no cards in `extraction_pending: true` state.)

**Expected**: `[INFO/NO_PENDING_EXTRACTIONS]` envelope. Clear message that
nothing was pending; suggests running `/xsync` to add.

### G12. [LIVE] `/xextract backlog` after a deferred `/xsync`

Sequence:
1. `/xsync backlog defer` (forces deferred mode regardless of N).
2. Verify cards on disk have `extraction_pending: true` in frontmatter.
3. `/xextract` (defaults to backlog).

**Expected**: `/xextract` emits `[INFO/SYNC_STARTING] Extracting N pending card(s)`.
For each card, host Claude (you) returns valid extraction JSON. Final
`[INFO/EXTRACT_DONE]` envelope.

### G13. Vault cleanliness check fires when prior sync output uncommitted

Sequence:
1. Run `/xsync since` (writes some cards).
2. Do NOT commit them.
3. Run `/xsync since` again.

**Expected**: second run emits `[INFO/VAULT_DIRTY_FIRST_RUN]` envelope listing
the uncommitted card paths + escape hatch (`proceed dirty` keyword OR env var).

### G14. `proceed dirty` keyword overrides the cleanliness check

After G13, run: `/xsync since proceed dirty`

**Expected**: sync proceeds (no `[INFO/VAULT_DIRTY_FIRST_RUN]` STOP). Cards
land normally.

### G15. `commit` keyword auto-commits new cards

In a vault that's a git repo: `/xsync since commit`

**Expected**: after sync completes, `git log` in the vault shows a new
commit with message `xsync: N new cards`. No push happened.

### G16. Smart-default boundary (manually verify branch decision)

```bash
.venv/bin/python -c "
from xsensai.sync.service import _decide_strategy
print(_decide_strategy(n=5, inline=False, defer=False))   # 'inline'
print(_decide_strategy(n=6, inline=False, defer=False))   # 'deferred'
print(_decide_strategy(n=30, inline=True, defer=False))   # 'inline'
print(_decide_strategy(n=2, inline=False, defer=True))    # 'deferred'
"
```

**Expected**: prints `inline`, `deferred`, `inline`, `deferred` (matches
UC-2=C threshold).

### G17. `/xsync` rejects `inline` + `defer` together

```
/xsync since inline defer
```

**Expected**: `[INVALID_FLAGS]` envelope with clear message. No fetch attempted.

### G18. [LIVE] Error envelope on auth failure

Manually break the Keychain entry:

```bash
security delete-generic-password -s x-sensai -a x-api-refresh-token
```

Then in Claude Code: `/xsync since`

**Expected**: `[OAUTH_SETUP_REQUIRED]` envelope with `next_action` pointing
at `python -m xsensai.sync.setup_oauth`.

(Re-run setup_oauth after this test to restore the token.)

### G19. install_commands.sh data-driven Available list

```bash
./scripts/install_commands.sh | grep "Available:"
```

**Expected**: `Available: /xask /xextract /xfind /xhelp /xnote /xpaste /xpin /xsync`
(alphabetical, derived from commands/*.md). NO hand-maintained string.

### G20. Doc consistency test passes

```bash
.venv/bin/pytest tests/test_doc_consistency.py
```

**Expected**: all assertions pass — `/xsync` and `/xextract` documented
across CLAUDE.md, README.md, TROUBLESHOOTING.md, commands/xhelp.md, CHANGELOG.md.

### G21. Error envelope contract test passes

```bash
.venv/bin/pytest tests/test_error_envelopes.py
```

**Expected**: every error code's rendered output contains a runnable
command or URL in `next_action`.

### G22. Concurrent `/xpaste` during `/xsync` (S-7 race fixture)

In one terminal: kick off `/xsync backlog` (a long run).
In Claude Code (different process): run `/xpaste` and paste any short content.

**Expected**: `/xpaste` does NOT corrupt the corpus or hit `[LOCK_HELD]`
errors that block forever. Per-card `card_write` lock cycles let `/xpaste`
slot in between cards. (Or `/xpaste` waits ~1s and proceeds.)

### G23. Reindex cross-process lock (S-9)

In one terminal: run `/xsync since` (which triggers reindex on finalize).
Immediately in another: run `/xfind <something>` (which would normally
trigger read-side reindex via the marker).

**Expected**: only ONE `qmd update` runs. The second process either
sees no marker (sync finished) OR waits on the `index_rebuild` lock.

---

## P2 — post-deploy nice-to-have

### G24. `/xsync preview` mode

```
/xsync preview
```

**Expected**: emits `[INFO/SYNC_DONE] Preview: N bookmark(s) would be fetched. NOTHING WRITTEN.`
followed by a list of {source_id, author, created_at, text_preview}. No
cards on disk.

### G25. `_skip-list.txt` honors permanent-skip

Manually create `$XSENSAI_CORPUS_PATH/_skip-list.txt`
with one source_id per line that you want to permanently skip.

(Note: Slice 4 ships the spec for `_skip-list.txt` but the integration
into dedup is left for a later micro-PR. P2.)

### G26. Threads >7 days old → `outside_window` envelope

[LIVE] Bookmark a tweet that's >7 days old (with replies). Then `/xsync since`.

**Expected**: card written; `thread_fetch_status: outside_window` in
frontmatter. `/xsync` final emit includes `[INFO/THREAD_OUTSIDE_7DAY_WINDOW]`
envelope (or `[INFO/SEARCH_ALL_UNAVAILABLE]` if your tier doesn't have
search_all).

### G27. Heartbeat thread observability

After a `/xsync` completes, check `~/.cache/xsensai/xsync-log.jsonl`:

```bash
tail -1 ~/.cache/xsensai/xsync-log.jsonl | python -m json.tool
```

**Expected**: structured JSON with `run_id`, `mode`, `outcome`, counts,
`duration_ms`, `sync_schema_version`. Run mode=`hash_only` (default) means
no sensitive content captured.

### G28. `_sync-status.md` is committed (NOT gitignored)

```bash
cd "$XSENSAI_CORPUS_PATH"
cat .gitignore 2>/dev/null | grep -q "_sync-status" && echo "FAIL: gitignored" || echo "OK: tracked"
```

**Expected**: "OK: tracked" — D-S3 promoted T-1 from taste decision to
auto-decided so cron's heartbeat is readable cross-host.

### G29. Token rotation handled silently

[LIVE, hard to reproduce] If X rotates your refresh token mid-sync,
`XClient` should silently re-store it via `KeychainTokenProvider.store_refresh_token()`
and continue. No user-visible error.

(Manual verification: log message `"X rotated the refresh token — persisting new value via TokenProvider"` appears in stderr.)

### G30. setup_oauth `--copy-url` mode

```bash
python -m xsensai.sync.setup_oauth --copy-url
```

**Expected**: prints the OAuth URL to stdout instead of opening the browser.
Useful when the default browser doesn't work or the user wants to use a
specific browser.

---

## What's NOT in this gauntlet

Items deliberately deferred (per Slice 4 OUT scope):

- `/xsync single <tweet-id>` — stubbed; Slice 4.5 candidate.
- GitHub Actions cron — Slice 5.
- `git push` — Slice 5.
- Cross-host conflict resolution — Slice 5.
- `/xtranscribe` — separate slice.
- Bounded-async backlog fetch — taste decision deferred per /autoplan.

---

## Done declaration

**Block merge if** any P0 fails (G1–G7).

**Don't declare deployed/healthy until** all P1 LIVE items have passed at least once (G8–G23 except G18 which is destructive).

**P2 items can land post-deploy** within a week.
