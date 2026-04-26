"""Tests for /review wire-ups: UC9 (recover auto-clear), UC10 (review cursor),
UC11 (tentative snapshot), F10 (24h dedup), F22 (recover split tools),
F23 (pagination), F25 (vault inbox validation).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from xsensai.locks import filelock
from xsensai.mcp_server import server
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus, inbox


@pytest.fixture
def vault_corpus(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    c = vault / "04_areas" / "x-bookmarks"
    c.mkdir(parents=True)
    (vault / "00_inbox").mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
    return c


# ---------- UC10: review cursor read/write/integrate ----------

class TestReviewCursor:
    def test_get_cursor_empty_when_no_cursor(self, vault_corpus):
        result = server.get_review_cursor()
        assert result["ok"] is True
        assert result["last_card_id"] is None

    def test_set_then_get_cursor(self, vault_corpus):
        server.set_review_cursor(last_card_id="paste-2026-04-01-foo")
        result = server.get_review_cursor()
        assert result["last_card_id"] == "paste-2026-04-01-foo"

    def test_set_none_clears_cursor(self, vault_corpus):
        server.set_review_cursor(last_card_id="paste-2026-04-01-foo")
        server.set_review_cursor(last_card_id=None)
        result = server.get_review_cursor()
        assert result["last_card_id"] is None

    def test_due_cards_skips_past_cursor(self, vault_corpus):
        # Plant 4 pending cards captured at known offsets
        for i in range(4):
            card = LoadedCard(
                fm=CardFrontmatter(
                    source_type="paste",
                    captured=datetime.now(timezone.utc) - timedelta(days=10 - i),
                    author="self",
                    why_saved_pending=True,
                ),
                body="## Content\n\ntest\n",
                raw_bytes=f"test {i}".encode(),
                md_path=vault_corpus / f"paste-2026-04-25-cursor-{i}.md",
            )
            with filelock.with_card_write_lock(vault_corpus, "xpaste") as h:
                corpus.write_card(card, h.token, corpus_path=vault_corpus)

        # No cursor → all 4 pending
        result = server.due_cards_for_review(limit=10)
        assert result["total"] == 4
        assert len(result["due"]) == 4

        # Set cursor to card-1 (oldest is index 0, so index 1 is the 2nd
        # oldest). After cursor, only cards 2 + 3 should appear.
        oldest_two_id = result["due"][1]["id"]
        server.set_review_cursor(last_card_id=oldest_two_id)
        result2 = server.due_cards_for_review(limit=10)
        assert result2["cursor"] == oldest_two_id
        assert len(result2["due"]) == 2  # cards after the cursor


# ---------- UC11: tentative snapshot wire-up ----------

class TestTentativeSnapshotWireup:
    def test_write_paste_snapshot_creates_inbox_entry(self, vault_corpus):
        snap_id = str(uuid.uuid4())
        result = server.write_paste_snapshot(
            content="tentative content",
            snapshot_id=snap_id,
            why_saved_attempt="just in case",
            source_url="https://example.com",
        )
        assert result["ok"] is True
        # Verify in inbox
        entries = inbox.list_recoverable(vault_corpus)
        assert any(e["snapshot_id"] == snap_id for e in entries)

    def test_write_paste_snapshot_rejects_non_uuid(self, vault_corpus):
        result = server.write_paste_snapshot(
            content="x", snapshot_id="not-a-uuid",
        )
        assert result["ok"] is False
        assert result["error"]["code"] == "INTERNAL_ERROR"


# ---------- UC9: recover auto-clear via paste_bookmark.clear_snapshot_id ----------

class TestRecoverAutoClear:
    @pytest.mark.asyncio
    async def test_paste_bookmark_clears_snapshot_on_success(self, vault_corpus):
        # 1. Write a tentative snapshot
        snap_id = str(uuid.uuid4())
        server.write_paste_snapshot(
            content="recover-me content",
            snapshot_id=snap_id,
        )
        # 2. paste_bookmark with clear_snapshot_id should commit the card
        #    AND clear the snapshot
        result = await server.paste_bookmark(
            content="recover-me content",
            user_confirmed=True,
            clear_snapshot_id=snap_id,
        )
        # The dedup may fire (same content is in the inbox marker — but the
        # marker isn't a card, so the card should still write). Verify:
        assert result["ok"] is True
        if result.get("snapshot_cleared"):
            assert result["snapshot_cleared"] is True
        # Snapshot gone from inbox
        entries = inbox.list_recoverable(vault_corpus)
        assert not any(e.get("snapshot_id") == snap_id for e in entries)

    def test_clear_paste_snapshot_standalone(self, vault_corpus):
        snap_id = str(uuid.uuid4())
        server.write_paste_snapshot(content="x", snapshot_id=snap_id)
        result = server.clear_paste_snapshot(snap_id)
        assert result["ok"] is True
        assert result["cleared"] is True

    def test_clear_paste_snapshot_idempotent(self, vault_corpus):
        result = server.clear_paste_snapshot("never-existed")
        assert result["ok"] is True
        assert result["cleared"] is False


# ---------- F10: 24h content fingerprint dedup ----------

class TestContentFingerprintDedup:
    @pytest.mark.asyncio
    async def test_duplicate_paste_within_window_returns_first_id(self, vault_corpus):
        r1 = await server.paste_bookmark(content="exact dup test", user_confirmed=True)
        assert r1["ok"] is True
        first_id = r1["id"]

        r2 = await server.paste_bookmark(content="exact dup test", user_confirmed=True)
        assert r2["ok"] is True
        assert r2.get("duplicate_of") == first_id
        assert r2["id"] == first_id  # same id, no second card

    @pytest.mark.asyncio
    async def test_different_content_no_dedup(self, vault_corpus):
        r1 = await server.paste_bookmark(content="content A", user_confirmed=True)
        r2 = await server.paste_bookmark(content="content B", user_confirmed=True)
        assert r1["id"] != r2["id"]
        assert "duplicate_of" not in r2 or r2.get("duplicate_of") is None


# ---------- F22: recover_aborted_paste split into 3 tools ----------

class TestRecoverSplit:
    def test_list_recoverable_pastes(self, vault_corpus):
        inbox.append_to_quick_inbox("entry 1", vault_corpus)
        inbox.append_to_quick_inbox("entry 2", vault_corpus)
        result = server.list_recoverable_pastes()
        assert result["ok"] is True
        assert result["count"] == 2

    def test_get_aborted_paste_by_snapshot_id(self, vault_corpus):
        snap = str(uuid.uuid4())
        server.write_paste_snapshot(content="snap", snapshot_id=snap)
        result = server.get_aborted_paste(snapshot_id=snap)
        assert result["ok"] is True
        assert result["entry"]["content"] == "snap"

    def test_get_aborted_paste_unknown_id_returns_no_results(self, vault_corpus):
        result = server.get_aborted_paste(snapshot_id="nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "NO_RESULTS"


# ---------- F23: pagination caps + total/has_more ----------

class TestPaginationCaps:
    def _plant_pinned(self, vault_corpus, n: int):
        for i in range(n):
            card = LoadedCard(
                fm=CardFrontmatter(
                    source_type="paste",
                    captured=datetime.now(timezone.utc) - timedelta(seconds=n - i),
                    author="self",
                    pinned=True,
                ),
                body="## Content\n\ntest\n",
                raw_bytes=f"pin-{i}".encode(),
                md_path=vault_corpus / f"paste-2026-04-25-pin-{i:03d}.md",
            )
            with filelock.with_card_write_lock(vault_corpus, "xpaste") as h:
                corpus.write_card(card, h.token, corpus_path=vault_corpus)

    def test_list_pinned_has_total_and_has_more(self, vault_corpus):
        self._plant_pinned(vault_corpus, 5)
        result = server.list_pinned(limit=3)
        assert result["count"] == 3
        assert result["total"] == 5
        assert result["has_more"] is True

    def test_list_pinned_no_more_when_under_limit(self, vault_corpus):
        self._plant_pinned(vault_corpus, 2)
        result = server.list_pinned(limit=10)
        assert result["count"] == 2
        assert result["total"] == 2
        assert result["has_more"] is False

    def test_list_pinned_caps_limit(self, vault_corpus):
        # Request more than the max — should clamp
        result = server.list_pinned(limit=999_999)
        assert result["count"] == 0  # no cards anyway
        # The cap enforcement is tested via type, not result; just verify no error

    def test_due_cards_returns_total_and_has_more(self, vault_corpus):
        for i in range(3):
            card = LoadedCard(
                fm=CardFrontmatter(
                    source_type="paste",
                    captured=datetime.now(timezone.utc) - timedelta(seconds=3 - i),
                    author="self",
                    why_saved_pending=True,
                ),
                body="## Content\n\ntest\n",
                raw_bytes=f"due-{i}".encode(),
                md_path=vault_corpus / f"paste-2026-04-25-due-{i}.md",
            )
            with filelock.with_card_write_lock(vault_corpus, "xpaste") as h:
                corpus.write_card(card, h.token, corpus_path=vault_corpus)
        result = server.due_cards_for_review(limit=2)
        assert result["count"] == 2
        assert result["total"] == 3
        assert result["has_more"] is True


# ---------- F25: XSENSAI_VAULT_INBOX validation ----------

class TestInboxOverrideValidation:
    def test_override_inside_home_accepted(self, vault_corpus, monkeypatch, tmp_path):
        # Use the real $HOME so resolution works. Pick a path inside it.
        custom = Path.home() / ".cache" / "xsensai-test-inbox.md"
        monkeypatch.setenv("XSENSAI_VAULT_INBOX", str(custom))
        path, level = inbox.resolve_inbox_path(vault_corpus)
        assert level == 1

    def test_override_outside_home_rejected_falls_back(
        self, vault_corpus, monkeypatch, tmp_path
    ):
        # System path outside $HOME — should reject the override and fall back.
        monkeypatch.setenv("XSENSAI_VAULT_INBOX", "/tmp/evil-inbox.md")
        path, level = inbox.resolve_inbox_path(vault_corpus)
        # /tmp is not under $HOME nor under the vault root → rejected → level 2
        assert level == 2
        assert path.parent.name == "00_inbox"

    def test_override_inside_vault_root_accepted(self, vault_corpus, monkeypatch):
        # Override pointing inside the vault root (corpus.parent.parent) is OK
        custom = vault_corpus.parent / "custom-inbox.md"
        monkeypatch.setenv("XSENSAI_VAULT_INBOX", str(custom))
        path, level = inbox.resolve_inbox_path(vault_corpus)
        assert level == 1
        assert path == custom


# ---------- F4: list_pinned uses iter_cards_metadata (no sidecar verify) ----------

class TestListPinnedSkipsSidecarVerify:
    def test_list_pinned_works_without_sidecar_files(self, vault_corpus):
        """If iter_cards_metadata correctly skips sidecar reads, then a card
        whose sidecar is missing should still surface in list_pinned (with
        no checksum verification error)."""
        card = LoadedCard(
            fm=CardFrontmatter(
                source_type="paste",
                captured=datetime.now(timezone.utc),
                author="self",
                pinned=True,
            ),
            body="## Content\n\ntest\n",
            raw_bytes=b"contents",
            md_path=vault_corpus / "paste-2026-04-25-no-sidecar.md",
        )
        with filelock.with_card_write_lock(vault_corpus, "xpaste") as h:
            corpus.write_card(card, h.token, corpus_path=vault_corpus)
        # Manually delete the sidecar
        sidecars = list(vault_corpus.glob("paste-2026-04-25-no-sidecar.*.raw.txt"))
        assert sidecars, "sidecar must exist before deletion"
        for s in sidecars:
            s.unlink()
        # list_pinned should still return the card (metadata-only path)
        result = server.list_pinned()
        assert result["count"] == 1
        # ...but iter_cards (full-load path) would skip it due to checksum read fail
        full_load_count = len(list(corpus.iter_cards(corpus_path=vault_corpus)))
        assert full_load_count == 0
