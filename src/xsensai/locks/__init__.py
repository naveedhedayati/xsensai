"""Concurrency primitives for x-sensai writes.

Slice 2 ships card_write only. Slice 4 (cron sync) adds index_rebuild +
heartbeat machinery. Public API:
  - with_lock(corpus_path, writer_kind) — context manager, returns LockHandle
  - LockHandle.token — UUID fencing token; pass to verify_fencing_token() before commit
  - verify_fencing_token(corpus_path, token) — assert lock still held by THIS owner

Implementation: fcntl.flock(LOCK_EX|LOCK_NB) on {corpus}/.locks/card_write.lock
+ JSON sidecar {corpus}/.locks/card_write.json for human-readable PID/host
metadata. Fencing token (UUID) prevents stale-owner-continues-writing under
the case where the OS releases the flock (process death) but the dead
writer's in-flight work continues.
"""

from xsensai.locks.filelock import (
    HEARTBEAT_INTERVAL_SECONDS,
    LockDomain,
    LockHandle,
    LockMetadata,
    verify_fencing_token,
    with_card_write_lock,
    with_index_rebuild_lock,
)

__all__ = [
    "HEARTBEAT_INTERVAL_SECONDS",
    "LockDomain",
    "LockHandle",
    "LockMetadata",
    "verify_fencing_token",
    "with_card_write_lock",
    "with_index_rebuild_lock",
]
