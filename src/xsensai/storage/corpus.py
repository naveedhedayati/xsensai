"""Corpus iteration — read v2 cards (with sidecar verification) + v1 cards (via adapter).

iter_cards() walks a corpus directory, yielding LoadedCard objects. Skips
malformed cards with a stderr log. Defends against duplicate source_id by
yielding only the first occurrence.

Slice 2 additions:
  - `_assert_inside_corpus(path, root)` security guard against MCP `id`
    parameter path traversal (e.g., id="../../etc/passwd"). Used by
    load_card_by_id and write_card.
  - iCloud detect: `resolve_corpus_path` warns once on startup if the
    corpus appears to live in an iCloud-synced directory, since os.replace
    atomicity does NOT survive iCloud's file-provider interposition.
  - `write_card(card, lock_token)` — atomic write via durable_replace with
    immutable per-version sidecars (raw_path includes a generation suffix
    derived from the checksum prefix). Caller MUST hold the card_write lock
    and pass its fencing token; verify_fencing_token is called before commit.
  - `discover_orphan_tmp(corpus)` — find leftover .tmp files from crashed
    writes. iter_cards calls this on entry and discards orphans with a
    [MID_WRITE_DETECTED] log.
  - `log_v1_mutation_blocked(...)` — append refused-mutation events to
    {corpus}/_v1-upgraded.jsonl so Slice 6 migration knows which cards the
    user wanted to mutate (E5 audit log).
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional

import frontmatter

from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import sidecar
from xsensai.storage import v1_adapter


log = logging.getLogger(__name__)


DEFAULT_CORPUS_PATH = "/Users/naveedhedayati/Documents/Vault/04_areas/x-bookmarks"

# Per Eng review: strict regex for MCP-supplied card ids. No path separators,
# no leading dot, no NUL, only the chars our slug/disambiguator can produce.
_VALID_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ICLOUD_WARNED = False  # one-time warn throttle

# Hex-prefix length used in immutable per-version sidecar filenames
# ({stem}.{prefix}.raw.txt). 12 hex chars = 48 bits of collision space — ample
# for distinguishing per-version sidecars within a single card's history.
_RAW_PATH_CHECKSUM_PREFIX_LEN = 12

# Orphan tmp files newer than this threshold are presumed in-flight by another
# writer (durable_replace mid-execution) and are NOT deleted. Without this
# gate, /xfind's iter_cards walk can unlink a live tmp file that /xpaste is
# about to os.replace into place — silently breaking the write with ENOENT.
# 5 minutes is well above any realistic atomic write (typically <1s on local
# disks; <10s even on iCloud-synced volumes).
ORPHAN_TMP_AGE_THRESHOLD_SEC = 300

# Vault navigation files that may legitimately live in the corpus directory
# but are NOT cards. The user's vault uses CLAUDE.md to give Claude context
# about the bookmarks area when operating there directly; README.md is the
# conventional human-facing readme. The walker silently skips both. Underscore-
# prefixed files (_sync-status.md, _v1-upgraded.jsonl, etc.) are already
# excluded by the prefix rule.
_NON_CARD_FILENAMES = frozenset({"CLAUDE.md", "README.md"})


def _is_card_shaped(md_path: Path) -> bool:
    """Cheap structural check: does this .md file have a YAML frontmatter
    block at all? A card always opens with `---\\n`. Files that don't are
    arbitrary markdown (vault notes, design docs, READMEs) and are silently
    skipped — without this, every retrieval logs a WARNING for every non-card
    .md in the corpus directory.

    Reads only the first 4 bytes. Treats any read error as "not card-shaped"
    (the strict load_card path would have surfaced a real error anyway).
    """
    try:
        with md_path.open("rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head.startswith(b"---\n") or head.startswith(b"---\r")


def _walk_card_files(corpus: Path) -> List[Path]:
    """Enumerate .md files in the corpus that look like cards.

    Filters out: underscore-prefixed metadata files (_sync-status.md, etc.),
    named navigation files (CLAUDE.md, README.md), and any .md without a
    YAML frontmatter opener. Returned list is sorted for deterministic
    iteration order across iter_cards and iter_cards_metadata.
    """
    return sorted(
        p for p in corpus.glob("*.md")
        if not p.name.startswith("_")
        and p.name not in _NON_CARD_FILENAMES
        and _is_card_shaped(p)
    )


def get_corpus_path() -> Path:
    """Resolve the corpus path from $XSENSAI_CORPUS_PATH or default."""
    return Path(os.environ.get("XSENSAI_CORPUS_PATH", DEFAULT_CORPUS_PATH))


def resolve_corpus_path(corpus_path: Optional[Path] = None) -> Path:
    """Resolve and validate that the corpus path exists.

    Raises XSensaiError(CORPUS_UNAVAILABLE) if the path doesn't exist or
    isn't a directory. Distinguishes 'broken corpus' from 'empty corpus'.
    """
    p = corpus_path if corpus_path is not None else get_corpus_path()
    try:
        resolved = p.resolve(strict=True)
    except (FileNotFoundError, OSError) as e:
        raise XSensaiError(
            code="CORPUS_UNAVAILABLE",
            cause=f"Corpus path does not exist or is not accessible: {p}",
            attempted=f"resolve_corpus_path({p})",
            next_action=(
                "Set $XSENSAI_CORPUS_PATH to your bookmark vault directory, "
                "or run scripts/bootstrap_qmd.sh to set up a fresh corpus."
            ),
            retryable=False,
            details=str(e),
        ) from e
    if not resolved.is_dir():
        raise XSensaiError(
            code="CORPUS_UNAVAILABLE",
            cause=f"Corpus path exists but is not a directory: {resolved}",
            attempted=f"resolve_corpus_path({p})",
            next_action="Point $XSENSAI_CORPUS_PATH at a directory of *.md cards.",
            retryable=False,
        )
    _maybe_warn_icloud(resolved)
    return resolved


def _maybe_warn_icloud(corpus_path: Path) -> None:
    """One-time stderr warning if the corpus looks iCloud-synced.

    iCloud's file-provider can interpose between the rename and the on-disk
    write, defeating os.replace's atomicity guarantee. The user can override
    by moving the vault to a non-synced local path.
    """
    global _ICLOUD_WARNED
    if _ICLOUD_WARNED:
        return
    if sidecar.is_likely_icloud_path(corpus_path):
        log.warning(
            "Corpus path appears iCloud-synced (%s). Atomic-write guarantees "
            "may be weakened by iCloud's file-provider. Consider moving the "
            "vault to a non-synced local path.",
            corpus_path,
        )
        _ICLOUD_WARNED = True


def _assert_inside_corpus(path: Path, corpus_root: Path) -> Path:
    """Resolve `path` and verify it stays inside `corpus_root`.

    Raises XSensaiError(NO_RESULTS) on traversal attempts (matches the
    existing API contract for missing cards — ambiguous "where did the
    card go" without leaking the attempted path back to a malicious caller).

    Returns the resolved Path on success.
    """
    try:
        resolved = path.resolve()
        resolved.relative_to(corpus_root.resolve())
    except (ValueError, OSError):
        raise XSensaiError(
            code="NO_RESULTS",
            cause="Card id refers to a path outside the corpus.",
            attempted=f"_assert_inside_corpus({path})",
            next_action="Use the id (filename without .md) returned by search_bookmarks.",
            retryable=False,
        )
    return resolved


def validate_card_id(card_id: str) -> None:
    """Validate user-supplied card id against the strict regex.

    Catches obvious bad inputs (path separators, NUL, leading dot) before
    they reach the filesystem layer. Raises XSensaiError(NO_RESULTS) so the
    error contract matches load_card_by_id's existing behavior on miss.
    """
    if not card_id or not _VALID_ID_RE.match(card_id):
        raise XSensaiError(
            code="NO_RESULTS",
            cause=f"Invalid card id: {card_id!r}",
            attempted=f"validate_card_id({card_id!r})",
            next_action=(
                "Card ids must be alphanumeric with optional dots/underscores/dashes "
                "(no slashes, no leading dot). Use the id returned by search_bookmarks."
            ),
            retryable=False,
        )


def load_card(md_path: Path, corpus_root: Optional[Path] = None) -> LoadedCard:
    """Load a single card. Handles v2 (with sidecar) and v1 (via adapter)."""
    try:
        post = frontmatter.load(md_path)
    except Exception as e:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"Frontmatter parse failed: {md_path}",
            attempted=f"frontmatter.load({md_path})",
            next_action="Open the card and check the YAML at the top is well-formed.",
            retryable=False,
            details=str(e),
        ) from e

    fm_dict = dict(post.metadata)
    body = post.content

    if v1_adapter.is_v1_shape(fm_dict):
        return v1_adapter.adapt_v1(md_path, fm_dict, body)

    try:
        cf = CardFrontmatter.model_validate(fm_dict)
    except Exception as e:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"Frontmatter validation failed: {md_path}",
            attempted=f"CardFrontmatter.model_validate({md_path})",
            next_action="Fix the frontmatter to match the v2 schema; see CLAUDE.md.",
            retryable=False,
            details=str(e),
        ) from e

    raw_path_str = cf.raw_path
    if raw_path_str is None:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"v2 card missing raw_path: {md_path}",
            attempted=f"load_card({md_path})",
            next_action="Add raw_path to the frontmatter or remove raw_checksum to fall back to v1 adapter.",
            retryable=False,
        )

    raw_path = (md_path.parent / raw_path_str).resolve()
    if corpus_root is not None:
        try:
            raw_path.relative_to(corpus_root.resolve())
        except ValueError as e:
            raise XSensaiError(
                code="DISK_WRITE_FAILED",
                cause=f"Sidecar path escapes corpus root: {raw_path}",
                attempted=f"load_card({md_path})",
                next_action=(
                    "raw_path must stay inside the corpus directory. "
                    "Restore from git or fix the frontmatter."
                ),
                retryable=False,
                details=f"corpus_root={corpus_root}, resolved={raw_path}",
            ) from e
    raw_bytes, computed_checksum = sidecar.read_sidecar(raw_path)

    if cf.raw_checksum and computed_checksum != cf.raw_checksum:
        raise XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Sidecar checksum mismatch: {raw_path}",
            attempted=f"load_card({md_path})",
            next_action=(
                "Card sidecar bytes do not match recorded checksum. "
                "The sidecar may have been edited by hand or corrupted; restore from git."
            ),
            retryable=False,
            details=f"expected={cf.raw_checksum}, got={computed_checksum}",
        )

    return LoadedCard(fm=cf, body=body, raw_bytes=raw_bytes, md_path=md_path)


def iter_cards(corpus_path: Optional[Path] = None) -> Iterator[LoadedCard]:
    """Iterate every card in the corpus directory (excluding _* metadata files).

    Skips malformed cards with a stderr WARNING and continues. Defends
    against duplicate source_id by logging and skipping subsequent occurrences.

    On entry: discovers and discards orphan .tmp files left from crashed
    writes (Slice 2 self-healing per spec).

    Raises XSensaiError(CORPUS_UNAVAILABLE) if the corpus path is missing or invalid.
    """
    corpus = resolve_corpus_path(corpus_path)

    # Self-heal mid-write crash debris before walking the corpus.
    # CRITICAL: gate on age. iter_cards is unlocked (called by /xfind via
    # _safe_corpus_count); discover_orphan_tmp returns ALL .tmp files. If
    # /xpaste is mid-durable_replace, its .tmp is "young" — deleting it
    # would race the upcoming os.replace into ENOENT. Only purge tmps older
    # than ORPHAN_TMP_AGE_THRESHOLD_SEC, presuming anything younger is live.
    import time as _time
    now = _time.time()
    orphans = discover_orphan_tmp(corpus)
    for orphan in orphans:
        try:
            stat = orphan.stat()
        except OSError as e:
            log.warning("could not stat orphan tmp %s: %s", orphan, e)
            continue
        age = now - stat.st_mtime
        if age < ORPHAN_TMP_AGE_THRESHOLD_SEC:
            log.debug(
                "skipping young .tmp (%.1fs old, threshold %ds — presumed live write): %s",
                age, ORPHAN_TMP_AGE_THRESHOLD_SEC, orphan,
            )
            continue
        try:
            orphan.unlink()
            log.warning(
                "[MID_WRITE_DETECTED] discarded orphan tmp (%.0fs old): %s",
                age, orphan,
            )
        except OSError as e:
            log.warning("could not unlink orphan tmp %s: %s", orphan, e)

    seen_source_ids: set[str] = set()

    md_files = _walk_card_files(corpus)
    for md_path in md_files:
        try:
            card = load_card(md_path, corpus_root=corpus)
        except XSensaiError as e:
            log.warning("skipping card %s: [%s] %s", md_path.name, e.code, e.cause)
            continue
        sid = card.fm.source_id
        if sid:
            if sid in seen_source_ids:
                log.warning(
                    "skipping duplicate source_id %r in %s", sid, md_path.name
                )
                continue
            seen_source_ids.add(sid)
        yield card


def iter_cards_metadata(corpus_path: Optional[Path] = None) -> Iterator[LoadedCard]:
    """Like iter_cards but SKIPS sidecar reads + checksum verification.

    Per /review F4 (Performance specialist): list_pinned and due_cards_for_review
    only filter on frontmatter fields (pinned bool, why_saved_pending bool,
    next_review_at). Loading + sha256-verifying every sidecar just to read
    those bools is O(N) work that scales with corpus size for no benefit.

    This iter yields LoadedCard objects with `raw_bytes=b""` and skips the
    sidecar read entirely. Callers that need the body or verified bytes
    should call iter_cards (the strict variant) instead.

    NOTE: this still parses YAML frontmatter and runs CardFrontmatter
    validation — those are needed to surface the metadata fields. Only the
    sidecar read + sha256 verify is skipped.
    """
    corpus = resolve_corpus_path(corpus_path)
    md_files = _walk_card_files(corpus)
    for md_path in md_files:
        try:
            post = frontmatter.load(md_path)
        except Exception as e:
            log.warning("skipping %s: yaml parse error: %s", md_path.name, e)
            continue
        fm_dict = dict(post.metadata)
        body = post.content
        if v1_adapter.is_v1_shape(fm_dict):
            try:
                yield v1_adapter.adapt_v1(md_path, fm_dict, body)
            except XSensaiError as e:
                log.warning("skipping v1 %s: [%s] %s", md_path.name, e.code, e.cause)
            continue
        try:
            cf = CardFrontmatter.model_validate(fm_dict)
        except Exception as e:
            log.warning("skipping %s: validation error: %s", md_path.name, e)
            continue
        # Construct LoadedCard with EMPTY raw_bytes — caller is responsible
        # for not relying on the sidecar via this fast path.
        yield LoadedCard(fm=cf, body=body, raw_bytes=b"", md_path=md_path)


def discover_orphan_tmp(corpus_path: Path) -> List[Path]:
    """Return any *.tmp files in the corpus directory.

    Orphan tmps are debris from crashed atomic writes (durable_replace was
    interrupted between O_CREAT and os.replace). Callers (typically
    iter_cards) discard them.
    """
    if not corpus_path.exists() or not corpus_path.is_dir():
        return []
    return sorted(corpus_path.glob("*.tmp"))


def write_card(
    card: LoadedCard,
    lock_token: str,
    corpus_path: Optional[Path] = None,
) -> LoadedCard:
    """Write a card (frontmatter + body + sidecar) atomically with immutable
    per-version sidecars.

    Pre-conditions:
      - Caller holds the card_write lock (see locks.with_card_write_lock)
      - Caller passes its lock fencing token (verify_fencing_token called
        before each on-disk commit; aborts if the token has been re-acquired
        by another writer)

    Sequence (UC5 fix — immutable per-version sidecars; .md written LAST):
      1. Verify fencing token still matches on-disk lock JSON
      2. Compute new_checksum = sha256(card.raw_bytes)
      3. Choose raw_path with checksum-prefix suffix so it's unique per version:
         {stem}.{checksum_prefix}.raw.txt
      4. Render frontmatter to YAML (sort_keys=False per Eng Q3) with
         raw_path + raw_checksum set
      5. write_sidecar_atomic(generation-suffixed raw_path, raw_bytes)
      6. Verify fencing token AGAIN
      7. atomic-write the .md (.tmp + fsync + rename + parent fsync)
      8. Touch _index-dirty marker
      9. Sidecar GC (per /review F5): if the prior on-disk .md referenced a
         different raw_path, unlink the old sidecar. Bounded growth.
      10. Return a fresh LoadedCard reflecting on-disk state

    Raises:
      XSensaiError(LOCK_HELD) if fencing token verification fails (lock
      stolen by another writer mid-flight — rare, but possible after process
      death + flock release + new acquire).
    """
    corpus = resolve_corpus_path(corpus_path)

    # Fencing token verification (UC6).
    if not filelock.verify_fencing_token(corpus, lock_token):
        raise XSensaiError(
            code="LOCK_HELD",
            cause="card_write lock fencing token mismatch — another writer holds the lock now.",
            attempted=f"write_card(id={card.id})",
            next_action="The lock was re-acquired during your write. Re-acquire and retry.",
            retryable=True,
            details=f"expected_token={lock_token[:8]}...",
        )

    raw_bytes = card.raw_bytes
    new_checksum = sidecar.compute_checksum(raw_bytes)
    checksum_prefix = new_checksum.split(":", 1)[1][:_RAW_PATH_CHECKSUM_PREFIX_LEN]

    md_path = card.md_path
    md_path = _assert_inside_corpus(md_path, corpus)
    raw_path = md_path.with_suffix(f".{checksum_prefix}.raw.txt")

    # /review F5 sidecar GC prep: read the OLD raw_path BEFORE we overwrite,
    # so step 9 can unlink it cleanly. Failure to read (file missing, parse
    # error) means there's no prior sidecar to GC — safe to ignore.
    old_raw_path: Optional[Path] = None
    if md_path.exists():
        try:
            old_post = frontmatter.load(md_path)
            old_raw_str = old_post.metadata.get("raw_path")
            if old_raw_str:
                old_raw_path = (md_path.parent / old_raw_str).resolve()
                # Don't GC the same path we're about to write (mutation that
                # produced the same checksum — content didn't change).
                if old_raw_path == raw_path.resolve():
                    old_raw_path = None
        except Exception as e:
            log.debug("could not read old raw_path for GC; skipping: %s", e)
            old_raw_path = None

    # Build frontmatter dict honoring strict serialization (sort_keys=False).
    fm_dict = card.fm.model_dump(mode="json", exclude_none=True)
    fm_dict["raw_path"] = f"./{raw_path.name}"
    fm_dict["raw_checksum"] = new_checksum

    # Step 5: sidecar first (immutable per-version → safe even on .md write
    # failure; old .md if any still references its old sidecar untouched).
    sidecar.write_sidecar_atomic(raw_path, raw_bytes)

    # Step 6: re-verify token before committing the .md.
    if not filelock.verify_fencing_token(corpus, lock_token):
        # Don't bother cleaning up the new sidecar — orphan-collect later.
        raise XSensaiError(
            code="LOCK_HELD",
            cause="card_write lock token mismatch after sidecar write — aborting before .md commit.",
            attempted=f"write_card(id={card.id}) — post-sidecar token check",
            next_action="Re-acquire lock and retry. Old card remains intact.",
            retryable=True,
        )

    # Step 7: atomic .md write (via frontmatter dump with sort_keys=False).
    post = frontmatter.Post(content=card.body, **fm_dict)
    md_bytes = frontmatter.dumps(
        post, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).encode("utf-8")
    sidecar.durable_replace(md_path, md_bytes)

    # Step 8: index-dirty marker (best-effort; no error on failure).
    try:
        (corpus / "_index-dirty").touch()
    except OSError as e:
        log.warning("could not touch _index-dirty marker: %s", e)

    # Step 9: sidecar GC (per /review F5). Now that the new .md commits the
    # new sidecar, the old one is unreachable. Unlink it. Bounds disk growth
    # for cards that get re-mutated (e.g., /xnote review walks). Best-effort
    # — failure here doesn't block the user write; orphan stays for a future
    # maintenance pass to clean.
    if old_raw_path is not None and old_raw_path.exists():
        try:
            # Defensive: only unlink if the resolved old path stays inside the
            # corpus root (paranoia against a malicious frontmatter raw_path
            # that survived load_card's _assert_inside_corpus on the prior write).
            old_raw_path.relative_to(corpus.resolve())
            old_raw_path.unlink()
            log.debug("[SIDECAR_GC] unlinked old sidecar: %s", old_raw_path)
        except (ValueError, OSError) as e:
            log.warning("[SIDECAR_GC] could not unlink %s: %s", old_raw_path, e)

    # Step 10: reload from disk for canonical state.
    return load_card(md_path, corpus_root=corpus)


def read_review_cursor(corpus_path: Path) -> Optional[str]:
    """Read the /xnote review walk cursor (per UC10 wire-up).

    The cursor records the LAST card id the user finished annotating in a
    review walk session. Next session resumes by skipping cards captured
    AT or BEFORE that id's captured time. Empty/missing cursor = walk from
    the oldest pending card.
    """
    cursor_path = corpus_path / "_review-cursor.json"
    if not cursor_path.exists():
        return None
    try:
        data = json.loads(cursor_path.read_text(encoding="utf-8"))
        return data.get("last_card_id")
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def write_review_cursor(corpus_path: Path, last_card_id: Optional[str]) -> None:
    """Write the /xnote review walk cursor.

    Pass last_card_id=None to clear the cursor (full walk completed).
    """
    cursor_path = corpus_path / "_review-cursor.json"
    if last_card_id is None:
        try:
            cursor_path.unlink()
        except FileNotFoundError:
            pass
        return
    payload = json.dumps({
        "last_card_id": last_card_id,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2)
    try:
        sidecar.durable_replace(
            cursor_path, payload.encode("utf-8"),
            durability="metadata",  # cursor is recovery-state, not durable
        )
    except XSensaiError as e:
        log.warning("could not write review cursor: %s", e)


def find_recent_paste_by_fingerprint(
    corpus_path: Path,
    fingerprint: str,
    window_seconds: int,
) -> Optional[str]:
    """Look up a recently-written paste card by content fingerprint (per F10).

    Scans paste cards captured within `window_seconds` and returns the id of
    any card whose `content_fingerprint` frontmatter field matches. Used by
    paste_bookmark to surface 'duplicate of {id}' instead of writing a 2nd
    card on accidental double-submit.

    Uses iter_cards_metadata (skips sidecar verify) since we only need the
    captured + fingerprint frontmatter fields.
    """
    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - window_seconds
    for card in iter_cards_metadata(corpus_path):
        if card.fm.source_type != "paste":
            continue
        if card.fm.captured.timestamp() < cutoff:
            continue
        # content_fingerprint is stored in CardFrontmatter via model_extra
        # (or equivalent). Pull from the raw fm_dict via a re-read of the .md.
        # For now, look up via a small attribute on the model — added below.
        cf_fp = getattr(card.fm, "content_fingerprint", None)
        if cf_fp == fingerprint:
            return card.id
    return None


__all_extra__ = ["read_review_cursor", "write_review_cursor", "find_recent_paste_by_fingerprint"]


def log_v1_mutation_blocked(
    corpus_path: Path,
    card_id: str,
    attempted_op: str,
) -> None:
    """Append a refused-v1-mutation event to {corpus}/_v1-upgraded.jsonl.

    Slice 6 migration consumes this log to prioritize re-fetches: cards the
    user actively tried to pin/annotate are higher-value than cards that
    just sit in the corpus untouched.

    Best-effort. Logging failures don't block the user-facing error.
    """
    log_path = corpus_path / "_v1-upgraded.jsonl"
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "card_id": card_id,
        "attempted_op": attempted_op,
        "outcome": "blocked",
    }
    line = json.dumps(entry) + "\n"
    try:
        # POSIX guarantees atomic appends below PIPE_BUF (~4KB); each entry
        # is ~150 bytes so concurrent writers won't tear lines. fsync after
        # write so Slice 6 migration sees the entry even after a crash.
        with log_path.open("a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass  # fsync on append-only logs is best-effort
    except OSError as e:
        log.warning("could not append to %s: %s", log_path, e)


def load_card_by_id(card_id: str, corpus_path: Optional[Path] = None) -> LoadedCard:
    """Look up a card by its id (filename without .md). Raises NO_RESULTS if missing.

    Per Slice 2 security guard: the id is validated against a strict regex
    before path construction, AND the resolved path is confirmed inside the
    corpus root. This blocks the path-traversal class (e.g., id="../../etc/passwd")
    that Slice 1 originally allowed.
    """
    validate_card_id(card_id)
    corpus = resolve_corpus_path(corpus_path)
    md_path = _assert_inside_corpus(corpus / f"{card_id}.md", corpus)
    if not md_path.exists() or not md_path.is_file():
        raise XSensaiError(
            code="NO_RESULTS",
            cause=f"No card with id {card_id!r}",
            attempted=f"load_card_by_id({card_id!r})",
            next_action="Check the id (filename without .md) returned by search_bookmarks.",
            retryable=False,
        )
    return load_card(md_path, corpus_root=corpus)


__all__ = [
    "DEFAULT_CORPUS_PATH",
    "get_corpus_path",
    "resolve_corpus_path",
    "load_card",
    "load_card_by_id",
    "iter_cards",
    "iter_cards_metadata",
    "validate_card_id",
    "write_card",
    "discover_orphan_tmp",
    "log_v1_mutation_blocked",
    "ORPHAN_TMP_AGE_THRESHOLD_SEC",
    "read_review_cursor",
    "write_review_cursor",
    "find_recent_paste_by_fingerprint",
]
