"""Git cleanliness check + optional --commit (per UC-3=C + S-10 fix).

Per /autoplan UC-3=C answered C: check git status before /xsync; warn if
the vault has uncommitted xsync output from a prior run; opt-in `commit`
keyword (or XSENSAI_VAULT_DIRTY_PROCEED=1 env) to auto-commit after.

Per S-10 fix: collapsed from interactive (y/n) to non-blocking
[INFO/VAULT_DIRTY_FIRST_RUN] envelope + `proceed dirty` opt-in. Preserves
the one-prompt /xsync contract.

Per E-5: subprocess always uses argv list + '--' separator; commit paths
validated via _assert_inside_corpus.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from xsensai.errors import XSensaiError, XSensaiInfo
from xsensai.storage.corpus import _assert_inside_corpus, resolve_corpus_path


log = logging.getLogger(__name__)


VAULT_DIRTY_PROCEED_ENV = "XSENSAI_VAULT_DIRTY_PROCEED"


@dataclass(frozen=True)
class GitState:
    """Result of `git status --porcelain` against the vault."""

    is_git_repo: bool
    has_dirty_xsync_output: bool
    dirty_paths: List[Path]
    git_locked: bool = False  # .git/index.lock exists


def check_vault_state(corpus_path: Optional[Path] = None) -> GitState:
    """Inspect the vault repo for cleanliness BEFORE /xsync writes any cards.

    Returns:
      - is_git_repo=False: vault is not a git repo (e.g., user uses Obsidian Sync).
        Caller should silently skip the cleanliness check.
      - is_git_repo=True, has_dirty_xsync_output=True: there are uncommitted
        cards from a prior /xsync run. Caller decides (warn vs proceed) based
        on the user's `proceed dirty` keyword OR XSENSAI_VAULT_DIRTY_PROCEED env.
      - is_git_repo=True, has_dirty_xsync_output=False: clean. Proceed.
      - git_locked=True: `.git/index.lock` exists. Caller surfaces [INFO/GIT_LOCKED].
    """
    corpus = resolve_corpus_path(corpus_path)
    git_dir = _find_git_root(corpus)
    if git_dir is None:
        return GitState(is_git_repo=False, has_dirty_xsync_output=False, dirty_paths=[])

    if (git_dir / ".git" / "index.lock").exists():
        return GitState(
            is_git_repo=True, has_dirty_xsync_output=False, dirty_paths=[], git_locked=True,
        )

    # Run `git status --porcelain` scoped to the corpus subdirectory.
    try:
        result = subprocess.run(
            ["git", "-C", str(git_dir), "status", "--porcelain", "--", str(corpus)],
            capture_output=True,
            text=True,
            check=False,
            timeout=10.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("git status check failed: %s — proceeding without dirty check", e)
        return GitState(is_git_repo=True, has_dirty_xsync_output=False, dirty_paths=[])

    if result.returncode != 0:
        log.warning(
            "git status returned %d (stderr=%r) — proceeding without dirty check",
            result.returncode, (result.stderr or "")[:200],
        )
        return GitState(is_git_repo=True, has_dirty_xsync_output=False, dirty_paths=[])

    dirty_paths: List[Path] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Porcelain format: XY <path>
        # Filter to .md / .raw.txt / _sync-* files (xsync's output surface).
        rel_path = line[3:].strip().split(" -> ", 1)[-1]
        if not (
            rel_path.endswith(".md")
            or rel_path.endswith(".raw.txt")
            or rel_path.split("/")[-1].startswith("_sync-")
        ):
            continue
        dirty_paths.append(git_dir / rel_path)

    return GitState(
        is_git_repo=True,
        has_dirty_xsync_output=bool(dirty_paths),
        dirty_paths=dirty_paths,
    )


def should_proceed_dirty(*, user_keyword: bool) -> bool:
    """Two ways to opt in: user typed `proceed dirty` OR env is set."""
    if user_keyword:
        return True
    return os.environ.get(VAULT_DIRTY_PROCEED_ENV, "").strip().lower() in {"1", "true", "yes"}


def commit_xsync_output(
    written_md_paths: List[Path],
    *,
    corpus_path: Optional[Path] = None,
    n_new_cards: int,
    extraction_pending_count: int,
) -> Optional[str]:
    """Run `git add` + `git commit` for the new cards.

    Returns the commit sha on success, None on failure (logs the failure but
    doesn't raise — git plumbing is best-effort, the cards are already on disk).

    Per E-5: argv list, '--' separator, all paths validated via
    _assert_inside_corpus before passing to subprocess.
    """
    if not written_md_paths:
        return None

    corpus = resolve_corpus_path(corpus_path)
    git_dir = _find_git_root(corpus)
    if git_dir is None:
        log.info("Vault is not a git repo; skipping --commit")
        return None

    # Defense-in-depth: validate every path is inside the corpus (E-5).
    safe_paths: List[Path] = []
    for md_path in written_md_paths:
        try:
            safe_paths.append(_assert_inside_corpus(md_path, corpus))
            # Also include the matching sidecar if present
            sidecar_glob = md_path.parent.glob(f"{md_path.stem}.*.raw.txt")
            for sc in sidecar_glob:
                safe_paths.append(_assert_inside_corpus(sc, corpus))
        except Exception as e:
            log.warning("Skipping path failed corpus validation: %s (%s)", md_path, e)

    if not safe_paths:
        log.warning("No safe paths to commit after validation")
        return None

    # git add -- <paths>
    add_cmd = ["git", "-C", str(git_dir), "add", "--"] + [str(p) for p in safe_paths]
    try:
        result = subprocess.run(
            add_cmd, capture_output=True, text=True, check=False, timeout=30.0,
        )
        if result.returncode != 0:
            log.warning("git add failed: %s", (result.stderr or "")[:300])
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("git add subprocess failed: %s", e)
        return None

    # git commit -m "xsync: N new cards (M extraction-pending)"
    msg = f"xsync: {n_new_cards} new cards"
    if extraction_pending_count > 0:
        msg += f" ({extraction_pending_count} extraction-pending)"
    commit_cmd = ["git", "-C", str(git_dir), "commit", "-m", msg]
    try:
        result = subprocess.run(
            commit_cmd, capture_output=True, text=True, check=False, timeout=30.0,
        )
        if result.returncode != 0:
            log.warning("git commit failed: %s", (result.stderr or "")[:300])
            return None
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        log.warning("git commit subprocess failed: %s", e)
        return None

    # Get the resulting commit sha
    try:
        sha_result = subprocess.run(
            ["git", "-C", str(git_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, check=False, timeout=5.0,
        )
        if sha_result.returncode == 0:
            return sha_result.stdout.strip()
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return "unknown"


def vault_dirty_envelope(state: GitState) -> XSensaiInfo:
    """Render the [INFO/VAULT_DIRTY_FIRST_RUN] envelope per S-10 + D-3."""
    n = len(state.dirty_paths)
    sample = "\n".join(f"  - {p.name}" for p in state.dirty_paths[:5])
    if n > 5:
        sample += f"\n  ... and {n - 5} more"
    action = (
        f"Found {n} uncommitted file(s) from a prior /xsync run inside the vault:\n"
        f"{sample}\n"
        "Either commit them (`cd <vault> && git add -A && git commit -m 'manual: prior xsync output'`) "
        "OR re-run with the `proceed dirty` keyword to sync anyway "
        f"(or set {VAULT_DIRTY_PROCEED_ENV}=1 to opt in permanently)."
    )
    return XSensaiInfo(
        code="VAULT_DIRTY_FIRST_RUN",
        cause=f"Vault has {n} uncommitted xsync output file(s); not safe to stack another sync on top.",
        action_or_note=action,
        source="sync.git_check.check_vault_state()",
    )


def vault_not_git_envelope() -> XSensaiInfo:
    """Render the [INFO/VAULT_NOT_GIT] envelope (one-shot per session)."""
    return XSensaiInfo(
        code="VAULT_NOT_GIT",
        cause="Vault directory is not a git repo — skipping cleanliness check.",
        action_or_note=(
            "If you sync the vault via Obsidian Sync or another non-git mechanism, "
            "this is fine. If you use git, initialize the vault repo: "
            "`cd <vault> && git init && git add . && git commit -m 'initial'`."
        ),
        source="sync.git_check.check_vault_state()",
    )


def git_locked_envelope() -> XSensaiInfo:
    """Render the [INFO/GIT_LOCKED] envelope — vault has .git/index.lock."""
    return XSensaiInfo(
        code="GIT_LOCKED",
        cause="Vault repo has .git/index.lock — another git operation may be in progress.",
        action_or_note=(
            "Wait a few seconds and re-run /xsync. If the lock persists, manually inspect "
            "the vault's .git directory and remove a stale lock file."
        ),
        source="sync.git_check.check_vault_state()",
    )


def _find_git_root(start: Path) -> Optional[Path]:
    """Walk upward from `start` looking for a .git directory."""
    cur = start.resolve()
    for _ in range(10):  # bounded walk
        if (cur / ".git").is_dir():
            return cur
        if cur.parent == cur:
            return None
        cur = cur.parent
    return None


__all__ = [
    "GitState",
    "check_vault_state",
    "should_proceed_dirty",
    "commit_xsync_output",
    "vault_dirty_envelope",
    "vault_not_git_envelope",
    "git_locked_envelope",
    "VAULT_DIRTY_PROCEED_ENV",
]


def _cli() -> int:
    """CLI entry for the slash command — emits JSON for /xsync to parse."""
    import argparse
    import json as _json

    parser = argparse.ArgumentParser(prog="python -m xsensai.sync.git_check")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_check = sub.add_parser("check", help="Inspect vault git state")
    p_check.set_defaults(fn="check")

    p_commit = sub.add_parser("commit", help="git add + git commit the new cards")
    p_commit.add_argument("--new-cards", type=int, default=0)
    p_commit.add_argument("--pending-count", type=int, default=0)
    p_commit.set_defaults(fn="commit")

    args = parser.parse_args()
    if args.fn == "check":
        state = check_vault_state()
        print(_json.dumps({
            "is_git_repo": state.is_git_repo,
            "has_dirty_xsync_output": state.has_dirty_xsync_output,
            "git_locked": state.git_locked,
            "dirty_paths": [str(p) for p in state.dirty_paths],
            "should_proceed_dirty": should_proceed_dirty(user_keyword=False),
        }))
        return 0
    if args.fn == "commit":
        # The slash command doesn't pass per-card paths — `git add -A` over
        # the corpus dir is too wide. We re-detect dirty xsync output and
        # commit those.
        state = check_vault_state()
        if not state.is_git_repo:
            print(_json.dumps({"committed": False, "reason": "vault is not a git repo"}))
            return 0
        if not state.dirty_paths:
            print(_json.dumps({"committed": False, "reason": "no dirty xsync output to commit"}))
            return 0
        sha = commit_xsync_output(
            state.dirty_paths,
            n_new_cards=args.new_cards,
            extraction_pending_count=args.pending_count,
        )
        print(_json.dumps({"committed": sha is not None, "sha": sha}))
        return 0
    return 2


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
