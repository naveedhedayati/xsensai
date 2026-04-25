# Verification Spike Results — Slice 0

Three autonomous spikes closed; one (Spike #4) deferred to Slice 4 because
the public X API docs do not state rotation behavior. Two (#1, #2) are
mobile-only and need user action — see `mobile-fixture/`.

---

## Spike #3 — QMD locking story ✅ CLOSED

**Question:** does QMD write its index atomically, or is external locking
load-bearing for safety?

**Finding:** QMD's index is a SQLite database at `~/.cache/qmd/index.sqlite`.
PRAGMA inspection shows:

```
journal_mode = wal
locking_mode = normal
synchronous  = 1 (NORMAL)
```

**Implications:**
- WAL mode means SQLite handles atomic writes internally. An interrupted
  `qmd update` cannot corrupt the DB.
- Multiple readers can run concurrently with one writer; readers don't
  block.
- Concurrent writers serialize at the SQLite layer; a second `qmd update`
  blocks until the first commits.

**Decision for Slice 2 lock design:** the spec's `index_rebuild` lock is
correct but is NOT load-bearing for corruption prevention — SQLite handles
that. The lock prevents duplicate work (two cron syncs reindexing the same
cards) and ensures a clean checkpoint state. Keep the design as specced;
just document that it's a "no duplicate work" lock, not a "no torn DB" lock.

**No contingency required.** The spec's "off-to-the-side index + atomic
swap" fallback is unnecessary.

---

## Spike #4 — X OAuth refresh-token rotation ⏸ DEFERRED to Slice 4

**Question:** does X rotate the refresh token on every refresh call?

**Finding:** the public X API OAuth 2.0 documentation
(`docs.x.com/resources/fundamentals/authentication/oauth-2-0/...`) does
NOT state rotation behavior. Multiple WebFetch passes confirmed this. The
docs show how to *use* a refresh token but not what the response contains.

**Reasonable prior:** OAuth 2.0 PKCE implementations commonly rotate
refresh tokens on every use as a security best practice. RFC 6749 allows
either behavior; rotation is a stricter security posture.

**Decision: empirical test in Slice 4.** Once we have a real refresh
token from the OAuth flow, the first refresh attempt will reveal whether
the response includes a new `refresh_token` field. If yes, Slice 4 needs
token-write-back.

**Slice 4 escalation path (write into the sync code from day one):**

If rotation IS happening on every use, the cron must persist the new
token after each refresh. Three options ranked by complexity:

1. **GitHub Actions secret update via fine-scoped PAT.** The cron writes
   the new refresh token back to the repo's secret store via
   `gh secret set` from inside the workflow. Requires a separate PAT with
   `repo:secrets:write` scope, stored as a bootstrap secret. This widens
   blast radius (the bootstrap PAT can write any secret) but is the most
   reliable; no external dependencies.
2. **External secret store (1Password, Doppler, AWS Secrets Manager).**
   The cron reads + writes the refresh token via an external service's
   API. Cleanest separation of concerns; adds a runtime dependency and
   another set of credentials to manage.
3. **Short-loop manual re-auth.** Accept that the cron breaks every N
   days and surfaces `SYNC_AUTH_FAILED.md`. User runs `setup.sh --reauth`
   weekly. Simplest but leans on user discipline; defeats the "unattended
   sync" goal.

**Recommendation if rotation is real:** option 1 with a fine-scoped PAT
narrowed to this single repo's secrets. Manageable blast radius, no
external dependencies, the cron is genuinely unattended.

**No code in Slice 0.** Just this writeup.

---

## Spike #6 — XDK availability and thread-fetch cost ✅ CLOSED

**Question:** can we use XDK for bookmarks + threads, and at what cost?

**Findings via XDK source inspection (`github.com/xdevplatform/xdk-python`):**

- **Bookmarks:** `xdk/users/client.py` exposes:
  - `users.get_bookmarks(id) -> Iterator[GetBookmarksResponse]` (auto-paginated)
  - `users.get_bookmarks_by_folder_id(...)`
  - `users.create_bookmark(...)`, `users.delete_bookmark(...)`
  - All authenticated with OAuth 2.0 PKCE (`OAuth2PKCEAuth` class).
- **Threads / reply chains:** no direct `get_conversation` method. Standard
  X API pattern is `posts.search_recent(query=f"conversation_id:{tweet_id}")`,
  which returns up to 100 posts per page in a single API call.
- **Pagination:** XDK auto-paginates via `Iterator` return types.

**Cost model for typical bookmark sync:**
- 1 `get_bookmarks` page = 1 API call, returns up to 100 bookmarks
- For threaded bookmarks: 1 `search_recent` per thread = 1 API call,
  returns up to 100 reply posts in one page
- Typical user has <500 bookmarks and most threads <20 replies
- Steady-state: <10 API calls per sync, well within rate limits and the
  spec's <$10/month budget

**Decision:** use XDK in Slice 4. Thread stitching is cheap (1 API call
per thread), so the spec's "lazy-fetch thread on demand" contingency is
not needed. Stitch threads at sync time.

**XDK install:** `pip install xdk` (Python 3.8+). Hash-locked alongside
other deps when Slice 4 adds it to `requirements.in`.

---

## Spike #1 — Mobile slash-command discovery ⏸ NEEDS USER

The fixture is at `spikes/mobile-fixture/`. See its README for the
~5-min procedure. Result feeds Slice 2/3 surface decisions.

## Spike #2 — Mobile MCP support ⏸ BLOCKED on MCP server existing

Now unblocked: Slice 0 ships a working MCP server (`ping` smoke test).
Procedure to be defined after Slice 1 lands a real tool worth testing on
mobile.

## Spike #5 — yt-dlp on GitHub Actions runners ⏸ DEFERRED to Slice 4/5

Not needed until video transcription is wired (Slice 4 enqueues; Slice 5
runs the cron). Defer.
