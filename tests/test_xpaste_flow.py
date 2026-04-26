"""Tests for the /xpaste MCP layer: paste_bookmark + recover_aborted_paste.

Tests the MCP tools directly (the slash command markdown orchestrates them).
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from xsensai.mcp_server import server
from xsensai.storage import corpus, inbox


@pytest.fixture
def vault_corpus(tmp_path, monkeypatch):
    """Vault-like layout so inbox path resolution works."""
    vault = tmp_path / "vault"
    c = vault / "04_areas" / "x-bookmarks"
    c.mkdir(parents=True)
    (vault / "00_inbox").mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
    return c


class TestPasteBookmarkConfirmation:
    @pytest.mark.asyncio
    async def test_rejects_without_user_confirmed(self, vault_corpus):
        result = await server.paste_bookmark(content="hi", user_confirmed=False)
        assert result["ok"] is False
        assert result["error"]["code"] == "USER_CONFIRMATION_REQUIRED"

    @pytest.mark.asyncio
    async def test_accepts_with_user_confirmed(self, vault_corpus):
        result = await server.paste_bookmark(content="hello world", user_confirmed=True)
        assert result["ok"] is True
        assert result["id"].startswith("paste-")


class TestPasteBookmarkValidation:
    @pytest.mark.asyncio
    async def test_empty_content_rejected(self, vault_corpus):
        result = await server.paste_bookmark(content="", user_confirmed=True)
        assert result["error"]["code"] == "PASTE_EMPTY"

    @pytest.mark.asyncio
    async def test_whitespace_only_rejected(self, vault_corpus):
        result = await server.paste_bookmark(content="   \n\t ", user_confirmed=True)
        assert result["error"]["code"] == "PASTE_EMPTY"

    @pytest.mark.asyncio
    async def test_content_over_10mb_rejected(self, vault_corpus):
        big = "x" * (11 * 1024 * 1024)
        result = await server.paste_bookmark(content=big, user_confirmed=True)
        assert result["error"]["code"] == "DISK_WRITE_FAILED"
        assert "10MB" in result["error"]["message"]

    @pytest.mark.asyncio
    async def test_content_at_exactly_max_bytes_accepted(self, vault_corpus):
        # T6 boundary test — exactly MAX_CONTENT_BYTES (10MB) should pass.
        from xsensai.mcp_server.server import MAX_CONTENT_BYTES
        payload = "a" * MAX_CONTENT_BYTES
        result = await server.paste_bookmark(content=payload, user_confirmed=True)
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_content_at_max_bytes_plus_one_rejected(self, vault_corpus):
        # T6 boundary test — exactly MAX_CONTENT_BYTES + 1 should fail.
        from xsensai.mcp_server.server import MAX_CONTENT_BYTES
        payload = "a" * (MAX_CONTENT_BYTES + 1)
        result = await server.paste_bookmark(content=payload, user_confirmed=True)
        assert result["error"]["code"] == "DISK_WRITE_FAILED"


class TestPasteBookmarkHappyPath:
    @pytest.mark.asyncio
    async def test_full_payload_writes_card(self, vault_corpus):
        result = await server.paste_bookmark(
            content="The deep work essay says ...",
            why_saved="for the article",
            source_url="https://example.com/deep-work",
            tags=["focus", "essay"],
            user_confirmed=True,
        )
        assert result["ok"] is True
        # Card is on disk and loadable
        loaded = corpus.load_card_by_id(result["id"], corpus_path=vault_corpus)
        assert loaded.fm.why_saved == "for the article"
        assert loaded.fm.source_url == "https://example.com/deep-work"
        assert "focus" in loaded.fm.tags
        assert loaded.fm.why_saved_pending is False
        assert loaded.raw_bytes == b"The deep work essay says ..."

    @pytest.mark.asyncio
    async def test_empty_why_saved_flags_pending(self, vault_corpus):
        result = await server.paste_bookmark(
            content="some content without reason yet",
            user_confirmed=True,
        )
        assert result["ok"] is True
        assert result["why_saved_pending"] is True
        loaded = corpus.load_card_by_id(result["id"], corpus_path=vault_corpus)
        assert loaded.fm.why_saved_pending is True
        assert loaded.fm.why_saved is None

    @pytest.mark.asyncio
    async def test_index_dirty_marker_written(self, vault_corpus):
        await server.paste_bookmark(content="hi", user_confirmed=True)
        assert (vault_corpus / "_index-dirty").exists()

    @pytest.mark.asyncio
    async def test_round_trip_appears_in_search_after_reindex(
        self, vault_corpus, monkeypatch
    ):
        """Verify that after paste, the read-side reindex trigger fires.

        We mock qmd.update + qmd.query so this doesn't need real QMD; the
        property under test is "engine.search() unlinks _index-dirty after
        reindex." The full QMD round-trip is integration-tested separately.
        """
        from unittest.mock import AsyncMock
        from xsensai.retrieval import engine, qmd
        monkeypatch.setattr(engine.qmd, "query", AsyncMock(return_value=[]))
        monkeypatch.setattr(engine.qmd, "update", AsyncMock(return_value=None))

        await server.paste_bookmark(content="findable later", user_confirmed=True)
        assert (vault_corpus / "_index-dirty").exists()
        await engine.search("anything", corpus_path=vault_corpus)
        # After search, marker is gone (reindex trigger unlinked it)
        assert not (vault_corpus / "_index-dirty").exists()

    @pytest.mark.asyncio
    async def test_slug_collision_disambiguates(self, vault_corpus):
        # Two pastes on same day with same first-40 chars
        same_prefix = "the same prefix " * 10  # > 40 chars
        r1 = await server.paste_bookmark(content=same_prefix + "first", user_confirmed=True)
        r2 = await server.paste_bookmark(content=same_prefix + "second", user_confirmed=True)
        assert r1["id"] != r2["id"]
        # Both load
        c1 = corpus.load_card_by_id(r1["id"], corpus_path=vault_corpus)
        c2 = corpus.load_card_by_id(r2["id"], corpus_path=vault_corpus)
        assert c1.raw_bytes != c2.raw_bytes


class TestRecoverAbortedPaste:
    @pytest.mark.asyncio
    async def test_lists_empty_when_no_aborts(self, vault_corpus):
        result = server.recover_aborted_paste()
        assert result["ok"] is True
        assert result["count"] == 0

    @pytest.mark.asyncio
    async def test_lists_aborted_pastes(self, vault_corpus):
        inbox.append_to_quick_inbox("aborted content", vault_corpus,
                                     why_saved_attempt="my reason")
        inbox.append_to_quick_inbox("second aborted", vault_corpus)
        result = server.recover_aborted_paste()
        assert result["ok"] is True
        assert result["count"] == 2
        # Newest first
        assert result["entries"][0]["content"] == "second aborted"

    @pytest.mark.asyncio
    async def test_recover_by_snapshot_id(self, vault_corpus):
        import uuid
        snap_id = str(uuid.uuid4())
        inbox.write_tentative_snapshot("snap content", vault_corpus, snap_id)
        result = server.recover_aborted_paste(snapshot_id=snap_id)
        assert result["ok"] is True
        assert result["entry"]["content"] == "snap content"

    @pytest.mark.asyncio
    async def test_recover_unknown_id_returns_no_results(self, vault_corpus):
        result = server.recover_aborted_paste(snapshot_id="nonexistent")
        assert result["ok"] is False
        assert result["error"]["code"] == "NO_RESULTS"
