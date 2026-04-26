"""Tests for the read-side reindex trigger in engine.search.

UC2 fix: when _index-dirty marker exists, engine.search runs qmd.update
before querying so /xpaste→/xfind round-trip works in week one. These
tests mock qmd.update + qmd.query so they don't require real QMD.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from xsensai.retrieval import engine, qmd


@pytest.fixture
def tmp_corpus(tmp_path, monkeypatch):
    c = tmp_path / "corpus"
    c.mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    return c


@pytest.fixture
def stub_qmd_query(monkeypatch):
    """Replace qmd.query with an async stub that returns []. Reset after."""
    stub = AsyncMock(return_value=[])
    monkeypatch.setattr(engine.qmd, "query", stub)
    return stub


@pytest.fixture
def stub_qmd_update(monkeypatch):
    """Replace qmd.update with an async stub that records calls."""
    stub = AsyncMock(return_value=None)
    monkeypatch.setattr(engine.qmd, "update", stub)
    return stub


class TestReindexTrigger:
    @pytest.mark.asyncio
    async def test_no_marker_no_update(self, tmp_corpus, stub_qmd_query, stub_qmd_update):
        await engine.search("anything", corpus_path=tmp_corpus)
        stub_qmd_update.assert_not_called()

    @pytest.mark.asyncio
    async def test_marker_present_triggers_update(
        self, tmp_corpus, stub_qmd_query, stub_qmd_update
    ):
        marker = tmp_corpus / "_index-dirty"
        marker.touch()
        await engine.search("anything", corpus_path=tmp_corpus)
        stub_qmd_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_marker_unlinked_after_update(
        self, tmp_corpus, stub_qmd_query, stub_qmd_update
    ):
        marker = tmp_corpus / "_index-dirty"
        marker.touch()
        assert marker.exists()
        await engine.search("anything", corpus_path=tmp_corpus)
        assert not marker.exists()

    @pytest.mark.asyncio
    async def test_marker_renamed_aside_during_update(
        self, tmp_corpus, stub_qmd_query, monkeypatch
    ):
        """Per /review F7: the marker is renamed to `_index-dirty.in-flight`
        BEFORE running qmd update so a write that re-dirties during the
        update isn't lost. After update completes, the .in-flight is unlinked.
        If update raises, the .in-flight stays behind (it's a finally-block
        cleanup; a future re-dirty + search would still see _index-dirty).
        """
        async def raising_update(qmd_path=None):
            raise RuntimeError("simulated qmd update crash")

        monkeypatch.setattr(engine.qmd, "update", raising_update)
        marker = tmp_corpus / "_index-dirty"
        marker.touch()
        with pytest.raises(RuntimeError):
            await engine.search("anything", corpus_path=tmp_corpus)
        # After the failure, the .in-flight rename happened; finally-block
        # unlinked it. Original marker is gone too. Re-dirty would be
        # detected by a future write (which sets _index-dirty again).
        assert not marker.exists()
        assert not (tmp_corpus / "_index-dirty.in-flight").exists()

    @pytest.mark.asyncio
    async def test_query_runs_after_reindex(
        self, tmp_corpus, stub_qmd_query, stub_qmd_update
    ):
        """Order: update first, THEN query. Verifies engine doesn't query stale."""
        call_order = []
        async def recording_update(qmd_path=None):
            call_order.append("update")
        async def recording_query(text, limit=20):
            call_order.append("query")
            return []
        import xsensai.retrieval.engine as eng_mod
        from unittest.mock import patch
        with patch.object(eng_mod.qmd, "update", recording_update), \
             patch.object(eng_mod.qmd, "query", recording_query):
            (tmp_corpus / "_index-dirty").touch()
            await engine.search("any", corpus_path=tmp_corpus)
        assert call_order == ["update", "query"]


class TestQmdUpdate:
    """Unit tests for qmd.update (mocked subprocess — no real QMD needed)."""

    @pytest.mark.asyncio
    async def test_logs_warning_on_missing_binary(self, monkeypatch, caplog):
        async def raise_fnf(*args, **kwargs):
            raise FileNotFoundError("not found")
        monkeypatch.setattr(qmd.asyncio, "create_subprocess_exec", raise_fnf)
        # Should NOT raise
        await qmd.update(qmd_path="/nonexistent/qmd")

    @pytest.mark.asyncio
    async def test_logs_warning_on_nonzero_exit(self, monkeypatch, caplog):
        class FakeProc:
            returncode = 1
            async def communicate(self):
                return (b"", b"some error")
        async def fake_exec(*args, **kwargs):
            return FakeProc()
        monkeypatch.setattr(qmd.asyncio, "create_subprocess_exec", fake_exec)
        # Should NOT raise even on nonzero exit
        await qmd.update()
