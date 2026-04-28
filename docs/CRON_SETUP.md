# CRON_SETUP — one-time setup for scheduled X bookmark sync

Slice 5 ships `.github/workflows/sync.yml`: every 2 days at 07:00 UTC,
GitHub Actions runs `python -m xsensai.entrypoints.headless`, fetches new
bookmarks from X, commits them to your vault repo, and pushes.

This document is the one-time setup guide. Honest expectation:

> **45-90 minutes for first-time setup**, mostly external bureaucracy
> (X dev portal, GitHub web UI, deploy key). 5-10 minutes on a second
> machine if the secrets already exist.
>
> **5-15 minutes** to refresh secrets when the X refresh token rotates
> (token rotation runbook below).

If you've never set up GitHub Actions secrets or generated a deploy
key, the time will be on the higher end. The `--emit-secrets-stdin`
helper (step 5) eliminates the most error-prone step.

> **Slice 6 update — guided wizard.** `./scripts/setup.sh --all`
> automates steps 0, 3, 4, 5, 6, and 7 below (preflight, OAuth, deploy
> key, GH secrets, GH variables, first run) plus the v1→v2 migration
> step. Each step is independently invokable
> (`--preflight` / `--oauth` / `--deploy-key` / `--gh-secrets` /
> `--gh-vars` / `--first-run` / `--migrate`) and idempotent — re-running
> after a partial failure resumes from the failed step via `--resume`.
> State lives at `~/.cache/xsensai/setup-state.json`. Realistic time
> after Slice 6: **< 15 min** end-to-end if you have an X dev app
> client_id pre-created. The manual steps below remain as the
> reference.

---

## Stopwatch checklist

```
[ ] 0. Preflight (5 min)
[ ] 1. Vault repo set up as a git remote (5-15 min)
[ ] 2. xsensai code repo has CI working (1 min — already done)
[ ] 3. X API tokens already in macOS Keychain (5-10 min if not — run setup_oauth)
[ ] 4. Deploy key generated + added to vault repo (10-15 min)
[ ] 5. GitHub Actions secrets set on xsensai repo (5-10 min — use helper)
[ ] 6. VAULT_REPO variable set on xsensai repo (1 min)
[ ] 7. Manual workflow_dispatch trigger + verify green (5-15 min)
[ ] 8. Commit + push lands in vault repo, then schedule takes over
```

Each step has a hard pass/fail check. If a step fails, fix it before
moving on — the workflow is built to fail fast on missing prereqs.

---

## Step 0 — Preflight

```bash
gh auth status              # must be authenticated against your xsensai repo
which uv                    # required for hash-locked deps
python --version            # must be 3.11+
```

If any fail:
- `gh auth login` (pick GitHub.com, HTTPS, browser auth)
- `pip install uv==0.11.7`
- macOS: `brew install python@3.11`

---

## Step 1 — Vault repo

The vault repo is your private GitHub repo containing
`04_areas/x-bookmarks/` (where the cards live). If it's not on GitHub
yet:

```bash
cd /path/to/your/vault
git init && git add . && git commit -m "initial vault commit"
gh repo create <owner>/<repo-name> --private --source=. --push
```

Verify push works manually:

```bash
echo "# vault-readme-test" >> README.md
git add README.md && git commit -m "test: vault push works"
git push origin main
```

Pass criteria: commit lands on remote with no auth prompt.

---

## Step 2 — xsensai repo (this repo)

You've already merged Slice 5 if you're reading this — nothing to do.

---

## Step 3 — X API tokens in macOS Keychain

If you've already run `/xsync` successfully on Mac, skip this step. The
tokens are already in Keychain.

If not, run the local OAuth flow (browser-based):

```bash
python -m xsensai.sync.setup_oauth --check    # verify preconditions
python -m xsensai.sync.setup_oauth            # interactive PKCE flow
```

The flow opens a browser, you authorize the X app, the redirect captures
the code, and the refresh token + client_id (and client_secret if
Confidential) get stored in Keychain.

