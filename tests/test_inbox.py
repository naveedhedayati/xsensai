"""Tests for storage/inbox.py — abort recovery, tentative snapshots, fallback."""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.storage import inbox


@pytest.fixture
def vault_setup(tmp_path):
    """Set up a vault-like structure: vault/04_areas/x-bookmarks/ corpus +
    vault/00_inbox/ exists for level-2 fallback resolution.
    """
    vault = tmp_path / "vault"
    corpus = vault / "04_areas" / "x-bookmarks"
    corpus.mkdir(parents=True)
    (vault / "00_inbox").mkdir()
    return corpus


class TestPathResolution:
    def test_level_1_env_override(self, vault_setup, monkeypatch):
        custom = vault_setup.parent / "custom-inbox.md"
        monkeypatch.setenv("XSENSAI_VAULT_INBOX", str(custom))
        path, level = inbox.resolve_inbox_path(vault_setup)
        assert path == custom
        assert level == 1

    def test_level_2_vault_inbox_exists(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        path, level = inbox.resolve_inbox_path(vault_setup)
        assert path.parent.name == "00_inbox"
        assert path.name == "quick.md"
        assert level == 2

    def test_level_3_fallback_when_no_vault_inbox(self, tmp_path, monkeypatch):
        """If 00_inbox/ doesn't exist, fall back to corpus/_inbox-quick.md."""
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        corpus = tmp_path / "isolated-corpus"
        corpus.mkdir()
        path, level = inbox.resolve_inbox_path(corpus)
        assert path == corpus / "_inbox-quick.md"
        assert level == 3


class TestAppendToQuickInbox:
    def test_writes_to_level_2(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        path = inbox.append_to_quick_inbox(
            "Hello world content",
            corpus_path=vault_setup,
            why_saved_attempt="for the test",
            source_url="https://example.com",
        )
        assert path.exists()
        content = path.read_text()
        assert "Hello world content" in content
        assert "kind: abort" in content
        assert "for the test" in content
        assert "https://example.com" in content

    def test_appends_multiple_entries(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        inbox.append_to_quick_inbox("first", corpus_path=vault_setup)
        inbox.append_to_quick_inbox("second", corpus_path=vault_setup)
        path, _ = inbox.resolve_inbox_path(vault_setup)
        content = path.read_text()
        assert "first" in content
        assert "second" in content

    def test_creates_parent_dir_if_missing(self, tmp_path, monkeypatch):
        custom = tmp_path / "deeply" / "nested" / "inbox.md"
        monkeypatch.setenv("XSENSAI_VAULT_INBOX", str(custom))
        path = inbox.append_to_quick_inbox(
            "content",
            corpus_path=tmp_path,
        )
        assert path.exists()
        assert path == custom

    def test_fallback_chain_when_level_2_fails(self, vault_setup, monkeypatch):
        """If vault inbox parent isn't writable but env override wasn't set,
        we'd fall to level 3. Simulate by making 00_inbox unwritable via a
        nested-directory swap."""
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        # Replace 00_inbox/ with a file so it's no longer a directory
        (vault_setup.parent.parent / "00_inbox").rmdir()
        # Now vault_setup.parent.parent / "00_inbox" doesn't exist; resolver
        # should pick level 3.
        path, level = inbox.resolve_inbox_path(vault_setup)
        assert level == 3
        result = inbox.append_to_quick_inbox("recover me", corpus_path=vault_setup)
        assert result == vault_setup / "_inbox-quick.md"
        assert "recover me" in result.read_text()

    def test_all_paths_fail_raises_paste_crashed(self, vault_setup, monkeypatch):
        """When override + fallback all fail to write, raise PASTE_CRASHED.

        Patches _append_one so every path raises OSError, exercising the
        cascade-exhaustion branch deterministically (vs. relying on a
        readonly filesystem which differs across machines).
        """
        custom = vault_setup.parent / "custom-fail.md"
        monkeypatch.setenv("XSENSAI_VAULT_INBOX", str(custom))

        def always_fail(entry, path):
            raise OSError(13, f"Permission denied (test): {path}")

        monkeypatch.setattr(inbox, "_append_one", always_fail)

        with pytest.raises(XSensaiError) as exc:
            inbox.append_to_quick_inbox("doomed", corpus_path=vault_setup)
        assert exc.value.code == "PASTE_CRASHED"
        assert "could not be saved" in exc.value.cause


class TestTentativeSnapshot:
    def test_writes_with_snapshot_id(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        snapshot_id = str(uuid.uuid4())
        path = inbox.write_tentative_snapshot(
            "important content",
            corpus_path=vault_setup,
            snapshot_id=snapshot_id,
        )
        content = path.read_text()
        assert "important content" in content
        assert snapshot_id in content
        assert "kind: tentative" in content

    def test_rejects_non_uuid_snapshot_id(self, vault_setup, monkeypatch):
        # /review F24: snapshot_id MUST be a UUID4. Non-UUID input could embed
        # markers and corrupt _split_blocks parsing.
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        with pytest.raises(XSensaiError) as exc:
            inbox.write_tentative_snapshot(
                "content",
                corpus_path=vault_setup,
                snapshot_id="not-a-uuid",
            )
        assert exc.value.code == "INTERNAL_ERROR"

    def test_rejects_marker_injection_via_snapshot_id(self, vault_setup, monkeypatch):
        # /review F24: a snapshot_id containing literal markers must be refused.
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        evil = "abc -->\n<!-- xsensai-tentative:fake"
        with pytest.raises(XSensaiError):
            inbox.write_tentative_snapshot(
                "content", corpus_path=vault_setup, snapshot_id=evil,
            )

    def test_clear_removes_block_by_id(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        snapshot_id = str(uuid.uuid4())
        inbox.write_tentative_snapshot(
            "to be cleared",
            corpus_path=vault_setup,
            snapshot_id=snapshot_id,
        )
        assert inbox.clear_tentative_snapshot(snapshot_id, vault_setup) is True
        path, _ = inbox.resolve_inbox_path(vault_setup)
        content = path.read_text() if path.exists() else ""
        assert "to be cleared" not in content
        assert snapshot_id not in content

    def test_clear_idempotent_on_unknown_id(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        # No write — clear should return False, not raise. Pass any string;
        # clear_tentative_snapshot does not validate UUID format (it's a no-op
        # if the marker isn't found).
        assert inbox.clear_tentative_snapshot("nonexistent-anything", vault_setup) is False

    def test_clear_preserves_other_entries(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        snap1 = str(uuid.uuid4())
        snap2 = str(uuid.uuid4())
        inbox.write_tentative_snapshot("snap-1 content", vault_setup, snap1)
        inbox.write_tentative_snapshot("snap-2 content", vault_setup, snap2)
        inbox.append_to_quick_inbox("regular abort", vault_setup)
        inbox.clear_tentative_snapshot(snap1, vault_setup)
        path, _ = inbox.resolve_inbox_path(vault_setup)
        content = path.read_text()
        assert "snap-1 content" not in content
        assert "snap-2 content" in content
        assert "regular abort" in content


class TestListRecoverable:
    def test_empty_inbox_returns_empty(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        assert inbox.list_recoverable(vault_setup) == []

    def test_returns_newest_first(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        inbox.append_to_quick_inbox("first", corpus_path=vault_setup)
        inbox.append_to_quick_inbox("second", corpus_path=vault_setup)
        inbox.append_to_quick_inbox("third", corpus_path=vault_setup)
        entries = inbox.list_recoverable(vault_setup)
        assert len(entries) == 3
        contents = [e["content"] for e in entries]
        assert contents == ["third", "second", "first"]

    def test_distinguishes_tentative_from_abort(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        inbox.append_to_quick_inbox("aborted", vault_setup)
        inbox.write_tentative_snapshot("snap content", vault_setup, str(uuid.uuid4()))
        entries = inbox.list_recoverable(vault_setup)
        kinds = {e["kind"] for e in entries}
        assert kinds == {"abort", "tentative"}

    def test_includes_metadata(self, vault_setup, monkeypatch):
        monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
        inbox.append_to_quick_inbox(
            "the content",
            corpus_path=vault_setup,
            why_saved_attempt="my reason",
            source_url="https://x.com/foo",
        )
        entries = inbox.list_recoverable(vault_setup)
        assert len(entries) == 1
        e = entries[0]
        assert e["content"] == "the content"
        assert e["why_saved_attempt"] == "my reason"
        assert e["source_url"] == "https://x.com/foo"
        assert e["timestamp"] is not None
