"""card_write lock via fcntl.flock + UUID fencing token.

Slice 2 design (per autoplan UC3+UC5+UC6 resolution):
  - fcntl.flock(LOCK_EX|LOCK_NB) on {corpus}/.locks/card_write.lock provides
    the OS-enforced mutual exclusion. Auto-released on process death.
  - JSON sidecar {corpus}/.locks/card_write.json holds human-readable PID +
    hostname + started_at + writer_kind + fencing_token (UUID4 string).
  - Fencing token (UUID) is returned by acquire and stored in the JSON. Every
    write_card call MUST verify_fencing_token() before commit. If the token
    on disk doesn't match the caller's, the lock has been re-acquired by
    another writer (possible after process death + resurrection) and the
    write must abort.

What we explicitly do NOT ship in Slice 2:
  - TTL math + heartbeat thread (Slice 4 cron sync ships these — short-hold
    /xpaste + /xnote + /xpin don't need them; flock on process death covers
    the equivalent failure mode).
  - index_rebuild + transcribe_queue lock domains (Slice 4).
  - rename-based reclamation race (the original draft's bug class — fcntl
    eliminates it).
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import platform
import socket
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

from xsensai.errors import XSensaiError
from xsensai.storage import sidecar


log = logging.getLogger(__name__)


WriterKind = Literal["xpaste", "xnote", "xpin", "xsync", "cron"]
LOCKS_DIR_NAME = ".locks"
CARD_WRITE_LOCK_NAME = "card_write.lock"
CARD_WRITE_JSON_NAME = "card_write.json"


@dataclass(frozen=True)
class LockMetadata:
    """Human-readable lock holder info, persisted in the JSON sidecar."""

    pid: int
    hostname: str
    started_at: str  # ISO 8601
    writer_kind: WriterKind
    fencing_token: str  # UUID4 string

    def to_json(self) -> str:
        return json.dumps(
            {
                "pid": self.pid,
                "hostname": self.hostname,
                "started_at": self.started_at,
                "writer_kind": self.writer_kind,
                "fencing_token": self.fencing_token,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, raw: str) -> "LockMetadata":
        data = json.loads(raw)
        return cls(
            pid=int(data["pid"]),
            hostname=str(data["hostname"]),
            started_at=str(data["started_at"]),
            writer_kind=data["writer_kind"],
            fencing_token=str(data["fencing_token"]),
        )


@dataclass(frozen=True)
class LockHandle:
    """Active card_write lock. Caller passes .token to verify_fencing_token()
    before each commit to defend against stale-owner-continues-writing."""

    metadata: LockMetadata
    lock_path: Path
    json_path: Path

    @property
    def token(self) -> str:
        return self.metadata.fencing_token


@contextmanager
def with_card_write_lock(
    corpus_path: Path,
    writer_kind: WriterKind,
) -> Iterator[LockHandle]:
    """Acquire the card_write lock or raise XSensaiError(LOCK_HELD).

    Atomicity: fcntl.flock(LOCK_EX|LOCK_NB) is the truth. JSON sidecar is
    metadata + fencing token. On context exit, JSON is unlinked (best-effort)
    and the lock fd is released (auto on close).

    On contention: read the existing JSON for the holder's PID/host and
    surface in the LOCK_HELD error so the user can decide whether to wait or
    manually clear (e.g., `rm {corpus}/.locks/card_write.lock` if they know
    the holder is dead).
    """
    locks_dir = corpus_path / LOCKS_DIR_NAME
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / CARD_WRITE_LOCK_NAME
    json_path = locks_dir / CARD_WRITE_JSON_NAME

    # Open the lock file (create if needed) and try to flock it.
    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # Lock is held by someone else. Read the JSON for diagnostics.
            os.close(fd)
            holder = _read_holder_metadata(json_path)
            raise XSensaiError(
                code="LOCK_HELD",
                cause=_format_lock_held_cause(holder),
                attempted=f"with_card_write_lock(corpus={corpus_path}, kind={writer_kind})",
                next_action=_format_lock_held_next_action(holder, lock_path),
                retryable=True,
                details=_format_lock_held_details(holder),
            )

        # We have the flock. Write fresh metadata + token.
        metadata = LockMetadata(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=datetime.now(timezone.utc).isoformat(),
            writer_kind=writer_kind,
            fencing_token=str(uuid.uuid4()),
        )
        try:
            # Per /review F17: lock JSON is transient/recoverable metadata,
            # not durable state. Skip F_FULLFSYNC; the OS still gives us
            # rename atomicity which is all we need.
            sidecar.durable_replace(
                json_path, metadata.to_json().encode("utf-8"),
                durability="metadata",
            )
        except XSensaiError:
            # If JSON write fails, release the flock and re-raise — we don't
            # want to hold a lock with no diagnostic metadata.
            os.close(fd)
            raise

        handle = LockHandle(
            metadata=metadata,
            lock_path=lock_path,
            json_path=json_path,
        )
        log.info(
            "card_write lock acquired (pid=%d, kind=%s, token=%s)",
            metadata.pid, metadata.writer_kind, metadata.fencing_token[:8],
        )

        try:
            yield handle
        finally:
            # Best-effort cleanup. Even if these fail, the OS releases the
            # flock when fd is closed.
            try:
                if json_path.exists():
                    json_path.unlink()
            except OSError as e:
                log.warning("Could not unlink lock JSON %s: %s", json_path, e)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def verify_fencing_token(corpus_path: Path, expected_token: str) -> bool:
    """Return True if the on-disk lock still belongs to expected_token.

    Called by write_card immediately before commit. False means the lock
    has been re-acquired by another writer (possibly after our process
    suspended/resumed past the OS's flock release on death). Caller MUST
    abort the write — the new lock owner's transaction is in flight.
    """
    json_path = corpus_path / LOCKS_DIR_NAME / CARD_WRITE_JSON_NAME
    if not json_path.exists():
        return False
    try:
        raw = json_path.read_text(encoding="utf-8")
        on_disk = LockMetadata.from_json(raw)
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return False
    return on_disk.fencing_token == expected_token


def _read_holder_metadata(json_path: Path) -> Optional[LockMetadata]:
    """Best-effort read of the lock-holder's metadata for diagnostic output."""
    if not json_path.exists():
        return None
    try:
        return LockMetadata.from_json(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _format_lock_held_cause(holder: Optional[LockMetadata]) -> str:
    if holder is None:
        return "card_write lock is held by another writer (no metadata available)."
    return (
        f"card_write lock held by {holder.writer_kind} "
        f"(pid {holder.pid} on {holder.hostname}, started {holder.started_at})."
    )


def _format_lock_held_next_action(holder: Optional[LockMetadata], lock_path: Path) -> str:
    if holder is None:
        return (
            "Wait a few seconds and retry. If the lock persists, the holder may have "
            f"crashed without releasing — manually clear with: rm {lock_path}"
        )
    return (
        f"Wait for {holder.writer_kind} to finish (typically a few seconds). "
        f"If you know that PID is dead, manually clear with: rm {lock_path}"
    )


def _format_lock_held_details(holder: Optional[LockMetadata]) -> str:
    if holder is None:
        return ""
    return (
        f"Lock holder: kind={holder.writer_kind} pid={holder.pid} "
        f"host={holder.hostname} started_at={holder.started_at} "
        f"token={holder.fencing_token}"
    )


__all__ = [
    "LockHandle",
    "LockMetadata",
    "WriterKind",
    "with_card_write_lock",
    "verify_fencing_token",
    "LOCKS_DIR_NAME",
    "CARD_WRITE_LOCK_NAME",
    "CARD_WRITE_JSON_NAME",
]
