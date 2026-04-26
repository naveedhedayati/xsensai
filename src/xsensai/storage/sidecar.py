"""Sidecar (.raw.txt) byte-exact I/O + sha256 verification + atomic writes.

Slice 1: read_sidecar + verify_checksum (read path).
Slice 2: durable_replace + write_sidecar_atomic + iCloud detection (write path).

The verbatim guarantee for v2 cards lives in card.raw.txt (byte-exact tweet
or paste source). durable_replace() is the centralized atomic-write helper
used by sidecar writes, .md writes, lock-JSON writes, and inbox writes —
every place where partial state on disk would be a bug.

macOS APFS note: os.fsync on a directory fd raises EINVAL on macOS. We
attempt fcntl.F_FULLFSYNC on the file (POSIX-blessed APFS-aware sync), then
os.fsync(parent_dir_fd) for cross-platform parity, swallowing only EINVAL
with a warning. iCloud-synced corpus paths get a startup warning since
os.replace atomicity does not survive iCloud's file-provider interposition.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import logging
import os
import platform
from pathlib import Path
from typing import Tuple

from xsensai.errors import XSensaiError

log = logging.getLogger(__name__)


_IS_DARWIN = platform.system() == "Darwin"
_F_FULLFSYNC_ATTEMPTED = False  # one-time log throttle


def read_sidecar(raw_path: Path) -> Tuple[bytes, str]:
    """Read a sidecar file and return (bytes, sha256_hex_with_prefix).

    Returns tuple of (raw_bytes, "sha256:" + hex_digest). Raises
    XSensaiError(DISK_WRITE_FAILED) on read failure.
    """
    try:
        raw_bytes = raw_path.read_bytes()
    except (OSError, FileNotFoundError) as e:
        raise XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Could not read sidecar: {raw_path}",
            attempted=f"read_sidecar({raw_path})",
            next_action="Check the file exists and is readable; check disk health.",
            retryable=False,
            details=str(e),
        ) from e
    digest = hashlib.sha256(raw_bytes).hexdigest()
    return raw_bytes, f"sha256:{digest}"


def verify_checksum(raw_bytes: bytes, expected: str) -> bool:
    """Return True if sha256(raw_bytes) matches the expected 'sha256:...' string."""
    digest = "sha256:" + hashlib.sha256(raw_bytes).hexdigest()
    return digest == expected


def compute_checksum(raw_bytes: bytes) -> str:
    """Compute the canonical 'sha256:<hex>' checksum for given bytes."""
    return f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"


def durable_replace(target: Path, content: bytes, durability: str = "full") -> None:
    """Atomically write `content` to `target` with the requested durability tier.

    Per /review F17: not every write needs full power-loss durability.
      - durability="full" (default): fsync + F_FULLFSYNC on macOS + parent
        dir fsync. Power-loss safe. Use for sidecars + .md cards.
      - durability="metadata": fsync(file) + os.replace + parent dir fsync.
        Skip F_FULLFSYNC. Use for transient state like lock JSON metadata
        (the OS ensures rename atomicity; we don't need APFS hardware barrier
        for state that's already best-effort recovery).

    Recipe (full):
      1. Open target.with_suffix(target.suffix + '.tmp') with O_WRONLY|O_CREAT|O_TRUNC.
      2. Write bytes; fsync(file); close.
      3. On macOS + full durability: open the file again read-only and call
         fcntl.F_FULLFSYNC for APFS-aware durability.
      4. os.replace(tmp, target) — atomic rename on the same volume.
      5. fsync the parent directory fd. On macOS, swallow EINVAL with one-time
         warning (POSIX dir fsync is not universally supported).

    Crash-injection hook: if XSENSAI_CRASH_AFTER_STEP=N is set in the env,
    raise XSensaiError(DISK_WRITE_FAILED) after step N. Used by deterministic
    atomic-write tests instead of timing-based SIGKILL races.

    Raises XSensaiError(DISK_WRITE_FAILED) on any irrecoverable I/O error;
    the .tmp path is included in details so a stuck .tmp can be cleaned up.
    """
    if durability not in ("full", "metadata"):
        raise ValueError(f"durability must be 'full' or 'metadata', got {durability!r}")
    target = Path(target)
    crash_after = _read_crash_step()
    tmp = target.with_suffix(target.suffix + ".tmp")

    try:
        # Step 1+2: write tmp + fsync.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, content)
            os.fsync(fd)
        finally:
            os.close(fd)
        _crash_check(crash_after, 1, tmp)

        # Step 3: F_FULLFSYNC on macOS for APFS power-loss durability.
        # Skip for "metadata" tier (lock JSON, etc. — recoverable state).
        if _IS_DARWIN and durability == "full":
            _try_fullfsync(tmp)
        _crash_check(crash_after, 2, tmp)

        # Step 4: atomic rename.
        try:
            os.replace(str(tmp), str(target))
        except OSError as e:
            if e.errno == errno.EXDEV:
                raise XSensaiError(
                    code="DISK_WRITE_FAILED",
                    cause=f"Cross-device rename: {tmp} → {target}",
                    attempted=f"durable_replace({target})",
                    next_action=(
                        "tmp file is on a different filesystem than target. "
                        "Most often this means the corpus directory is on a "
                        "different mount than $TMPDIR (e.g., iCloud-synced "
                        "vault). Set the corpus to a non-synced local path."
                    ),
                    retryable=False,
                    details=f"errno={e.errno}, tmp={tmp}",
                ) from e
            raise
        _crash_check(crash_after, 3, target)

        # Step 5: parent-dir fsync.
        _fsync_dir(target.parent)
        _crash_check(crash_after, 4, target)
    except XSensaiError:
        raise
    except OSError as e:
        # Best-effort cleanup of orphan tmp; don't mask the original error.
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError:
            pass
        raise XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Write failed: {target}",
            attempted=f"durable_replace({target})",
            next_action=(
                f"Check disk space and write permissions. Orphan tmp may exist at {tmp}."
            ),
            retryable=False,
            details=f"errno={e.errno}, error={e}",
        ) from e


def write_sidecar_atomic(raw_path: Path, raw_bytes: bytes) -> str:
    """Write raw_bytes to raw_path atomically. Returns 'sha256:...' of the bytes.

    Per UC5 (immutable per-version sidecars): callers should NOT reuse a
    raw_path for mutated content — use a generation-suffixed path so the old
    sidecar stays unchanged. write_sidecar_atomic itself does not enforce
    this; it's the caller's contract.
    """
    durable_replace(raw_path, raw_bytes)
    return compute_checksum(raw_bytes)


def is_likely_icloud_path(path: Path) -> bool:
    """Heuristic: does this path look like it lives in an iCloud-synced
    location? Used at startup to warn about atomicity assumption violations.

    Catches the common cases (~/Library/Mobile Documents/, paths under
    ~/Documents on machines with "Desktop & Documents" iCloud enabled) but
    is conservative — false negatives (no warning when warning was due) are
    safer than false positives (annoying warning on every startup).
    """
    p = path.expanduser().resolve()
    parts = p.parts
    # Direct iCloud Drive container.
    if any("Mobile Documents" in part for part in parts):
        return True
    if any("CloudDocs" in part for part in parts):
        return True
    # ~/Documents or ~/Desktop with iCloud sync enabled writes a marker file
    # at the user's home root. We don't check the marker (privileged); we
    # just warn weakly when the path is under ~/Documents or ~/Desktop.
    home = Path.home().resolve()
    try:
        rel = p.relative_to(home)
        first = rel.parts[0] if rel.parts else ""
        if first in ("Documents", "Desktop") and (home / first / ".com.apple.containermanagerd.metadata.plist").exists():
            return True
    except ValueError:
        pass
    return False


def _try_fullfsync(path: Path) -> None:
    """Attempt fcntl.F_FULLFSYNC on a freshly opened fd. macOS-only."""
    global _F_FULLFSYNC_ATTEMPTED
    full_fsync_const = getattr(fcntl, "F_FULLFSYNC", None)
    if full_fsync_const is None:
        return
    try:
        fd = os.open(str(path), os.O_RDONLY)
        try:
            fcntl.fcntl(fd, full_fsync_const)
        finally:
            os.close(fd)
    except OSError as e:
        # Don't fail the whole write on F_FULLFSYNC errors — some filesystems
        # (network mounts, tmpfs) don't support it. Log once.
        if not _F_FULLFSYNC_ATTEMPTED:
            log.warning(
                "F_FULLFSYNC unavailable on %s (errno=%d); falling back to fsync only. "
                "Power-loss durability is best-effort.",
                path.parent,
                e.errno,
            )
            _F_FULLFSYNC_ATTEMPTED = True


def _fsync_dir(parent: Path) -> None:
    """fsync the parent directory fd; swallow macOS EINVAL with one-time warn."""
    try:
        dir_fd = os.open(str(parent), os.O_RDONLY)
    except OSError as e:
        log.warning("Could not open parent dir for fsync: %s (errno=%d)", parent, e.errno)
        return
    try:
        os.fsync(dir_fd)
    except OSError as e:
        if e.errno == errno.EINVAL:
            # macOS is the common case; we already F_FULLFSYNC'd the file itself.
            log.debug("os.fsync(parent_dir) returned EINVAL on %s — relying on F_FULLFSYNC.", parent)
        else:
            log.warning("os.fsync(parent_dir) failed on %s: errno=%d", parent, e.errno)
    finally:
        os.close(dir_fd)


def _read_crash_step() -> int:
    """Parse XSENSAI_CRASH_AFTER_STEP for deterministic crash injection.

    Returns 0 (= no injection) if unset or malformed. Used by atomic-write
    tests to assert post-crash state without flaky SIGKILL timing.
    """
    raw = os.environ.get("XSENSAI_CRASH_AFTER_STEP")
    if not raw:
        return 0
    try:
        return int(raw)
    except ValueError:
        return 0


def _crash_check(crash_after: int, current_step: int, path: Path) -> None:
    """If env says crash after step N and we just finished step N, raise.

    Tests set XSENSAI_CRASH_AFTER_STEP=1 to crash before rename, =3 to crash
    before parent dir fsync, etc. Production never sets this.
    """
    if crash_after and current_step == crash_after:
        raise XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Crash injection: stopped after step {current_step} (XSENSAI_CRASH_AFTER_STEP={crash_after})",
            attempted=f"durable_replace({path})",
            next_action="This is a test injection; not a real failure.",
            retryable=False,
            details=f"step={current_step}, target={path}",
        )


__all__ = [
    "read_sidecar",
    "verify_checksum",
    "compute_checksum",
    "durable_replace",
    "write_sidecar_atomic",
    "is_likely_icloud_path",
]
