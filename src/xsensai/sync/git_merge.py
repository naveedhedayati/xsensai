"""Cross-host conflict resolution: heartbeat fast-path + fail-loud sidecar.

Two flavors of conflict happen during cron's `git pull --rebase`:

1. **`_sync-status.md` heartbeat conflict** — happens almost every
   cron-after-manual cycle since both writers update the heartbeat.
   Generic fail-loud here would livelock cron (autoplan E1/CRITICAL).
   FAST PATH: regenerate from in-memory SyncStatus (max-merge counters
   and timestamps), restage, continue rebase.

2. **Card file conflict** (`*.md` or `*.raw.txt`) — rare; means user
   and cron both wrote the same source_id. FAIL-LOUD: capture both
   versions to `_conflicts/<run_id>/`, abort rebase, reset to remote,
   commit a marker, push the marker. User resolves manually
   (docs/CONFLICT_RESOLUTION.md).

Sequence is non-trivial because `git rebase --abort` leaves the worktree
on the LOCAL diverged tip. To get a fast-forward push, we must capture
the conflicting blobs from the index BEFORE abort, then `git reset
--hard origin/main` to incorporate the remote, then write sidecars on
top of the remote state. This finding came from spike #8.

Defense-in-depth: every path crossing the subprocess boundary is
validated through `_assert_inside_corpus` per E-5 + autoplan E6.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from xsensai.storage.corpus import _assert_inside_corpus
from xsensai.sync.heartbeat import (
    STATUS_FILE_NAME,
    SyncStatus,
    write_status,
)

log = logging.getLogger(__name__)


CONFLICTS_DIR = "_conflicts"
CONFLICTS_LOG = "_conflicts.md"


ConflictKind = Literal["heartbeat", "card", "raw_sidecar", "other"]


@dataclass(frozen=True)
class ConflictResolution:
    file_rel: str
    kind: ConflictKind
    resolved: bool
    note: str


def parse_porcelain_v2_conflicts(porcelain_output: bytes) -> List[str]:
    """Parse `git status --porcelain=v2 -z` output for unmerged paths.

    Format (NUL-delimited):
      `u <state> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>`

    Returns the list of conflicting relative paths. NUL-delimited parsing
    is mandatory (autoplan E6 — string slicing on porcelain v1 is brittle
    and traversal-vulnerable on quoted paths).
    """
    paths: List[str] = []
    if not porcelain_output:
        return paths
    for record in porcelain_output.split(b"\x00"):
        if not record:
            continue
        if not record.startswith(b"u "):
            continue
        # `u XY <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>`
        # 11 fields total; path is the 11th. Spaces in fields are not
        # possible because every field except path is a fixed-shape token.
        try:
            text = record.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            log.warning("non-utf8 path in porcelain output; skipping")
            continue
        parts = text.split(" ", 10)
        if len(parts) != 11:
            log.warning("malformed porcelain v2 'u' record: %r", text[:80])
            continue
        paths.append(parts[10])
    return paths


def classify_conflict(path_rel: str) -> ConflictKind:
    name = Path(path_rel).name
    if name == STATUS_FILE_NAME:
        return "heartbeat"
    if name.endswith(".raw.txt"):
        return "raw_sidecar"
    if name.endswith(".md"):
        return "card"
    return "other"


def _git(corpus_path: Path, *args: str, check: bool = False, capture: bool = True) -> subprocess.CompletedProcess:
    """Run `git -C <corpus> <args>` with timeout + argv discipline."""
    cmd = ["git", "-C", str(corpus_path), *args]
    return subprocess.run(
        cmd,
        capture_output=capture,
        text=False,  # bytes for NUL parsing
        check=check,
        timeout=30.0,
    )


def _read_index_blob(corpus_path: Path, stage: int, rel_path: str) -> Optional[bytes]:
    """Read `git show :<stage>:<path>` from the rebase index.

    Stage 1 = base, 2 = ours (= remote during rebase), 3 = theirs (= local
    being applied). NB: rebase REVERSES the conventional `git merge`
    ours/theirs because rebase replays your local commits onto upstream.
    """
    try:
        res = _git(corpus_path, "show", f":{stage}:{rel_path}")
    except subprocess.TimeoutExpired:
        return None
    if res.returncode != 0:
        return None
    return res.stdout


def _abort_rebase(corpus_path: Path) -> bool:
    res = _git(corpus_path, "rebase", "--abort")
    if res.returncode != 0:
        log.warning(
            "git rebase --abort failed: %s",
            (res.stderr or b"").decode("utf-8", errors="replace")[:200],
        )
        return False
    return True


def _reset_hard_to_remote(corpus_path: Path, remote_ref: str = "origin/main") -> bool:
    res = _git(corpus_path, "reset", "--hard", remote_ref)
    if res.returncode != 0:
        log.warning(
            "git reset --hard %s failed: %s",
            remote_ref,
            (res.stderr or b"").decode("utf-8", errors="replace")[:200],
        )
        return False
    return True


def resolve_heartbeat_fast_path(
    corpus_path: Path,
    in_memory_status: SyncStatus,
) -> ConflictResolution:
    """Heartbeat fast-path: regenerate `_sync-status.md` from in-memory
    state, restage, return resolved=True so caller continues the rebase.

    The in-memory status is authoritative for "what we just wrote" — it
    already reflects this run's success/failure + counters. We don't need
    to merge with the remote-side heartbeat because it was overwritten on
    every prior run too; the only sane "merge" is "the latest write wins,"
    and the latest write IS the run currently in progress.

    Caller should then run `git rebase --continue` after this returns.
    """
    rel = STATUS_FILE_NAME
    # Defense-in-depth: validate path stays inside corpus.
    target = _assert_inside_corpus(corpus_path / rel, corpus_path)
    write_status(corpus_path, in_memory_status)  # atomic via durable_replace
    add = _git(corpus_path, "add", "--", str(target))
    if add.returncode != 0:
        return ConflictResolution(
            file_rel=rel,
            kind="heartbeat",
            resolved=False,
            note=f"git add failed: {(add.stderr or b'')[:120]!r}",
        )
    return ConflictResolution(
        file_rel=rel,
        kind="heartbeat",
        resolved=True,
        note="heartbeat regenerated from in-memory SyncStatus",
    )


def resolve_card_conflict_failloud(
    corpus_path: Path,
    *,
    conflicting_paths: List[str],
    run_id: str,
    remote_ref: str = "origin/main",
) -> List[ConflictResolution]:
    """Fail-loud sidecar: capture remote + local versions to
    `_conflicts/<run_id>/`, abort rebase, reset to remote, write marker.

    Caller must STOP the run after this returns; cron exits 2 with
    [CRON_CONFLICT_UNRESOLVED]. User resolves manually.

    Sequence (per spike #8 finding):
      1. Read `:2:<path>` (= remote/cron-on-server) and `:3:<path>`
         (= local/cron-this-run) from index for each conflicting file.
      2. `git rebase --abort`.
      3. `git reset --hard <remote_ref>` so worktree matches remote tip.
      4. Write captured blobs to `_conflicts/<run_id>/<basename>.local`
         (= local) and `<basename>.remote` (= remote).
      5. Append entry to `_conflicts.md`.
      6. `git add` the conflicts/ dir + `_conflicts.md`.
    """
    # Step 0: validate every conflicting path is inside the corpus
    # BEFORE any subprocess + write. Defense per autoplan E6.
    for rel in conflicting_paths:
        # The path is git-relative to the corpus root.
        candidate = (corpus_path / rel).resolve()
        _assert_inside_corpus(candidate, corpus_path)

    # Step 1: capture both blobs from the rebase index.
    captured: List[Tuple[str, Optional[bytes], Optional[bytes]]] = []
    for rel in conflicting_paths:
        # In rebase: stage 2 = upstream/remote, stage 3 = local being applied.
        remote_blob = _read_index_blob(corpus_path, 2, rel)
        local_blob = _read_index_blob(corpus_path, 3, rel)
        captured.append((rel, local_blob, remote_blob))

    # Step 2: abort the rebase.
    if not _abort_rebase(corpus_path):
        return [
            ConflictResolution(
                file_rel=rel,
                kind=classify_conflict(rel),
                resolved=False,
                note="git rebase --abort failed; manual cleanup required",
            )
            for rel in conflicting_paths
        ]

    # Step 3: reset hard to remote tip so we have a fast-forwardable base.
    if not _reset_hard_to_remote(corpus_path, remote_ref):
        return [
            ConflictResolution(
                file_rel=rel,
                kind=classify_conflict(rel),
                resolved=False,
                note=f"git reset --hard {remote_ref} failed; manual cleanup required",
            )
            for rel in conflicting_paths
        ]

    # Step 4: write `_conflicts/<run_id>/` sidecars.
    conflicts_dir = corpus_path / CONFLICTS_DIR / run_id
    conflicts_dir.mkdir(parents=True, exist_ok=True)
    # Validate the resolved dir is inside corpus.
    _assert_inside_corpus(conflicts_dir, corpus_path)

    results: List[ConflictResolution] = []
    log_entries: List[str] = []
    for rel, local_blob, remote_blob in captured:
        kind = classify_conflict(rel)
        base = Path(rel).name
        local_path = conflicts_dir / f"{base}.local"
        remote_path = conflicts_dir / f"{base}.remote"
        # Validate inside corpus.
        _assert_inside_corpus(local_path, corpus_path)
        _assert_inside_corpus(remote_path, corpus_path)
        if local_blob is not None:
            local_path.write_bytes(local_blob)
        else:
            local_path.write_bytes(b"<missing>")
        if remote_blob is not None:
            remote_path.write_bytes(remote_blob)
        else:
            remote_path.write_bytes(b"<missing>")
        results.append(
            ConflictResolution(
                file_rel=rel,
                kind=kind,
                resolved=False,  # FALSE — manual review required
                note=f"sidecars at {CONFLICTS_DIR}/{run_id}/{base}.local|.remote",
            )
        )
        log_entries.append(
            f"- {rel} → {CONFLICTS_DIR}/{run_id}/{base}.{{local,remote}}"
        )

    # Step 5: append marker to `_conflicts.md` (forensic trail).
    marker_path = corpus_path / CONFLICTS_LOG
    _assert_inside_corpus(marker_path, corpus_path)
    ts = datetime.now(timezone.utc).isoformat()
    new_block = (
        f"\n## {ts} — run_id={run_id}\n"
        f"\n{len(log_entries)} unresolved conflict(s):\n\n"
        + "\n".join(log_entries)
        + "\n"
        + (
            "\nSee `docs/CONFLICT_RESOLUTION.md` for the resolution workflow.\n"
        )
    )
    if marker_path.exists():
        existing = marker_path.read_text(encoding="utf-8")
        marker_path.write_text(existing + new_block, encoding="utf-8")
    else:
        header = (
            "# x-sensai conflict log\n\n"
            "Cron writes one block per failed cross-host merge. See "
            "`docs/CONFLICT_RESOLUTION.md` for resolution workflow.\n"
        )
        marker_path.write_text(header + new_block, encoding="utf-8")

    # Step 6: stage the conflicts/ dir + the marker for the caller's commit.
    add = _git(
        corpus_path,
        "add",
        "--",
        str(conflicts_dir),
        str(marker_path),
    )
    if add.returncode != 0:
        log.warning(
            "git add of conflict files failed: %s",
            (add.stderr or b"")[:200],
        )
        # Even if add fails, results are still meaningful — the caller will
        # see the failures and surface CRON_CONFLICT_UNRESOLVED.

    return results


# ----------------------------------------------------------------------------
# Slice 6 — Shadow union resolver. 2-way only. Logs candidate to
# _conflicts.md but does NOT change the actual rebase outcome (fail-loud
# stays primary). Promotion to primary happens in a future slice after
# real-world conflicts confirm zero manual overrides on the union output.
# ----------------------------------------------------------------------------


def compute_union_candidate(
    local_bytes: bytes,
    remote_bytes: bytes,
    base_bytes: Optional[bytes],
) -> Tuple[bytes, dict]:
    """Compute the spec-literal union merge candidate.

    Spec line 213-214 rules (no per-key cleverness — see /autoplan eng-review):
      - Frontmatter: union of all keys. On collision, prefer LOCAL.
      - Lists (tags, applicability, media.external_urls): union with
        order preservation.
      - Body: prefer LOCAL in full.

    Returns (merged_bytes, diff_summary) where diff_summary describes
    which fields would have been merged vs dropped. Used by the shadow
    log; never written to the working tree in Slice 6.
    """
    import frontmatter as _fm  # local import to avoid hard dep at module level

    try:
        local_post = _fm.loads(local_bytes.decode("utf-8"))
        remote_post = _fm.loads(remote_bytes.decode("utf-8"))
    except Exception as e:
        return local_bytes, {"error": f"parse_failed: {e}"}

    local_meta = dict(local_post.metadata)
    remote_meta = dict(remote_post.metadata)

    merged_meta: dict = dict(remote_meta)  # start with remote
    merged_meta.update(local_meta)  # local wins on collision

    would_have_merged: List[str] = []
    would_have_dropped: List[str] = []

    # List union with order preservation for known list-shape fields.
    list_fields = {"tags", "applicability"}
    for key in list_fields:
        l_val = local_meta.get(key)
        r_val = remote_meta.get(key)
        if isinstance(l_val, list) and isinstance(r_val, list):
            seen = set()
            unioned = []
            for item in (l_val + r_val):
                if item not in seen:
                    seen.add(item)
                    unioned.append(item)
            merged_meta[key] = unioned
            if r_val and any(item not in l_val for item in r_val):
                would_have_merged.append(key)

    # media.external_urls (nested)
    l_media = local_meta.get("media") or {}
    r_media = remote_meta.get("media") or {}
    if isinstance(l_media, dict) and isinstance(r_media, dict):
        l_urls = l_media.get("external_urls") or []
        r_urls = r_media.get("external_urls") or []
        if isinstance(l_urls, list) and isinstance(r_urls, list):
            seen = set()
            unioned = []
            for u in (l_urls + r_urls):
                if u not in seen:
                    seen.add(u)
                    unioned.append(u)
            merged_media = dict(l_media)
            merged_media["external_urls"] = unioned
            merged_meta["media"] = merged_media
            if r_urls and any(u not in l_urls for u in r_urls):
                would_have_merged.append("media.external_urls")

    # Track simple key adds (remote-only fields)
    for key in remote_meta:
        if key not in local_meta and key not in {"tags", "applicability", "media"}:
            would_have_merged.append(key)

    # Track local-prefer drops for diagnostic
    for key in local_meta:
        if key in remote_meta and local_meta[key] != remote_meta[key] and key not in {"tags", "applicability", "media"}:
            would_have_dropped.append(f"{key}:remote")

    # Reconstruct merged content with local body
    out_post = _fm.Post(content=local_post.content, **{})
    out_post.metadata = merged_meta
    merged_bytes = _fm.dumps(out_post).encode("utf-8")

    diff_summary = {
        "would_have_merged": would_have_merged,
        "would_have_dropped": would_have_dropped,
        "byte_size_local": len(local_bytes),
        "byte_size_remote": len(remote_bytes),
        "byte_size_union": len(merged_bytes),
    }
    return merged_bytes, diff_summary


def append_shadow_union_log(
    corpus_path: Path,
    *,
    run_id: str,
    card_path: str,
    diff_summary: dict,
    ts: Optional[datetime] = None,
) -> bool:
    """Append a shadow union log entry to `_conflicts.md`.

    Idempotent (per /autoplan eng-review): if `_conflicts.md` already
    contains an entry for this `(run_id, card_path)`, skip. Prevents
    the 3x retry-loop duplication concern.

    Returns True if appended, False if skipped (duplicate or write
    failure).
    """
    import json as _json
    from json import JSONDecodeError as _JSONDecodeError  # noqa: F401
    log_path = corpus_path / CONFLICTS_LOG
    if ts is None:
        ts = datetime.now(timezone.utc)
    if log_path.exists():
        try:
            existing = log_path.read_text(encoding="utf-8")
            # Idempotency check: parse each line as JSON and compare the
            # actual `run_id` + `card` fields. Substring-needle matching
            # breaks for paths containing `"` or `\` (JSON escaping changes
            # the stored representation; needle wouldn't match and retries
            # would double-log).
            for line in existing.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = _json.loads(line)
                except (_json.JSONDecodeError, ValueError):
                    continue
                if entry.get("run_id") == run_id and entry.get("card") == card_path:
                    return False
        except OSError:
            pass
    entry = {
        "run_id": run_id,
        "ts": ts.isoformat(),
        "card": card_path,
        "shadow_resolution": "union",
        **diff_summary,
    }
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(_json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
        return True
    except OSError as e:
        log.warning("shadow log append failed: %s", e)
        return False


__all__ = [
    "ConflictKind",
    "ConflictResolution",
    "CONFLICTS_DIR",
    "CONFLICTS_LOG",
    "classify_conflict",
    "parse_porcelain_v2_conflicts",
    "resolve_heartbeat_fast_path",
    "resolve_card_conflict_failloud",
    "compute_union_candidate",
    "append_shadow_union_log",
]
