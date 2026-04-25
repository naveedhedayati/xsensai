"""Sidecar (.raw.txt) byte-exact I/O + sha256 verification.

The verbatim guarantee for v2 cards lives in card.raw.txt (byte-exact tweet
or paste source). This module reads sidecar bytes and computes/verifies
sha256 against the frontmatter's raw_checksum.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Tuple

from xsensai.errors import XSensaiError


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


__all__ = ["read_sidecar", "verify_checksum"]
