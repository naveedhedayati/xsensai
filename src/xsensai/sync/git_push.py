"""Slice 5 commit + push automation for cron sync.

Sequence (per autoplan E2 + spike #8):
  1. Stage allowlist of intentional paths (NEVER `git add -A`).
     Allow: *.md, *.raw.txt, _sync-status.md, _conflicts/<run_id>/*,
            _conflicts.md.
     Exclude: *.rej, *.local, *.remote outside _conflicts/.
  2. Commit with a static message; skip if no staged changes.
  3. Pull-rebase with retry up to max_retries:
     - on heartbeat conflict: heartbeat fast-path resolver + continue
     - on card conflict: fail-loud (capture both, abort, reset, sidecars)
     - on push reject: retry pull-rebase
  4. After fail-loud, return PushResult(unresolved=True, exit code 2).
  5. After max_retries push rejects, write static SYNC_PUSH_REJECTED.md
     flag, commit + push the flag, return PushResult(success=False).

All paths through _assert_inside_corpus per E-5 + autoplan E6.
Subprocess always argv-list + `--` separator; porcelain v2 + NUL.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from xsensai.errors import XSensaiError
from xsensai.storage.corpus import _assert_inside_corpus
from xsensai.sync import git_merge
from xsensai.sync.auth import redact_token_strings
from xsensai.sync.heartbeat import STATUS_FILE_NAME, SyncStatus

log = logging.getLogger(__name__)


# Paths that should ALWAYS be staged (relative to corpus root, glob-ish).
ALLOW_TOP_LEVEL = {STATUS_FILE_NAME, git_merge.CONFLICTS_LOG}
ALLOW_DIRS = {git_merge.CONFLICTS_DIR}
ALLOW_SUFFIXES = {".md", ".raw.txt"}

# Paths that should NEVER be staged outside of _conflicts/.
EXCLUDE_SUFFIXES = {".rej", ".local", ".remote"}

# Push reject retry policy (autoplan E2).
DEFAULT_MAX_RETRIES = 3

# Flag file written when push fails after retries (static template only —
# autoplan E7 / DX D6 / no env-var interpolation, no exception text).
PUSH_REJECTED_FLAG = "SYNC_PUSH_REJECTED.md"


@dataclass
class PushResult:
    success: bool
    cards_committed: int = 0
    flag_written: Optional[Path] = None
    conflict_unresolved: bool = False
    error: Optional[XSensaiError] = None
    notes: List[str] = field(default_factory=list)


def _git(corpus_path: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(corpus_path), *args],
        capture_output=capture,
        text=False,
        check=False,
        timeout=30.0,
    )


def _stderr_str(result: subprocess.CompletedProcess, limit: int = 200) -> str:
    """Decode stderr + redact token shapes (autoplan E7 defense in depth).

    Use everywhere subprocess stderr lands in a user-visible envelope or
    log line. Committed flag files use static templates instead — see
    `_push_rejected_recovery_text` and `_auth_failed_recovery_text`.
    """
    raw = (result.stderr or b"").decode("utf-8", errors="replace")[:limit]
    return redact_token_strings(raw)


def _porcelain_v2_modified_paths(corpus_path: Path) -> List[str]:
    """Return git-relative paths of all modified/added files via porcelain v2."""
    res = _git(corpus_path, "status", "--porcelain=v2", "-z")
    if res.returncode != 0:
        return []
    paths: List[str] = []
    for record in res.stdout.split(b"\x00"):
        if not record:
            continue
        try:
            text = record.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            continue
        # `1 XY <sub> <mH> <mI> <mW> <hH> <hI> <path>` — 9 fields
        # `2 XY <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path><tab><origPath>`
        # `? <path>` — untracked
        if text.startswith("? "):
            paths.append(text[2:])
        elif text.startswith(("1 ", "2 ")):
            parts = text.split(" ", 8)
            if len(parts) >= 9:
                last = parts[8]
                # type-2 has tab separator with origPath; take first
                paths.append(last.split("\t", 1)[0])
    return paths


def _is_allowed(rel: str) -> bool:
    """Path allowlist policy (autoplan E2 + E8).

    Allow if ANY of:
      - top-level allowlisted file (`_sync-status.md`, `_conflicts.md`)
      - inside an allowlisted dir (`_conflicts/...`)
      - has an allowlisted suffix (`.md`, `.raw.txt`) AND not excluded suffix
    Excluded suffixes (`.rej`, `.local`, `.remote`) are blocked even when
    they live under an allowlisted dir EXCEPT inside `_conflicts/` (where
    `.local`/`.remote` are intentional sidecars).
    """
    p = Path(rel)
    parts = p.parts
    suffixes = "".join(p.suffixes)  # e.g., ".raw.txt"

    # Excluded suffixes outside _conflicts/ are always rejected.
    if any(rel.endswith(s) for s in EXCLUDE_SUFFIXES):
        if not (parts and parts[0] in ALLOW_DIRS):
            return False
        # Inside _conflicts/, .local/.remote are allowed; .rej still no.
        if rel.endswith(".rej"):
            return False
        return True

    # Top-level allowed names.
    if rel in ALLOW_TOP_LEVEL:
        return True

    # Inside allowed dirs.
    if parts and parts[0] in ALLOW_DIRS:
        return True

    # Suffix allowlist (.md or .raw.txt).
    if rel.endswith(".raw.txt"):
        return True
    if rel.endswith(".md"):
        return True
    if suffixes in ALLOW_SUFFIXES:
        return True
    return False


def _allowlist_changed_paths(corpus_path: Path) -> List[str]:
    return [rel for rel in _porcelain_v2_modified_paths(corpus_path) if _is_allowed(rel)]


def _has_staged_changes(corpus_path: Path) -> bool:
    res = _git(corpus_path, "diff", "--cached", "--quiet")
    # Returns 1 if there are staged changes, 0 if not.
    return res.returncode == 1


def _push_rejected_recovery_text(run_id: str, attempt_count: int) -> str:
    """Static template for SYNC_PUSH_REJECTED.md (autoplan E7).

    NEVER interpolates secrets, exception text, refresh-token snippets.
    Test test_no_secrets_in_flags asserts this.
    """
    return (
        "# x-sensai: scheduled sync couldn't push to vault\n\n"
        f"After {attempt_count} pull-rebase + push attempts, the cron run\n"
        f"`{run_id}` could not fast-forward the vault repo. The cards from\n"
        "this run are NOT lost — they live on cron's GH Actions runner copy\n"
        "until the next successful run rebases past them.\n\n"
        "## Recover\n\n"
        "On your Mac:\n\n"
        "```bash\n"
        "cd <vault>\n"
        "git pull --rebase origin main\n"
        "# resolve any conflicts in your editor\n"
        "git push origin main\n"
        "# re-trigger the cron run from xsensai's GitHub Actions UI\n"
        "```\n\n"
        "After resolution, delete this file and commit:\n\n"
        "```bash\n"
        "git rm SYNC_PUSH_REJECTED.md && git commit -m 'cron: push recovered'\n"
        "git push origin main\n"
        "```\n\n"
        "See `docs/CRON_SETUP.md` for the full recovery runbook.\n"
    )


def commit_and_push(
    corpus_path: Path,
    *,
    message: str,
    in_memory_status: SyncStatus,
    run_id: str,
    remote_ref: str = "origin/main",
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> PushResult:
    """Commit allowlisted changes, pull-rebase with conflict handling, push.

    `in_memory_status` is the SyncStatus the run wrote to disk this turn —
    used by the heartbeat fast-path resolver if rebase conflicts on it.
    """
    # 1. Stage allowlist.
    allowed = _allowlist_changed_paths(corpus_path)
    if not allowed:
        # Nothing to commit; still report success so caller can decide
        # whether to push (likely no — nothing changed).
        return PushResult(success=True, cards_committed=0, notes=["no changes"])

    # Defense-in-depth: validate every staged path is inside corpus.
    safe_paths: List[str] = []
    for rel in allowed:
        try:
            _assert_inside_corpus((corpus_path / rel).resolve(), corpus_path)
            safe_paths.append(rel)
        except Exception as e:
            log.warning("path %r failed corpus validation: %s", rel, e)

    if not safe_paths:
        return PushResult(success=True, cards_committed=0, notes=["no safe paths"])

    add_cmd = ["add", "--", *safe_paths]
    add = _git(corpus_path, *add_cmd)
    if add.returncode != 0:
        return PushResult(
            success=False,
            error=XSensaiError(
                code="DISK_WRITE_FAILED",
                cause="git add failed during commit_and_push",
                attempted=f"git add -- ({len(safe_paths)} paths)",
                next_action="Inspect vault repo state manually.",
                retryable=True,
                details=_stderr_str(add),
            ),
        )

    # 2. Commit (skip if nothing staged).
    if not _has_staged_changes(corpus_path):
        return PushResult(success=True, cards_committed=0, notes=["no staged changes"])

    commit = _git(corpus_path, "commit", "-m", message)
    if commit.returncode != 0:
        return PushResult(
            success=False,
            error=XSensaiError(
                code="DISK_WRITE_FAILED",
                cause="git commit failed during commit_and_push",
                attempted="git commit",
                next_action="Inspect vault repo manually.",
                retryable=True,
                details=_stderr_str(commit),
            ),
        )
    cards_committed = len(safe_paths)

    # 3. Pull-rebase + push loop.
    for attempt in range(max_retries):
        # Fetch first.
        fetch = _git(corpus_path, "fetch", "origin")
        if fetch.returncode != 0:
            log.warning("git fetch failed (attempt %d): %s", attempt + 1,
                        (fetch.stderr or b"")[:120])
            continue

        # Rebase (with non-interactive editor).
        rebase = subprocess.run(
            ["git", "-C", str(corpus_path), "-c", "core.editor=true",
             "rebase", remote_ref],
            capture_output=True, text=False, check=False, timeout=60.0,
        )
        if rebase.returncode != 0:
            # Conflict path. Detect what's conflicting.
            porcelain = _git(corpus_path, "status", "--porcelain=v2", "-z")
            conflicts = git_merge.parse_porcelain_v2_conflicts(
                porcelain.stdout or b""
            )
            if not conflicts:
                # Some other rebase failure (e.g., no upstream). Abort.
                _git(corpus_path, "rebase", "--abort")
                return PushResult(
                    success=False,
                    cards_committed=cards_committed,
                    error=XSensaiError(
                        code="REBASE_CONFLICT",
                        cause="rebase failed without parsable conflict paths",
                        attempted=f"git rebase {remote_ref}",
                        next_action="Inspect vault repo manually.",
                        retryable=True,
                        details=_stderr_str(rebase),
                    ),
                )

            heartbeat_only = all(
                git_merge.classify_conflict(p) == "heartbeat" for p in conflicts
            )
            if heartbeat_only:
                # Fast path: regenerate status, continue rebase.
                git_merge.resolve_heartbeat_fast_path(corpus_path, in_memory_status)
                cont = subprocess.run(
                    ["git", "-C", str(corpus_path), "-c", "core.editor=true",
                     "rebase", "--continue"],
                    capture_output=True, text=False, check=False, timeout=60.0,
                )
                if cont.returncode != 0:
                    # Fast-path resolver couldn't continue — likely a
                    # secondary conflict snuck in (e.g., heartbeat plus a
                    # card that we missed in the initial classify).
                    # Abort cleanly + surface to caller.
                    log.warning(
                        "heartbeat fast-path rebase --continue failed: %s",
                        _stderr_str(cont),
                    )
                    _git(corpus_path, "rebase", "--abort")
                    return PushResult(
                        success=False,
                        cards_committed=cards_committed,
                        error=XSensaiError(
                            code="REBASE_CONFLICT",
                            cause="heartbeat fast-path could not continue rebase",
                            attempted="git rebase --continue after heartbeat regen",
                            next_action=(
                                "Pull the vault on Mac, inspect _sync-status.md "
                                "and recent commits, resolve manually."
                            ),
                            retryable=True,
                            details=_stderr_str(cont),
                        ),
                    )
                # Rebase succeeded; fall through to push below.
            else:
                # Card conflict.
                card_paths = [
                    p for p in conflicts
                    if git_merge.classify_conflict(p) != "heartbeat"
                ]
                # Slice 6: BEFORE the fail-loud sequence (which abort+resets
                # the rebase index, destroying access to the conflicted
                # blobs), compute the shadow union candidate per card and
                # append to _conflicts.md. Idempotent — if commit_and_push
                # retries on the same conflict, the log won't double-write.
                # Shadow does NOT change actual rebase outcome (fail-loud
                # stays primary).
                for card_path in card_paths:
                    try:
                        local = git_merge._read_index_blob(corpus_path, 3, card_path)
                        remote = git_merge._read_index_blob(corpus_path, 2, card_path)
                        base = git_merge._read_index_blob(corpus_path, 1, card_path)
                    except Exception as e:
                        log.warning("shadow union: blob read failed for %s: %s", card_path, e)
                        continue
                    if local is None or remote is None:
                        continue
                    try:
                        _, diff_summary = git_merge.compute_union_candidate(
                            local, remote, base
                        )
                        git_merge.append_shadow_union_log(
                            corpus_path,
                            run_id=run_id,
                            card_path=card_path,
                            diff_summary=diff_summary,
                        )
                    except Exception as e:
                        log.warning("shadow union compute/log failed for %s: %s", card_path, e)
                # Now the fail-loud sequence runs unchanged.
                resolutions = git_merge.resolve_card_conflict_failloud(
                    corpus_path,
                    conflicting_paths=card_paths,
                    run_id=run_id,
                    remote_ref=remote_ref,
                )
                # Commit + push the conflict marker so user sees it next vault-pull.
                _git(
                    corpus_path,
                    "commit",
                    "-m",
                    f"[CRON_CONFLICT_UNRESOLVED] {run_id}",
                )
                push = _git(corpus_path, "push", "--force-with-lease", "origin", "main")
                return PushResult(
                    success=False,
                    cards_committed=cards_committed,
                    conflict_unresolved=True,
                    error=XSensaiError(
                        code="CRON_CONFLICT_UNRESOLVED",
                        cause=(
                            f"Cron rebase hit unresolvable conflict(s) in "
                            f"{len(resolutions)} card file(s)."
                        ),
                        attempted="git pull --rebase + fail-loud sidecar capture",
                        next_action=(
                            f"Pull the vault on Mac and resolve "
                            f"_conflicts/{run_id}/ sidecars manually. "
                            "See docs/CONFLICT_RESOLUTION.md."
                        ),
                        retryable=False,
                    ),
                    notes=[r.note for r in resolutions],
                )

        # 4. Push. Plain `git push` (cooperative): if a concurrent commit
        # landed on the remote between our fetch and push, the push
        # rejects and the loop's pull-rebase incorporates the divergence.
        # `--force-with-lease` is reserved for the fail-loud + flag-write
        # paths below where forced overwrite IS the intent.
        push = _git(corpus_path, "push", "origin", "main")
        if push.returncode == 0:
            return PushResult(
                success=True, cards_committed=cards_committed,
                notes=[f"pushed on attempt {attempt + 1}"],
            )
        # Push reject — try again.
        stderr_redacted = redact_token_strings(
            (push.stderr or b"").decode("utf-8", errors="replace")[:120]
        )
        log.info("push reject (attempt %d/%d): %s", attempt + 1, max_retries,
                 stderr_redacted)
        continue

    # Max retries exhausted: write static-template flag, commit + push.
    # --force-with-lease here IS the intent — we've exhausted the
    # cooperative path and want the flag to land for user visibility.
    flag_path = corpus_path / PUSH_REJECTED_FLAG
    _assert_inside_corpus(flag_path, corpus_path)
    flag_path.write_text(_push_rejected_recovery_text(run_id, max_retries))
    _git(corpus_path, "add", "--", str(flag_path))
    _git(
        corpus_path,
        "commit",
        "-m",
        f"[SYNC_PUSH_REJECTED] {run_id}",
    )
    final_push = _git(corpus_path, "push", "--force-with-lease", "origin", "main")
    return PushResult(
        success=False,
        cards_committed=cards_committed,
        flag_written=flag_path,
        error=XSensaiError(
            code="SYNC_PUSH_REJECTED",
            cause=(
                f"Cron sync wrote {cards_committed} cards but couldn't push "
                f"to vault repo after {max_retries} retries."
            ),
            attempted=f"git push --force-with-lease (×{max_retries})",
            next_action=(
                "Pull the vault on Mac, resolve any local divergence, push "
                "manually. SYNC_PUSH_REJECTED.md flag committed for "
                "recovery instructions."
            ),
            retryable=True,
            details=(
                f"Final push exit: {final_push.returncode}. "
                "Cards still in cron's HEAD."
            ),
        ),
    )


__all__ = [
    "PushResult",
    "commit_and_push",
    "DEFAULT_MAX_RETRIES",
    "PUSH_REJECTED_FLAG",
]
