# QA Report — x-sensai Slice 1

**Date:** 2026-04-25
**Branch:** `main`
**Tier:** Standard (smoke-test-equivalent for a no-UI product)
**Mode:** Product surface verification (browser-driven QA does not apply — no web UI)

## Fit context

`/qa` is built for browser-driven testing. x-sensai has no web UI. The skill's
intent (test it like a real user, find what's broken) was applied to the actual
product surfaces: MCP tools, slash commands, shell scripts, error contracts.

## Summary

| Surface | Tests | Status |
|---|---|---|
| Unit + integration tests | 77 | ✅ all pass |
| `search_bookmarks` MCP, 5 real queries | 5 | ✅ |
| `get_bookmark` MCP, real ids + missing-id | 2 | ✅ |
| Error contracts (`CORPUS_UNAVAILABLE`, `NO_RESULTS`, error envelope shape) | 3 | ✅ |
| Slash command files (`xfind.md`, `xhelp.md`) frontmatter + body | 2 | ✅ |
| `bootstrap_qmd.sh` idempotency | 1 | ✅ |
| `install_commands.sh` cmp-skip + backup-on-edit | 2 | ✅ |
| F1 golden gate vs fixture corpus | 15 queries | ✅ top1=93%, top3=100% |
| F1 informational gate vs real vault (8 queries) | 8 queries | ✅ top1=75%, top3=88% |

**Final health score: 9.5/10.**

## Issues found

### ISSUE-001 — v1 adapter silently rejected 17/31 real-vault cards (CRITICAL, FIXED)

**Found:** Smoke test 2 (search_bookmarks against real vault).

**Symptom:** Real vault has 31 markdown files, but only 14 loaded as cards.
17 silently rejected with `[YAML_PARSE_FAILED]` to stderr, invisible to user.

**Root cause:** Real vault has multiple v1 dialects, but the v1 adapter's
`is_v1_shape()` positive-signal check was too narrow:
- 11 "minimal-v1" cards have just `source` + `author` (no `x_post_id`, no `type: x-bookmark`) — rejected
- 1 "manual-note" card has `x_source_url: ""` — rejected by bookmark validator (empty source)
- 4 orphan cards have `type: x-bookmark` but no `source` or `x_post_id` — correctly rejected (truly broken in source)
- 1 vault `CLAUDE.md` README — correctly rejected (not a card)

This regression was introduced in `/review` when I added the positive-signal
requirement to defend against `claude.md`-style files becoming cards. The
defense was correct in principle but too strict for the real vault's diversity.

**Fix:** Broadened `is_v1_shape` to accept `source + author` as a valid v1
signal, plus added manual-note routing (cards with `x_post_id="manual_..."`,
`x_type="note"`, or empty `x_source_url` → `source_type=paste`). ~15 LoC in
`src/xsensai/storage/v1_adapter.py`.

**Result:** Real vault corpus loaded: **14 → 26 cards (+12, +86%)**. The 4
orphan cards + `CLAUDE.md` correctly stay rejected.

**Verification:**
- All 77 unit tests still pass after the fix
- Real-vault search returns @lydiahallie, @trq212, @itsolelehmann, @boringmarketer, @garrytan cards (formerly invisible)
- Real-vault F1 golden gate: top-1 75%, top-3 88% (above 80% target)

**Decision:** Kept the broadening per user direction (D1 in this session).
The adapter accepts ~15 more LoC of v1-dialect handling now; deletes cleanly
when Slice 6 ships proper v1→v2 migration. Trade: throwaway code for 4 slices
of richer real-corpus dogfooding.

### ISSUE-002 — Real-vault BM25 misses digit/word unification (LOW, NOT FIXED)

**Found:** Smoke test 7 (real-vault F1 golden).

**Symptom:** Query "seo agent fifty dollars" returns no hits. Expected card
title is "OpenClaw SEO Agent for $50/Month" — the card uses "50" (digit), the
query uses "fifty" (word). QMD's BM25 indexer doesn't unify these.

**Severity:** Low. The card is findable with the literal phrase ("seo agent
50") and most users would search that way. Defer to Slice 3 LLM rerank (which
handles synonym/digit-word unification natively).

**Not fixing in Slice 1.** Documented as a known limitation.

## Smoke test details

### S1 — Test suite regression
```
77 passed in 3.05s (XSENSAI_RUN_INTEGRATION=1)
- 76 unit tests
- 1 integration test (real QMD against fixture corpus)
- F1 golden gate: top1=93%, top3=100% on fixture corpus
```
No regressions from Slice 1 implementation, /review fixes, or /qa fix.

### S2 — `search_bookmarks` MCP round-trip
5 real queries against real vault:

| Query | Hits | Top score | Top hit |
|---|---|---|---|
| "claude code skills" | 5 | 0.676 | @garrytan plan-review |
| "openclaw" | 3 | 0.670 | @TheMattBerman seo-agent |
| "context engineering" | 3 | 0.670 | @hooeem ai-engineer-stack |
| "agent observability" | 1 | 0.655 | @nearlydaniel observability |
| "garbage_query_xyz_no_match" | 0 | — | (fallback fired correctly) |

All hits are coherent and topical. Empty-query path correctly triggers fallback
without crashing.

### S3 — `get_bookmark` round-trip
- Lookup by id from search hit → returns full card detail (8 keys: id, source_type, source, author_or_self, captured, body, etc.)
- Lookup by missing id → returns `NO_RESULTS` error envelope with `next_action` populated
- Body content present (704 chars) and well-formed

### S4 — Error contracts
- `CORPUS_UNAVAILABLE` from invalid path: 5-line spec format renders correctly
- `NO_RESULTS` from valid path + nonsense query: returns hits=0, fallback_fired=true
- MCP error envelope: `{hits: [], meta: {...}, error: {code, message, next_action, retryable}, rendered_markdown}` — every key present (the `/review` fix that added `hits`/`meta` keys to error path holds)

### S5 — Slash command files
- `commands/xfind.md`: 57 lines, frontmatter parses, body 2089 chars, has `description` field
- `commands/xhelp.md`: 71 lines, frontmatter parses, body 1928 chars, has `description` field

### S6 — Scripts
- `bootstrap_qmd.sh` idempotent: re-run on existing collection → "already exists; no action"
- `install_commands.sh` content-aware: cmp-match skip works, user-edit detection backs up to `.bak.<timestamp>` correctly
- Both scripts pass `bash -n` syntax check

### S7 — F1 quality gate (real vault, informational)
8 hand-picked queries against real vault:
- top-1: 6/8 (75%)
- top-3: 7/8 (88%) — exceeds 80% target
- One miss: "fifty dollars" vs "$50/month" (digit/word mismatch — see ISSUE-002)

## Health score breakdown

| Category | Score | Weight | Weighted |
|---|---|---|---|
| Tests (regression-free) | 100 | 25% | 25.0 |
| MCP tool surfaces | 100 | 25% | 25.0 |
| Error contracts | 100 | 15% | 15.0 |
| Scripts (idempotent + safe) | 100 | 10% | 10.0 |
| Slash command files | 100 | 5% | 5.0 |
| Real-vault quality (top-3 88%) | 88 | 15% | 13.2 |
| BM25 limitations (ISSUE-002) | 70 | 5% | 3.5 |

**Total: 96.7/100 → 9.7/10.** Rounded to 9.5/10 for the v1 dialect surprise factor.

## Top 3 things to fix (none blocking)

1. ~~v1 adapter rejects minimal-v1 + manual notes~~ — **FIXED in this session**
2. BM25 digit/word mismatch — defer to Slice 3 LLM rerank
3. (none — Slice 1 is ship-ready)

## PR summary line

> QA found 2 issues, fixed 1 (v1 adapter dialect support, +12 cards loaded), 1 deferred to Slice 3. Health score 9.5/10.

## Files changed by /qa

- `src/xsensai/storage/v1_adapter.py` — broadened `is_v1_shape` (+source+author dialect) and `_map_v1_to_v2` (manual-note routing). Net +15 LoC.

## Next step

`/ship` to land Slice 1.
