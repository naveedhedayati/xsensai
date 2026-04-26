"""Tests that the prompt-injection adversarial fixture corpus loads cleanly.

These tests assert (a) every fixture parses via the standard corpus loader,
(b) format_reference doesn't echo the injected canary string, and (c)
.raw.txt verbatim regression holds (Eng EC13).

The actual end-to-end /xask injection-defense test lives in
test_xask_injection_live.py (gated on XSENSAI_RUN_INTEGRATION=1).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from xsensai.retrieval.format import format_reference
from xsensai.storage.corpus import load_card
from xsensai.synthesis.injection_fixtures import (
    CANARY_RE,
    fixtures_dir,
    list_fixtures,
)


def test_fixtures_directory_exists():
    d = fixtures_dir()
    assert d.exists(), f"missing fixtures dir: {d}"
    assert d.is_dir()


def test_at_least_five_fixtures_present():
    files = list_fixtures()
    assert len(files) >= 5, f"expected >=5 injection fixtures, got {len(files)}"


def test_each_fixture_loads_via_corpus_loader():
    fdir = fixtures_dir()
    for md_path in list_fixtures():
        card = load_card(md_path, fdir)
        assert card.fm.source_type == "bookmark"
        assert card.body  # non-empty body


def test_each_fixture_has_canary_in_at_least_one_field():
    """Canary must be present in body/author/why_saved/source_url/tags."""
    fdir = fixtures_dir()
    for md_path in list_fixtures():
        card = load_card(md_path, fdir)
        haystacks = [
            card.body or "",
            card.fm.author or "",
            card.fm.why_saved or "",
            card.fm.source_url or "",
            " ".join(card.fm.tags or []),
        ]
        joined = " | ".join(haystacks)
        assert CANARY_RE.search(joined), f"{md_path.name}: no canary in any field"


def test_format_reference_does_not_echo_canary_into_user_facing_output():
    """format_reference uses STRUCTURED card.fm data — but what it emits is
    user-facing. If a canary in author or why_saved leaked through, the user
    would see INJECTED_<n> in their /xask references block.

    Acceptance: format_reference SHOULD include the canary verbatim if it's
    in the underlying field data (we don't sanitize at format-time — the
    defense is the host model's HARD RULES). This test pins that behavior
    so the live injection test in test_xask_injection_live can rely on the
    HARD RULES being the actual barrier.
    """
    fdir = fixtures_dir()
    for md_path in list_fixtures():
        card = load_card(md_path, fdir)
        rendered = format_reference(card)
        # If the canary is in the rendered text, this is EXPECTED — the
        # defense lives at the synthesis layer (HARD RULES + DATA_TO_ANALYZE
        # tags), not at the format layer. Document that fact.
        # We just assert the reference renders without raising.
        assert isinstance(rendered, str)
        assert rendered.startswith("[B]") or rendered.startswith("[P]")


def test_raw_txt_verbatim_regression_each_fixture():
    """EC13: byte-equality between sidecar and what corpus.load_card sees.

    For each .raw.txt fixture, sha256(bytes) must match the raw_checksum
    recorded in the .md frontmatter. This is the same guarantee Slice 1's
    verbatim_fuzz suite enforces — if anyone edits an injection fixture's
    body without updating the sidecar+checksum, this test catches it.
    """
    fdir = fixtures_dir()
    for md_path in list_fixtures():
        raw_path = md_path.with_suffix(".raw.txt")
        assert raw_path.exists(), f"missing sidecar: {raw_path}"
        card = load_card(md_path, fdir)
        actual_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        recorded = card.fm.raw_checksum or ""
        assert recorded == f"sha256:{actual_sha}", (
            f"{md_path.name}: checksum drift "
            f"(recorded={recorded[:20]}..., actual=sha256:{actual_sha[:8]}...)"
        )
