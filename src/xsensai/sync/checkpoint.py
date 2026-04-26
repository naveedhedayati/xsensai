"""Sync checkpoint — append-on-success JSONL for crash-resume.

Per spec section "Resumable sync": append a record after every fetched-and-
written card; on restart, skip cards already in the checkpoint; on full
success, archive the checkpoint to ~/.cache/xsensai/sync-checkpoints/{ts}.jsonl
(per /autoplan S-5 fix — out of corpus, with a 30-day retention purge).

Per /autoplan E-4 fix: checkpoint append IS the commit point. Crash between
card-write and checkpoint-append leaves the card on disk + dedup detects on
next run (idempotent skip). Crash between checkpoint-append and extraction
leaves `extraction_pending: true` — `/xextract retry-failed` picks up.

Per S-5 fix: in-corpus _sync-checkpoint.jsonl is transient working state
(survives Mac reboot mid-sync). Archives go to user cache (cross-host
forensics aren't useful since archived files are gitignored).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, List, Optional, Set

from xsensai.sync.version import SYNC_SCHEMA_VERSION


log = logging.getLogger(__name__)


CHECKPOINT_FILE_NAME = "_sync-checkpoint.jsonl"


def _archive_dir() -> Path:
    """User-cache archive directory — outside corpus, 30-day retention."""
    return Path.home() / ".cache" / "xsensai" / "sync-checkpoints"


@dataclass(frozen=True)
class CheckpointRecord:
    """One line of _sync-checkpoint.jsonl."""

    source_id: str
    captured_at: str  # ISO 8601 UTC
    mode: str         # since-last-run / backlog / single / retry-failed
    run_id: str       # forensic, links to card._xsync_run_id
    schema_version: str = SYNC_SCHEMA_VERSION

    def to_jsonl(self) -> str:
        return json.dumps({
            "source_id": self.source_id,
            "captured_at": self.captured_at,
            "mode": self.mode,
            "run_id": self.run_id,
            "schema_version": self.schema_version,
        })

    @classmethod
    def from_jsonl(cls, line: str) -> "CheckpointRecord":
        d = json.loads(line)
        return cls(
            source_id=str(d["source_id"]),
            captured_at=str(d["captured_at"]),
            mode=str(d.get("mode", "unknown")),
            run_id=str(d.get("run_id", "")),
            schema_version=str(d.get("schema_version", "0.0.0")),
        )


class CheckpointFile:
    """Append-only JSONL of source_ids successfully written this run.

    Atomic-write semantics for individual appends:
      - Open with O_APPEND so writes are atomic at the syscall level
        (per the spec). Multiple writers can't interleave a single line.
      - Each line ends with \\n which serves as the commit marker for
        recovery — partial-write recovery skips trailing un-newlined lines.
    """

    def __init__(self, corpus_path: Path) -> None:
        self.path = corpus_path / CHECKPOINT_FILE_NAME

    def append(self, record: CheckpointRecord) -> None:
        line = record.to_jsonl() + "\n"
        # O_APPEND = atomic single-write (POSIX). UTF-8 only.
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        fd = os.open(str(self.path), flags, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
            # No fsync per write — checkpoint is recoverable transient state.
            # On crash we skip un-newline-terminated trailing lines.
        finally:
            os.close(fd)

    def existing_source_ids(self) -> Set[str]:
        """Return source_ids already recorded — caller skips these on resume.

        Tolerant to malformed lines: skips them with a warning.
        Tolerant to partial-line writes: lines without trailing \\n are
        silently dropped (write was crashed, card may not be on disk).
        """
        if not self.path.exists():
            return set()
        out: Set[str] = set()
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                for raw_line in f:
                    if not raw_line.endswith("\n"):
                        # Partial line — ignore.
                        continue
                    line = raw_line.rstrip("\n")
                    if not line:
                        continue
                    try:
                        rec = CheckpointRecord.from_jsonl(line)
                    except (json.JSONDecodeError, KeyError, ValueError) as e:
                        log.warning("Malformed checkpoint line skipped: %s (%s)", line[:80], e)
                        continue
                    out.add(rec.source_id)
        except OSError as e:
            log.warning("Could not read checkpoint %s: %s", self.path, e)
        return out

    def all_records(self) -> List[CheckpointRecord]:
        """Read all valid records — for diagnostics or in-test inspection."""
        if not self.path.exists():
            return []
        out: List[CheckpointRecord] = []
        with open(self.path, "r", encoding="utf-8") as f:
            for raw_line in f:
                if not raw_line.endswith("\n"):
                    continue
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    out.append(CheckpointRecord.from_jsonl(line))
                except (json.JSONDecodeError, KeyError, ValueError):
                    continue
        return out

    def archive(self, *, run_id: str) -> Optional[Path]:
        """Archive the live checkpoint to ~/.cache/xsensai/sync-checkpoints/{ts}-{run_id}.jsonl.

        Called on full successful run. The live file is REMOVED so the next
        run starts clean. Returns the archive path on success, None if there
        was nothing to archive (e.g., run wrote zero cards).
        """
        if not self.path.exists():
            return None
        try:
            archive_dir = _archive_dir()
            archive_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            dest = archive_dir / f"{ts}-{run_id[:8]}.jsonl"
            self.path.replace(dest)
            return dest
        except OSError as e:
            log.warning("Could not archive checkpoint %s: %s", self.path, e)
            return None

    @staticmethod
    def purge_old_archives(*, max_age_days: int = 30) -> int:
        """Delete archived checkpoints older than `max_age_days`. Return count."""
        archive_dir = _archive_dir()
        if not archive_dir.exists():
            return 0
        cutoff = time.time() - (max_age_days * 86400)
        count = 0
        for p in archive_dir.glob("*.jsonl"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
                    count += 1
            except OSError:
                pass
        return count


__all__ = ["CheckpointFile", "CheckpointRecord", "CHECKPOINT_FILE_NAME"]
