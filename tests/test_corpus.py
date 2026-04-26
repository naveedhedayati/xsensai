"""Tests for storage.corpus: iter_cards, dup defense, path resolution."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.storage import corpus


def test_iter_cards_yields_all_fixture_cards(cards_fixture_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(cards_fixture_dir))
    cards = list(corpus.iter_cards())
    assert len(cards) >= 10  # 10 v2 + 1 v1 = 11
    sources = {c.fm.source_type for c in cards}
    assert {"bookmark", "paste"} <= sources


def test_iter_cards_skips_underscore_prefixed_files(tmp_path: Path) -> None:
    (tmp_path / "_index-errors.md").write_text("# index errors\n")
    (tmp_path / "_sync-status.md").write_text("---\nlast_run: 2026-04-25\n---\n")
    cards = list(corpus.iter_cards(corpus_path=tmp_path))
    assert cards == []


def test_iter_cards_skips_vault_navigation_files_silently(
    tmp_path: Path, caplog
) -> None:
    """The user's vault may legitimately contain CLAUDE.md (Claude context for
    the bookmarks area when operating directly in the vault) and README.md
    (human-facing readme). Neither is a card. The walker MUST skip them
    silently — without this, every retrieval logs a WARNING for both.
    """
    (tmp_path / "CLAUDE.md").write_text(
        "# X-Bookmarks\n\nVault navigation file, not a card.\n"
    )
    (tmp_path / "README.md").write_text("# Bookmarks vault README\n")
    with caplog.at_level(logging.WARNING, logger="xsensai.storage.corpus"):
        cards = list(corpus.iter_cards(corpus_path=tmp_path))
    assert cards == []
    # Critical: silent skip. No warnings for navigation files.
    assert not any("CLAUDE.md" in r.message for r in caplog.records)
    assert not any("README.md" in r.message for r in caplog.records)


def test_iter_cards_skips_non_frontmatter_md_silently(
    tmp_path: Path, caplog
) -> None:
    """Any .md without a YAML frontmatter opener (`---`) is not a card. The
    walker silently skips. Without this, dropping a topic-index.md or design
    note in the corpus dir would WARN on every retrieval.
    """
    (tmp_path / "topic-notes.md").write_text(
        "# My personal notes\n\nNo frontmatter, just markdown.\n"
    )
    with caplog.at_level(logging.WARNING, logger="xsensai.storage.corpus"):
        cards = list(corpus.iter_cards(corpus_path=tmp_path))
    assert cards == []
    assert not any("topic-notes.md" in r.message for r in caplog.records)


def test_iter_cards_metadata_also_skips_navigation_files(
    tmp_path: Path, caplog
) -> None:
    """iter_cards_metadata (used by /xpin list, due_cards_for_review) shares
    the walker. Same skip rules must apply, otherwise pin-listing would
    spam warnings on every invocation.
    """
    (tmp_path / "CLAUDE.md").write_text("# Vault navigation\n")
    (tmp_path / "topic.md").write_text("# Free-form note, no frontmatter\n")
    with caplog.at_level(logging.WARNING, logger="xsensai.storage.corpus"):
        cards = list(corpus.iter_cards_metadata(corpus_path=tmp_path))
    assert cards == []
    assert not any("CLAUDE.md" in r.message for r in caplog.records)
    assert not any("topic.md" in r.message for r in caplog.records)


def test_iter_cards_skips_malformed_with_warning(tmp_path: Path, caplog) -> None:
    bad = tmp_path / "broken.md"
    bad.write_text("---\nnot: valid: yaml:\n---\n")
    good = tmp_path / "good.md"
    raw = tmp_path / "good.raw.txt"
    raw.write_bytes(b"hi")
    import hashlib
    digest = hashlib.sha256(b"hi").hexdigest()
    good.write_text(
        "---\n"
        "source_type: paste\n"
        "author: self\n"
        "captured: 2026-04-20T10:00:00Z\n"
        f"raw_path: ./good.raw.txt\n"
        f"raw_checksum: sha256:{digest}\n"
        "---\nbody\n"
    )
    with caplog.at_level(logging.WARNING, logger="xsensai.storage.corpus"):
        cards = list(corpus.iter_cards(corpus_path=tmp_path))
    assert len(cards) == 1
    assert cards[0].md_path.name == "good.md"
    assert any("broken.md" in r.message for r in caplog.records)


def test_iter_cards_dup_source_id_skipped(tmp_path: Path, caplog) -> None:
    """Two cards with same source_id: only first is yielded, second logged."""
    import hashlib
    body = b"sample"
    digest = hashlib.sha256(body).hexdigest()
    for name in ("a.md", "b.md"):
        raw = tmp_path / name.replace(".md", ".raw.txt")
        raw.write_bytes(body)
        (tmp_path / name).write_text(
            "---\n"
            "source_type: bookmark\n"
            "source: https://x.com/foo/status/999\n"
            "source_id: '999'\n"
            "author: '@foo'\n"
            "captured: 2026-04-20T10:00:00Z\n"
            f"raw_path: ./{name.replace('.md', '.raw.txt')}\n"
            f"raw_checksum: sha256:{digest}\n"
            "---\n## Content\n\nbody\n"
        )
    with caplog.at_level(logging.WARNING, logger="xsensai.storage.corpus"):
        cards = list(corpus.iter_cards(corpus_path=tmp_path))
    assert len(cards) == 1
    assert any("duplicate source_id" in r.message and "'999'" in r.message
               for r in caplog.records)


def test_resolve_corpus_path_missing_raises(tmp_path: Path) -> None:
    nope = tmp_path / "does-not-exist"
    with pytest.raises(XSensaiError) as ei:
        corpus.resolve_corpus_path(nope)
    assert ei.value.code == "CORPUS_UNAVAILABLE"


def test_resolve_corpus_path_not_a_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi")
    with pytest.raises(XSensaiError) as ei:
        corpus.resolve_corpus_path(f)
    assert ei.value.code == "CORPUS_UNAVAILABLE"


def test_load_card_by_id_missing(tmp_corpus: Path) -> None:
    with pytest.raises(XSensaiError) as ei:
        corpus.load_card_by_id("does-not-exist")
    assert ei.value.code == "NO_RESULTS"


def test_load_card_by_id_round_trip(cards_fixture_dir: Path, monkeypatch) -> None:
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(cards_fixture_dir))
    card = corpus.load_card_by_id("2026-04-20-paulg-1234567890")
    assert card.fm.author == "@paulg"
