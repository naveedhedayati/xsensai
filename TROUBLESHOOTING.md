# Troubleshooting

Keyed by error code. Every error in x-sensai routes through `XSensaiError.format()` so you see exactly which one fired.

---

## `[CORPUS_UNAVAILABLE]`

**Cause:** the corpus directory is missing, empty, or not a directory.

**Fix:**
1. Check `$XSENSAI_CORPUS_PATH` (defaults to `~/.local/share/xsensai/corpus`).
2. Make sure the directory exists and contains `.md` files.
3. Run `scripts/bootstrap_qmd.sh` to (re-)create the QMD index pointed at it.

If you have v1 cards in there, the v1 adapter loads them automatically (no action needed). If you have no cards yet, wait for Slice 6 migration or `/xpaste` (Slice 2).

---

## `[NO_RESULTS]`

**Cause:** the corpus has cards but your query matched nothing.

**Fix:**
- Try a broader query (drop adjectives, use simpler keywords).
- Remove `no decay` or `skip pins` if you used them — those filter results.
- Check `xsensai-eval-history` to see if quality has regressed.

---

## `[INTERNAL_ERROR]`

**Cause:** something broke in the QMD subprocess wrapper or JSON parser.

**Fix:**
1. Check QMD is installed: `which qmd` or `$XSENSAI_QMD_PATH`. Install with `bun install -g qmd`.
2. Check QMD index health: `qmd status`.
3. If `details` mention "schema drift" or "non-JSON output", QMD updated its CLI shape. Re-spike: `qmd search "test" --json -c xsensai-cards | head` and update `tests/fixtures/qmd_query_output.json` + the parser in `src/xsensai/retrieval/qmd.py`.
4. If it's a timeout (QMD didn't respond in 10s), re-run /xfind. If it persists, the index may be huge or stuck — `qmd status` to investigate.

---

## `[YAML_PARSE_FAILED]`

**Cause:** a card has malformed YAML frontmatter, or the v2 schema rejected it.

