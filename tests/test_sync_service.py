"""Slice 4 — service.run, apply_extraction, finalize_run, extract_pending.

Service tests use a stub TokenProvider + stub XClient (the orchestrator's
two seams). XDK is never imported.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List
from unittest.mock import MagicMock, patch

import pytest

from xsensai.errors import XSensaiError
from xsensai.sync.client import BookmarkPage, ThreadFetchResult, XClient
from xsensai.sync.service import (
    apply_extraction,
    extract_pending,
    finalize_run,
    run,
)


def _bookmark(sid: str, text: str = "hello", author: str = "alice"):
    return {
        "id": sid,
        "text": text,
        "created_at": "2026-04-25T10:00:00.000Z",
        "author_id": "u1",
        "conversation_id": sid,  # single-tweet (no thread)
        "_author": {"id": "u1", "username": author, "name": author.title()},
        "_media": [],
        "entities": {"urls": []},
    }


def _stub_xclient(bookmarks: List[Dict[str, Any]]) -> Any:
    """Build a stubbed XClient that returns the given bookmarks once + then nothing."""
    inst = MagicMock(spec=XClient)
    inst.iter_bookmarks.return_value = iter([
        BookmarkPage(bookmarks=bookmarks, next_cursor=None),
    ])
    inst.get_thread.return_value = ThreadFetchResult(status="not_applicable")
    return inst


@pytest.fixture
def stub_provider(monkeypatch):
    """Stub provider — service builds XClient(provider, client_id) but we
    monkeypatch XClient to skip real network."""
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "fake-client")
    return MagicMock(get_refresh_token=MagicMock(return_value="t"), store_refresh_token=MagicMock())


def test_run_inline_when_n_le_5(tmp_path, stub_provider, monkeypatch):
    bookmarks = [_bookmark(f"100{i}") for i in range(3)]  # 3 cards → inline
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient(bookmarks),
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path,
    )
    assert result.status == "ok"
    assert result.extraction_strategy == "inline"
    assert len(result.cards_written) == 3
    assert len(result.extraction_prompts) == 3


def test_run_deferred_when_n_gt_5(tmp_path, stub_provider, monkeypatch):
    bookmarks = [_bookmark(f"100{i}") for i in range(8)]  # 8 cards → deferred
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient(bookmarks),
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path,
    )
    assert result.status == "ok"
    assert result.extraction_strategy == "deferred"
    assert len(result.cards_written) == 8
    # Deferred mode doesn't produce extraction prompts upfront
    assert result.extraction_prompts == []


def test_run_inline_override_at_n_30(tmp_path, stub_provider, monkeypatch):
    bookmarks = [_bookmark(f"100{i}") for i in range(8)]
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient(bookmarks),
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path, inline_override=True,
    )
    assert result.extraction_strategy == "inline"
    assert len(result.extraction_prompts) == 8


def test_run_defer_override_at_n_2(tmp_path, stub_provider, monkeypatch):
    bookmarks = [_bookmark(f"100{i}") for i in range(2)]
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient(bookmarks),
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path, defer_override=True,
    )
    assert result.extraction_strategy == "deferred"


def test_run_inline_and_defer_conflict_returns_invalid_flags(tmp_path, stub_provider):
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path, inline_override=True, defer_override=True,
    )
    assert result.status == "failed"
    assert "INVALID_FLAGS" in result.rendered_message


def test_run_empty_when_no_new_bookmarks(tmp_path, stub_provider, monkeypatch):
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient([]),  # empty
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path,
    )
    assert result.status == "empty"
    assert "SYNC_DONE" in result.rendered_message


def test_run_dedup_skips_existing_source_ids(tmp_path, stub_provider, monkeypatch):
    """If a bookmark's source_id is already on disk, it's skipped."""
    # Pre-write a card with source_id "200"
    pre = (
        "---\n"
        "source_type: bookmark\n"
        "captured: 2026-04-25T00:00:00+00:00\n"
        "source: https://x.com/example/status/200\n"
        "source_id: '200'\n"
        "author: '@example'\n"
        "date: 2026-04-25T00:00:00+00:00\n"
        "raw_path: ./2026-04-25-example-200.aabbcc.raw.txt\n"
        "raw_checksum: 'sha256:" + ("a" * 64) + "'\n"
        "---\n## Content\nhello\n"
    )
    (tmp_path / "2026-04-25-example-200.md").write_text(pre)
    (tmp_path / "2026-04-25-example-200.aabbcc.raw.txt").write_text("hello")

    bookmarks = [_bookmark("200"), _bookmark("300")]  # 200 dup, 300 new
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient(bookmarks),
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path,
    )
    # Only the new card writes
    assert len(result.cards_written) == 1
    assert "300" in result.cards_written[0]["card_id"]


def test_extract_pending_empty_when_no_pending(tmp_path):
    result = extract_pending(corpus_path=tmp_path)
    assert result.status == "empty"
    assert "NO_PENDING_EXTRACTIONS" in result.rendered_message


def test_finalize_run_writes_status_file(tmp_path):
    fr = finalize_run(
        run_id="run-x",
        success=True,
        n_new_cards=3,
        extraction_inline=2,
        extraction_pending=1,
        threads_unfetched_this_run=0,
        corpus_path=tmp_path,
        skip_reindex=True,  # don't trigger qmd in tests
    )
    status_path = tmp_path / "_sync-status.md"
    assert status_path.exists()
    assert fr.sync_status.new_cards_this_run == 3
    assert fr.sync_status.extraction_pending_count == 1


def test_apply_extraction_validation_failure_keeps_pending(tmp_path, stub_provider, monkeypatch):
    """Empty summary triggers extraction_pending=True; ok=False returned."""
    # First write a card via run() so we have a real LoadedCard
    monkeypatch.setattr(
        "xsensai.sync.service.XClient",
        lambda **kw: _stub_xclient([_bookmark("400")]),
    )
    result = run(
        mode="backlog", token_provider=stub_provider, client_id="fake",
        corpus_path=tmp_path,
    )
    assert len(result.cards_written) == 1
    card_id = result.cards_written[0]["card_id"]

    # Now try to apply an empty extraction
    apply_result = apply_extraction(
        card_id=card_id, summary="", tags=["a"],  # invalid: empty summary, <3 tags
        run_id=result.run_id, corpus_path=tmp_path,
    )
    assert apply_result.ok is False
    assert apply_result.extraction_pending is True
