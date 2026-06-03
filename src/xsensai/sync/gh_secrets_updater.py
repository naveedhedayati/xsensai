"""Cron self-rotation — write a rotated X refresh token back to a GitHub
Actions repository secret.

P0 fix (TODOS.md "Cron token-rotation architectural gap"): X rotates the
OAuth 2.0 refresh token on every refresh and invalidates the previous one.
The cron's EnvSecretTokenProvider.store_refresh_token is a no-op, so the
rotated token is lost and every run after the first dies AUTH_FAILED. This
module closes the loop: after X rotates the token, persist the new value
back to the `XSENSAI_X_REFRESH_TOKEN` repo secret so the NEXT run reads it.

Mechanism (eng-review decision): a fine-grained PAT scoped to `Secrets:write`
on this repo only, shelled out via the `gh` CLI. We deliberately do NOT use
the built-in GITHUB_TOKEN — it cannot write Actions secrets at any permission
level (`actions: write` covers runs/artifacts/caches, NOT secrets).

Security properties:
  - The token value is passed to `gh` via STDIN, never argv — no `ps -ef`
    leak (mirrors the auth.py F10 fix rationale).
  - The PAT is passed via the `GH_TOKEN` env var of the child process, never
    argv.
  - We do NOT use `gh secret set --body -` (it strips the trailing newline —
    TODOS.md "gh secret set --body - strips trailing newline"). `gh secret
    set <name>` with no `--body` reads the value verbatim from stdin.
  - In GitHub Actions, the rotated token is emitted as `::add-mask::` to
    stdout BEFORE spawning `gh`, so the runner masks it in logs (GitHub only
    auto-masks secrets known at job start; a value rotated mid-run is not).

Crash window (irreducible, documented limitation): X consumes the old
refresh token the moment `refresh_token()` succeeds. If the process is
killed / times out / GitHub is unreachable between that point and a
successful secret write, the rotated token is lost and the next run needs
manual re-auth. Catching write failures only covers the clean-failure path.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

from xsensai.errors import XSensaiError
from xsensai.sync.auth import redact_token_strings

log = logging.getLogger(__name__)

# Throwaway secret name used by the preflight canary to prove the PAT can
# actually write before any single-use X token is consumed.
CANARY_SECRET_NAME = "XSENSAI_PAT_CANARY"

# Subprocess timeout for any single `gh` invocation (seconds). gh hits the
# GitHub API; keep it bounded so a hung call can't eat the workflow budget.
_GH_TIMEOUT_S = 30


def _running_in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS", "").strip().lower() == "true"


def _gh_binary() -> str:
    """Resolve the `gh` CLI path or raise GH_SECRET_WRITE_FAILED."""
    gh = shutil.which("gh")
    if not gh:
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED",
            cause="GitHub CLI (`gh`) not found on PATH.",
            attempted="shutil.which('gh')",
            next_action=(
                "Install `gh` (preinstalled on ubuntu-latest runners) or run "
                "the cron on a runner that has it."
            ),
            retryable=True,
        )
    return gh


def _add_mask(value: str) -> None:
    """Register `value` for log masking in GitHub Actions.

    GitHub only auto-masks secrets known at job start; a refresh token
    rotated mid-run is not, so we emit the workflow command explicitly. The
    runner consumes the `::add-mask::` line without echoing the value. Outside
    Actions this is a harmless no-op-ish print, so we gate on the env.

    Workflow-command DATA must be escaped (`%`, CR, LF) or a value containing
    those bytes could break masking or inject a spurious command line. `%`
    must be escaped first so we don't double-escape the `%0D`/`%0A` we add.
    """
    if value and _running_in_actions():
        escaped = (
            value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        )
        # Workflow commands are read from the step's STDOUT by the runner.
        print(f"::add-mask::{escaped}", flush=True)


def _run_gh(
    args: list[str],
    *,
    pat: str,
    input_text: Optional[str] = None,
) -> subprocess.CompletedProcess:
    """Invoke `gh <args>` with GH_TOKEN=pat in env and optional stdin.

    Returns the CompletedProcess (does not raise on non-zero — callers decide).
    """
    gh = _gh_binary()
    env = {**os.environ, "GH_TOKEN": pat}
    # Defensive: ensure a stray GITHUB_TOKEN doesn't shadow GH_TOKEN for gh.
    env.pop("GITHUB_TOKEN", None)
    try:
        return subprocess.run(
            [gh, *args],
            input=input_text,
            capture_output=True,
            text=True,
            timeout=_GH_TIMEOUT_S,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # Convert to XSensaiError so it flows through the same
        # store_refresh_token -> last_persist_error -> TOKEN_PERSIST_FAILED
        # path instead of escaping as a raw exception and bypassing the gate.
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED",
            cause=f"`gh {args[0] if args else ''}` timed out after {_GH_TIMEOUT_S}s.",
            attempted=f"gh {' '.join(args)}",
            next_action=(
                "GitHub API may be slow or unreachable; the cron retries next "
                "run. If persistent, check the GitHub status page."
            ),
            retryable=True,
        )
    except OSError as e:
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED",
            cause=f"failed to spawn gh: {type(e).__name__}: {e}",
            attempted=f"gh {' '.join(args)}",
            next_action="Verify `gh` is installed and executable on the runner.",
            retryable=True,
        )


def update_repo_secret(repo: str, name: str, value: str, *, pat: str) -> None:
    """Set repo Actions secret `name` to `value` via the `gh` CLI.

    Raises XSensaiError(GH_SECRET_WRITE_FAILED) on any failure. The token
    `value` is masked (in Actions) before `gh` is spawned, passed via stdin,
    and never appears in argv. `pat` is passed via env, never argv.
    """
    if not repo:
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED",
            cause="No repository specified for secret write.",
            attempted="update_repo_secret(repo='')",
            next_action="Ensure GITHUB_REPOSITORY is set (Actions provides it automatically).",
            retryable=False,
        )
    if not value:
        # Refuse to overwrite a live secret with an empty value.
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED",
            cause=f"Refusing to write an empty value to secret {name}.",
            attempted=f"update_repo_secret({repo!r}, {name!r}, '')",
            next_action="This is a bug; the rotated token should never be empty.",
            retryable=False,
        )

    _add_mask(value)
    # `gh secret set <name>` (NO --body) reads the value verbatim from stdin.
    # --app actions pins the secret type (vs codespaces/dependabot).
    proc = _run_gh(
        ["secret", "set", name, "--repo", repo, "--app", "actions"],
        pat=pat,
        input_text=value,
    )
    if proc.returncode != 0:
        stderr = redact_token_strings(
            (proc.stderr or "").strip(), extra_secrets=(pat, value)
        )
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED",
            cause=f"`gh secret set {name}` exited {proc.returncode}.",
            attempted=f"gh secret set {name} --repo {repo} --app actions",
            next_action=(
                "Verify XSENSAI_SECRETS_PAT is a fine-grained PAT with "
                "Secrets:write on this repo and has not expired "
                "(see docs/CRON_SETUP.md#token-rotation)."
            ),
            retryable=True,
            details=f"gh stderr: {stderr}" if stderr else None,
        )
    log.info("Persisted rotated secret %s to %s via gh", name, repo)


def verify_secret_write(repo: str, *, pat: str) -> None:
    """Preflight canary: prove the PAT can write a repo secret, then clean up.

    Writes a throwaway secret (CANARY_SECRET_NAME) and deletes it. This runs
    BEFORE any single-use X token is consumed, so a broken/expired/wrong-scope
    PAT is caught while it's still cheap — instead of after we've already
    burned the X refresh token and can't persist the replacement.

    Raises XSensaiError(GH_SECRET_WRITE_FAILED) if the write fails. A failed
    cleanup is logged but not fatal (a leftover canary is harmless).
    """
    # The canary value is non-sensitive but mask it anyway for hygiene.
    update_repo_secret(repo, CANARY_SECRET_NAME, "canary-ok", pat=pat)
    # Best-effort delete; the write already proved the permission. A delete
    # failure (incl. timeout, now raised as XSensaiError by _run_gh) must NOT
    # fail preflight — leftover canary is harmless.
    try:
        proc = _run_gh(
            ["secret", "delete", CANARY_SECRET_NAME, "--repo", repo, "--app", "actions"],
            pat=pat,
        )
        if proc.returncode != 0:
            log.warning(
                "Canary secret %s written but cleanup delete failed (exit %s); "
                "harmless leftover, delete manually if desired.",
                CANARY_SECRET_NAME,
                proc.returncode,
            )
    except XSensaiError as e:
        log.warning(
            "Canary secret %s written but cleanup delete errored (%s); "
            "harmless leftover.",
            CANARY_SECRET_NAME,
            e.cause,
        )


def gh_diagnostics(*, pat: Optional[str] = None) -> str:
    """Return a redacted one-block summary of `gh --version` + `gh auth status`.

    For preflight logging (Codex #8 — `gh` is preinstalled on runners but its
    behavior is not pinned, so record what we actually ran against). Never
    raises; returns a human string.
    """
    parts: list[str] = []
    gh = shutil.which("gh")
    if not gh:
        return "gh: NOT FOUND on PATH"
    try:
        v = subprocess.run(
            [gh, "--version"], capture_output=True, text=True,
            timeout=_GH_TIMEOUT_S, check=False,
        )
        parts.append((v.stdout or "").strip().splitlines()[0] if v.stdout else "gh --version: (no output)")
    except Exception as e:  # noqa: BLE001 — diagnostics must never crash preflight
        parts.append(f"gh --version failed: {type(e).__name__}")
    if pat:
        try:
            env = {**os.environ, "GH_TOKEN": pat}
            env.pop("GITHUB_TOKEN", None)
            a = subprocess.run(
                [gh, "auth", "status"], capture_output=True, text=True,
                timeout=_GH_TIMEOUT_S, env=env, check=False,
            )
            status = (a.stdout or "") + (a.stderr or "")
            parts.append("auth: " + redact_token_strings(status.strip(), extra_secrets=(pat,)))
        except Exception as e:  # noqa: BLE001
            parts.append(f"gh auth status failed: {type(e).__name__}")
    return "\n".join(parts)


__all__ = [
    "update_repo_secret",
    "verify_secret_write",
    "gh_diagnostics",
    "CANARY_SECRET_NAME",
]
