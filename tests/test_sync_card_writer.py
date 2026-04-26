"""Slice 4 — card_writer: XDK bookmark dict → v2 card on disk.

Per /autoplan E-4 invariant: extraction_pending=True ALWAYS at write time.
Per E-5: author handle sanitized via _safe_handle.
Per spec line 123: filename pattern YYYY-MM-DD-{author}-{tweet-id}.md.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from xsensai.locks import with_card_write_lock
from xsensai.sync.card_writer import (
    build_body,
    build_filename,
    build_frontmatter,
    build_raw_bytes,
    write_one,
)
from xsensai.sync.card_writer import _safe_handle  # type: ignore[attr-defined]
from xsensai.sync.client import ThreadFetchResult


def _bookmark(text="hello world", **overrides):
    base = {
        "id": "1234567890123456789",
        "text": text,
        "created_at": "2026-04-25T10:30:00.000Z",
        "author_id": "999",
        "conversation_id": "1234567890123456789",
        "_author": {"id": "999", "username": "example_user", "name": "Example"},
        "_media": [],
        "entities": {"urls": []},
    }
    base.update(overrides)
    return base


def test_build_frontmatter_extraction_pending_invariant():
    """E-4 fix: extraction_pending MUST be True at write time, regardless of mode."""
    fm = build_frontmatter(
        bookmark=_bookmark(),
        thread=ThreadFetchResult(status="not_applicable"),
        captured=datetime(2026, 4, 26, tzinfo=timezone.utc),
        run_id="run-abc",
    )
    assert fm.extraction_pending is True


def test_build_frontmatter_sets_thread_fetch_status():
    fm = build_frontmatter(
        bookmark=_bookmark(),
        thread=ThreadFetchResult(status="complete", replies=[{"text": "reply", "_author": {"username": "x"}}]),
        captured=datetime(2026, 4, 26, tzinfo=timezone.utc),
        run_id="run-abc",
    )
    assert fm.thread_fetch_status == "complete"


def test_build_frontmatter_xsync_run_id_persisted():
    fm = build_frontmatter(
        bookmark=_bookmark(),
        thread=ThreadFetchResult(status="not_applicable"),
        captured=datetime(2026, 4, 26, tzinfo=timezone.utc),
        run_id="run-XYZ-12345",
    )
    assert fm.xsync_run_id == "run-XYZ-12345"


def test_build_filename_matches_spec():
    """Spec line 123: YYYY-MM-DD-{author}-{tweet-id}.md."""
    fm = build_frontmatter(
        bookmark=_bookmark(),
        thread=ThreadFetchResult(status="not_applicable"),
        captured=datetime(2026, 4, 26, 14, 30, tzinfo=timezone.utc),
        run_id="r",
    )
    name = build_filename(fm)
    assert name == "2026-04-26-example_user-1234567890123456789.md"


def test_safe_handle_passes_clean_handle():
    assert _safe_handle("Nate_Google") == "Nate_Google"
    assert _safe_handle("user123") == "user123"


def test_safe_handle_sanitizes_dirty_handle():
    """Defense-in-depth: malicious handle gets neutralized."""
    cleaned = _safe_handle("evil; rm -rf /")
    assert ";" not in cleaned
    assert " " not in cleaned
    assert "/" not in cleaned


def test_safe_handle_caps_at_15_chars():
    """X handles are max 15 chars; longer input truncated."""
    cleaned = _safe_handle("a" * 50)
    assert len(cleaned) <= 15


def test_build_body_renders_content_section():
    body = build_body(
        bookmark=_bookmark(text="this is the tweet"),
        thread=ThreadFetchResult(status="not_applicable"),
    )
    assert "## Content" in body
    assert "this is the tweet" in body


def test_build_body_renders_thread_when_complete():
    thread = ThreadFetchResult(
        status="complete",
        replies=[
            {"text": "reply one", "_author": {"username": "alice"}},
            {"text": "reply two", "_author": {"username": "alice"}},
        ],
    )
    body = build_body(bookmark=_bookmark(), thread=thread)
    assert "## Thread" in body
    assert "@alice" in body
    assert "reply one" in body
    assert "reply two" in body


def test_build_body_omits_thread_when_not_complete():
    body = build_body(
        bookmark=_bookmark(),
        thread=ThreadFetchResult(status="outside_window"),
    )
    assert "## Thread" not in body


def test_build_raw_bytes_byte_exact():
    """Verbatim guarantee — raw bytes match the tweet text exactly."""
    text = "tweet with emoji 🚀 and unicode: café"
    raw = build_raw_bytes(bookmark=_bookmark(text=text))
    assert raw == text.encode("utf-8")


def test_write_one_round_trip(tmp_path):
    """End-to-end: bookmark dict → on-disk card with all invariants."""
    with with_card_write_lock(tmp_path, "xsync") as h:
        result = write_one(
            bookmark=_bookmark(text="round trip test"),
            thread=ThreadFetchResult(status="not_applicable"),
            corpus_path=tmp_path,
            lock_token=h.token,
            run_id="run-rt-1",
        )
    assert result.md_path.exists()
    assert result.extraction_pending is True
    assert result.thread_fetch_status == "not_applicable"
    # Sidecar exists (write_card writes one)
    sidecars = list(tmp_path.glob("*.raw.txt"))
    assert len(sidecars) == 1
