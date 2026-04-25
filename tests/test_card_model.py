"""Tests for the Card model: CardFrontmatter validation + LoadedCard helpers.

Coverage:
- model_validator: source_type invariants (bookmark vs paste)
- field_validator: tz-aware datetime requirement
- field_validator: scalar-to-list coercion (YAML 1.1 trap)
- field_validator: raw_checksum shape
- LoadedCard.content_section parser
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus


def test_bookmark_requires_source_id_and_author() -> None:
    with pytest.raises(ValueError, match="source_id"):
        CardFrontmatter(
            source_type="bookmark",
            captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
            source="https://x.com/foo/status/123",
            author="@foo",
            raw_path="./x.raw.txt",
            raw_checksum="sha256:" + "0" * 64,
        )


def test_paste_must_not_have_source_id() -> None:
    with pytest.raises(ValueError, match="source_id"):
        CardFrontmatter(
            source_type="paste",
            captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
            source_id="oops",
            author="self",
            raw_path="./x.raw.txt",
            raw_checksum="sha256:" + "0" * 64,
        )


def test_naive_datetime_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        CardFrontmatter(
            source_type="bookmark",
            captured=datetime(2026, 4, 25),  # no tzinfo
            source="https://x.com/foo/status/1",
            source_id="1",
            author="@foo",
            raw_path="./x.raw.txt",
            raw_checksum="sha256:" + "0" * 64,
        )


def test_scalar_tag_coerced_to_list() -> None:
    """YAML 1.1 trap: `tags: foo` (scalar) is normalized to `[foo]`."""
    cf = CardFrontmatter(
        source_type="bookmark",
        captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
        source="https://x.com/foo/status/1",
        source_id="1",
        author="@foo",
        tags="lonely_scalar",  # type: ignore[arg-type]
        raw_path="./x.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    assert cf.tags == ["lonely_scalar"]


def test_invalid_checksum_shape() -> None:
    with pytest.raises(ValueError, match="raw_checksum must match"):
        CardFrontmatter(
            source_type="bookmark",
            captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
            source="https://x.com/foo/status/1",
            source_id="1",
            author="@foo",
            raw_path="./x.raw.txt",
            raw_checksum="md5:abcdef",
        )


def test_loaded_card_id_strips_md_suffix() -> None:
    cf = CardFrontmatter(
        source_type="paste",
        captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
        author="self",
        raw_path="./p.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    card = LoadedCard(fm=cf, body="hi", raw_bytes=b"hi", md_path=Path("paste-foo.md"))
    assert card.id == "paste-foo"


def test_content_section_extracts_between_headers() -> None:
    cf = CardFrontmatter(
        source_type="paste",
        captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
        author="self",
        raw_path="./p.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    body = "## Content\n\nthe real content here\n\n## Thread\n\nrandom thread stuff"
    card = LoadedCard(fm=cf, body=body, raw_bytes=b"", md_path=Path("p.md"))
    assert card.content_section == "the real content here"


def test_content_section_falls_back_to_full_body() -> None:
    cf = CardFrontmatter(
        source_type="paste",
        captured=datetime(2026, 4, 25, tzinfo=timezone.utc),
        author="self",
        raw_path="./p.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    body = "no headers here, just text"
    card = LoadedCard(fm=cf, body=body, raw_bytes=b"", md_path=Path("p.md"))
    assert card.content_section == body


def test_load_fixture_card_round_trip(cards_fixture_dir: Path) -> None:
    """Load each v2 fixture card and verify it parses + checksum matches."""
    md_path = cards_fixture_dir / "2026-04-20-paulg-1234567890.md"
    card = corpus.load_card(md_path)
    assert card.fm.source_type == "bookmark"
    assert card.fm.author == "@paulg"
    assert card.fm.pinned is True
    assert "side projects" in card.body
    # raw_bytes round-trip
    assert b"side projects" in card.raw_bytes


def test_load_fixture_paste_card(cards_fixture_dir: Path) -> None:
    md_path = cards_fixture_dir / "paste-2026-04-18-cofounder-meeting-notes.md"
    card = corpus.load_card(md_path)
    assert card.fm.source_type == "paste"
    assert card.fm.source_url == "https://example.com/notes"
    assert card.fm.author == "self"
    assert card.fm.pinned is False


def test_load_v1_card_via_adapter(cards_fixture_dir: Path) -> None:
    md_path = cards_fixture_dir / "v1-2024-09-30-old-bookmark-3434343434.md"
    card = corpus.load_card(md_path)
    assert card.fm.source_type == "bookmark"
    assert card.fm.extraction_pending is True
    # v1 adapter synthesizes raw_bytes from the body
    assert b"legacy v1 bookmark" in card.raw_bytes


def test_load_card_file_missing(tmp_path: Path) -> None:
    md_path = tmp_path / "nope.md"
    with pytest.raises(XSensaiError) as ei:
        corpus.load_card(md_path)
    assert ei.value.code == "YAML_PARSE_FAILED"


def test_load_card_sidecar_missing(tmp_path: Path) -> None:
    md_path = tmp_path / "card.md"
    md_path.write_text(
        "---\n"
        "source_type: bookmark\n"
        "source: https://x.com/foo/status/1\n"
        "source_id: '1'\n"
        "author: '@foo'\n"
        "captured: 2026-04-20T10:00:00Z\n"
        "raw_path: ./missing.raw.txt\n"
        "raw_checksum: sha256:" + "0" * 64 + "\n"
        "---\nbody\n"
    )
    with pytest.raises(XSensaiError) as ei:
        corpus.load_card(md_path)
    assert ei.value.code == "DISK_WRITE_FAILED"


def test_load_card_checksum_mismatch(tmp_path: Path) -> None:
    raw = tmp_path / "x.raw.txt"
    raw.write_bytes(b"actual content")
    md = tmp_path / "card.md"
    md.write_text(
        "---\n"
        "source_type: paste\n"
        "author: self\n"
        "captured: 2026-04-20T10:00:00Z\n"
        "raw_path: ./x.raw.txt\n"
        "raw_checksum: sha256:" + "f" * 64 + "\n"
        "---\nbody\n"
    )
    with pytest.raises(XSensaiError) as ei:
        corpus.load_card(md)
    assert ei.value.code == "DISK_WRITE_FAILED"
    assert "mismatch" in ei.value.cause.lower()
