"""Slice 6 tombstone tests.

Covers:
- Schema validator (deleted/deleted_at invariants + backward compat)
- delete_bookmark / restore_bookmark / list_deleted MCP flows
- annotate_card / set_pin TOMBSTONE_BLOCKED guard
- iter_cards / iter_cards_metadata / load_card_by_id include_deleted filter
- Round-trip preservation (mutate non-tombstone card; deleted=False round-trips)
- Dedup tombstone helper (existing_source_ids_with_tombstones)
- TOMBSTONE_BLOCKED canonical envelope (no stale-slice suffix)

Slice 7 update: delete_bookmark/restore_bookmark switched to a 2-call
nonce/handshake. The bulk of these tests aren't testing the handshake
itself — they're testing tombstone semantics on the corpus side. The
autouse `_destructive_bypass` fixture enables `XSENSAI_DESTRUCTIVE_BYPASS=1`
for this file so destructive calls skip the handshake (AD7 bypass path).
Tests that specifically cover the handshake semantics — including the
v0.9.1.0 TypeError on the removed `user_confirmed` kwarg — live in
`tests/test_destructive_token_flow.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest
import yaml
import frontmatter

from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from xsensai.mcp_server import nonce_store, server
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus
from xsensai.sync import dedup


@pytest.fixture(autouse=True)
def _destructive_bypass(monkeypatch):
    """Enable the documented test-fixture bypass so destructive
    delete_bookmark/restore_bookmark calls skip the nonce handshake.
    Tombstone semantics are what's under test here; the handshake itself
    is exercised in test_destructive_token_flow.py.
    """
    monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "1")
    nonce_store.reset_store()
    yield
    nonce_store.reset_store()


# ----------------------------------------------------------------------------
# Schema validator
# ----------------------------------------------------------------------------


class TestTombstoneSchemaValidator:
    def test_default_deleted_false(self):
        fm = CardFrontmatter(
            source_type="paste",
            captured=datetime.now(timezone.utc),
        )
        assert fm.deleted is False
        assert fm.deleted_at is None

    def test_deleted_true_requires_deleted_at(self):
        with pytest.raises(Exception) as exc_info:
            CardFrontmatter(
                source_type="paste",
                captured=datetime.now(timezone.utc),
                deleted=True,
            )
        assert "deleted_at" in str(exc_info.value)

    def test_deleted_at_without_deleted_true(self):
        with pytest.raises(Exception) as exc_info:
            CardFrontmatter(
                source_type="paste",
                captured=datetime.now(timezone.utc),
                deleted_at=datetime.now(timezone.utc),
            )
        assert "deleted=False" in str(exc_info.value)

    def test_valid_tombstone(self):
        now = datetime.now(timezone.utc)
        fm = CardFrontmatter(
            source_type="paste",
            captured=now,
            deleted=True,
            deleted_at=now,
        )
        assert fm.deleted is True
        assert fm.deleted_at == now

    def test_deleted_at_requires_utc(self):
        # Naive datetime should be rejected by require_utc validator
        naive = datetime.now()  # no tz
        with pytest.raises(Exception):
            CardFrontmatter(
                source_type="paste",
                captured=datetime.now(timezone.utc),
                deleted=True,
                deleted_at=naive,
            )


# ----------------------------------------------------------------------------
# Backward compat — cards without `deleted` field on disk
# ----------------------------------------------------------------------------


class TestTombstoneBackwardCompat:
    def test_load_pre_slice6_card(self):
        # Pre-Slice-6 v2 card has no `deleted` field in YAML
        fm_dict = {
            "source_type": "paste",
            "captured": datetime.now(timezone.utc),
            "raw_path": "card.abc123.raw.txt",
            "raw_checksum": "sha256:" + "a" * 64,
        }
        fm = CardFrontmatter.model_validate(fm_dict)
        assert fm.deleted is False
        assert fm.deleted_at is None


# ----------------------------------------------------------------------------
# Helpers + fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def vault_corpus(tmp_path, monkeypatch):
    vault = tmp_path / "vault"
    c = vault / "04_areas" / "x-bookmarks"
    c.mkdir(parents=True)
    (vault / "00_inbox").mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(c))
    monkeypatch.delenv("XSENSAI_VAULT_INBOX", raising=False)
    return c


def _make_v2_paste(
    corpus_path: Path,
    stem: str,
    why_saved: str | None = None,
    pinned: bool = False,
) -> str:
    """Build and persist a v2 paste card; returns the id."""
    captured = datetime.now(timezone.utc)
    body = f"## Content\n\n{stem} body\n"
    fm = CardFrontmatter(
        source_type="paste",
        captured=captured,
        author="self",
        why_saved=why_saved,
        pinned=pinned,
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


def _make_v2_bookmark(
    corpus_path: Path,
    stem: str,
    source_id: str,
) -> str:
    captured = datetime.now(timezone.utc)
    body = f"## Content\n\n{stem} body\n"
    fm = CardFrontmatter(
        source_type="bookmark",
        captured=captured,
        author="testuser",
        source="https://x.com/testuser/status/" + source_id,
        source_id=source_id,
        date=captured,
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


# ----------------------------------------------------------------------------
# delete_bookmark + restore_bookmark MCP flow
# ----------------------------------------------------------------------------


class TestDeleteBookmark:
    def test_delete_requires_nonce_when_bypass_off(self, vault_corpus, monkeypatch):
        """delete_bookmark requires a confirmation_nonce in the 2-call
        flow when the bypass env var is not set. The legacy
        `user_confirmed` kwarg was removed in v0.9.1.0 (TypeError now);
        that path is tested in test_destructive_token_flow.py.
        """
        monkeypatch.delenv("XSENSAI_DESTRUCTIVE_BYPASS", raising=False)
        target_id = _make_v2_paste(vault_corpus, "alpha")
        # No nonce → first-call challenge
        result = server.delete_bookmark(id=target_id)
        assert result["ok"] is False
        assert result["error"]["code"] == "NONCE_REQUIRED"

    def test_delete_marks_card(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        result = server.delete_bookmark(id=target_id)
        assert result["ok"] is True
        assert result["deleted"] is True
        assert result["deleted_at"] is not None
        # Re-load and verify on-disk
        card = corpus.load_card_by_id(target_id, include_deleted=True)
        assert card.fm.deleted is True
        assert card.fm.deleted_at is not None

    def test_double_delete_is_noop(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target_id)
        result = server.delete_bookmark(id=target_id)
        assert result.get("already_deleted") is True

    def test_delete_excludes_card_from_default_load(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target_id)
        with pytest.raises(XSensaiError) as exc_info:
            corpus.load_card_by_id(target_id)
        assert exc_info.value.code == "NO_RESULTS"

    def test_delete_v1_card_refused(self, vault_corpus):
        # Plant a v1-shape card
        md_path = vault_corpus / "v1-card.md"
        md_path.write_text(
            "---\n"
            "type: x-bookmark\n"
            'x_post_id: "1234567890"\n'
            "x_author: paulg\n"
            "x_source_url: https://x.com/paulg/status/1234567890\n"
            "x_date: 2024-12-01T10:00:00Z\n"
            "captured: 2024-12-01T10:00:00Z\n"
            "x_extraction_status: success\n"
            "---\n\n## Content\n\nold v1\n"
        )
        result = server.delete_bookmark(id="v1-card")
        assert "V1_MUTATION_BLOCKED" in str(result)


class TestRestoreBookmark:
    def test_restore_clears_tombstone(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target_id)
        result = server.restore_bookmark(id=target_id)
        assert result["ok"] is True
        assert result["restored"] is True
        # Re-load and verify on-disk
        card = corpus.load_card_by_id(target_id)
        assert card.fm.deleted is False
        assert card.fm.deleted_at is None

    def test_restore_already_active(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        result = server.restore_bookmark(id=target_id)
        assert result.get("already_active") is True

    def test_restore_requires_nonce_when_bypass_off(self, vault_corpus, monkeypatch):
        """Mirror of test_delete_requires_nonce_when_bypass_off."""
        target_id = _make_v2_paste(vault_corpus, "alpha")
        # Set up a deleted card (bypass active here)
        server.delete_bookmark(id=target_id)
        # Now drop bypass and assert the handshake gates restore
        monkeypatch.delenv("XSENSAI_DESTRUCTIVE_BYPASS", raising=False)
        result = server.restore_bookmark(id=target_id)
        assert result["ok"] is False
        assert result["error"]["code"] == "NONCE_REQUIRED"


class TestListDeleted:
    def test_empty(self, vault_corpus):
        result = server.list_deleted()
        assert result["count"] == 0
        assert result["total"] == 0

    def test_lists_deleted_cards(self, vault_corpus):
        a = _make_v2_paste(vault_corpus, "alpha")
        b = _make_v2_paste(vault_corpus, "beta")
        c = _make_v2_paste(vault_corpus, "gamma")
        server.delete_bookmark(id=a)
        server.delete_bookmark(id=c)
        result = server.list_deleted()
        assert result["count"] == 2
        ids = [row["id"] for row in result["deleted"]]
        assert a in ids
        assert c in ids
        assert b not in ids


# ----------------------------------------------------------------------------
# annotate_card + set_pin TOMBSTONE_BLOCKED
# ----------------------------------------------------------------------------


class TestTombstoneMutationGuard:
    def test_annotate_on_deleted_blocked(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target_id)
        result = server.annotate_card(
            id=target_id, why_saved="changed mind", user_confirmed=True
        )
        rendered = str(result)
        assert "TOMBSTONE_BLOCKED" in rendered
        assert "/xrestore" in rendered
        # Critical: no stale "Slice 7" string
        assert "Slice 7" not in rendered

    def test_set_pin_on_deleted_blocked(self, vault_corpus):
        target_id = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target_id)
        result = server.set_pin(id=target_id, pinned=True, user_confirmed=True)
        rendered = str(result)
        assert "TOMBSTONE_BLOCKED" in rendered
        assert "Slice 7" not in rendered


# ----------------------------------------------------------------------------
# Corpus iteration
# ----------------------------------------------------------------------------


class TestCorpusIteration:
    def test_iter_cards_excludes_deleted_default(self, vault_corpus):
        a = _make_v2_paste(vault_corpus, "alpha")
        b = _make_v2_paste(vault_corpus, "beta")
        server.delete_bookmark(id=a)
        ids = [c.id for c in corpus.iter_cards()]
        assert b in ids
        assert a not in ids

    def test_iter_cards_include_deleted(self, vault_corpus):
        a = _make_v2_paste(vault_corpus, "alpha")
        b = _make_v2_paste(vault_corpus, "beta")
        server.delete_bookmark(id=a)
        ids = [c.id for c in corpus.iter_cards(include_deleted=True)]
        assert a in ids
        assert b in ids

    def test_iter_cards_metadata_excludes_deleted_default(self, vault_corpus):
        a = _make_v2_paste(vault_corpus, "alpha")
        b = _make_v2_paste(vault_corpus, "beta")
        server.delete_bookmark(id=a)
        ids = [c.id for c in corpus.iter_cards_metadata()]
        assert b in ids
        assert a not in ids

    def test_load_card_by_id_excludes_deleted_default(self, vault_corpus):
        target = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target)
        with pytest.raises(XSensaiError) as exc_info:
            corpus.load_card_by_id(target)
        assert exc_info.value.code == "NO_RESULTS"

    def test_load_card_by_id_include_deleted(self, vault_corpus):
        target = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target)
        card = corpus.load_card_by_id(target, include_deleted=True)
        assert card.fm.deleted is True


# ----------------------------------------------------------------------------
# Round-trip preservation
# ----------------------------------------------------------------------------


class TestRoundTrip:
    def test_set_pin_preserves_deleted_false(self, vault_corpus):
        target = _make_v2_paste(vault_corpus, "alpha")
        # Mutate via set_pin
        server.set_pin(id=target, pinned=True, user_confirmed=True)
        # Reload — deleted field must still be False
        card = corpus.load_card_by_id(target)
        assert card.fm.deleted is False
        assert card.fm.deleted_at is None
        assert card.fm.pinned is True

    def test_annotate_preserves_deleted_false(self, vault_corpus):
        target = _make_v2_paste(vault_corpus, "alpha")
        server.annotate_card(
            id=target, why_saved="some reason", user_confirmed=True
        )
        card = corpus.load_card_by_id(target)
        assert card.fm.deleted is False
        assert card.fm.deleted_at is None
        assert card.fm.why_saved == "some reason"


# ----------------------------------------------------------------------------
# Dedup helper
# ----------------------------------------------------------------------------


class TestDedupTombstone:
    def test_existing_source_ids_unchanged_signature(self, vault_corpus):
        # Layer 4 contract: existing_source_ids() still returns Set[str]
        # for backward compat with service.py:620, 643.
        _make_v2_bookmark(vault_corpus, "card-1", "12345")
        sids = dedup.existing_source_ids(corpus_path=vault_corpus)
        assert isinstance(sids, set)
        assert "12345" in sids

    def test_with_tombstones_returns_tuple(self, vault_corpus):
        a_id = _make_v2_bookmark(vault_corpus, "card-1", "12345")
        _make_v2_bookmark(vault_corpus, "card-2", "67890")
        # Delete the first card — but delete_bookmark uses MCP path which
        # requires lock. Let's set tombstone directly via low-level write.
        card = corpus.load_card_by_id(a_id)
        fm_dict = card.fm.model_dump(mode="python")
        fm_dict["deleted"] = True
        fm_dict["deleted_at"] = datetime.now(timezone.utc)
        new_card = LoadedCard(
            fm=CardFrontmatter.model_validate(fm_dict),
            body=card.body,
            raw_bytes=card.raw_bytes,
            md_path=card.md_path,
        )
        with filelock.with_card_write_lock(vault_corpus, "xtest") as h:
            corpus.write_card(new_card, h.token, corpus_path=vault_corpus)

        sids, tombstoned = dedup.existing_source_ids_with_tombstones(
            corpus_path=vault_corpus
        )
        assert "12345" in sids
        assert "67890" in sids
        assert tombstoned["12345"] is True
        assert tombstoned["67890"] is False


# ----------------------------------------------------------------------------
# Canonical TOMBSTONE_BLOCKED envelope
# ----------------------------------------------------------------------------


class TestTombstoneBlockedFormat:
    def test_no_slice_7_suffix_anywhere(self, vault_corpus):
        target = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target)
        result = server.annotate_card(
            id=target, why_saved="x", user_confirmed=True
        )
        rendered = str(result)
        # The exact stale string the /autoplan dual voices flagged
        assert "(Slice 7)" not in rendered
        assert "Slice 7" not in rendered

    def test_format_contains_required_fields(self, vault_corpus):
        target = _make_v2_paste(vault_corpus, "alpha")
        server.delete_bookmark(id=target)
        result = server.annotate_card(
            id=target, why_saved="x", user_confirmed=True
        )
        rendered = str(result)
        assert "TOMBSTONE_BLOCKED" in rendered
        assert "/xrestore" in rendered
        assert "/xpaste" in rendered
