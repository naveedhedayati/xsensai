"""Slice 4 — dedup. Per /autoplan S-7 fix: re-check under lock before write."""

from __future__ import annotations

from pathlib import Path

from xsensai.sync.dedup import existing_source_ids, source_id_exists_under_lock


def _write_card(corpus: Path, name: str, source_id: str | None) -> None:
    """Write a minimal v2 card to disk for fixture purposes."""
    body = "## Content\nhello\n"
    if source_id is None:
        # v1 dialect: no source_id in frontmatter, but it's in the filename
        fm = "---\ntype: x-bookmark\nx_post_id: \"" + name.split("_")[1] + "\"\n---\n"
        full = fm + body
    else:
        fm = (
            "---\n"
            f"source_type: bookmark\n"
            f"captured: 2026-04-26T00:00:00+00:00\n"
            f"source: https://x.com/example/status/{source_id}\n"
            f"source_id: '{source_id}'\n"
            f"author: '@example'\n"
            f"date: 2026-04-25T00:00:00+00:00\n"
            f"---\n"
        )
        full = fm + body
    (corpus / name).write_text(full, encoding="utf-8")
    # Sidecar so the card is "v2 shaped" for callers that probe shape
    if source_id is not None:
        sidecar = corpus / (name.replace(".md", ".aabbcc.raw.txt"))
        sidecar.write_text("hello", encoding="utf-8")


def test_existing_source_ids_reads_v2_frontmatter(tmp_path):
    _write_card(tmp_path, "2026-04-25-example-111.md", "111")
    _write_card(tmp_path, "2026-04-26-example-222.md", "222")
    ids = existing_source_ids(tmp_path)
    assert ids == {"111", "222"}


def test_existing_source_ids_falls_back_to_v1_filename_regex(tmp_path):
    """v1 dialect cards have the tweet id only in the filename."""
    # v1 cards don't have source_id in frontmatter — they use x_post_id which
    # the v1_adapter normalizes. iter_cards_metadata uses v1_adapter so we
    # need to verify this works for cards whose pydantic frontmatter has
    # no source_id BUT whose filename embeds it.
    # Use the same filename pattern the user's v1 cards have:
    _write_card(tmp_path, "2026-03-01_2028162355511583052_test-card.md", None)
    # The v1 adapter may or may not load this; if it does, source_id will
    # be populated (adapter parses the filename). If not, the fallback
    # filename regex catches it.
    ids = existing_source_ids(tmp_path)
    # Either the adapter or the filename regex must catch it
    assert "2028162355511583052" in ids


def test_source_id_exists_under_lock_finds_v2_card(tmp_path):
    _write_card(tmp_path, "2026-04-25-example-111.md", "111")
    assert source_id_exists_under_lock("111", tmp_path)
    assert not source_id_exists_under_lock("999", tmp_path)


def test_source_id_exists_under_lock_finds_v1_filename(tmp_path):
    _write_card(tmp_path, "2026-03-01_2028162355511583052_test.md", None)
    assert source_id_exists_under_lock("2028162355511583052", tmp_path)
