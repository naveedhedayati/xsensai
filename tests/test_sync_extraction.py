"""Slice 4 — extraction adapters + prompt building + result validation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.sync.extraction import (
    BODY_MAX_CHARS,
    DeferredExtractor,
    ExtractionPrompt,
    ExtractionResult,
    HostExtractor,
    build_extraction_prompt,
    validate_extraction_result,
)


def _card(body: str = "Some content here.", card_id: str = "card-1") -> LoadedCard:
    fm = CardFrontmatter(
        source_type="bookmark",
        captured=datetime(2026, 4, 26, tzinfo=timezone.utc),
        source="https://x.com/example/status/1",
        source_id="1",
        author="@example",
        date=datetime(2026, 4, 25, tzinfo=timezone.utc),
    )
    return LoadedCard(
        fm=fm, body=f"## Content\n{body}\n",
        raw_bytes=body.encode("utf-8"),
        md_path=Path(f"/tmp/{card_id}.md"),
    )


def test_deferred_extractor_returns_pending_for_all_cards():
    cards = [_card(card_id=f"c{i}") for i in range(3)]
    results = DeferredExtractor().extract_batch(cards)
    assert len(results) == 3
    for r in results.values():
        assert r.pending is True
        assert r.summary == ""
        assert r.tags == []


def test_host_extractor_produces_pending_results_and_prompts():
    cards = [_card(card_id=f"c{i}") for i in range(2)]
    extractor = HostExtractor()
    results = extractor.extract_batch(cards)
    assert all(r.pending for r in results.values())
    assert len(extractor.pending_prompts) == 2
    for p in extractor.pending_prompts:
        assert p.card_id in results
        assert "<DATA_TO_ANALYZE>" in p.prompt_text
        assert "</DATA_TO_ANALYZE>" in p.prompt_text


def test_build_extraction_prompt_includes_card_context():
    card = _card(body="The actual tweet text", card_id="c-abc")
    p = build_extraction_prompt(card)
    assert p.card_id == card.id
    assert "The actual tweet text" in p.prompt_text
    assert "@example" in p.prompt_text
    assert card.fm.captured.isoformat() in p.prompt_text


def test_build_extraction_prompt_truncates_long_body():
    long_body = "x" * (BODY_MAX_CHARS + 500)
    card = _card(body=long_body)
    p = build_extraction_prompt(card)
    assert "[TRUNCATED]" in p.prompt_text
    # The full long_body should NOT appear
    assert long_body not in p.prompt_text


def test_validate_extraction_result_accepts_valid():
    raw = {"summary": "Two sentences. Some text.", "tags": ["one", "two", "three"]}
    r = validate_extraction_result(raw)
    assert r.pending is False
    assert r.summary == "Two sentences. Some text."
    assert r.tags == ["one", "two", "three"]


def test_validate_extraction_result_rejects_short_summary():
    raw = {"summary": "", "tags": ["one", "two", "three"]}
    r = validate_extraction_result(raw)
    assert r.pending is True


def test_validate_extraction_result_rejects_too_few_tags():
    raw = {"summary": "Valid summary text.", "tags": ["only-two", "tags"]}
    r = validate_extraction_result(raw)
    assert r.pending is True


def test_validate_extraction_result_caps_at_5_tags():
    raw = {"summary": "Valid", "tags": ["a", "b", "c", "d", "e", "f", "g"]}
    r = validate_extraction_result(raw)
    assert len(r.tags) == 5


def test_validate_extraction_result_normalizes_tag_chars():
    """Strips weird chars; lowercases; keeps alnum + hyphen + underscore."""
    raw = {"summary": "Valid", "tags": ["TAG_one", "Tag-Two", "tag three"]}
    r = validate_extraction_result(raw)
    # `tag three` gets cleaned to `tagthree` (space stripped)
    assert r.tags == ["tag_one", "tag-two", "tagthree"]


def test_validate_extraction_result_trims_long_summary():
    raw = {"summary": "x" * 600, "tags": ["a", "b", "c"]}
    r = validate_extraction_result(raw)
    assert len(r.summary) <= 410  # trim point + ellipsis margin
