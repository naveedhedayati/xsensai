#!/usr/bin/env python3
"""Slice 6 — v1→v2 migration script with byte-exact rollback.

Three exclusive modes (mutually exclusive argparse group, exactly one required):
  --dry-run   List cards that WOULD migrate; no writes.
  --apply     Migrate v1 cards to v2 in-place under card_write lock.
              Writes byte-exact rollback journal at
              {corpus}/migrate_v1_to_v2.rollback.jsonl BEFORE any mutation.
  --rollback  Restore v1 state byte-exact from the journal. Safe to re-run.

Confirmation: --apply and --rollback prompt for an explicit "Type APPLY/ROLLBACK"
unless --yes is passed for non-interactive use (e.g., from setup wizard).

Per spec line 378 + /autoplan eng-review: byte-exact means the FULL ORIGINAL
.md bytes are stored in the journal, not parsed frontmatter. fsync the journal
entry BEFORE the write_card mutation so a crash between journal-fsync and
write_card-success leaves the journal entry intact for rollback.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import frontmatter  # type: ignore

from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus, sidecar, v1_adapter


JOURNAL_FILENAME = "migrate_v1_to_v2.rollback.jsonl"


@dataclass
class V1Card:
    md_path: Path
    fm_dict: dict
    body: str
    md_bytes: bytes  # original on-disk bytes — byte-exact for rollback


def _discover_v1_cards(corpus_path: Path) -> List[V1Card]:
    """Walk the corpus and return every card whose frontmatter is v1-shape."""
    out: List[V1Card] = []
    for md_path in sorted(corpus_path.glob("*.md")):
        if md_path.name.startswith("_") or md_path.name in {"CLAUDE.md", "README.md"}:
            continue
        try:
            md_bytes = md_path.read_bytes()
            post = frontmatter.loads(md_bytes.decode("utf-8"))
        except Exception:
            continue
        fm_dict = dict(post.metadata)
        if not v1_adapter.is_v1_shape(fm_dict):
            continue
        out.append(V1Card(
            md_path=md_path, fm_dict=fm_dict, body=post.content, md_bytes=md_bytes,
        ))
    return out


def _journal_path(corpus_path: Path) -> Path:
    return corpus_path / JOURNAL_FILENAME


def _append_journal_entry(journal: Path, entry: dict) -> None:
    """Append entry to journal + fsync. Per /autoplan eng-review: must complete
    BEFORE the corresponding write_card mutation so rollback can restore."""
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
    fd = os.open(str(journal), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)  # critical — entry MUST be on disk before write_card
    finally:
        os.close(fd)


def _migrate_one(
    v1: V1Card,
    *,
    corpus_path: Path,
    lock_token: str,
    journal: Path,
) -> dict:
    """Migrate one v1 card. Returns a status dict suitable for log/report."""
    # Step 1: synthesize a v2 LoadedCard via the adapter.
    adapted = v1_adapter.adapt_v1(v1.md_path, v1.fm_dict, v1.body)

    # Step 2: build the v2 frontmatter with sidecar fields populated.
    raw_bytes = adapted.raw_bytes
    checksum = sidecar.compute_checksum(raw_bytes)
    checksum_prefix = checksum[len("sha256:"):][:12]
    raw_path = f"{v1.md_path.stem}.{checksum_prefix}.raw.txt"

    fm_dict = adapted.fm.model_dump(mode="python")
    fm_dict["raw_path"] = raw_path
    fm_dict["raw_checksum"] = checksum
    fm_dict["extraction_pending"] = True  # newly-migrated cards drain via /xextract

    new_fm = CardFrontmatter.model_validate(fm_dict)
    new_card = LoadedCard(
        fm=new_fm,
        body=adapted.body,
        raw_bytes=raw_bytes,
        md_path=v1.md_path,
    )

    # Step 3: write journal entry FIRST + fsync.
    journal_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "id": v1.md_path.stem,
        "v1_md_path": str(v1.md_path),
        "v1_md_bytes_b64": base64.b64encode(v1.md_bytes).decode("ascii"),
        "v1_md_sha256": "sha256:" + hashlib.sha256(v1.md_bytes).hexdigest(),
        "v2_md_path": str(v1.md_path),
        "v2_raw_path": raw_path,
    }
    _append_journal_entry(journal, journal_entry)

    # Step 4: ONLY NOW mutate to v2 via write_card.
    written = corpus.write_card(new_card, lock_token, corpus_path=corpus_path)

    return {"id": written.id, "status": "migrated", "raw_path": raw_path}


def cmd_dry_run(corpus_path: Path) -> int:
    cards = _discover_v1_cards(corpus_path)
    print(f"Found {len(cards)} v1 card(s) in {corpus_path}")
    for v1 in cards:
        print(f"  - {v1.md_path.name}")
    if cards:
        print()
        print("Run with --apply to migrate. A byte-exact rollback journal will be")
        print(f"written to {_journal_path(corpus_path).name} before any mutation.")
    return 0


def cmd_apply(corpus_path: Path, *, assume_yes: bool) -> int:
    cards = _discover_v1_cards(corpus_path)
    if not cards:
        print(f"No v1 cards in {corpus_path}.")
        return 0
    print(f"Will migrate {len(cards)} v1 card(s).")
    if not assume_yes:
        confirm = input("Type APPLY to confirm: ").strip()
        if confirm != "APPLY":
            print("Aborted.")
            return 2
    journal = _journal_path(corpus_path)
    # Treat empty journal (0 bytes) as not-existing — recovery from a
    # crashed prior --apply that opened the file but wrote no entries.
    if journal.exists() and journal.stat().st_size > 0:
        print(
            f"Refusing to overwrite existing journal {journal.name}. "
            "Run --rollback first or move it aside.",
            file=sys.stderr,
        )
        return 1
    if journal.exists():
        # Empty file from a prior crash. Unlink so we start fresh.
        try:
            journal.unlink()
        except OSError:
            pass
    results = []
    failed = []
    for v1 in cards:
        try:
            with filelock.with_card_write_lock(corpus_path, "migrate-v1") as h:
                result = _migrate_one(
                    v1, corpus_path=corpus_path, lock_token=h.token, journal=journal,
                )
            results.append(result)
            print(f"  ✓ {result['id']}")
        except Exception as e:
            failed.append({"id": v1.md_path.stem, "error": str(e)})
            print(f"  ✗ {v1.md_path.stem}: {type(e).__name__}: {e}", file=sys.stderr)
    print()
    print(f"Migrated: {len(results)} / {len(cards)}")
    if failed:
        print(f"Failed: {len(failed)}", file=sys.stderr)
        for f in failed:
            print(f"  {f['id']}: {f['error']}", file=sys.stderr)
        return 1
    return 0


def cmd_rollback(corpus_path: Path, *, assume_yes: bool) -> int:
    journal = _journal_path(corpus_path)
    if not journal.exists():
        err = XSensaiError(
            code="NO_ROLLBACK_JOURNAL",
            cause=f"Rollback journal not found at {journal}",
            attempted="migrate_v1_to_v2 --rollback",
            next_action="Ensure --apply ran successfully and produced the journal first.",
            retryable=False,
        )
        print(err.format(), file=sys.stderr)
        return 1
    entries = []
    # Track corrupt-line skips during JSON parse — needed below to refuse
    # archive when entries were silently dropped (would create silent
    # rollback gaps).
    skipped_corrupt_lines = 0
    with journal.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"WARNING: skipping corrupt journal line: {e}", file=sys.stderr)
                skipped_corrupt_lines += 1
                continue
    if not entries:
        # Empty journal — archive so future --apply is not blocked.
        archive = journal.with_suffix(
            f".empty-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
        )
        try:
            journal.rename(archive)
            print(f"Journal {journal.name} was empty. Archived to {archive.name}.")
        except OSError:
            print(f"Journal {journal} is empty. Could not archive; remove manually.")
        return 0
    print(f"Will roll back {len(entries)} migration entry(ies) (in reverse order).")
    if skipped_corrupt_lines > 0:
        print(
            f"WARNING: {skipped_corrupt_lines} corrupt journal line(s) detected. "
            "These cards will NOT be restored; the journal will NOT be archived "
            "after rollback so you can investigate.",
            file=sys.stderr,
        )
    if not assume_yes:
        confirm = input("Type ROLLBACK to confirm: ").strip()
        if confirm != "ROLLBACK":
            print("Aborted.")
            return 2
    # Defense-in-depth: verify every journal-supplied path is inside the corpus.
    # Tampered (or stale) journals must not be able to overwrite arbitrary files
    # via the durable_replace below.
    from xsensai.storage.corpus import _assert_inside_corpus
    restored = 0
    failed = []
    for entry in reversed(entries):
        try:
            v1_md_path = Path(entry["v1_md_path"])
            # Path-traversal guard: refuse if v1_md_path is not inside the
            # corpus we're rolling back. Catches journal tampering AND the
            # accidental-stale-journal case (corpus moved, journal points at
            # old absolute paths).
            _assert_inside_corpus(v1_md_path, corpus_path)
            v1_md_bytes = base64.b64decode(entry["v1_md_bytes_b64"])
            # Verify checksum before write
            expected = entry["v1_md_sha256"]
            actual = "sha256:" + hashlib.sha256(v1_md_bytes).hexdigest()
            if expected != actual:
                raise ValueError(f"journal entry checksum mismatch: {expected} != {actual}")
            # Atomic restore via durable_replace
            sidecar.durable_replace(v1_md_path, v1_md_bytes)
            # Unlink the v2 sidecar (if present). Same boundary check.
            v2_raw_path = corpus_path / entry["v2_raw_path"]
            try:
                _assert_inside_corpus(v2_raw_path, corpus_path)
            except Exception:
                # Skip sidecar unlink if it would escape the corpus. The .md
                # restore already succeeded.
                pass
            else:
                if v2_raw_path.exists():
                    try:
                        v2_raw_path.unlink()
                    except OSError:
                        pass
            restored += 1
            print(f"  ✓ {entry['id']}")
        except Exception as e:
            failed.append({"id": entry.get("id"), "error": str(e)})
            print(f"  ✗ {entry.get('id')}: {type(e).__name__}: {e}", file=sys.stderr)
    print()
    print(f"Restored: {restored} / {len(entries)}")
    if failed:
        print(f"Failed: {len(failed)}", file=sys.stderr)
        return 1
    if skipped_corrupt_lines > 0:
        # Refuse to archive — keeps the corrupt journal for forensic review.
        print(
            f"Journal NOT archived: {skipped_corrupt_lines} corrupt line(s) "
            f"present in {journal.name}. Inspect, repair, or remove manually.",
            file=sys.stderr,
        )
        return 1
    # On full success, archive the journal so re-run is a no-op.
    archive = journal.with_suffix(
        f".applied-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.jsonl"
    )
    journal.rename(archive)
    print(f"Journal archived to {archive.name}.")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="migrate_v1_to_v2.py",
        description="Slice 6 v1→v2 migration with byte-exact rollback.",
    )
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true",
                   help="List cards that would migrate; no writes.")
    g.add_argument("--apply", action="store_true",
                   help="Migrate v1 cards to v2 in-place. Requires confirmation.")
    g.add_argument("--rollback", action="store_true",
                   help="Restore v1 state byte-exact from the journal.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the interactive APPLY/ROLLBACK confirmation prompt.")
    p.add_argument("--corpus", type=Path, default=None,
                   help="Override corpus path (otherwise XSENSAI_CORPUS_PATH).")
    return p


def main() -> int:
    args = _build_parser().parse_args()
    try:
        corpus_path = corpus.resolve_corpus_path(args.corpus)
    except XSensaiError as e:
        print(e.format(), file=sys.stderr)
        return 1
    if args.dry_run:
        return cmd_dry_run(corpus_path)
    if args.apply:
        return cmd_apply(corpus_path, assume_yes=args.yes)
    if args.rollback:
        return cmd_rollback(corpus_path, assume_yes=args.yes)
    return 1  # unreachable


if __name__ == "__main__":
    sys.exit(main())