Verify:

```bash
security find-generic-password -s x-sensai -a x-api-refresh-token -w
# Should print a long opaque string. If "could not be found":
# re-run setup_oauth.
```

---

## Step 4 — Deploy key for vault repo

Generate a passphrase-less ed25519 key (passphrases break unattended
runs):

```bash
ssh-keygen -t ed25519 -N "" -C "xsensai-cron" -f /tmp/xsensai-cron-key
```

This produces `/tmp/xsensai-cron-key` (private) and
`/tmp/xsensai-cron-key.pub` (public).

**Add public half to vault repo Deploy keys:**

```bash
gh repo set-default <owner>/<vault-repo>            # if needed
gh api -X POST repos/<owner>/<vault-repo>/keys \
   --field title="xsensai-cron" \
   --field key="$(cat /tmp/xsensai-cron-key.pub)" \
   --field read_only=false
```

Or via web UI: vault repo → Settings → Deploy keys → Add deploy key,
title "xsensai-cron", paste the contents of
`/tmp/xsensai-cron-key.pub`, **check "Allow write access"**, Add key.

Pass criteria: a deploy key with name "xsensai-cron" and write access
appears in vault repo Settings.

---

## Step 5 — GitHub Actions secrets on xsensai repo

The fast path uses the `--emit-secrets-stdin` helper. **Run it on the
Mac that has the X API tokens in Keychain (i.e., the same machine
you ran setup_oauth on):**

```bash
cd /path/to/xsensai
python -m xsensai.entrypoints.headless --emit-secrets-stdin
```

This prints the four `gh secret set` commands (with `security
find-generic-password` reading from your Keychain). Copy them out and
run, OR pipe them through your shell:

```bash
python -m xsensai.entrypoints.headless --emit-secrets-stdin | bash
```

(Only pipe to bash if you trust your environment — the helper output
is plain text, not bash-untrusted; this is for convenience.)

**Manually for `VAULT_DEPLOY_KEY`:**

```bash
gh secret set VAULT_DEPLOY_KEY --body "$(cat /tmp/xsensai-cron-key)"
```

Then immediately delete the local key files:

```bash
shred -u /tmp/xsensai-cron-key /tmp/xsensai-cron-key.pub 2>/dev/null \
  || rm -P /tmp/xsensai-cron-key /tmp/xsensai-cron-key.pub
```

Verify:

```bash
gh secret list
# Should show: XSENSAI_X_REFRESH_TOKEN, XSENSAI_X_CLIENT_ID,
# (XSENSAI_X_CLIENT_SECRET if Confidential), VAULT_DEPLOY_KEY
```

---

## Step 6 — VAULT_REPO variable

The vault repo slug is NOT secret — store it as a workflow variable
(not a secret):

```bash
gh variable set VAULT_REPO --body "<owner>/<vault-repo-name>"
# Example: gh variable set VAULT_REPO --body "naveedhedayati/obsidian-vault"
```

If your vault corpus lives at a non-default subpath (default:
`04_areas/x-bookmarks`), also set:

```bash
gh variable set VAULT_CORPUS_SUBPATH --body "your/custom/subpath"
```

Verify:

```bash
gh variable list
# Should show: VAULT_REPO, (VAULT_CORPUS_SUBPATH if set)
```

---

## Step 7 — First manual run

Trigger the workflow manually before waiting for the cron schedule:

```bash
gh workflow run sync.yml
gh run watch
```

What you should see:
1. `Sanity — vault repo configured` ✓
2. `Checkout xsensai code` ✓
3. `Checkout vault via deploy key` ✓ (this proves the deploy key works)
4. `Set up Python` ✓
5. `Install uv` ✓
6. `Install dependencies (hash-locked)` ✓
7. `Install xsensai package` ✓
8. `Configure git identity for the cron commit` ✓
9. `Preflight check (env + xdk)` → `PREFLIGHT OK`
10. `Run headless sync` → `[INFO/CRON_NO_NEW_BOOKMARKS]` (first run, if you've
    already /xsynced everything from Mac) OR a list of new cards.
