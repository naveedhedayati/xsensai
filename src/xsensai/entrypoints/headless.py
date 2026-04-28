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
    EnvSecretTokenProvider,
)
from xsensai.sync.cost_ceiling import BudgetTracker
from xsensai.sync.extraction import DeferredExtractor
from xsensai.sync.heartbeat import read_status, update_after_run

log = logging.getLogger(__name__)


# Auth-failure flag committed to vault for user visibility (autoplan E7
# = static template only, no exception text).
SYNC_AUTH_FAILED_FLAG = "SYNC_AUTH_FAILED.md"

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


def _check_preflight() -> int:
    """Verify env + xdk + keychain readiness without burning a token.

    Used by the workflow as a fast-fail before running real sync.
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

    token_provider = EnvSecretTokenProvider()
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

    if run_result.status != "ok":
        # Generic non-auth failure path (already includes empty fetch +
        # internal errors via _failed_result). Heartbeat update only —
        # finalize_run skipped because we don't trust the partial state.
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
        if msg:
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
        success=True,
        n_new_cards=n_new_cards,
        extraction_inline=0,
        extraction_pending=n_pending,
        threads_unfetched_this_run=run_result.threads_unfetched_this_run,
        corpus_path=corpus,
        duration_ms=run_result.duration_ms,
        mode="headless",
        cron_runner=runner,
    )

    if n_new_cards == 0:
        print(
            "[INFO/CRON_NO_NEW_BOOKMARKS] cron found no new bookmarks since last run.",
            file=sys.stderr,
        )
        return 0

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
