"""Sync log — privacy-aware JSONL append for /xsync runs.

Mirrors xsensai.xask.log convention: chmod 600 file, fcntl.flock for safe
append, mode env var (off | hash_only | full), retention purge.

Slice 4 sync runs are infrequent (~daily), so the log size stays tiny.
The `full` mode is mostly for empirical steering during the first weeks
post-merge; default is `hash_only` (no sensitive content captured).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator, List, Optional


log = logging.getLogger(__name__)


DEFAULT_RETENTION_DAYS = 90
DEFAULT_MODE = "hash_only"  # off | hash_only | full

_LOG_FILE_MODE = 0o600
_LOG_DIR_MODE = 0o700


@dataclass(frozen=True)
class SyncLogEntry:
    """One row of the /xsync run log."""

    ts: str
    run_id: str
    mode: str  # since-last-run | backlog | single | retry-failed
    outcome: str  # success | partial | failed | aborted
    n_new_cards: int
    extraction_inline: int
    extraction_pending: int
    threads_unfetched_this_run: int
    duration_ms: int
    sync_schema_version: str
    error_code: Optional[str] = None  # XSensaiError.code if outcome="failed"


def resolve_log_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "xsensai"


def resolve_log_file() -> Path:
    return resolve_log_dir() / "xsync-log.jsonl"


def resolve_mode() -> str:
    raw = os.environ.get("XSENSAI_XSYNC_LOG_MODE", DEFAULT_MODE).strip().lower()
    if raw not in {"off", "hash_only", "full"}:
        log.warning("XSENSAI_XSYNC_LOG_MODE=%r invalid; defaulting to %s", raw, DEFAULT_MODE)
        return DEFAULT_MODE
    return raw


def resolve_retention_days() -> int:
    raw = os.environ.get("XSENSAI_XSYNC_LOG_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        n = int(raw)
        return n if n >= 1 else DEFAULT_RETENTION_DAYS
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def append_log(entry: SyncLogEntry) -> Optional[Path]:
    """Append one row. Returns log file path on success, None when mode=off."""
    if resolve_mode() == "off":
        return None

    log_dir = resolve_log_dir()
    log_file = resolve_log_file()
    log_dir.mkdir(parents=True, exist_ok=True, mode=_LOG_DIR_MODE)
    try:
        os.chmod(log_dir, _LOG_DIR_MODE)
    except OSError:
        pass

    line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
    fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _LOG_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    try:
        os.chmod(log_file, _LOG_FILE_MODE)
    except OSError:
        pass
    return log_file


def iter_entries(log_file: Optional[Path] = None) -> Iterator[dict]:
    p = log_file or resolve_log_file()
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("xsync-log line %d malformed: %s", i, e)


def purge(retention_days: Optional[int] = None) -> int:
    """Delete entries older than retention_days. Returns count purged."""
    days = retention_days if retention_days is not None else resolve_retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    p = resolve_log_file()
    if not p.exists():
        return 0

    live_fd = os.open(p, os.O_RDONLY)
    try:
        fcntl.flock(live_fd, fcntl.LOCK_EX)
        survivors: List[dict] = []
        purged = 0
        for entry in iter_entries(p):
            ts = entry.get("ts")
            try:
                entry_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                survivors.append(entry)
                continue
            if entry_ts >= cutoff:
                survivors.append(entry)
            else:
                purged += 1

        tmp = p.with_suffix(".jsonl.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _LOG_FILE_MODE)
        try:
            for entry in survivors:
                os.write(fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
        try:
            os.chmod(p, _LOG_FILE_MODE)
        except OSError:
            pass
    finally:
        try:
            fcntl.flock(live_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(live_fd)
    return purged


def _cli() -> int:
    args = sys.argv[1:]
    if not args:
        print("usage: python -m xsensai.sync.log [purge | show]", file=sys.stderr)
        return 2
    cmd = args[0]
    if cmd == "purge":
        n = purge()
        print(json.dumps({"ok": True, "purged": n, "retention_days": resolve_retention_days()}))
        return 0
    if cmd == "show":
        for entry in iter_entries():
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())


__all__ = [
    "SyncLogEntry",
    "append_log",
    "iter_entries",
    "purge",
    "DEFAULT_MODE",
    "DEFAULT_RETENTION_DAYS",
]
