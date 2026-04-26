"""Tests for /xnote (annotate_card + due_cards_for_review) and /xpin
(set_pin + list_pinned) MCP layers.

Includes the V1_MUTATION_BLOCKED behavior (UC1+UC8) — v1 cards are refused
in Slice 2 with logging to {corpus}/_v1-upgraded.jsonl.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from xsensai.locks import filelock
from xsensai.mcp_server import server
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus


@pytest.fixture
def vault_corpus(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    c = vault / "04_areas" / "x-bookmarks"
    c.mkdir(parents=True)
    (vault / "00_inbox").mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
    return c


def _make_v2_paste(corpus_path: Path, stem: str, why_saved=None,
                   pinned=False, captured_offset_days=0,
                   why_saved_pending=False, next_review_at=None) -> str:
    """Build and persist a v2 paste card; returns the id."""
    captured = datetime.now(timezone.utc) - timedelta(days=captured_offset_days)
    body = f"## Content\n\n{stem} body\n"
    fm = CardFrontmatter(
        source_type="paste",
        captured=captured,
        author="self",
        why_saved=why_saved,
        why_saved_pending=why_saved_pending,
        pinned=pinned,
        next_review_at=next_review_at,
    )
    card = LoadedCard(
        fm=fm,
        body=body,
        raw_bytes=f"{stem} body".encode("utf-8"),
        md_path=corpus_path / f"{stem}.md",
    )
    with filelock.with_card_write_lock(corpus_path, "xpaste") as h:
        written = corpus.write_card(card, h.token, corpus_path=corpus_path)
    return written.id


def _make_v1_card(corpus_path: Path, stem: str = "v1-card") -> str:
    """Plant a v1-shape card on disk (no raw_path/raw_checksum)."""
    md_path = corpus_path / f"{stem}.md"
    md_path.write_text("""---
type: x-bookmark
x_post_id: "1234567890"
x_author: paulg
x_source_url: https://x.com/paulg/status/1234567890
x_date: 2024-12-01T10:00:00Z
captured: 2024-12-01T10:00:00Z
x_extraction_status: success
---

## Content

Old v1 tweet content.

## Thread

