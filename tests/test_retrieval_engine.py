"""Tests for retrieval.engine: pin dominance, end-to-end search.

Engine integration with real QMD is gated on XSENSAI_RUN_INTEGRATION=1
(per autoplan H7). Pin-dominance tests use mocked qmd.query().
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List

import pytest

from xsensai.errors import XSensaiError
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.retrieval import engine, qmd, scoring


_INTEGRATION = os.environ.get("XSENSAI_RUN_INTEGRATION") == "1"


def _make_hit(qmd_score: float, recency: float, pinned: bool, name: str = "x") -> engine.SearchHit:
    cf = CardFrontmatter(
        source_type="paste",
        author="self",
        captured=datetime.now(timezone.utc),
        pinned=pinned,
        raw_path="./x.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    card = LoadedCard(fm=cf, body="", raw_bytes=b"", md_path=Path(f"{name}.md"))
    return engine.SearchHit(
        card=card,
        qmd_score=qmd_score,
        recency=recency,
        combined_score=qmd_score * recency,
    )


def test_pin_dominance_drops_irrelevant_pins() -> None:
    """5 pinned + 5 unpinned, low pinned scores: most pinned dropped."""
    hits = [
        _make_hit(0.9, 1.0, pinned=False, name=f"unpinned-{i}") for i in range(5)
    ] + [
        _make_hit(0.10, 1.0, pinned=True, name=f"pinned-{i}") for i in range(5)
    ]
    out = engine._apply_pin_dominance(hits, limit=5)
    pinned_kept = [h for h in out if h.card.fm.pinned]
    # All pinned scored 0.10; threshold is 0.5*0.9 = 0.45 → all dropped
    assert len(pinned_kept) == 0


def test_pin_dominance_keeps_relevant_pins_within_quota() -> None:
    """High-scoring pinned cards kept, but capped at ceil(limit/2)."""
    hits = [
        _make_hit(0.9, 1.0, pinned=False, name=f"unpinned-{i}") for i in range(5)
    ] + [
        _make_hit(0.85, 1.0, pinned=True, name=f"pinned-{i}") for i in range(5)
    ]
    out = engine._apply_pin_dominance(hits, limit=5)
    pinned_kept = [h for h in out if h.card.fm.pinned]
    # All 5 pins are above 0.5*0.9=0.45, but quota cap = ceil(5/2)=3
    assert len(pinned_kept) == 3


def test_pin_dominance_no_unpinned_keeps_all_pins() -> None:
    """If there are no unpinned cards, all pinned are kept (no baseline)."""
    hits = [_make_hit(0.5, 1.0, pinned=True, name=f"p-{i}") for i in range(3)]
    out = engine._apply_pin_dominance(hits, limit=5)
    assert len(out) == 3


def test_pin_dominance_no_pinned_passthrough() -> None:
    hits = [_make_hit(0.5, 1.0, pinned=False, name=f"u-{i}") for i in range(3)]
    out = engine._apply_pin_dominance(hits, limit=5)
    assert out == hits


# Integration tests below — real QMD required.

@pytest.fixture
def qmd_test_collection(cards_fixture_dir: Path):
    """Create + tear down a QMD collection pointed at the fixture cards."""
    if not _INTEGRATION:
        pytest.skip("XSENSAI_RUN_INTEGRATION not set")
    import subprocess
    import shutil
    qmd_bin = os.environ.get("XSENSAI_QMD_PATH") or shutil.which("qmd")
    if not qmd_bin:
        pytest.skip("qmd binary not found ($XSENSAI_QMD_PATH / PATH)")
    subprocess.run(
        [qmd_bin, "collection", "remove", "xsensai-test"],
        capture_output=True, check=False,
    )
    res = subprocess.run(
        [qmd_bin, "collection", "add", str(cards_fixture_dir),
         "--name", "xsensai-test", "--mask", "*.md"],
        capture_output=True, text=True, check=False,
    )
    if res.returncode != 0:
        pytest.fail(f"qmd collection add failed: {res.stderr}")
    yield "xsensai-test"
    subprocess.run(
        [qmd_bin, "collection", "remove", "xsensai-test"],
        capture_output=True, check=False,
    )


@pytest.mark.skipif(not _INTEGRATION, reason="XSENSAI_RUN_INTEGRATION not set")
async def test_search_against_fixture_corpus(qmd_test_collection, cards_fixture_dir: Path, monkeypatch) -> None:
    """End-to-end: query 'startups' should hit the paulg card."""
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(cards_fixture_dir))
    monkeypatch.setattr(qmd, "COLLECTION_NAME", qmd_test_collection)
    results = await engine.search("startups", limit=5)
    assert len(results.hits) >= 1
    top = results.hits[0]
    assert "paulg" in top.card.id or "side projects" in top.card.body


def test_engine_corpus_unavailable(monkeypatch) -> None:
    """engine.search raises CORPUS_UNAVAILABLE if path is missing."""
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", "/nonexistent/path/here")
    import asyncio
    with pytest.raises(XSensaiError) as ei:
        asyncio.run(engine.search("anything"))
    assert ei.value.code == "CORPUS_UNAVAILABLE"
