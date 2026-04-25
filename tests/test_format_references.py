"""Tests for retrieval.format: [B]/[P] rendering + grapheme-cluster truncation."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.retrieval import format as fmt


def _make_bookmark(why: str = "explained well") -> LoadedCard:
    cf = CardFrontmatter(
        source_type="bookmark",
        source="https://x.com/paulg/status/123",
        source_id="123",
        author="@paulg",
        captured=datetime(2026, 4, 20, tzinfo=timezone.utc),
        why_saved=why,
        raw_path="./x.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    body = "## Content\n\nMost great startups began as side projects.\n\n## Thread\n\nreplies"
    return LoadedCard(fm=cf, body=body, raw_bytes=b"", md_path=Path("card.md"))


def _make_paste(source_url: str | None = "https://example.com/blog/post", why: str | None = "great point") -> LoadedCard:
    cf = CardFrontmatter(
        source_type="paste",
        author="self",
        source_url=source_url,
        captured=datetime(2026, 4, 18, tzinfo=timezone.utc),
        why_saved=why,
        raw_path="./p.raw.txt",
        raw_checksum="sha256:" + "0" * 64,
    )
    body = "## Content\n\nA pasted observation about cofounder fit."
    return LoadedCard(fm=cf, body=body, raw_bytes=b"", md_path=Path("paste-foo.md"))


def test_bookmark_format_basic() -> None:
    out = fmt.format_reference(_make_bookmark())
    assert out.startswith("[B] @paulg — ")
    assert "x.com/paulg/status/123" in out
    assert "why: explained well" in out


def test_paste_format_with_domain() -> None:
    out = fmt.format_reference(_make_paste())
    assert out.startswith("[P] example.com — ")
    assert "paste-foo.md" in out
    assert "why: great point" in out


def test_paste_format_no_url_uses_self() -> None:
    out = fmt.format_reference(_make_paste(source_url=None))
    assert out.startswith("[P] self — ")


def test_no_why_saved_renders_placeholder() -> None:
    out = fmt.format_reference(_make_bookmark(why=""))
    # empty string is falsy → placeholder
    assert "(no annotation yet)" in out


def test_grapheme_cluster_truncation_preserves_zwj_emoji() -> None:
    """80 grapheme clusters; ZWJ emoji counts as one."""
    text = ("👨‍👩‍👧 " * 100).strip()
    out = fmt.truncate_graphemes(text, limit=10)
    # Should not contain a partial ZWJ sequence (no orphan ZWJ at end)
    assert not out.rstrip(".").endswith("‍")
    # And should be shorter than original
    assert len(out) < len(text)


def test_short_text_not_truncated() -> None:
    assert fmt.truncate_graphemes("short", limit=80) == "short"


def test_truncation_appends_ellipsis() -> None:
    long = "x" * 200
    out = fmt.truncate_graphemes(long, limit=80)
    assert out.endswith("...")
    assert len(out) == 83  # 80 + "..."


def test_newlines_replaced_with_spaces() -> None:
    out = fmt.truncate_graphemes("line one\nline two", limit=80)
    assert "\n" not in out
    assert "line one line two" == out
