"""Slice 5 — lazy-extract claim/release coordination tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xsensai.locks import filelock
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus as storage_corpus
from xsensai.sync import lazy_extract
from xsensai.sync.lazy_extract import (
    CLAIM_STALE_SECONDS,
    claim_for_lazy_extract,
    release_lazy_claim,
)


def _write_pending_card(
    corpus_path: Path,
    card_id: str = "test-pending",
    extraction_pending: bool = True,
    lazy_in_progress: bool = False,
    lazy_claim_at: datetime | None = None,
) -> None:
    fm = CardFrontmatter(
        source_type="bookmark",
        captured=datetime(2026, 4, 28, 10, 0, 0, tzinfo=timezone.utc),
        source_id="42",
        source="https://x.com/test/status/42",
        date=datetime(2026, 4, 28, 9, 0, 0, tzinfo=timezone.utc),
        author="@test",
        extraction_pending=extraction_pending,
        lazy_extract_in_progress=lazy_in_progress,
        lazy_extract_claim_at=lazy_claim_at,
    )
    raw_bytes = b"raw tweet body"
    body = "## Content\nbody text"
    md_path = corpus_path / f"{card_id}.md"
    card = LoadedCard(fm=fm, body=body, raw_bytes=raw_bytes, md_path=md_path)
    with filelock.with_card_write_lock(corpus_path, "test-fixture") as h:
        storage_corpus.write_card(card, h.token, corpus_path=corpus_path)


def test_claim_first_caller_wins(tmp_path: Path):
    _write_pending_card(tmp_path)
    result = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert result.outcome == "claimed"
    assert result.run_id is not None
    assert result.run_id.startswith("lazy-extract-")


def test_claim_second_caller_skips_when_active(tmp_path: Path):
    """First caller claims; second sees fresh flag and skips."""
    _write_pending_card(tmp_path)
    first = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert first.outcome == "claimed"
    second = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert second.outcome == "skip_active"
    assert second.run_id is None
    assert "another session" in second.note


def test_claim_skips_when_already_extracted(tmp_path: Path):
    """If the card is no longer extraction_pending, skip without claim."""
    _write_pending_card(tmp_path, extraction_pending=False)
    result = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert result.outcome == "skip_done"
    assert result.run_id is None


def test_claim_reclaims_stale_flag(tmp_path: Path):
    """If prior claim is older than CLAIM_STALE_SECONDS, reclaim."""
    stale_at = datetime.now(timezone.utc) - timedelta(seconds=CLAIM_STALE_SECONDS + 5)
    _write_pending_card(
        tmp_path,
        lazy_in_progress=True,
        lazy_claim_at=stale_at,
    )
    result = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert result.outcome == "reclaimed"
    assert result.run_id is not None
    assert result.run_id.startswith("lazy-extract-")
    assert "old" in result.note.lower()


def test_claim_missing_card(tmp_path: Path):
    """Nonexistent card_id → outcome=missing."""
    result = claim_for_lazy_extract(
        "this-card-does-not-exist", corpus_path=tmp_path
    )
    assert result.outcome == "missing"


def test_release_clears_flag_after_failure(tmp_path: Path):
    _write_pending_card(tmp_path)
    claim_for_lazy_extract("test-pending", corpus_path=tmp_path)

    # Simulate extraction failure → release.
    ok = release_lazy_claim("test-pending", corpus_path=tmp_path)
    assert ok is True

    # Next claim should succeed (flag cleared).
    second = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert second.outcome == "claimed"


def test_release_idempotent_when_no_flag(tmp_path: Path):
    """Releasing a card that was never claimed should not error."""
    _write_pending_card(tmp_path)
    ok = release_lazy_claim("test-pending", corpus_path=tmp_path)
    assert ok is True


def test_lazy_claim_run_id_is_extraction_owner_path(tmp_path: Path):
    """The run_id format must match service.apply_extraction's authz path."""
    _write_pending_card(tmp_path)
    result = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert result.run_id is not None
    assert result.run_id.startswith("lazy-extract-")
    # Verify service-side check accepts this prefix.
    from xsensai.sync.service import apply_extraction  # noqa: F401
    # Don't actually call apply_extraction here (would need full plumbing);
    # the prefix discipline is the contract.


def test_claim_then_extracted_state_transition(tmp_path: Path):
    """Full claim → emulate extraction success → next /xfind sees done."""
    _write_pending_card(tmp_path)
    result = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert result.outcome == "claimed"

    # Simulate `service.apply_extraction()` writing the result by manually
    # updating the card to extraction_pending=False (the real code path
    # is verified by service's existing tests).
    card = storage_corpus.load_card_by_id("test-pending", corpus_path=tmp_path)
    new_fm = card.fm.model_copy(update={
        "extraction_pending": False,
        "lazy_extract_in_progress": False,
        "lazy_extract_claim_at": None,
        "retrieval_summary": "extracted summary",
        "retrieval_tags": ["tag1", "tag2", "tag3"],
    })
    new_card = LoadedCard(
        fm=new_fm, body=card.body, raw_bytes=card.raw_bytes,
        md_path=card.md_path,
    )
    with filelock.with_card_write_lock(tmp_path, "test-fixture") as h:
        storage_corpus.write_card(new_card, h.token, corpus_path=tmp_path)

    # Next /xfind sees skip_done.
    again = claim_for_lazy_extract("test-pending", corpus_path=tmp_path)
    assert again.outcome == "skip_done"
