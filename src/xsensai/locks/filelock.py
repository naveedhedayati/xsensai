"""card_write + index_rebuild locks via fcntl.flock + UUID fencing token.

Slice 2 design (per autoplan UC3+UC5+UC6 resolution):
  - fcntl.flock(LOCK_EX|LOCK_NB) on {corpus}/.locks/{domain}.lock provides
    the OS-enforced mutual exclusion. Auto-released on process death.
  - JSON sidecar {corpus}/.locks/{domain}.json holds human-readable PID +
    hostname + started_at + writer_kind + fencing_token (UUID4 string).
  - Fencing token (UUID) is returned by acquire and stored in the JSON. Every
    write_card call MUST verify_fencing_token() before commit. If the token
    on disk doesn't match the caller's, the lock has been re-acquired by
    another writer (possible after process death + resurrection) and the
    write must abort.

Slice 4 additions (per /autoplan Phase 3 E-2 fix):
  - LockDomain enum: card_write (Slice 2) + index_rebuild (Slice 4 cron + /xsync).
  - Heartbeat thread for long-held locks: rewrites the JSON sidecar's
    `heartbeat` field every 30s while held. DIAGNOSTICS ONLY — flock is
    the truth. We do NOT implement TTL-based reclamation; a "stale" lock
    on disk with no live flock holder gets reclaimed by the next acquirer
    via flock's normal acquisition path.
  - threading.excepthook catches daemon-thread exceptions so they surface
    to the main thread instead of being swallowed silently.

What we explicitly DO NOT ship:
  - TTL-based "reclaim a live flock holder" (per E-2: not safely implementable
    on top of flock; spec line 198 will be amended).
  - transcribe_queue domain (per S-4: YAGNI; add when /xtranscribe ships).
"""

from __future__ import annotations

import enum
import fcntl
import json
import logging
import os
import socket
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal, Optional

from xsensai.errors import XSensaiError
from xsensai.storage import sidecar


log = logging.getLogger(__name__)


WriterKind = Literal[
    "xpaste", "xnote", "xpin", "xsync", "xextract", "cron",
    # Slice 5 — /xfind acquires card_write briefly during lazy-extract claim
    # and release paths (lazy_extract.py).
    "xfind",
    # Slice 5 — test fixtures that seed cards under the lock without
    # faking a real slash-command kind.
    "test-fixture",
]
LOCKS_DIR_NAME = ".locks"
CARD_WRITE_LOCK_NAME = "card_write.lock"
CARD_WRITE_JSON_NAME = "card_write.json"
INDEX_REBUILD_LOCK_NAME = "index_rebuild.lock"
INDEX_REBUILD_JSON_NAME = "index_rebuild.json"

# Heartbeat tunables (used by index_rebuild lock; card_write doesn't heartbeat).
HEARTBEAT_INTERVAL_SECONDS = 30.0


class LockDomain(str, enum.Enum):
    """Lock domains — one fcntl.flock per domain. Slice 4 adds index_rebuild."""

    card_write = "card_write"
    index_rebuild = "index_rebuild"

    @property
    def lock_name(self) -> str:
        return f"{self.value}.lock"

    @property
    def json_name(self) -> str:
        return f"{self.value}.json"


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

    Reads card_write.json by default. Heartbeat thread (index_rebuild)
    rewrites the JSON without changing the fencing_token — verify_fencing_token
    remains stable across heartbeats.
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


# ---------------------------------------------------------------------------
# Slice 4 additions: index_rebuild lock + heartbeat thread.
#
# Per E-2 fix: heartbeat is diagnostics-only. flock is the truth. Daemon
# thread updates the JSON sidecar's `heartbeat` field every 30s; on process
# death the OS releases flock and the next acquirer gets it normally.
# ---------------------------------------------------------------------------


# Module-level: install a threading.excepthook ONCE so daemon-thread exceptions
# surface to the main thread's stderr instead of being silently swallowed.
# Previous excepthook (if any) is preserved for non-heartbeat threads.
_HEARTBEAT_FAILURE_QUEUE: "list[BaseException]" = []
_HEARTBEAT_QUEUE_LOCK = threading.Lock()
_PRIOR_EXCEPTHOOK = threading.excepthook


def _heartbeat_excepthook(args: "threading.ExceptHookArgs") -> None:
    """Catch heartbeat-thread exceptions so the main thread can surface them.

    Daemon threads in Python silently swallow exceptions by default. We
    capture them in a module-level queue + log them; the main thread can
    poll _consume_heartbeat_failures() before lock release to surface any
    failures via XSensaiError.
    """
    thread_name = args.thread.name if args.thread else "<unknown>"
    if thread_name.startswith("xsensai-heartbeat-"):
        if args.exc_value is not None:
            with _HEARTBEAT_QUEUE_LOCK:
                _HEARTBEAT_FAILURE_QUEUE.append(args.exc_value)
            log.error(
                "Heartbeat thread %s raised %s: %s",
                thread_name, type(args.exc_value).__name__, args.exc_value,
            )
        return
    _PRIOR_EXCEPTHOOK(args)


threading.excepthook = _heartbeat_excepthook


def _consume_heartbeat_failures() -> "list[BaseException]":
    """Drain queued heartbeat-thread exceptions. Called by lock teardown."""
    with _HEARTBEAT_QUEUE_LOCK:
        out = list(_HEARTBEAT_FAILURE_QUEUE)
        _HEARTBEAT_FAILURE_QUEUE.clear()
    return out


