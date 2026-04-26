# Troubleshooting

Keyed by error code. Every error in x-sensai routes through `XSensaiError.format()` so you see exactly which one fired.

---

## `[CORPUS_UNAVAILABLE]`

**Cause:** the corpus directory is missing, empty, or not a directory.

**Fix:**
1. Check `$XSENSAI_CORPUS_PATH` (defaults to `~/Documents/Vault/04_areas/x-bookmarks/`).
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

