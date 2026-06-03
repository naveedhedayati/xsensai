"""Slice 6 — guided setup wizard for x-sensai.

Mirrors `setup_oauth.py`'s `--check` / `--dry-run` pattern. Each step is
independently invokable via flags; `--all` runs them in sequence with
confirmation prompts between steps.

Steps (each idempotent):
  --preflight    validate Python, QMD, gh, ssh-keygen, git remote
  --oauth        delegate to setup_oauth (already a wizard)
  --deploy-key   ssh-keygen + register via gh; skip if title-matched key exists
  --gh-secrets   read Keychain → gh secret set; skip names already set
  --gh-vars      gh variable set VAULT_REPO + VAULT_CORPUS_SUBPATH
  --first-run    gh workflow run sync.yml + gh run watch
  --migrate      invoke scripts/migrate_v1_to_v2.py --apply --yes
  --all          all of the above, in order

State in `~/.cache/xsensai/setup-state.json` enables `--resume`.

Error codes (full XSensaiError envelopes per the contract):
  SETUP_GH_AUTH_REQUIRED       gh auth status fails
  SETUP_DEPLOY_KEY_REJECTED    gh api rejected the deploy key
  SETUP_FIRST_RUN_FAILED       workflow run reached FAILED
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from xsensai.errors import XSensaiError


def _state_path() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "xsensai" / "setup-state.json"


def _load_state() -> Dict[str, Any]:
    p = _state_path()
    if not p.exists():
        return {"version": 1, "started_at": None, "steps": {}}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"version": 1, "started_at": None, "steps": {}}


def _save_state(state: Dict[str, Any]) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _mark_step(state: Dict[str, Any], name: str, status: str, **extra: Any) -> None:
    state.setdefault("steps", {})[name] = {
        "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    _save_state(state)


def _step_done(state: Dict[str, Any], name: str) -> bool:
    return state.get("steps", {}).get(name, {}).get("status") in {"completed", "skipped"}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def cmd_preflight(state: Dict[str, Any]) -> int:
    print("[preflight] checking prerequisites...")
    issues: List[str] = []
    # Python ≥ 3.11
    if sys.version_info < (3, 11):
        issues.append(f"Python 3.11+ required (have {sys.version.split()[0]})")
    # gh
    if not shutil.which("gh"):
        issues.append("gh CLI not found (install via `brew install gh`)")
    # ssh-keygen
    if not shutil.which("ssh-keygen"):
        issues.append("ssh-keygen not found (install OpenSSH)")
    # qmd (best-effort: env var or default path)
    qmd_path = os.environ.get(
        "XSENSAI_QMD_PATH", "/Users/naveedhedayati/.bun/bin/qmd"
    )
    if not shutil.which("qmd") and not Path(qmd_path).exists():
        issues.append(
            f"qmd not found at {qmd_path} (run scripts/bootstrap_qmd.sh)"
        )
    # git remote (origin) for the xsensai repo
    try:
        repo_root = Path(__file__).resolve().parents[3]
        r = subprocess.run(
            ["git", "-C", str(repo_root), "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0:
            issues.append("git remote 'origin' not configured")
    except Exception as e:
        issues.append(f"git probe failed: {e}")
    if issues:
        print("[preflight] FAIL")
        for i in issues:
            print(f"  - {i}")
        _mark_step(state, "preflight", "failed", issues=issues)
        return 1
    print("[preflight] OK")
    _mark_step(state, "preflight", "completed")
    return 0


# ---------------------------------------------------------------------------
# gh auth probe
# ---------------------------------------------------------------------------


def _ensure_gh_auth() -> Optional[XSensaiError]:
    if not shutil.which("gh"):
        return XSensaiError(
            code="SETUP_GH_AUTH_REQUIRED",
            cause="gh CLI is not installed",
            attempted="setup wizard step (--gh-secrets / --gh-vars / --first-run)",
            next_action=(
                "Install gh via `brew install gh`, then run "
                "`gh auth login` and re-run `./scripts/setup.sh --resume`"
            ),
            retryable=True,
        )
    r = subprocess.run(
        ["gh", "auth", "status"], capture_output=True, text=True, timeout=10
    )
    if r.returncode != 0:
        return XSensaiError(
            code="SETUP_GH_AUTH_REQUIRED",
            cause="gh CLI is not authenticated to GitHub",
            attempted="setup wizard step (--gh-secrets / --gh-vars / --first-run)",
            next_action=(
                "Run `gh auth login` then re-run `./scripts/setup.sh --resume`"
            ),
            retryable=True,
        )
    return None


# ---------------------------------------------------------------------------
# OAuth (delegates to setup_oauth)
# ---------------------------------------------------------------------------


def cmd_oauth(state: Dict[str, Any]) -> int:
    if _step_done(state, "oauth"):
        print("[oauth] already configured (skip)")
        return 0
    print("[oauth] launching setup_oauth flow...")
    rc = subprocess.run(
        [sys.executable, "-m", "xsensai.sync.setup_oauth"],
    ).returncode
    if rc == 0:
        _mark_step(state, "oauth", "completed")
    else:
        _mark_step(state, "oauth", "failed", returncode=rc)
    return rc


# ---------------------------------------------------------------------------
# Deploy key (idempotent: skip if title-matched key exists)
# ---------------------------------------------------------------------------


DEPLOY_KEY_TITLE = "xsensai-cron-deploy"
DEPLOY_KEY_PATH = Path.home() / ".ssh" / "xsensai_deploy_key"


def cmd_deploy_key(state: Dict[str, Any], vault_repo: Optional[str]) -> int:
    err = _ensure_gh_auth()
    if err:
        print(err.format(), file=sys.stderr)
        _mark_step(state, "deploy-key", "failed")
        return 1
    if not vault_repo:
        vault_repo = state.get("vault_repo") or input(
            "Vault repo slug (e.g., yourname/obsidian-vault): "
        ).strip()
        if not vault_repo:
            print("[deploy-key] empty repo slug; aborting", file=sys.stderr)
            return 1
        state["vault_repo"] = vault_repo

    # Idempotency: list existing keys, skip if one with our title AND
    # matching public-key bytes is present. Title-only match is unsafe —
    # if GitHub holds a stale key with the same title but different bytes
    # (e.g., user regenerated locally and didn't update GitHub), the wizard
    # would skip silently and the cron would auth-fail at first run.
    local_pub_key: Optional[str] = None
    pub_path = DEPLOY_KEY_PATH.with_suffix(".pub")
    if pub_path.exists():
        try:
            local_pub_key = pub_path.read_text().strip()
        except OSError:
            local_pub_key = None
    r = subprocess.run(
        ["gh", "api", f"repos/{vault_repo}/keys"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        try:
            keys = json.loads(r.stdout)
            for k in keys:
                if k.get("title") != DEPLOY_KEY_TITLE:
                    continue
                # GitHub stores keys without the trailing comment field.
                # Compare just the type + base64 portion (first two whitespace-
                # separated tokens of the local pubkey).
                remote_key_str = (k.get("key") or "").strip()
                local_key_match = ""
                if local_pub_key:
                    parts = local_pub_key.split()
                    if len(parts) >= 2:
                        local_key_match = f"{parts[0]} {parts[1]}"
                if local_pub_key and remote_key_str:
                    remote_parts = remote_key_str.split()
                    remote_key_match = " ".join(remote_parts[:2]) if len(remote_parts) >= 2 else remote_key_str
                    if local_key_match == remote_key_match:
                        print(f"[deploy-key] '{DEPLOY_KEY_TITLE}' already exists with matching key (skip)")
                        _mark_step(state, "deploy-key", "completed",
                                   already_configured=True, key_id=k.get("id"))
                        return 0
                # Title matches but key differs (or local missing). Refuse
                # to skip silently — surfaces the mismatch at setup time
                # rather than as a cron auth failure later.
                err = XSensaiError(
                    code="SETUP_DEPLOY_KEY_REJECTED",
                    cause=(
                        f"GitHub holds a deploy key with title {DEPLOY_KEY_TITLE!r} "
                        "but its public-key bytes differ from your local key "
                        f"({DEPLOY_KEY_PATH}.pub). Skipping without verification "
                        "would cause the cron to auth-fail at first run."
                    ),
                    attempted=f"gh api repos/{vault_repo}/keys (idempotency check)",
                    next_action=(
                        f"Either delete the stale GitHub key via "
                        f"`gh api -X DELETE repos/{vault_repo}/keys/{k.get('id')}` "
                        "and re-run `./scripts/setup.sh --resume`, or remove "
                        f"{DEPLOY_KEY_PATH} and {DEPLOY_KEY_PATH}.pub locally if "
                        "you want to re-create from the GitHub-side key."
                    ),
                    retryable=True,
                )
                print(err.format(), file=sys.stderr)
                _mark_step(state, "deploy-key", "failed", reason="title-key-mismatch")
                return 1
        except json.JSONDecodeError:
            pass

    # Generate the key if not present
    if not DEPLOY_KEY_PATH.exists():
        DEPLOY_KEY_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        kg = subprocess.run([
            "ssh-keygen", "-t", "ed25519", "-C", DEPLOY_KEY_TITLE,
            "-f", str(DEPLOY_KEY_PATH), "-N", "",
        ])
        if kg.returncode != 0:
            print("[deploy-key] ssh-keygen failed", file=sys.stderr)
            _mark_step(state, "deploy-key", "failed")
            return 1

    pub_key = DEPLOY_KEY_PATH.with_suffix(".pub").read_text().strip()
    add = subprocess.run([
        "gh", "api", "-X", "POST", f"repos/{vault_repo}/keys",
        "--field", f"title={DEPLOY_KEY_TITLE}",
        "--field", f"key={pub_key}",
        "--field", "read_only=false",
    ], capture_output=True, text=True)
    if add.returncode != 0:
        err = XSensaiError(
            code="SETUP_DEPLOY_KEY_REJECTED",
            cause=f"GitHub rejected the deploy key (gh exit {add.returncode})",
            attempted=f"gh api -X POST repos/{vault_repo}/keys",
            next_action=(
                f"ensure gh user has admin permission on {vault_repo}; if a "
                f"key with title {DEPLOY_KEY_TITLE!r} already exists, delete it via "
                "`gh api -X DELETE repos/{vault}/keys/{id}` and re-run "
                "`./scripts/setup.sh --resume`"
            ),
            retryable=True,
            details=add.stderr.strip()[:500],
        )
        print(err.format(), file=sys.stderr)
        _mark_step(state, "deploy-key", "failed", stderr=add.stderr[:200])
        return 1
    print(f"[deploy-key] registered with {vault_repo}")
    _mark_step(state, "deploy-key", "completed", vault_repo=vault_repo)
    return 0


# ---------------------------------------------------------------------------
# GH secrets (delegates to existing --emit-secrets-stdin helper)
# ---------------------------------------------------------------------------


def cmd_gh_secrets(state: Dict[str, Any]) -> int:
    err = _ensure_gh_auth()
    if err:
        print(err.format(), file=sys.stderr)
        _mark_step(state, "gh-secrets", "failed")
        return 1
    print("[gh-secrets] reading from Keychain → piping to gh secret set...")
    cmd = [
        sys.executable, "-m", "xsensai.entrypoints.headless",
        "--emit-secrets-stdin",
    ]
    rc = subprocess.run(cmd).returncode
    if rc == 0:
        _mark_step(state, "gh-secrets", "completed")
        print("[gh-secrets] done — copy/paste the printed `gh secret set` lines into your shell")
    else:
        _mark_step(state, "gh-secrets", "failed", returncode=rc)
    return rc


# ---------------------------------------------------------------------------
# GH vars
# ---------------------------------------------------------------------------


def cmd_gh_vars(state: Dict[str, Any], vault_repo: Optional[str]) -> int:
    err = _ensure_gh_auth()
    if err:
        print(err.format(), file=sys.stderr)
        _mark_step(state, "gh-vars", "failed")
        return 1
    if not vault_repo:
        vault_repo = state.get("vault_repo")
    if not vault_repo:
        vault_repo = input("Vault repo slug: ").strip()
    subpath = state.get("vault_corpus_subpath") or os.environ.get(
        "VAULT_CORPUS_SUBPATH", "04_areas/x-bookmarks"
    )
    for name, value in [("VAULT_REPO", vault_repo), ("VAULT_CORPUS_SUBPATH", subpath)]:
        r = subprocess.run(
            ["gh", "variable", "set", name, "--body", value],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            print(f"[gh-vars] failed to set {name}: {r.stderr}", file=sys.stderr)
            _mark_step(state, "gh-vars", "failed", stderr=r.stderr[:200])
            return 1
    state["vault_repo"] = vault_repo
    state["vault_corpus_subpath"] = subpath
    _mark_step(state, "gh-vars", "completed",
               VAULT_REPO=vault_repo, VAULT_CORPUS_SUBPATH=subpath)
    print(f"[gh-vars] set VAULT_REPO={vault_repo}, VAULT_CORPUS_SUBPATH={subpath}")
    return 0


# ---------------------------------------------------------------------------
# First run
# ---------------------------------------------------------------------------


def cmd_first_run(state: Dict[str, Any]) -> int:
    err = _ensure_gh_auth()
    if err:
        print(err.format(), file=sys.stderr)
        _mark_step(state, "first-run", "failed")
        return 1
    print("[first-run] triggering sync.yml workflow...")
    r = subprocess.run(
        ["gh", "workflow", "run", "sync.yml", "--ref", "main"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        err = XSensaiError(
            code="SETUP_FIRST_RUN_FAILED",
            cause=f"workflow dispatch failed (gh exit {r.returncode})",
            attempted="gh workflow run sync.yml --ref main",
            next_action=(
                "verify VAULT_REPO + VAULT_CORPUS_SUBPATH GitHub Actions "
                "variables and the deploy-key were set; re-run "
                "`./scripts/setup.sh --resume`"
            ),
            retryable=True,
            details=r.stderr.strip()[:300],
        )
        print(err.format(), file=sys.stderr)
        _mark_step(state, "first-run", "failed", stderr=r.stderr[:200])
        return 1
    # Watch for completion
    print("[first-run] watching most recent run...")
    rc = subprocess.run(["gh", "run", "watch"]).returncode
    if rc != 0:
        err = XSensaiError(
            code="SETUP_FIRST_RUN_FAILED",
            cause=f"workflow run reached non-zero exit ({rc})",
            attempted="gh workflow run sync.yml + gh run watch",
            next_action=(
                "inspect logs via `gh run view <id> --log`; common cause "
                "is missing/wrong secret value — re-run "
                "`./scripts/setup.sh --gh-secrets` and verify"
            ),
            retryable=True,
        )
        print(err.format(), file=sys.stderr)
        _mark_step(state, "first-run", "failed", returncode=rc)
        return 1
    _mark_step(state, "first-run", "completed")
    return 0


# ---------------------------------------------------------------------------
# Migrate (delegates to scripts/migrate_v1_to_v2.py)
# ---------------------------------------------------------------------------


def cmd_migrate(state: Dict[str, Any], dry_run: bool = False) -> int:
    repo_root = Path(__file__).resolve().parents[3]
    script = repo_root / "scripts" / "migrate_v1_to_v2.py"
    if not script.exists():
        print(f"[migrate] script not found at {script}", file=sys.stderr)
        return 1
    args = [sys.executable, str(script)]
    args.append("--dry-run" if dry_run else "--apply")
    if not dry_run:
        args.append("--yes")
    rc = subprocess.run(args).returncode
    _mark_step(state, "migrate", "completed" if rc == 0 else "failed",
               dry_run=dry_run)
    return rc


# ---------------------------------------------------------------------------
# All
# ---------------------------------------------------------------------------


def cmd_all(state: Dict[str, Any], vault_repo: Optional[str]) -> int:
    state["started_at"] = state.get("started_at") or datetime.now(timezone.utc).isoformat()
    _save_state(state)
    print("== xsensai setup wizard — running --all ==")
    steps = [
        ("preflight", lambda: cmd_preflight(state)),
        ("oauth", lambda: cmd_oauth(state)),
        ("deploy-key", lambda: cmd_deploy_key(state, vault_repo)),
        ("gh-secrets", lambda: cmd_gh_secrets(state)),
        ("gh-vars", lambda: cmd_gh_vars(state, vault_repo)),
        ("first-run", lambda: cmd_first_run(state)),
        ("migrate", lambda: cmd_migrate(state, dry_run=False)),
    ]
    for name, fn in steps:
        if _step_done(state, name):
            print(f"[{name}] already done — skip (use --resume to re-run failed steps)")
            continue
        print(f"\n--- {name} ---")
        rc = fn()
        if rc != 0:
            print(f"\n[wizard] step {name!r} failed with rc={rc}.")
            print(
                "Re-run `./scripts/setup.sh --resume` after fixing the issue. "
                "Completed steps will be skipped."
            )
            return rc
    print("\n[wizard] all steps complete.")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="setup_wizard",
        description="Slice 6 guided setup wizard for x-sensai.",
    )
    # required=False so a bare `./scripts/setup.sh` (no flag) does NOT crash
    # with an argparse error — it falls through to the full guided flow.
    g = p.add_mutually_exclusive_group(required=False)
    g.add_argument("--preflight", action="store_true")
    g.add_argument("--oauth", action="store_true")
    g.add_argument("--deploy-key", action="store_true")
    g.add_argument("--gh-secrets", action="store_true")
    g.add_argument("--gh-vars", action="store_true")
    g.add_argument("--first-run", action="store_true")
    g.add_argument("--migrate", action="store_true")
    g.add_argument("--all", action="store_true")
    g.add_argument("--resume", action="store_true",
                   help="Synonym for --all that highlights skip-completed semantics.")
    p.add_argument("--vault-repo", default=None,
                   help="Vault repo slug (e.g., yourname/obsidian-vault).")
    p.add_argument("--migrate-dry-run", action="store_true",
                   help="Used with --migrate; preview without applying.")
    return p


def main() -> int:
    # Step 0: macOS-only guard. Fail loud up front (not mid-install) on a
    # non-Darwin platform — x-sensai relies on the macOS Keychain and
    # F_FULLFSYNC for crash-safe sidecar writes.
    if sys.platform != "darwin":
        err = XSensaiError(
            code="UNSUPPORTED_PLATFORM",
            cause=f"x-sensai is macOS-only; detected platform {sys.platform!r}",
            attempted="setup_wizard step 0 (platform check)",
            next_action=(
                "Run x-sensai on macOS. It uses the macOS Keychain for secrets "
                "and F_FULLFSYNC for crash-safe writes; Linux/Windows are not supported."
            ),
            retryable=False,
        )
        print(err.format(), file=sys.stderr)
        return 2
    args = _build_parser().parse_args()
    state = _load_state()
    if args.preflight:
        return cmd_preflight(state)
    if args.oauth:
        return cmd_oauth(state)
    if args.deploy_key:
        return cmd_deploy_key(state, args.vault_repo)
    if args.gh_secrets:
        return cmd_gh_secrets(state)
    if args.gh_vars:
        return cmd_gh_vars(state, args.vault_repo)
    if args.first_run:
        return cmd_first_run(state)
    if args.migrate:
        return cmd_migrate(state, dry_run=args.migrate_dry_run)
    if args.all or args.resume:
        return cmd_all(state, args.vault_repo)
    # No subcommand flag → default to the full guided flow rather than crashing.
    # (The core-install steps — corpus/QMD/MCP — land in PR-2; today --all runs
    # the cron/setup steps, which is the safe default for a bare invocation.)
    return cmd_all(state, args.vault_repo)


if __name__ == "__main__":
    sys.exit(main())
