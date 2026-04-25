"""Verbatim fuzz: byte-exact round-trip through .raw.txt for adversarial inputs.

Per autoplan T4 cherry-pick: trimmed from 9 fixtures to 3 critical ones
(triple-dash-in-body, triple-backticks, ## Content literal) — the cases
that actually break sidecar/markdown round-trip. Drop NFC/NFD, surrogate
pairs, ZWSP, CRLF mixed (add back if a real card ever fails them).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from xsensai.storage import sidecar


@pytest.mark.parametrize("fixture_name", [
    "triple_dash_in_body.raw.txt",
    "triple_backticks.raw.txt",
    "content_heading_literal.raw.txt",
])
def test_verbatim_round_trip(fuzz_fixture_dir: Path, tmp_path: Path, fixture_name: str) -> None:
    """Read fixture, write to temp, read back, assert byte equality + checksum."""
    src = fuzz_fixture_dir / fixture_name
    original_bytes = src.read_bytes()

    dst = tmp_path / fixture_name
    dst.write_bytes(original_bytes)

    bytes_back, checksum = sidecar.read_sidecar(dst)
    assert bytes_back == original_bytes
    expected = "sha256:" + hashlib.sha256(original_bytes).hexdigest()
    assert checksum == expected


def test_emoji_zwj_round_trip(tmp_path: Path) -> None:
    """ZWJ family emoji should not be split or corrupted."""
    payload = "Family: 👨‍👩‍👧 / Pride: 🏳️‍🌈\n".encode("utf-8")
    raw = tmp_path / "emoji.raw.txt"
    raw.write_bytes(payload)
    back, checksum = sidecar.read_sidecar(raw)
    assert back == payload