Reply chain that would be lost if upgrade-on-write happened.
""")
    return stem


class TestAnnotateCardConfirmation:
    def test_rejects_without_user_confirmed(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-foo")
        result = server.annotate_card(id=target_id, why_saved="new reason", user_confirmed=False)
        assert result["error"]["code"] == "USER_CONFIRMATION_REQUIRED"


class TestAnnotateCardV2:
    def test_writes_why_saved(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-foo")
        result = server.annotate_card(
            id=target_id, why_saved="new reason", user_confirmed=True
        )
        assert result["ok"] is True
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.why_saved == "new reason"
        assert loaded.fm.why_saved_pending is False

    def test_clears_pending_when_why_saved_set(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-bar",
                                    why_saved=None, why_saved_pending=True)
        server.annotate_card(id=target_id, why_saved="finally", user_confirmed=True)
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.why_saved_pending is False

    def test_applicability_replaces_list(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-baz")
        server.annotate_card(
            id=target_id,
            applicability=["[[project-a]]", "[[project-b]]"],
            user_confirmed=True,
        )
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.applicability == ["[[project-a]]", "[[project-b]]"]

    def test_pinned_flag(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-qux")
        server.annotate_card(id=target_id, pinned=True, user_confirmed=True)
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.pinned is True

    def test_next_review_at_set(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-quux")
        future = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
        server.annotate_card(id=target_id, next_review_at=future, user_confirmed=True)
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.next_review_at is not None

    def test_invalid_next_review_at_rejected(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-quuux")
        result = server.annotate_card(
            id=target_id, next_review_at="not-a-date", user_confirmed=True
        )
        assert result["error"]["code"] == "DISK_WRITE_FAILED"


class TestAnnotateCardEdgeCases:
    """T7 — /review testing specialist: invalid id + log_v1 raise + all-None
    edge cases were uncovered."""

    def test_invalid_id_returns_no_results(self, vault_corpus):
        result = server.annotate_card(
            id="nonexistent-card", why_saved="trying", user_confirmed=True
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "NO_RESULTS"

    def test_path_traversal_id_rejected_at_validate(self, vault_corpus):
        result = server.annotate_card(
            id="../../etc/passwd", why_saved="evil", user_confirmed=True
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "NO_RESULTS"

    def test_all_none_fields_no_op_writes_unchanged(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-noop",
                                    why_saved="original")
        result = server.annotate_card(
            id=target_id, user_confirmed=True
        )
        assert result["ok"] is True
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        # Nothing should have changed
        assert loaded.fm.why_saved == "original"
        assert loaded.fm.pinned is False

    def test_whitespace_only_why_saved_flips_pending(self, vault_corpus):
        # /review F11 fix — annotate predicate now matches paste predicate
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-ws",
                                    why_saved="annotated")
        # Clear via whitespace-only
        result = server.annotate_card(
            id=target_id, why_saved="   ", user_confirmed=True
        )
        assert result["ok"] is True
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.why_saved is None
        assert loaded.fm.why_saved_pending is True


class TestAnnotateCardV1Refused:
    def test_v1_mutation_blocked(self, vault_corpus):
        v1_id = _make_v1_card(vault_corpus)
        result = server.annotate_card(
            id=v1_id, why_saved="trying to annotate", user_confirmed=True
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "V1_MUTATION_BLOCKED"
        # The card on disk is unchanged
        loaded = corpus.load_card_by_id(v1_id, corpus_path=vault_corpus)
        assert loaded.fm.why_saved is None  # still untouched

    def test_v1_attempt_logged(self, vault_corpus):
        v1_id = _make_v1_card(vault_corpus)
        server.annotate_card(id=v1_id, why_saved="x", user_confirmed=True)
        log_path = vault_corpus / "_v1-upgraded.jsonl"
        assert log_path.exists()
        entry = json.loads(log_path.read_text().strip())
        assert entry["card_id"] == v1_id
        assert entry["attempted_op"] == "annotate"


class TestSetPin:
    def test_rejects_without_user_confirmed(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-pin")
        result = server.set_pin(id=target_id, pinned=True, user_confirmed=False)
        assert result["error"]["code"] == "USER_CONFIRMATION_REQUIRED"

    def test_pin_idempotent(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-pin")
        server.set_pin(id=target_id, pinned=True, user_confirmed=True)
        # Second call should no-op cleanly
        result = server.set_pin(id=target_id, pinned=True, user_confirmed=True)
        assert result["ok"] is True
        assert "no-op" in result["rendered_message"]

    def test_unpin(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "paste-2026-04-25-unpin", pinned=True)
        server.set_pin(id=target_id, pinned=False, user_confirmed=True)
        loaded = corpus.load_card_by_id(target_id, corpus_path=vault_corpus)
        assert loaded.fm.pinned is False

    def test_v1_pin_blocked(self, vault_corpus):
        v1_id = _make_v1_card(vault_corpus)
        result = server.set_pin(id=v1_id, pinned=True, user_confirmed=True)
        assert result["error"]["code"] == "V1_MUTATION_BLOCKED"


class TestListPinned:
    def test_empty(self, vault_corpus):
        result = server.list_pinned()
        assert result["ok"] is True
        assert result["count"] == 0

    def test_only_pinned_appear(self, vault_corpus):
        _make_v2_paste(vault_corpus, "paste-2026-04-25-pinned-1", pinned=True)
        _make_v2_paste(vault_corpus, "paste-2026-04-25-not-pinned")
        _make_v2_paste(vault_corpus, "paste-2026-04-25-pinned-2", pinned=True)
        result = server.list_pinned()
        assert result["count"] == 2
        names = sorted(p["id"] for p in result["pinned"])
        assert names == ["paste-2026-04-25-pinned-1", "paste-2026-04-25-pinned-2"]

    def test_sorted_captured_desc(self, vault_corpus):
        _make_v2_paste(vault_corpus, "paste-2026-04-25-old",
                       pinned=True, captured_offset_days=10)
        _make_v2_paste(vault_corpus, "paste-2026-04-25-new",
                       pinned=True, captured_offset_days=1)
        result = server.list_pinned()
        ids = [p["id"] for p in result["pinned"]]
        # Newest first
        assert ids[0] == "paste-2026-04-25-new"
        assert ids[1] == "paste-2026-04-25-old"


class TestDueCardsForReview:
    def test_empty(self, vault_corpus):
        result = server.due_cards_for_review()
        assert result["ok"] is True
        assert result["count"] == 0

    def test_pending_cards_appear(self, vault_corpus):
        _make_v2_paste(vault_corpus, "paste-2026-04-25-pending",
                       why_saved=None, why_saved_pending=True)
        _make_v2_paste(vault_corpus, "paste-2026-04-25-annotated",
                       why_saved="done")
        result = server.due_cards_for_review()
        assert result["count"] == 1
        assert result["due"][0]["id"] == "paste-2026-04-25-pending"
        assert result["due"][0]["reason"] == "pending"

    def test_review_at_due_appears(self, vault_corpus):
        past = datetime.now(timezone.utc) - timedelta(days=1)
        _make_v2_paste(vault_corpus, "paste-2026-04-25-past",
                       why_saved="set", next_review_at=past)
        result = server.due_cards_for_review()
        assert result["count"] == 1
        assert result["due"][0]["reason"] == "review_at_due"

    def test_future_review_at_skipped(self, vault_corpus):
        future = datetime.now(timezone.utc) + timedelta(days=7)
        _make_v2_paste(vault_corpus, "paste-2026-04-25-future",
                       why_saved="set", next_review_at=future)
        result = server.due_cards_for_review()
        assert result["count"] == 0

    def test_sorted_oldest_first(self, vault_corpus):
        _make_v2_paste(vault_corpus, "paste-2026-04-25-newer",
                       why_saved_pending=True, captured_offset_days=1)
        _make_v2_paste(vault_corpus, "paste-2026-04-25-older",
                       why_saved_pending=True, captured_offset_days=10)
        result = server.due_cards_for_review()
        # Oldest first per spec deterministic walk order
        assert result["due"][0]["id"] == "paste-2026-04-25-older"
        assert result["due"][1]["id"] == "paste-2026-04-25-newer"

    def test_limit_respected(self, vault_corpus):
        for i in range(5):
            _make_v2_paste(vault_corpus, f"paste-2026-04-25-due-{i}",
                          why_saved_pending=True,
                          captured_offset_days=i)
        result = server.due_cards_for_review(limit=3)
        assert result["count"] == 3