**Fix:**
- The card path is in stderr (look at the MCP server's log output).
- Open the card and check the `---` frontmatter at the top.
- Common gotchas:
  - YAML 1.1 traps: use `true`/`false` not `yes`/`no`; quote strings that look like numbers.
  - Datetimes must be timezone-aware ISO-8601 (e.g., `2026-04-25T10:00:00Z`).
  - Bookmark cards require `source`, `source_id`, `author`. Paste cards must NOT have `source_id`.
- v1-shape cards (no `raw_path`/`raw_checksum`) are loaded via the v1 adapter; if a v1 card still fails, its frontmatter has another problem.

---

## `[DISK_WRITE_FAILED]` (sidecar OR write-path)

**Cause:** sidecar `.raw.txt` is missing/unreadable/checksum-mismatched, OR an atomic-write step (durable_replace) failed mid-flight.

**Fix:**
- Missing sidecar: restore from git or remove `raw_path`/`raw_checksum` from the card (downgrades to v1-adapter path).
- Checksum mismatch: someone (or some process) edited the `.raw.txt` after the card was created. Restore from git, OR re-compute the checksum manually:
  ```bash
  shasum -a 256 path/to/card.raw.txt
  # update raw_checksum in the .md to "sha256:<the hash>"
  ```
- Atomic-write failure (Slice 2): error details include the orphan `.tmp` path. Check disk space + write permissions. Orphan `.tmp` files are auto-discarded on next `iter_cards` walk.
- iCloud-synced corpus warning: if `XSENSAI_CORPUS_PATH` lives in `~/Documents` with iCloud "Desktop & Documents" enabled, atomic-rename guarantees may be weakened. Move the vault to a non-synced local path (e.g., `~/x-bookmarks`).
- Cross-device rename: `tmp` ended up on a different filesystem than the target. Set `$TMPDIR` to a path on the same volume as `$XSENSAI_CORPUS_PATH`, or use a non-synced corpus path.

---

## `[LOCK_HELD]`

**Cause:** another writer is holding the `card_write` lock. /xpaste, /xnote, /xpin all serialize through this lock so concurrent writes don't corrupt the corpus.

**Fix:**
1. Wait a few seconds and retry. Most writes are sub-second.
2. The error message lists the holder's PID, hostname, and started_at. If you know that PID is dead (process crashed without releasing), manually clear:
   ```bash
   rm "$XSENSAI_CORPUS_PATH/.locks/card_write.lock"
   ```
3. Slice 2 uses `fcntl.flock` so process death automatically releases the lock — manual cleanup is rarely needed.
4. Fencing token mismatch: if a write fails with "fencing token mismatch," your write was started under one lock generation but the lock was re-acquired before commit. Re-run the slash command.

---

## `[MID_WRITE_DETECTED]`

**Cause:** an orphan `.tmp` file was found in the corpus directory — debris from a crashed atomic write (Ctrl-C between sidecar rename and `.md` rename, or kill -9 mid-flight). NOT a user-visible error; surfaces as a stderr log line during `iter_cards`.

**Fix:**
- Self-healing — the orphan `.tmp` is unlinked automatically. Your card is intact (the transaction never committed); re-run /xpaste with the original content.
- If you see this repeatedly without crashes, file an issue with the orphan path; could indicate a bug in `durable_replace`.

---

## `[PASTE_EMPTY]`

**Cause:** `paste_bookmark` was called with empty/whitespace-only content.

**Fix:**
- Re-run /xpaste with non-empty content. The slash command's step-2 guard normally catches this before the MCP layer sees it; if you hit PASTE_EMPTY directly, you're likely calling the MCP tool from a non-slash context.

---

## `[PASTE_CRASHED]`

**Cause:** all three inbox fallback paths (`$XSENSAI_VAULT_INBOX` → `vault/00_inbox/quick.md` → `corpus/_inbox-quick.md`) failed to write. Your aborted paste content could not be saved.

**Fix:**
1. Check filesystem permissions on the vault directory and the corpus directory.
2. The error details name the last filesystem error encountered.
3. Your pasted content was NOT preserved — re-run /xpaste with the content still in your scrollback.
4. If `$XSENSAI_VAULT_INBOX` is set to a path that doesn't exist, unset it and retry.

---

## `[USER_CONFIRMATION_REQUIRED]`

**Cause:** a mutation MCP tool (`paste_bookmark`, `annotate_card`, `set_pin`) was called without `user_confirmed=True`. This is intentional — Slice 2 cannot hide tools from `tools/list` (FastMCP limitation), so we runtime-guard mutations against accidental Claude calls in non-/xpaste contexts.

**Fix:**
- Use the corresponding slash command (`/xpaste`, `/xnote`, `/xpin`) which prompts the user explicitly and sets the flag.
- For scripted automation: pass `user_confirmed=True` to the tool call. You're acknowledging the mutation will happen without an interactive confirm.

---

## `[V1_MUTATION_BLOCKED]`

**Cause:** /xnote or /xpin tried to mutate a v1-shape card (no `raw_path` / `raw_checksum`). Slice 2 refuses these to preserve the verbatim guarantee — synthesizing `raw_bytes` from rendered body would lose `## Thread` / `## Video Transcript` content.

**Fix:**
- Wait for Slice 6 migration which re-fetches v1 cards from XDK and writes proper sidecars.
- Your refused-mutation attempt is logged to `{corpus}/_v1-upgraded.jsonl` so Slice 6 prioritizes those cards.
- Workaround for high-priority cards: manually edit the card's `.md`, add `raw_path: ./{stem}.raw.txt` + `raw_checksum: sha256:<hash>` to the frontmatter, and `shasum -a 256 < some_source > {stem}.raw.txt`. After that the card is v2-shape and mutable.

---

## /xask

### `[WEB_FORK_FAILED]`

**Cause:** the `last30days` subprocess crashed, returned non-zero, or its output didn't parse.

**Fix:**
1. Verify `last30days` is installed: `ls $XSENSAI_LAST30DAYS_PATH` (default `~/.claude/skills/last30days/scripts/last30days.py`).
2. Verify it's owned by you: `ls -l $XSENSAI_LAST30DAYS_PATH` — if not, the runner refuses to execute it (env-allowlist security).
3. Re-run with `/xask <q> no web` to skip web entirely.
4. Investigate the skill itself: try invoking it directly outside `/xask`.

### `[EMPTY_CORPUS]`

**Cause:** your corpus directory has zero loadable cards.

**Fix:**
- Run `/xpaste` to add cards manually, or
- Check `$XSENSAI_CORPUS_PATH` points at the right vault directory: `echo $XSENSAI_CORPUS_PATH && ls "$XSENSAI_CORPUS_PATH"/*.md | head`.

### `[TEMPLATE_VALIDATION_FAILED]`

**Cause:** the host Claude session emitted output that did not match the locked `/xask` template after one re-prompt with stricter instructions.

**Fix:**
- The raw output is emitted under the banner — read it; usually still useful.
- Check `~/.cache/xsensai/xask-log.jsonl` for the bisect record (q_hash + output_sha256 + prompt_template_version).
- Re-run the same `/xask` query to retry. If it consistently fails on the same question, the prompt template may need a tweak (`src/xsensai/synthesis/template.py`).

### `[INFO/NO_CORPUS_MATCH]`

**Cause:** your question was processed but no cards in the corpus matched it. Not an error — just a "no hits" signal.

**Fix:**
- Try `/xfind <broader terms>` to explore.
- Rephrase the question.
- If you expected a hit, see "Tests pass but `/xfind` feels wrong" below.

### `[INFO/WEB_TIMEOUT]`

**Cause:** `last30days` exceeded `XSENSAI_XASK_WEB_TIMEOUT_S` (default 20s). Output renders corpus-only.

**Fix:**
- Re-run with `/xask <q> no web` if you don't want web context.
- Bump the timeout: `XSENSAI_XASK_WEB_TIMEOUT_S=40 /xask <q>`.

### `[INFO/WEB_PARSE]`

**Cause:** `last30days` returned output that didn't parse as JSON. Likely a CLI shape change in the upstream skill.

**Fix:**
- Verify `last30days` install is current.
- Re-run with `no web` to skip.
- Investigate: invoke `last30days` directly with the same question and inspect stdout.

### `[INFO/CHALLENGE_NO_DISSENT]`

**Cause:** `/xask <q> challenge` ran the dissent-finding pass but the candidates surfaced were the same as the top-3. Not an error.

**Fix:**
- Your corpus may not contain a genuine dissenter on this topic. That's signal, not noise.

### `/xask` log file & privacy

The question log lives at `~/.cache/xsensai/xask-log.jsonl` with mode 0600 (dir 0700).

- **Default mode is `hash_only`** — the log captures `q_hash` + meta but NOT raw question text (DX privacy default).
- **Switch to full text logging:** `export XSENSAI_XASK_LOG_MODE=full` (e.g. for empirical steering of Slices 4-6).
- **Disable entirely:** `export XSENSAI_XASK_LOG_MODE=off`.
- **Purge old entries:** `python -m xsensai.xask.log purge` (honors `XSENSAI_XASK_LOG_RETENTION_DAYS`, default 90).

---

## Slash commands (`/xfind`, `/xhelp`) don't appear in Claude Code

**Cause:** install script didn't run, or you haven't restarted Claude Code.

**Fix:**
1. `./scripts/install_commands.sh` (copies to `~/.claude/commands/`).
2. Restart Claude Code.
3. Type `/x` and check autocomplete.

---

## MCP tools don't appear in Claude Desktop

**Cause:** Claude Desktop's MCP config doesn't point at the venv-installed `xsensai-mcp` binary, or you haven't restarted.

**Fix:**
1. Find `~/Library/Application Support/Claude/claude_desktop_config.json`.
2. Make sure it has an `xsensai` server entry pointing at `.venv/bin/xsensai-mcp` (or `python -m xsensai.mcp_server` against the venv's Python).
3. Restart Claude Desktop.
4. From any conversation: "use the xsensai server's ping tool with echo=hello" → should return "pong: hello".

---

## Tests pass but `/xfind` feels wrong

Symptoms: real-corpus `/xfind` returns weird matches, or pinned cards dominate.

**Investigate:**
- Run the F1 quality gate against your real corpus: write a `~/.cache/xsensai/golden_set.json` with `(query, expected_id_list)` tuples and adapt `tests/eval/golden_set.py`. Aim for top-3 hit rate ≥ 80%.
- Inspect scoring constants in `src/xsensai/retrieval/scoring.py`:
  - `RECENCY_HALF_LIFE_DAYS = 90.0` — tighten (smaller value) to favor newer cards more aggressively.
  - `FALLBACK_TOP_SCORE_FLOOR = 0.35` — raise to fire fallback more often.
  - `PIN_DOMINANCE_FRACTION = 0.5` — raise to be stricter about pinned cards.
- If quality is consistently below 80% top-3, the autoplan D1 deferral was wrong: pull Claude/GPT re-rank forward (currently scheduled for Slice 3).

---

# Slice 4 — sync errors

## `[OAUTH_SETUP_REQUIRED]` — refresh token not in Keychain

`/xsync` printed: `OAUTH_SETUP_REQUIRED: X API refresh token not found in macOS Keychain.`

**Fix:** run `python -m xsensai.sync.setup_oauth`. This walks you through the
PKCE OAuth flow (opens browser, you grant, captures the redirect, stores the
refresh token in Keychain under service=`x-sensai`, account=`x-api-refresh-token`).

If you don't have an X dev app yet:
1. Register at https://developer.x.com (~5-15 min, browser, dev portal approval).
2. Buy ~$10 of API credits at https://console.x.com (one-time).
3. Export your client_id: `export XSENSAI_X_CLIENT_ID=<your-client-id>`
4. Then run setup_oauth.

Verify preconditions WITHOUT burning a token first: `python -m xsensai.sync.setup_oauth --check`.

## `[OAUTH_CLIENT_ID_MISSING]` — no client_id

You need an X dev app's client_id. Two ways to provide it:

1. **Recommended (set-and-forget):** run `python -m xsensai.sync.setup_oauth`
   once with `--client-id <your-id>` (or with `XSENSAI_X_CLIENT_ID` exported).
   It persists the value to macOS Keychain alongside the refresh token, so
   future `/xsync` calls from any Claude Code session "just work" without
   re-exporting the env var.
2. **Per-shell:** export `XSENSAI_X_CLIENT_ID=<your-id>` in the shell that
   launches Claude Code. (Doesn't carry into a fresh `claude` invocation
   from a different terminal — that's why option 1 exists.)

If you already ran setup_oauth but `/xsync` still complains, your Keychain
entry may be stale. Re-run `python -m xsensai.sync.setup_oauth` to refresh it.

## `[OAUTH_PORT_COLLISION]` — could not bind localhost port

The PKCE callback server couldn't bind a 127.0.0.1 ephemeral port. This is
rare. Wait a moment and retry. If persistent, restart your shell.

## `[OAUTH_BROWSER_NOT_DEFAULT]` — couldn't auto-open the browser

Auto-open via `webbrowser.open()` failed. setup_oauth falls back to printing
the URL to stdout. Copy it into any browser to grant access. Or run with
`--copy-url` from the start to skip the auto-open attempt.

## `[OAUTH_GRANT_REFUSED]` — X returned an error or callback timeout

Either:
1. You denied the grant in the browser (intentional or accidental). Re-run.
2. The 5-minute callback timeout expired. Re-run + complete grant within 5 min.
3. State parameter mismatch (potential CSRF). Re-run in a clean browser session.

## `[OAUTH_KEYCHAIN_BLOCKED]` — Keychain ACL or prompt issue

The macOS Keychain refused the read or write. x-sensai stores up to 3
entries under service `x-sensai`:
- `x-api-refresh-token` (always)
- `x-api-client-id` (after first successful setup_oauth)
- `x-api-client-secret` (only for Confidential clients)

Open Keychain Access, search for "x-sensai", and grant the calling Python
access. If any entry is corrupt, delete it and re-run `setup_oauth`. If
the `keyring` library itself is the problem, verify the backend:

```bash
python -c "import keyring; print(keyring.get_keyring())"
```

Should print a macOS-keychain backend on macOS.

## Browser shows "Something went wrong" during setup_oauth

You opened the OAuth URL but X redirected to a "Something went wrong" page
without sending you back to localhost. This is a callback URL mismatch.

**Fix:** the X dev portal stores an exact-match callback URL (no wildcards).
`setup_oauth` binds to `http://127.0.0.1:8765/callback` by default.

1. Open https://developer.x.com → your app → User authentication settings.
2. In "Callback URI / Redirect URL", set exactly: `http://127.0.0.1:8765/callback`
3. Save, then re-run `python -m xsensai.sync.setup_oauth`.

If port 8765 is taken, override with `--port <free-port>` and update the
dev portal to match the same port.

## `[AUTH_FAILED]` — `client_secret is required for token refresh`

Your X dev app is registered as a Confidential Client (the dev portal's
"Web App" type). Confidential clients need a client_secret in addition to
the PKCE flow; Public Clients (Native App / Single Page App) don't.

**Fix:**
1. In the X dev portal, find your app's client_secret (rotate-and-copy if
   you've never viewed it).
2. Re-run setup with the secret:
   ```bash
   python -m xsensai.sync.setup_oauth --client-secret <your-secret>
   ```
   Or export `XSENSAI_X_CLIENT_SECRET=<your-secret>` first.
3. The secret is persisted to Keychain (account `x-api-client-secret`),
   so future `/xsync` calls find it automatically.

Alternative: change your dev app type to Native App or Single Page App
(Public Client) — then no client_secret is needed and you can re-run
setup_oauth without `--client-secret`.

## `[X_API_RATE_LIMITED]` — 429 after 3 backoffs

The XDK wrapper exhausted its rate-limit retry budget. X's bookmarks
endpoint allows 180 reqs/15min per user; search-recent allows 300/15min.

**Fix:** wait 15 minutes (the rate-limit window resets) and re-run `/xsync`.
The checkpoint persists, so resume picks up where you left off.

## `[X_API_NETWORK_ERROR]` — 5xx / network timeout after 3 retries

Transient network issue on X's side. Wait a few minutes and re-run. Same
checkpoint-resume guarantee as rate-limit.

## `[INFO/THREAD_OUTSIDE_7DAY_WINDOW]` — bookmark too old for thread fetch

X's `search_recent` endpoint only goes back 7 days. For older bookmarks,
the OP's reply chain isn't fetchable through this endpoint. The bookmarked
tweet is still saved (with `thread_fetch_status: outside_window` on the
card). The graceful-degradation path tried `search_all` (full archive)
once; if your X API tier doesn't include it, you'll see
`[INFO/SEARCH_ALL_UNAVAILABLE]` instead.

**Recovery:** if you really want the OP-replies, paste them via `/xpaste`
or upgrade your X API tier (full-archive search costs more credits).

## `[INFO/SEARCH_ALL_UNAVAILABLE]` — Full Archive not in your tier

Your X API tier doesn't expose `/2/tweets/search/all`. The card landed
with the bookmarked tweet text only. This envelope fires ONCE per session
so you know the upgrade path exists if you want it.

## `[INFO/VAULT_DIRTY_FIRST_RUN]` — uncommitted xsync output detected

A prior `/xsync` wrote cards but you haven't committed them yet. To avoid
stacking syncs on top of unreviewed output, `/xsync` STOPPED. Two ways
forward:

1. Commit the prior output: `cd <vault> && git add -A && git commit -m 'manual: prior xsync output'`. Then re-run `/xsync`.
2. Force the new sync anyway: re-run with `proceed dirty` keyword (e.g., `/xsync since proceed dirty`). Or set `XSENSAI_VAULT_DIRTY_PROCEED=1` to opt in permanently.

## `[INFO/VAULT_NOT_GIT]` — vault is not a git repo

You're probably using Obsidian Sync or another non-git mechanism. The
cleanliness check is informational only; `/xsync` continues. To opt into
git-based tracking: `cd <vault> && git init && git add . && git commit -m 'initial'`.

## `[INFO/GIT_LOCKED]` — `.git/index.lock` exists

Another git operation is in progress. Wait a few seconds and retry. If
persistent, manually inspect `.git/index.lock` and remove if stale.

## `[INFO/EXTRACTION_DEFERRED]` — cards saved with extraction_pending

Smart default chose deferred mode (>5 new cards). The cards are on disk
and `/xfind` will find them via rendered-body search. To backfill the
`retrieval_summary` + `retrieval_tags` for sharper retrieval, run
`/xextract` whenever convenient. (No urgency; Slice 5 cron will handle
this automatically once it lands.)

## `[INFO/NO_PENDING_EXTRACTIONS]` — `/xextract` had nothing to do

Either you've already extracted everything (good), or you ran `/xextract`
without first running `/xsync` to add pending cards. Run `/xsync` first
to fetch new bookmarks; if smart-default puts them in deferred mode,
`/xextract` will pick them up.

## `[INVALID_FLAGS]` — conflicting modifiers

You typed both `inline` and `defer` in the same `/xsync` invocation.
They conflict — pick one or neither (smart default chooses for you).

## `[CORPUS_UNREACHABLE]` — vault directory unresponsive

Likely your vault is on a network volume (NFS, iCloud Drive in
"Optimize Mac Storage" mode, mounted SMB) that's offline or paused.
Check that the vault sync isn't paused; verify `ls $XSENSAI_CORPUS_PATH`
returns quickly. Re-run `/xsync` once the volume is responsive.

---

# Slice 5 — scheduled sync error codes

## `[COST_LIMIT_REACHED]` — cron hit the API call cap

**Cause**: cron made `XSENSAI_CRON_API_CAP` (default 200) X API calls in
a single run before completing. Either you have a huge backlog (first
run after a long offline period) or something in the API is misbehaving
(retries piling up without progress).

**Recover**:
1. **If it's a one-time backlog**: raise the cap and re-trigger.
   ```bash
   gh secret set XSENSAI_CRON_API_CAP --body "1000"
   gh workflow run sync.yml
   ```
   Reset to 200 after the backlog drains.
2. **If it's persistent**: run `/xsync` from Mac to bypass cron's cap.
   The next scheduled run resumes from the checkpoint.
3. **Check for runaway retries**: inspect the GH Actions log for
   429/5xx response patterns from X.

The flag's `details` field says how many cards committed before bail.

---

## `[SYNC_PUSH_REJECTED]` — cron couldn't push to vault repo

**Cause**: cron's `git push --force-with-lease` was rejected 3 times.
Usually means you pushed to the vault from Mac at the same window as
cron, and cron lost the race.

**Recover** (on Mac):
```bash
cd /path/to/your/vault
git pull --rebase origin main          # incorporate any divergence
# resolve any conflicts in your editor
git push origin main
gh workflow run sync.yml               # re-trigger cron
gh run watch
git rm SYNC_PUSH_REJECTED.md && git commit -m "cron: push recovered"
git push origin main
```

The flag file in your vault contains a static-template recovery script
(no secrets interpolated — autoplan E7).

---

## `[CRON_CONFLICT_UNRESOLVED]` — cross-host card conflict

**Cause**: cron and your Mac both wrote the same card with different
content. Cron failed loudly: cards landed in
`_conflicts/<run-id>/<card>.local` (your Mac's version) and
`<card>.remote` (cron's version) for manual resolution.

**Recover**: see [docs/CONFLICT_RESOLUTION.md](docs/CONFLICT_RESOLUTION.md).
Manual review takes ~2-3 minutes per conflicted card.

The `[CRON_CONFLICT_UNRESOLVED]` envelope's `next_action` always
includes the run-id; look in `_conflicts/<run-id>/` for sidecars.

---

## `[SYNC_AUTH_FAILED]` — refresh token rotated

**Cause**: X rotated your OAuth refresh token (or you revoked the dev
app) and the cron could not refresh. With self-rotation configured
(`XSENSAI_SECRETS_PAT`), this should now be **rare** — it usually means the
PAT expired, the crash-window was hit (process killed between X consuming the
old token and the secret write landing), or a local `/xsync` rotated the
token without re-pushing the secret.

**Recover** (on Mac):
```bash
# 1. Re-authorize locally; updates the Keychain.
python -m xsensai.sync.setup_oauth --reauth

# 2. Update GitHub Actions secret (shell-portable form).
security find-generic-password -s x-sensai \
  -a x-api-refresh-token -w \
  | gh secret set XSENSAI_X_REFRESH_TOKEN --app actions

# 2b. If XSENSAI_SECRETS_PAT expired, renew it (see CRON_SETUP.md#token-rotation):
gh secret set XSENSAI_SECRETS_PAT --app actions

# 3. Verify with a manual run.
gh workflow run sync.yml
gh run watch

# 4. Clean the flag file.
cd /path/to/your/vault
git pull origin main
git rm SYNC_AUTH_FAILED.md && git commit -m "cron: auth recovered"
git push origin main
```

The flag file's recovery instructions repeat these steps.

---

## `[TOKEN_PERSIST_FAILED]` — synced OK but couldn't save the rotated token

**Cause**: the run synced bookmarks fine, but the rotated X refresh token
could not be written back to the `XSENSAI_X_REFRESH_TOKEN` GitHub secret —
almost always because `XSENSAI_SECRETS_PAT` expired, was revoked, or lost its
`Secrets:write` scope. X refresh tokens are single-use, so **the next run will
fail `AUTH_FAILED`** unless you fix it. The cron writes
`SYNC_TOKEN_PERSIST_FAILED.md` and marks the heartbeat failed so the staleness
banner fires immediately (exit code 1 — partial).

**Recover** (on Mac):
```bash
# 1. Renew the fine-grained PAT if it expired (Settings -> Developer settings
#    -> Fine-grained tokens; Secrets: Read and write on this repo only), then:
gh secret set XSENSAI_SECRETS_PAT --app actions

# 2. Re-auth + re-push the refresh token (the run already consumed the old one).
python -m xsensai.sync.setup_oauth --reauth
security find-generic-password -s x-sensai \
  -a x-api-refresh-token -w \
  | gh secret set XSENSAI_X_REFRESH_TOKEN --app actions

# 3. Verify + clean the flag.
gh workflow run sync.yml && gh run watch
cd /path/to/your/vault && git pull origin main
git rm SYNC_TOKEN_PERSIST_FAILED.md && git commit -m "cron: token persistence recovered" && git push
```

**Prevent**: set the longest PAT expiry you're comfortable with. See
`docs/CRON_SETUP.md#token-rotation`.

---

## `[GH_SECRET_WRITE_FAILED]` — couldn't write the GitHub Actions secret

**Cause**: the `gh secret set` call failed (PAT missing/expired/wrong-scope,
`gh` not on PATH, or a GitHub API error). Surfaced in cron logs and rolled up
into `[TOKEN_PERSIST_FAILED]` above. The preflight (`--check`) canary catches
most of these before any X token is consumed.

**Recover**: confirm `XSENSAI_SECRETS_PAT` is a fine-grained token with
`Secrets: write` on this repo and hasn't expired; re-run
`python -m xsensai.entrypoints.headless --check` to verify the canary passes.

---

## `[INFO/EXTRACTION_BACKLOG_GROWING]` — many cards still pending

**Cause**: `extraction_pending_count >= 50` OR oldest pending card is
>= 30 days old. Slice 5's lazy-extract handles top-3 hits in `/xfind`,
but cards never queried stay pending forever — and `/xask`'s top-20
retrieval pays a recall tax (Spike #10: ~27pp drop body-only).

**Recover**:
```
/xextract backlog
```
Drains all pending cards via host LLM extraction. Takes ~1-3 seconds
per card depending on host model. After completion, banner clears on
next `/xfind`.

If you don't want to drain manually, the banner is informational only —
your retrieval still works at degraded quality on those cards. But
plan to drain weekly for steady-state.

---

## `[INFO/CRON_NO_NEW_BOOKMARKS]` — cron ran successfully, found nothing

**Cause**: scheduled cron run; X had no new bookmarks since last sync.
Heartbeat updated, no commit.

**Recover**: nothing to do. This is the steady-state quiet outcome.

---

## `[INFO/CRON_PARTIAL_DUE_TO_COST]` — cron committed some cards before cap

**Cause**: cron hit `[COST_LIMIT_REACHED]` partway through but
managed to commit and push the cards it had already written. Next run
resumes from checkpoint.

**Recover**: typically nothing — next cron run picks up. If you want
to drain the backlog faster, run `/xsync` from Mac.

---

## `[INFO/CRON_RECOVERED_FROM_CONFLICT]` — heartbeat fast-path resolved conflict

**Cause**: cron's pull-rebase hit a conflict on `_sync-status.md`
(heartbeat). Slice 5 has a deterministic resolver for this case
(autoplan E1) — regenerates from in-memory state, max-merges counters,
continues. No user action needed.

**Recover**: nothing — informational only. Forensic trail in
`_conflicts.md` if you care to inspect.

---

## `[INFO/LAZY_EXTRACT_TRIGGERED]` — `/xfind` extracted a pending card

**Cause**: a pending card surfaced in `/xfind` results; the lazy-extract
pass kicked in. Logged for forensic purposes; not user-facing in the
default render (autoplan DX D7 — log-only by default).

**Recover**: nothing. This is the happy path.

If you want to opt out of lazy extraction for a query, append `no
lazy` to your `/xfind` invocation. Cards stay extraction_pending until
you `/xextract backlog` them.

---

## `[TOMBSTONE_BLOCKED]` — mutation refused on a deleted card (Slice 6)

**Cause**: you (or Claude on your behalf) called `annotate_card` /
`set_pin` / `delete_bookmark` on a card whose frontmatter has
`deleted: true`. Tombstoned cards are excluded from default search,
list, and dedup paths; the only legal mutation is restore.

**Recover**:
- If you want the card back, restore it: `/xrestore` walks the Slice 7
  2-call nonce flow (lists recently-deleted, picks by number, prints an
  8-character confirmation code that you echo). For scripted recovery,
  set `XSENSAI_DESTRUCTIVE_BYPASS=1` in the spawning shell and call
  `restore_bookmark(id)` directly — see "Resolved (Slice 7)" below.
- If you want a fresh card with the same content, use `/xpaste`.
- If you didn't mean to delete this card, restore it first then audit
  recent activity in `~/.cache/xsensai/xsync-log.jsonl` (sync replay
  skipped it after deletion — sticky).

---

## `[NO_ROLLBACK_JOURNAL]` — `--rollback` ran with no journal (Slice 6)

**Cause**: `python scripts/migrate_v1_to_v2.py --rollback` was invoked
but the corpus has no `migrate_v1_to_v2.rollback.jsonl`. Either
`--apply` was never run, or the journal was archived after a previous
successful rollback (filename `migrate_v1_to_v2.rollback.applied-...jsonl`).

**Recover**:
- If you meant to roll back a recent migration, look for the archived
  journal. Slice 6 archives the file on successful rollback rather than
  deleting it.
- If you have not run `--apply`, there's nothing to roll back.

---

## `[SETUP_GH_AUTH_REQUIRED]` — setup wizard needs `gh` (Slice 6)

**Cause**: a setup-wizard step (`--gh-secrets`, `--gh-vars`,
`--first-run`) requires `gh auth status` to succeed. Either `gh` isn't
installed or you haven't logged in.

**Recover**:
1. `brew install gh` (if not present).
2. `gh auth login` (interactive — accept browser auth).
3. Re-run `./scripts/setup.sh --resume` — completed steps are skipped.

---

## `[SETUP_DEPLOY_KEY_REJECTED]` — GitHub rejected the deploy-key POST (Slice 6)

**Cause**: `gh api -X POST repos/{vault}/keys` returned a non-zero
exit. Common causes:
- The `gh` user lacks admin permission on the vault repo.
- A deploy key with the same title (`xsensai-cron-deploy`) already
  exists. The wizard dedups by title on subsequent runs, but the first
  attempt may have raced or stopped mid-flight.

**Recover**:
1. Check permissions: `gh api repos/{vault}` should return 200 and
   include `"permissions": {"admin": true}`.
2. If a stale key exists: list with `gh api repos/{vault}/keys` and
   delete via `gh api -X DELETE repos/{vault}/keys/{id}`.
3. Re-run `./scripts/setup.sh --resume`.

---

## `[SETUP_FIRST_RUN_FAILED]` — first cron workflow run failed (Slice 6)

**Cause**: `gh workflow run sync.yml` triggered correctly but the
resulting GitHub Actions run reached a FAILED state. Most often a
missing or wrong secret value (the deploy key, the X refresh token).

**Recover**:
1. `gh run view {id} --log` (id from `gh run list -w sync.yml -L 1`).
2. Inspect the failure step. If it's auth, re-run
   `./scripts/setup.sh --gh-secrets` and verify each secret was set
   from the Keychain values.
3. Re-trigger via `./scripts/setup.sh --first-run` once secrets are
   correct.

---

## Resolved (Slice 7): destructive-tool confirmation nonce/handshake

The Slice 6 known limitation (`user_confirmed: bool` host-attestable
on `delete_bookmark` and `restore_bookmark`) is now closed by a 2-call
confirmation handshake.

**New flow.** `delete_bookmark(id)` — first call without
`confirmation_nonce` — returns `[NONCE_REQUIRED]` with a short-lived
8-character code in `rendered_message` between `<<<NONCE: ` and `>>>`
markers. The host shows the message verbatim. The user types the
8-character code (case-insensitive, hyphens optional). The host calls
`delete_bookmark(id, confirmation_nonce=<echoed>)` to redeem.
`restore_bookmark` uses the same shape.

**Honest framing.** This raises the social-engineering bar from "the
host sets a bool" to "the user manually echoes a one-time code." The
same host LLM can still mint and redeem the nonce in one tool-use
chain — the nonce alone is NOT a cryptographic user-attestation
boundary. The user remains the only true boundary; the handshake just
makes the path through the boundary visible.

For genuine user-attestation, configure
[Claude Code's per-tool permission prompt](https://code.claude.com/docs/en/permissions)
on `mcp__xsensai__delete_bookmark` and `mcp__xsensai__restore_bookmark`
in `~/.claude/settings.json`. That gates the tool call at the host level.

**Slice 7.5 (v0.9.0.0) auto-installs this gate.**
`./scripts/install_commands.sh` writes the `permissions.ask` entries to your
user-global `~/.claude/settings.json` via `scripts/_settings_merge.py`. See
[docs/PERMISSIONS_ASK.md](docs/PERMISSIONS_ASK.md) for the JSON shape, the
three options when the modal fires (Allow once / Allow for this session /
Always allow), the precedence caveat (`permissions.allow` supersedes
`permissions.ask`), and the override warning (`[PERMISSIONS_WILDCARD_OVERRIDE]`)
that fires when a pre-existing wildcard subsumes the gate.

**Five new error envelopes** (all retryable):

### NONCE_REQUIRED

The first half of the 2-call destructive flow. Returned when
`delete_bookmark` or `restore_bookmark` is called without a
`confirmation_nonce`. The `rendered_message` contains the issued
8-character code. The host displays it verbatim and asks the user to
echo it.

The legacy Slice 6 `user_confirmed: bool` kwarg was removed in v0.9.1.0.
Calls that still pass it raise `TypeError` rather than being shimmed
into this envelope. See "Stale tools/list schema after v0.9.1.0 upgrade"
below if you see a JSON-RPC validation error mentioning `user_confirmed`.

### Stale tools/list schema after v0.9.1.0 upgrade

**Symptom**: After upgrading the MCP server to v0.9.1.0+, calling
`delete_bookmark` or `restore_bookmark` returns a JSON-RPC validation
error (-32602 *invalid params* or -32603 *internal error*) mentioning
`user_confirmed`. Unlike `[NONCE_REQUIRED]`, this error is NOT a
structured `[CODE] / cause / attempted / next_action / retryable`
envelope — it's a raw FastMCP / JSON-RPC failure.

**Cause**: Your Claude Code session is running a cached `tools/list`
schema from before the kwarg removal. The host LLM still believes
`user_confirmed: bool` is a valid argument and sends it; the v0.9.1.0+
server rejects the unknown property.

**Fix**: Restart Claude Code. The MCP `tools/list` response is
re-fetched on session start, picks up the new signature, and subsequent
calls go through the 2-call nonce flow (or `XSENSAI_DESTRUCTIVE_BYPASS=1`
if set) cleanly.

### NONCE_INVALID

The supplied confirmation code did not match any pending request.
Likely cause: typo in the echoed code, OR the code expired and was
garbage-collected, OR the MCP server restarted between issue and
redeem.

**Fix**: Re-run `/xrestore` or `/xdelete` to issue a fresh code.

### NONCE_EXPIRED

The 90-second window passed between issue and redeem.

**Fix**: Re-run the slash command — a new code is issued each time.

### NONCE_OPERATION_MISMATCH

The supplied code matches a code that was issued for a different
operation or different card. (E.g., you typed a delete-code while
calling restore_bookmark; the cause text names both.) Codes are
single-use and per-(operation, target).

**Fix**: Re-run the correct slash command for the operation you intend.

### NONCE_ALREADY_REDEEMED

The supplied code was already used. Each code is single-use.

**Fix**: Re-run the slash command for a fresh code.

---

## Resolved (Slice 7.5 / v0.9.0.0): permissions.ask install + new failure modes

### `[SETTINGS_MALFORMED]` (install-time)

`./scripts/install_commands.sh` tried to merge `permissions.ask` entries into
`~/.claude/settings.json` but the file is not valid JSON.

**Fix**: The install helper backed up the original to
`~/.claude/settings.json.bak.<timestamp>` and skipped the merge (the rest of
the install continued — slash commands installed normally). Fix the JSON in
the original file and re-run `./scripts/install_commands.sh`.

### `[PERMISSIONS_WILDCARD_OVERRIDE]` (install-time WARN)

Your `~/.claude/settings.json` has `permissions.allow` entries (literal or
wildcard like `mcp__*`) that subsume the `ask` entries the install helper
just wrote. Claude Code's permission prompt will NOT fire for these tools —
the cryptographic gate is bypassed.

**Fix**: edit `~/.claude/settings.json`, narrow or remove the matching
`permissions.allow` entry, re-run `./scripts/install_commands.sh`. See
[docs/PERMISSIONS_ASK.md](docs/PERMISSIONS_ASK.md#3-precedence--permissionsallow-supersedes-permissionsask).

### Permissions modal not appearing on `/xdelete` or `/xrestore`

Most common cause: a `permissions.allow` wildcard subsumes the `ask` entry.
Diagnostic:

```bash
cat ~/.claude/settings.json | python -m json.tool | grep -A5 permissions
```

Look for entries like `"mcp__*"` or `"mcp__xsensai__*"` in `allow`. Remove or
narrow them. Restart Claude Code.

Other causes: (a) Claude Code is using a project-local `.claude/settings.json`
that overrides user-global; check `<repo>/.claude/settings.json`. (b) The
install helper wasn't run — check for `permissions.ask` entries in the file;
re-run `./scripts/install_commands.sh` if missing.

### "Always allow" accidentally clicked on the permissions modal

Clicking "Always allow" moves the tool from `permissions.ask` to
`permissions.allow` permanently. To restore the gate:

```bash
# Edit ~/.claude/settings.json — remove the matching entry from allow
$EDITOR ~/.claude/settings.json
# Re-run install to ensure ask entries are present
./scripts/install_commands.sh
# Restart Claude Code
```

### MCP server version mismatch (returns `[USER_CONFIRMATION_REQUIRED]`)

The Slice 7+ slash commands (`/xdelete`, `/xrestore`) call `delete_bookmark`
or `restore_bookmark` and expect `[NONCE_REQUIRED]` envelopes. If the MCP
server is on a pre-Slice-7 version, it returns `[USER_CONFIRMATION_REQUIRED]`
instead — the slash commands won't know what to do.

**Fix**:

```bash
cd /path/to/xsensai
pip install -e .
# Then restart Claude Code to reload the MCP server
```

The Slice 7.5 install script prints a warning when it detects this mismatch
(`WARN: xsensai MCP server is version X but commands target Y`).

---

## Power-user bypass: `XSENSAI_DESTRUCTIVE_BYPASS=1`

For scripted maintenance (cron-side bulk cleanup, test fixtures,
recovery scripts), set `XSENSAI_DESTRUCTIVE_BYPASS=1` in the parent
shell that spawns the MCP server. The handshake is skipped and a loud
audit-log warning is emitted on every destructive call.

The env var is read by the MCP server process at call time. Because
the host LLM cannot inject env vars into a parent process, this bypass
is NOT host-attestable in the prompt-injection sense — it requires
shell-level access by the user. Documented as shell-only.

**Do not** set this env var in your `.zshrc` / `.bashrc` permanently
without understanding that it removes the destructive-tool gate. Use
it for the duration of a specific maintenance script.