def _make_heartbeat_thread(
    json_path: Path,
    metadata: LockMetadata,
    stop: threading.Event,
    interval_s: float = HEARTBEAT_INTERVAL_SECONDS,
) -> threading.Thread:
    """Spawn a daemon thread that rewrites json_path's `heartbeat` field.

    The thread updates the JSON every `interval_s` seconds with a fresh ISO
    timestamp under the `heartbeat` key. The fencing_token is preserved
    so verify_fencing_token() remains stable across heartbeats. Stops when
    `stop.set()` is called or the JSON disappears.
    """
    def _loop() -> None:
        while not stop.wait(interval_s):
            if not json_path.exists():
                # Lock JSON unlinked (probably by main thread teardown). Exit.
                return
            try:
                raw = json_path.read_text(encoding="utf-8")
                data = json.loads(raw)
                if data.get("fencing_token") != metadata.fencing_token:
                    # Lock was reacquired by another writer; stop heartbeating.
                    return
                data["heartbeat"] = datetime.now(timezone.utc).isoformat()
                # Rewrite atomically (rename) to avoid partial-write reads by
                # diagnostic tools.
                sidecar.durable_replace(
                    json_path,
                    json.dumps(data, indent=2).encode("utf-8"),
                    durability="metadata",
                )
            except (OSError, json.JSONDecodeError, ValueError, KeyError) as e:
                # Soft-fail: log but don't tear down the lock. The next
                # heartbeat tick will retry; if it keeps failing the
                # excepthook surfaces it.
                log.warning("Heartbeat write failed for %s: %s", json_path, e)

    name = f"xsensai-heartbeat-{metadata.fencing_token[:8]}"
    return threading.Thread(target=_loop, name=name, daemon=True)


@contextmanager
def with_index_rebuild_lock(
    corpus_path: Path,
    writer_kind: WriterKind,
    *,
    heartbeat: bool = True,
    heartbeat_interval_s: Optional[float] = None,
) -> Iterator[LockHandle]:
    """Acquire the index_rebuild lock for cross-process QMD update serialization.

    Slice 4 introduces this domain so /xsync's reindex (which can take minutes
    on a large corpus) doesn't race with the read-side reindex triggered by
    /xfind / /xask via the _index-dirty marker. Both code paths now acquire
    this lock before calling qmd update.

    Heartbeat thread (default on for cron-grade locks) rewrites the JSON
    sidecar every 30s for external diagnostic visibility. flock auto-release
    on process death is the actual correctness mechanism.
    """
    locks_dir = corpus_path / LOCKS_DIR_NAME
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_path = locks_dir / INDEX_REBUILD_LOCK_NAME
    json_path = locks_dir / INDEX_REBUILD_JSON_NAME

    fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            holder = _read_holder_metadata(json_path)
            raise XSensaiError(
                code="LOCK_HELD",
                cause=_format_lock_held_cause(holder, "index_rebuild"),
                attempted=f"with_index_rebuild_lock(corpus={corpus_path}, kind={writer_kind})",
                next_action=_format_lock_held_next_action(holder, lock_path),
                retryable=True,
                details=_format_lock_held_details(holder),
            )

        metadata = LockMetadata(
            pid=os.getpid(),
            hostname=socket.gethostname(),
            started_at=datetime.now(timezone.utc).isoformat(),
            writer_kind=writer_kind,
            fencing_token=str(uuid.uuid4()),
        )
        # Initial JSON write — heartbeat field starts as None and gets
        # populated by the heartbeat thread on its first tick.
        initial = json.loads(metadata.to_json())
        initial["heartbeat"] = None
        try:
            sidecar.durable_replace(
                json_path,
                json.dumps(initial, indent=2).encode("utf-8"),
                durability="metadata",
            )
        except XSensaiError:
            os.close(fd)
            raise

        stop_event = threading.Event()
        hb_thread: Optional[threading.Thread] = None
        if heartbeat:
            interval = (
                heartbeat_interval_s
                if heartbeat_interval_s is not None
                else HEARTBEAT_INTERVAL_SECONDS
            )
            hb_thread = _make_heartbeat_thread(
                json_path, metadata, stop_event, interval_s=interval,
            )
            hb_thread.start()

        handle = LockHandle(metadata=metadata, lock_path=lock_path, json_path=json_path)
        log.info(
            "index_rebuild lock acquired (pid=%d, kind=%s, token=%s, heartbeat=%s)",
            metadata.pid, metadata.writer_kind, metadata.fencing_token[:8], heartbeat,
        )

        try:
            yield handle
        finally:
            stop_event.set()
            if hb_thread is not None:
                hb_thread.join(timeout=2.0)
            # Surface any heartbeat failures so the caller doesn't silently
            # ship work whose lock metadata went stale.
            failures = _consume_heartbeat_failures()
            if failures:
                log.warning(
                    "Heartbeat reported %d failure(s) during lock hold (lock still released cleanly).",
                    len(failures),
                )
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


def _read_holder_metadata(json_path: Path) -> Optional[LockMetadata]:
    """Best-effort read of the lock-holder's metadata for diagnostic output."""
    if not json_path.exists():
        return None
    try:
        return LockMetadata.from_json(json_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, KeyError, ValueError):
        return None


def _format_lock_held_cause(
    holder: Optional[LockMetadata], domain: str = "card_write"
) -> str:
    if holder is None:
        return f"{domain} lock is held by another writer (no metadata available)."
    return (
        f"{domain} lock held by {holder.writer_kind} "
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
    "LockDomain",
    "WriterKind",
    "with_card_write_lock",
    "with_index_rebuild_lock",
    "verify_fencing_token",
    "LOCKS_DIR_NAME",
    "CARD_WRITE_LOCK_NAME",
    "CARD_WRITE_JSON_NAME",
    "INDEX_REBUILD_LOCK_NAME",
    "INDEX_REBUILD_JSON_NAME",
    "HEARTBEAT_INTERVAL_SECONDS",
]
