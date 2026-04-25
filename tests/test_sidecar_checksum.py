"""Tests for storage.sidecar: byte-exact round-trip + checksum verification."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.storage import sidecar


def test_round_trip_byte_exact(tmp_path: Path) -> None:
    raw = tmp_path / "x.raw.txt"
    payload = b"\x00\x01hello\xc3\xa9\nworld\n"
    raw.write_bytes(payload)
    bytes_back, checksum = sidecar.read_sidecar(raw)
    assert bytes_back == payload
    expected = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert checksum == expected


def test_verify_checksum_match() -> None:
    payload = b"hello"
    digest = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert sidecar.verify_checksum(payload, digest) is True


def test_verify_checksum_mismatch() -> None:
    payload = b"hello"
    wrong = "sha256:" + "f" * 64
    assert sidecar.verify_checksum(payload, wrong) is False


def test_verify_checksum_bit_flip() -> None:
    """A single bit flip in raw_bytes invalidates the checksum."""
    payload = b"hello"
    flipped = b"hellp"  # one byte different
    correct = "sha256:" + hashlib.sha256(payload).hexdigest()
    assert sidecar.verify_checksum(flipped, correct) is False


def test_read_sidecar_missing(tmp_path: Path) -> None:
    with pytest.raises(XSensaiError) as ei:
        sidecar.read_sidecar(tmp_path / "missing.raw.txt")
    assert ei.value.code == "DISK_WRITE_FAILED"