11. `Show heartbeat` → prints `_sync-status.md` content with
    `last_cron_run` set to now.

Pass criteria: workflow finishes green; if cards landed, vault repo
has a new commit `cron: synced N bookmark(s)`.

---

## Step 8 — Schedule takes over

The cron schedule is `0 7 */2 * *` (every 2 days at 07:00 UTC). After
the first manual run, the schedule fires automatically. Check:

```bash
gh workflow view sync.yml
# Shows the schedule + next run estimate.
```

After 2 days, expect to see a new scheduled run in the Actions tab.

---

## Token rotation runbook

X may rotate your refresh token. The cron run will fail with `[SYNC_AUTH_FAILED]`
and write `SYNC_AUTH_FAILED.md` to your vault. The banner in `/xfind`
and `/xask` will surface it.

**Recovery (on Mac):**

```bash
# 1. Re-authorize locally (browser flow; updates Keychain)
python -m xsensai.sync.setup_oauth --reauth

# 2. Update GitHub Actions secret with the new token (shell-portable form)
security find-generic-password -s x-sensai \
  -a x-api-refresh-token -w \
  | gh secret set XSENSAI_X_REFRESH_TOKEN --body -

# 3. Trigger a manual cron run to verify
gh workflow run sync.yml
gh run watch

# 4. After green, clean up the flag file in your vault
cd /path/to/your/vault
git pull origin main           # pull cron's latest
git rm SYNC_AUTH_FAILED.md
git commit -m "cron: auth recovered"
git push origin main
```

Time: ~10-15 minutes if you remember the runbook. The flag file's
recovery instructions repeat the steps so you don't have to dig for
this doc.

---

## Push rejection runbook

If `SYNC_PUSH_REJECTED.md` appears, cron's push lost the race after 3
retries. This usually means you pushed to the vault from Mac at the
exact same window as cron's schedule.

**Recovery (on Mac):**

```bash
cd /path/to/your/vault
git pull --rebase origin main
# resolve any conflicts in your editor
git push origin main
gh workflow run sync.yml      # re-trigger; should succeed now
git rm SYNC_PUSH_REJECTED.md && git commit -m "cron: push recovered"
git push origin main
```

---

## Cross-host conflict runbook

If `_conflicts/<run-id>/` appears in your vault, cron and your Mac both
wrote the same card with different content. See
[CONFLICT_RESOLUTION.md](CONFLICT_RESOLUTION.md) for the resolution
workflow.

---

## Cost expectations

- **GitHub Actions compute**: ~3 min/run × 15 runs/month = 45 min/month.
  Free tier is 2000 min/month. Functionally $0.
- **X API**: pay-per-Post. Steady-state expectation is ~50
  bookmarks/month + occasional thread-walks ≈ $1-2/month.
- **No LLM cost in cron** — Slice 5 keeps extraction on the host
  (lazy-extract in `/xfind`).

Total ongoing: under $5/month.

---

## Disabling cron

If you decide to go back to manual `/xsync`-only:

```bash
gh workflow disable sync.yml
```

Re-enable later with `gh workflow enable sync.yml`. No data is lost
either way — your vault stays git-versioned.

---

## Troubleshooting

See `TROUBLESHOOTING.md` for envelope-by-envelope recovery for every
new error code in Slice 5: `[COST_LIMIT_REACHED]`, `[SYNC_PUSH_REJECTED]`,
`[CRON_CONFLICT_UNRESOLVED]`, `[SYNC_AUTH_FAILED]`,
`[INFO/EXTRACTION_BACKLOG_GROWING]`.

If you hit a state not covered, the workflow's `Show heartbeat` step
always runs (`if: always()`) — check `_sync-status.md` content first
for cron's view of the world.
