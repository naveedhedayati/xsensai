"""Slice 5 — headless cron entrypoint (GitHub Actions).

Orchestration:
  1. Read env (refresh token, client_id, optional client_secret, corpus path).
  2. Build EnvSecretTokenProvider (Slice 4 seam) + DeferredExtractor
     (Slice 4 default for headless mode).
  3. Run service.run(mode="headless") which auto-proceeds-dirty (cron's
     vault clone is always "dirty"-OK by design — autoplan F8 / TODOS P1).
  4. service.finalize_run with cron_runner — heartbeat + checkpoint
     archival + reindex trigger + cron-mirror counters (autoplan E5).
  5. On cards-written: commit_and_push to vault repo with conflict + push
     reject handling.
  6. Exit codes:
       0 = full success (cards committed + pushed)
       0 = no new bookmarks (heartbeat updated, no commit)
       1 = partial — some cards committed but cap or non-fatal error hit
       2 = fatal — auth fail, conflict-unresolved, no commit at all

CLI:
  python -m xsensai.entrypoints.headless
  python -m xsensai.entrypoints.headless --check
  python -m xsensai.entrypoints.headless --emit-secrets-stdin

Logs to stderr only (matches Slice 0 MCP stdio rule).

Known limitation (autoplan E9): `BudgetTracker` is built here but not
threaded into `XClient` yet — the cost cap is currently advertised but
not enforced. Wiring is a follow-up TODO. The 10-minute workflow
timeout serves as the hard ceiling in the meantime.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from xsensai.errors import XSensaiError
from xsensai.sync import git_push
from xsensai.sync.auth import (
    CLIENT_ID_ENV,
    CLIENT_SECRET_ENV,
    ENV_VAR_NAME as REFRESH_TOKEN_ENV,
    SECRETS_PAT_ENV,
    EnvSecretTokenProvider,
    GhSecretTokenProvider,
)
from xsensai.sync.cost_ceiling import BudgetTracker
from xsensai.sync.extraction import DeferredExtractor
from xsensai.sync.heartbeat import read_status, update_after_run

log = logging.getLogger(__name__)


# Auth-failure flag committed to vault for user visibility (autoplan E7
# = static template only, no exception text).
SYNC_AUTH_FAILED_FLAG = "SYNC_AUTH_FAILED.md"

# Token-persist-failure flag: the run synced fine but the rotated refresh
# token could not be written back to the GH secret, so the NEXT run will die.
# Distinct from SYNC_AUTH_FAILED so logs/recovery can tell the two apart.
SYNC_TOKEN_PERSIST_FAILED_FLAG = "SYNC_TOKEN_PERSIST_FAILED.md"

# Explicit opt-out so a missing PAT in GitHub Actions is non-fatal (e.g. an
# intentionally rotation-disabled run). Without it, missing PAT in Actions is
# fatal — a silent no-op fallback would resurrect the P0 chronic-failure bug.
ALLOW_NO_PERSIST_ENV = "XSENSAI_ALLOW_NO_PERSIST"

# Error codes whose presence in `RunResult.rendered_message` triggers the
# auth-failed flag-write path. Keep the list explicit — easier to audit
# than a substring match.
AUTH_FAIL_PREFIXES = (
    "[AUTH_FAILED]",
    "[OAUTH_SETUP_REQUIRED]",
    "[OAUTH_GRANT_REFUSED]",
    "[OAUTH_KEYCHAIN_BLOCKED]",
    "[OAUTH_CLIENT_ID_MISSING]",
)


def _auth_failed_recovery_text(run_id: str) -> str:
    """Static template; never interpolates secrets (autoplan E7 / DX D6)."""
    return (
        "# x-sensai: cron OAuth refresh failed\n\n"
        f"Run `{run_id}` could not refresh the X API token. The refresh "
        "token in GitHub Actions secrets is likely rotated or expired.\n\n"
        "## Recover (on your Mac)\n\n"
        "```bash\n"
        "# 1. Re-authorize X API locally\n"
        "python -m xsensai.sync.setup_oauth --reauth\n\n"
        "# 2. Update GitHub Actions secret with the new token\n"
        "security find-generic-password -s x-sensai \\\n"
        "  -a x-api-refresh-token -w \\\n"
        "  | gh secret set XSENSAI_X_REFRESH_TOKEN --body -\n\n"
        "# 3. Trigger a manual cron run\n"
        "gh workflow run sync.yml\n\n"
        "# 4. After the manual run is green, delete this flag\n"
        "git rm SYNC_AUTH_FAILED.md && git commit -m 'cron: auth recovered'\n"
        "```\n\n"
        "See `docs/CRON_SETUP.md` for the full token-rotation runbook.\n"
    )


def _token_persist_failed_text(run_id: str) -> str:
    """Static template; never interpolates secrets.

    The run synced fine but the rotated refresh token could not be written
    back to the GH secret. The token X handed us is single-use and already
    consumed, so the NEXT run will fail until the secret is refreshed.
    """
    return (
        "# x-sensai: cron token-persist failed (next run will fail)\n\n"
        f"Run `{run_id}` synced bookmarks successfully, but could NOT save the "
        "rotated X refresh token back to the GitHub Actions secret. X refresh "
        "tokens are single-use, so the next scheduled run will fail "
        "`AUTH_FAILED` until you refresh the secret.\n\n"
        "## Most likely cause\n\n"
        "The `XSENSAI_SECRETS_PAT` fine-grained token expired, was revoked, or "
        "lost its `Secrets:write` permission on this repo.\n\n"
        "## Recover (on your Mac)\n\n"
        "```bash\n"
        "# 1. Re-authorize X API locally\n"
        "python -m xsensai.sync.setup_oauth --reauth\n\n"
        "# 2. Re-push the refresh token secret\n"
        "python -m xsensai.entrypoints.headless --emit-secrets-stdin\n\n"
        "# 3. Renew XSENSAI_SECRETS_PAT if it expired, then re-push it:\n"
        "#    gh secret set XSENSAI_SECRETS_PAT --app actions   (paste the new PAT)\n\n"
        "# 4. Trigger a manual run, then delete this flag when green\n"
        "gh workflow run sync.yml\n"
        "git rm SYNC_TOKEN_PERSIST_FAILED.md && git commit -m 'cron: token persistence recovered'\n"
        "```\n\n"
        "See `docs/CRON_SETUP.md#token-rotation` for details.\n"
    )


def _emit_secrets_stdin() -> int:
    """Print ready-to-paste `gh secret set` commands using local Keychain.

    DX D1: cuts cron setup time by eliminating the most error-prone step
    (manually piping refresh token from Keychain into `gh secret set`).
    User runs this on their Mac after `setup_oauth.py` has populated
    the Keychain.

    Output goes to stdout (NOT stderr) — by design, it's meant to be
    eval'd or copy-pasted.
    """
    print(
        "# Run these on your Mac with `gh` authenticated against the\n"
        "# xsensai (this) repo. Each `gh secret set` reads stdin from\n"
        "# the macOS Keychain. Empty lines / comments are safe.\n",
        flush=True,
    )
    print("# 1. Refresh token (set first; it's the load-bearing one):")
    print(
        'security find-generic-password -s x-sensai '
        '-a x-api-refresh-token -w | '
        f'gh secret set {REFRESH_TOKEN_ENV} --body -'
    )
    print()
    print("# 2. Client ID:")
    print(
        'security find-generic-password -s x-sensai '
        '-a x-api-client-id -w | '
        f'gh secret set {CLIENT_ID_ENV} --body -'
    )
    print()
    print(
        "# 3. Client SECRET (only if your X dev app is a Confidential "
        "client — \"Web App\" type):"
    )
    print(
        '# security find-generic-password -s x-sensai '
        '-a x-api-client-secret -w | '
        f'gh secret set {CLIENT_SECRET_ENV} --body -'
    )
    print()
    print(
        "# 4. Vault deploy key (private half — generate with `ssh-keygen "
        "-t ed25519 -N \"\"`):"
    )
    print('# cat /path/to/your/deploy-key | gh secret set VAULT_DEPLOY_KEY --body -')
    return 0


def _in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def _persist_config() -> tuple[str, str, bool]:
    """Resolve token-persistence config from env: (pat, repo, allow_no_persist).

    Single source of truth shared by `_check_preflight` and `run()` so the two
    can't drift on which env vars gate self-rotation.
    """
    pat = os.environ.get(SECRETS_PAT_ENV, "").strip()
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    allow_no_persist = os.environ.get(ALLOW_NO_PERSIST_ENV, "").strip().lower() in (
        "1", "true", "yes",
    )
    return pat, repo, allow_no_persist


def _check_preflight() -> int:
    """Verify env + xdk + secret-write readiness without burning an X token.

    Used by the workflow as a fast-fail before running real sync. Critically,
    when a PAT is configured this PROVES `Secrets:write` works via a canary
    secret write/delete BEFORE any single-use X token is consumed — so a
    broken/expired/wrong-scope PAT is caught while it's still cheap, not after
    we've burned the X token and can't persist the replacement (Codex #1).
    """
    issues = []
    if not os.environ.get(REFRESH_TOKEN_ENV):
        issues.append(f"missing env: {REFRESH_TOKEN_ENV}")
    if not os.environ.get(CLIENT_ID_ENV):
        issues.append(f"missing env: {CLIENT_ID_ENV}")
    try:
        import xdk  # type: ignore[import-untyped]  # noqa: F401
    except ImportError:
        issues.append("xdk not installed (pip install xdk)")

    # Token-persistence preflight.
    pat, repo, allow_no_persist = _persist_config()
    if pat and repo:
        # Log what `gh` we're running against (Codex #8 — preinstalled but not
        # version-pinned), then prove the PAT can actually write a secret.
        from xsensai.sync.gh_secrets_updater import gh_diagnostics, verify_secret_write
        print("gh diagnostics:", file=sys.stderr)
        for line in gh_diagnostics(pat=pat).splitlines():
            print(f"    {line}", file=sys.stderr)
        try:
            verify_secret_write(repo, pat=pat)
            print("    canary secret write/delete OK", file=sys.stderr)
        except XSensaiError as e:
            issues.append(f"PAT cannot write repo secrets: {e.cause}")
    elif _in_actions() and not allow_no_persist:
        # Fatal in CI: a missing PAT means rotation can't persist and the cron
        # silently resurrects the P0. Opt out with XSENSAI_ALLOW_NO_PERSIST=1.
        issues.append(
            f"missing env: {SECRETS_PAT_ENV} (token rotation cannot persist in "
            f"GitHub Actions; set {ALLOW_NO_PERSIST_ENV}=1 to override)"
        )

    if issues:
        print("PREFLIGHT FAIL", file=sys.stderr)
        for i in issues:
            print(f"  - {i}", file=sys.stderr)
        return 2
    print("PREFLIGHT OK", file=sys.stderr)
    return 0


def _extract_error_code(rendered_message: Optional[str]) -> str:
    """Pull the bracketed code out of a XSensaiError-formatted message.

    `rendered_message` shape: `[CODE] cause...`. Returns "unknown" if
    parsing fails.
    """
    if not rendered_message:
        return "unknown"
    m = rendered_message.lstrip()
    if not m.startswith("["):
        return "unknown"
    end = m.find("]")
    if end < 1:
        return "unknown"
    return m[1:end]


def _write_and_push_persist_flag(corpus: Path, *, now: datetime) -> None:
    """Write the TOKEN_PERSIST_FAILED flag and commit/push it (best-effort).

    Heartbeat success/last_error are set by the caller's finalize_run /
    update_after_run. This only handles the vault-visible recovery flag.
    """
    run_id = f"headless-{now.strftime('%Y%m%dT%H%M%SZ')}"
    flag_path = corpus / SYNC_TOKEN_PERSIST_FAILED_FLAG
    try:
        flag_path.write_text(_token_persist_failed_text(run_id))
    except OSError as e:
        log.exception("failed to write TOKEN_PERSIST_FAILED flag: %s", e)
    try:
        current_status = read_status(corpus)
        if current_status is not None:
            git_push.commit_and_push(
                corpus,
                message=f"[SYNC_TOKEN_PERSIST_FAILED] {run_id}",
                in_memory_status=current_status,
                run_id=run_id,
            )
    except Exception:
        log.exception("failed to commit + push token-persist-failure flag")


def _emit_token_persist_failed_flag(corpus: Path, *, now: datetime) -> int:
    """Success-path persist failure: write+push flag, warn, return exit 1.

    Used by the no-card success paths (empty / no-new). The cards path writes
    the flag inline so it rides the cards commit.
    """
    _write_and_push_persist_flag(corpus, now=now)
    print(
        "[TOKEN_PERSIST_FAILED] synced OK but could not persist the rotated X "
        "token; the next run will fail until re-auth. See "
        "SYNC_TOKEN_PERSIST_FAILED.md.",
        file=sys.stderr,
    )
    return 1


def run(
    *,
    corpus_path: Optional[Path] = None,
    runner: str = "github-actions",
    now: Optional[datetime] = None,
) -> int:
    """Headless cron orchestrator. Returns the process exit code.

    Sequence:
      1. Build providers + extractor + tracker.
      2. service.run(mode="headless").
      3. Detect AUTH_FAILED via rendered_message; on hit, write flag +
         commit + push, exit 2.
      4. service.finalize_run(cron_runner=runner) — heartbeat + checkpoint
         + reindex + cron-mirror counters.
      5. If success → git_push.commit_and_push.
      6. Map result to exit code.
    """
    now = now or datetime.now(timezone.utc)

    refresh_token = os.environ.get(REFRESH_TOKEN_ENV, "").strip()
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    if not refresh_token or not client_id:
        print(
            "[OAUTH_SETUP_REQUIRED] missing env vars; cron cannot run.\n"
            f"Need: {REFRESH_TOKEN_ENV}, {CLIENT_ID_ENV}.\n"
            "See docs/CRON_SETUP.md for the runbook.",
            file=sys.stderr,
        )
        return 2

    from xsensai.storage.corpus import resolve_corpus_path
    corpus = resolve_corpus_path(corpus_path)

    # Provider selection (T3 / Codex #4): use the persisting provider when a
    # PAT + repo are available so rotation closes the loop. In GitHub Actions a
    # missing PAT is FATAL (a silent no-op fallback would resurrect the P0
    # chronic-failure bug) unless explicitly opted out.
    pat, repo, allow_no_persist = _persist_config()
    if pat and repo:
        token_provider: EnvSecretTokenProvider = GhSecretTokenProvider(repo=repo, pat=pat)
        log.info("token persistence: ENABLED via GH secret on %s", repo)
    else:
        if _in_actions() and not allow_no_persist:
            print(
                "[TOKEN_PERSIST_FAILED] running in GitHub Actions but "
                f"{SECRETS_PAT_ENV} / GITHUB_REPOSITORY is missing; a rotated X "
                "token could not be persisted and the cron would silently break "
                f"on the next run. Set {SECRETS_PAT_ENV} (see "
                f"docs/CRON_SETUP.md#token-rotation) or {ALLOW_NO_PERSIST_ENV}=1 "
                "to override.",
                file=sys.stderr,
            )
            return 2
        token_provider = EnvSecretTokenProvider()
        log.warning(
            "token persistence: DISABLED (no %s) — a rotated X token will NOT "
            "be saved; the next run may fail AUTH_FAILED.",
            SECRETS_PAT_ENV,
        )
    deferred = DeferredExtractor()
    # NB: BudgetTracker constructed but not threaded into service.run
    # yet — see module docstring + autoplan E9 known limitation.
    _ = BudgetTracker.from_env()

    # service.run is imported lazily — keeps --check / --emit-secrets-stdin
    # callable even when the dev hasn't installed xdk yet.
    from xsensai.sync import service as _service

    run_result: Optional[_service.RunResult] = None
    try:
        run_result = _service.run(
            mode="headless",
            token_provider=token_provider,
            client_id=client_id,
            corpus_path=corpus,
            extractor_override=deferred,
            # max_pages defense-in-depth until BudgetTracker is threaded
            # into XClient (autoplan E9 known limitation). 10 pages * 100
            # bookmarks/page = 1000 candidates max — ~30x typical cron
            # volume. Combined with the 10-min workflow timeout, prevents
            # runaway pagination if X API regresses.
            max_pages=10,
            now=now,
        )
    except Exception as e:
        log.exception("Unhandled exception in service.run: %s", type(e).__name__)
        # Best-effort heartbeat + cron mirror so the user sees the failure
        # next vault pull.
        existing = read_status(corpus)
        update_after_run(
            corpus,
            success=False,
            new_cards_this_run=0,
            extraction_pending_count=existing.extraction_pending_count if existing else 0,
            total_cards=existing.total_cards if existing else 0,
            now=now,
            cron_runner=runner,
            last_error=type(e).__name__,
        )
        return 2

    msg = run_result.rendered_message or ""
    is_auth_failed = any(msg.startswith(p) for p in AUTH_FAIL_PREFIXES)

    if is_auth_failed:
        flag_path = corpus / SYNC_AUTH_FAILED_FLAG
        flag_run_id = f"headless-{now.strftime('%Y%m%dT%H%M%SZ')}"
        try:
            flag_path.write_text(_auth_failed_recovery_text(flag_run_id))
        except OSError as e:
            log.exception("failed to write SYNC_AUTH_FAILED flag: %s", e)
        existing = read_status(corpus)
        update_after_run(
            corpus,
            success=False,
            new_cards_this_run=0,
            extraction_pending_count=existing.extraction_pending_count if existing else 0,
            total_cards=existing.total_cards if existing else 0,
            now=now,
            cron_runner=runner,
            last_error=_extract_error_code(msg),
        )
        try:
            current_status = read_status(corpus)
            if current_status is not None:
                git_push.commit_and_push(
                    corpus,
                    message=f"[SYNC_AUTH_FAILED] {flag_run_id}",
                    in_memory_status=current_status,
                    run_id=flag_run_id,
                )
        except Exception:
            log.exception("failed to commit + push auth-failure flag")
        return 2

    # Token-persist-failure detection (T4 / Codex #5): the rotated token is
    # single-use and already consumed by X, so a failed writeback means THIS
    # run synced fine but the NEXT run dies AUTH_FAILED. Surfaced on EVERY
    # success return below (empty / no-new / cards), not just the cards path.
    persist_failed = (
        isinstance(token_provider, GhSecretTokenProvider)
        and token_provider.last_persist_error is not None
    )
    persist_last_error = "TOKEN_PERSIST_FAILED" if persist_failed else None

    if run_result.status == "empty":
        # No new bookmarks since last sync. Per CLAUDE.md spec:
        # "Exit codes: 0 full / 0 no-new / 1 partial / 2 fatal".
        # Treat as success: heartbeat success=True (resets
        # consecutive_cron_failures), no commit/push, exit 0.
        _service.finalize_run(
            run_id=run_result.run_id,
            success=not persist_failed,
            n_new_cards=0,
            extraction_inline=0,
            extraction_pending=0,
            threads_unfetched_this_run=run_result.threads_unfetched_this_run,
            last_error=persist_last_error,
            corpus_path=corpus,
            duration_ms=run_result.duration_ms,
            mode="headless",
            cron_runner=runner,
        )
        if persist_failed:
            return _emit_token_persist_failed_flag(corpus, now=now)
        print(
            "[INFO/CRON_NO_NEW_BOOKMARKS] cron found no new bookmarks since last run.",
            file=sys.stderr,
        )
        return 0

    if run_result.status != "ok":
        # Generic non-auth failure path. Heartbeat update only —
        # finalize_run skipped because we don't trust the partial state.
        # Persist-failure check (Claude review F6): rotation happens at first
        # auth (before sync work), so the token can be consumed + writeback
        # failed even when the run later returns a non-ok status. Surface it so
        # the next-run AUTH_FAILED is explained instead of silent.
        existing = read_status(corpus)
        update_after_run(
            corpus,
            success=False,
            new_cards_this_run=0,
            extraction_pending_count=existing.extraction_pending_count if existing else 0,
            total_cards=existing.total_cards if existing else 0,
            now=now,
            cron_runner=runner,
            last_error="TOKEN_PERSIST_FAILED" if persist_failed else _extract_error_code(msg),
        )
        if persist_failed:
            _write_and_push_persist_flag(corpus, now=now)
            print(
                "[TOKEN_PERSIST_FAILED] run failed AND the rotated X token could "
                "not be persisted; re-auth needed before the next run. See "
                "SYNC_TOKEN_PERSIST_FAILED.md.",
                file=sys.stderr,
            )
        elif msg:
            print(msg, file=sys.stderr)
        return 2

    # Success path — DeferredExtractor leaves cards with extraction_pending=True
    # so all cards_written are also extraction_pending.
    n_new_cards = len(run_result.cards_written)
    n_pending = n_new_cards  # all deferred per headless mode

    # finalize_run handles: total_cards count, heartbeat update (with
    # cron_runner mirror), checkpoint archive, reindex trigger, log append.
    _service.finalize_run(
        run_id=run_result.run_id,
        success=not persist_failed,
        n_new_cards=n_new_cards,
        extraction_inline=0,
        extraction_pending=n_pending,
        threads_unfetched_this_run=run_result.threads_unfetched_this_run,
        last_error=persist_last_error,
        corpus_path=corpus,
        duration_ms=run_result.duration_ms,
        mode="headless",
        cron_runner=runner,
    )

    if n_new_cards == 0:
        if persist_failed:
            return _emit_token_persist_failed_flag(corpus, now=now)
        print(
            "[INFO/CRON_NO_NEW_BOOKMARKS] cron found no new bookmarks since last run.",
            file=sys.stderr,
        )
        return 0

    # If the rotated token couldn't be persisted, write the flag NOW so it
    # rides the same cards commit below (.md is in the push allowlist). The
    # cards still ship; the flag warns that the next run will fail.
    if persist_failed:
        try:
            (corpus / SYNC_TOKEN_PERSIST_FAILED_FLAG).write_text(
                _token_persist_failed_text(
                    f"headless-{now.strftime('%Y%m%dT%H%M%SZ')}"
                )
            )
        except OSError as e:
            log.exception("failed to write TOKEN_PERSIST_FAILED flag: %s", e)

    # Commit + push. The in_memory_status used by heartbeat fast-path
    # is fresh (just written by finalize_run).
    final_status = read_status(corpus)
    if final_status is None:
        log.error("read_status returned None after finalize_run; aborting push")
        return 2

    push_result = git_push.commit_and_push(
        corpus,
        message=f"cron: synced {n_new_cards} bookmark(s)",
        in_memory_status=final_status,
        run_id=run_result.run_id,
    )
    if push_result.success:
        if persist_failed:
            print(
                "[TOKEN_PERSIST_FAILED] synced + pushed cards, but could not "
                "persist the rotated X token; the next run will fail until "
                "re-auth. See SYNC_TOKEN_PERSIST_FAILED.md.",
                file=sys.stderr,
            )
            return 1
        return 0
    if push_result.conflict_unresolved:
        if push_result.error:
            print(push_result.error.format(), file=sys.stderr)
        return 2
    if push_result.flag_written:
        if push_result.error:
            print(push_result.error.format(), file=sys.stderr)
        return 1
    return 1


def _cli() -> int:
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("XSENSAI_LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(
        prog="python -m xsensai.entrypoints.headless",
        description="Slice 5 cron entrypoint (also DX setup helpers).",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Verify env + xdk readiness; print PREFLIGHT OK/FAIL.",
    )
    parser.add_argument(
        "--emit-secrets-stdin", action="store_true",
        help=(
            "Print ready-to-paste `gh secret set` commands that read from "
            "the macOS Keychain. DX D1 helper for cron setup."
        ),
    )
    args = parser.parse_args()

    if args.emit_secrets_stdin:
        return _emit_secrets_stdin()
    if args.check:
        return _check_preflight()
    return run()


if __name__ == "__main__":
    sys.exit(_cli())
