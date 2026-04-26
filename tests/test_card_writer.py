"""Tests for storage/corpus.write_card + discover_orphan_tmp + log_v1_mutation_blocked.

write_card uses immutable per-version sidecars (UC5 fix) and verifies the
caller's fencing token before each commit (UC6 fix).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import corpus, sidecar


def _make_paste_card(corpus_path: Path, stem: str = "paste-2026-04-25-test") -> LoadedCard:
    """Build a fresh paste-shape LoadedCard for write_card to persist."""
    fm = CardFrontmatter(
        source_type="paste",
        captured=datetime(2026, 4, 25, 18, 0, tzinfo=timezone.utc),
        author="self",
        why_saved="for the test",
        tags=["test"],
    )
    body = "## Content\n\nhello world\n"
    raw_bytes = b"hello world"
    return LoadedCard(
        fm=fm,
        body=body,
        raw_bytes=raw_bytes,
        md_path=corpus_path / f"{stem}.md",
    )


@pytest.fixture
def tmp_corpus(tmp_path):
    c = tmp_path / "corpus"
    c.mkdir()
    return c


class TestWriteCardHappyPath:
    def test_write_then_load_round_trip(self, tmp_corpus):
        card = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            written = corpus.write_card(card, h.token, corpus_path=tmp_corpus)
        # md exists
        assert (tmp_corpus / "paste-2026-04-25-test.md").exists()
        # Sidecar exists with checksum-prefixed name
        sidecars = list(tmp_corpus.glob("paste-2026-04-25-test.*.raw.txt"))
        assert len(sidecars) == 1
        # Round-trip loads
        loaded = corpus.load_card_by_id("paste-2026-04-25-test", corpus_path=tmp_corpus)
        assert loaded.fm.source_type == "paste"
        assert loaded.fm.why_saved == "for the test"
        assert loaded.raw_bytes == b"hello world"
        assert loaded.fm.raw_checksum == sidecar.compute_checksum(b"hello world")

    def test_index_dirty_marker_written(self, tmp_corpus):
        card = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card, h.token, corpus_path=tmp_corpus)
        assert (tmp_corpus / "_index-dirty").exists()

    def test_per_version_sidecar_then_gc_old(self, tmp_corpus):
        """Writing the same id twice with different content: the new sidecar
        is checksum-prefixed (immutable per-version safety on crash), and
        per /review F5 the old sidecar is GC'd after the new .md commits.
        Net result: one sidecar on disk per card, but the per-version naming
        means a crash mid-rewrite leaves the old sidecar intact."""
        card1 = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card1, h.token, corpus_path=tmp_corpus)
        first_sidecars = list(tmp_corpus.glob("paste-2026-04-25-test.*.raw.txt"))
        assert len(first_sidecars) == 1

        # Write a new version with different content
        card2 = LoadedCard(
            fm=card1.fm,
            body=card1.body,
            raw_bytes=b"second version content",
            md_path=card1.md_path,
        )
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card2, h.token, corpus_path=tmp_corpus)

        # F5 sidecar GC: only ONE sidecar on disk now (the new one), old GC'd.
        all_sidecars = list(tmp_corpus.glob("paste-2026-04-25-test.*.raw.txt"))
        assert len(all_sidecars) == 1, f"expected exactly 1 sidecar after GC, got {[s.name for s in all_sidecars]}"
        # .md points at the new sidecar
        loaded = corpus.load_card_by_id("paste-2026-04-25-test", corpus_path=tmp_corpus)
        assert loaded.raw_bytes == b"second version content"

    def test_sidecar_naming_unique_per_version(self, tmp_corpus):
        """Verify the per-version checksum-prefix naming pattern, even with
        GC. The contract is: filename derives from content checksum, so a
        crash mid-mutation never collides with the prior sidecar."""
        card1 = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card1, h.token, corpus_path=tmp_corpus)
        first_path = next(tmp_corpus.glob("paste-2026-04-25-test.*.raw.txt"))
        first_prefix = first_path.name.split(".")[1]

        card2 = LoadedCard(
            fm=card1.fm, body=card1.body,
            raw_bytes=b"different bytes for unique prefix",
            md_path=card1.md_path,
        )
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card2, h.token, corpus_path=tmp_corpus)
        new_path = next(tmp_corpus.glob("paste-2026-04-25-test.*.raw.txt"))
        new_prefix = new_path.name.split(".")[1]
        # Different content → different checksum → different prefix → different filename
        assert first_prefix != new_prefix


class TestWriteCardFencingToken:
    def test_rejects_invalid_token(self, tmp_corpus):
        card = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste"):
            with pytest.raises(XSensaiError) as exc:
                corpus.write_card(card, "not-the-real-token", corpus_path=tmp_corpus)
            assert exc.value.code == "LOCK_HELD"
            assert "fencing token mismatch" in exc.value.cause.lower() or "mismatch" in exc.value.cause.lower()

    def test_rejects_token_when_lock_released(self, tmp_corpus):
        card = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            captured_token = h.token
        # Lock released — JSON unlinked, token verification fails
        with pytest.raises(XSensaiError) as exc:
            corpus.write_card(card, captured_token, corpus_path=tmp_corpus)
        assert exc.value.code == "LOCK_HELD"

    def test_token_revoked_mid_write_aborts(self, tmp_corpus, monkeypatch):
        """If the lock is stolen between the pre-sidecar check and the
        post-sidecar check (rare but possible), the .md write is aborted."""
        card = _make_paste_card(tmp_corpus)
        check_count = [0]
        original_verify = filelock.verify_fencing_token

        def flaky_verify(corpus_path, token):
            check_count[0] += 1
            if check_count[0] == 1:
                return True  # initial check passes
            return False  # subsequent checks fail (token revoked)

        monkeypatch.setattr(filelock, "verify_fencing_token", flaky_verify)
        # Patch the corpus module's reference too (it imports filelock as-is)
        monkeypatch.setattr(corpus.filelock, "verify_fencing_token", flaky_verify)

        with pytest.raises(XSensaiError) as exc:
            corpus.write_card(card, "any-token", corpus_path=tmp_corpus)
        assert exc.value.code == "LOCK_HELD"
        # Sidecar got written (step 5) but .md should NOT exist (aborted at step 6)
        assert not (tmp_corpus / "paste-2026-04-25-test.md").exists()


class TestOrphanTmpRecovery:
    def test_discover_finds_tmps(self, tmp_corpus):
        (tmp_corpus / "paste-foo.md.tmp").touch()
        (tmp_corpus / "paste-foo.abc123.raw.txt.tmp").touch()
        (tmp_corpus / "real-card.md").touch()  # not a tmp; ignored
        orphans = corpus.discover_orphan_tmp(tmp_corpus)
        names = sorted(p.name for p in orphans)
        assert names == ["paste-foo.abc123.raw.txt.tmp", "paste-foo.md.tmp"]

    def test_discover_empty_corpus(self, tmp_corpus):
        assert corpus.discover_orphan_tmp(tmp_corpus) == []

    def test_iter_cards_cleans_old_orphans(self, tmp_corpus):
        # Plant orphan tmps and backdate their mtime past the threshold so they
        # are recognized as truly stale (per /review F1 race fix: young tmps
        # are presumed live writes by another process and skipped).
        import os as _os
        old_mtime = _os.path.getmtime(tmp_corpus) - (corpus.ORPHAN_TMP_AGE_THRESHOLD_SEC + 60)
        for name in ("stale-write.md.tmp", "stale-write.deadbe.raw.txt.tmp"):
            p = tmp_corpus / name
            p.touch()
            _os.utime(p, (old_mtime, old_mtime))
        # Iterate (no real cards, but this exercises the cleanup hook)
        list(corpus.iter_cards(corpus_path=tmp_corpus))
        # Orphans gone (they were backdated past the threshold)
        remaining = list(tmp_corpus.glob("*.tmp"))
        assert remaining == []

    def test_iter_cards_skips_young_orphans(self, tmp_corpus):
        # /review F1: a freshly-created .tmp must be PRESERVED — it might be
        # an in-flight write from another process. Only old tmps get unlinked.
        (tmp_corpus / "live-write.md.tmp").touch()
        list(corpus.iter_cards(corpus_path=tmp_corpus))
        # Young orphan still present (presumed in-flight)
        assert (tmp_corpus / "live-write.md.tmp").exists()

    def test_iter_cards_does_not_touch_non_tmp(self, tmp_corpus):
        import os as _os
        # A real card next to an old orphan
        card = _make_paste_card(tmp_corpus)
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card, h.token, corpus_path=tmp_corpus)
        stale = tmp_corpus / "stale.md.tmp"
        stale.touch()
        old_mtime = _os.path.getmtime(stale) - (corpus.ORPHAN_TMP_AGE_THRESHOLD_SEC + 60)
        _os.utime(stale, (old_mtime, old_mtime))
        cards = list(corpus.iter_cards(corpus_path=tmp_corpus))
        assert len(cards) == 1
        assert cards[0].id == "paste-2026-04-25-test"
        assert not stale.exists()


class TestSidecarChecksumMismatch:
    """T2 — /review testing specialist: load_card's checksum-mismatch branch
    was uncovered. Tamper with the .raw.txt after writing and assert load
    surfaces DISK_WRITE_FAILED with 'checksum mismatch'."""

    def test_tampered_sidecar_raises_on_load(self, tmp_corpus):
        card = _make_paste_card(tmp_corpus, "paste-tampered-test")
        with filelock.with_card_write_lock(tmp_corpus, "xpaste") as h:
            corpus.write_card(card, h.token, corpus_path=tmp_corpus)
        sidecar_path = next(tmp_corpus.glob("paste-tampered-test.*.raw.txt"))
        sidecar_path.write_bytes(b"tampered content")
        with pytest.raises(XSensaiError) as exc:
            corpus.load_card_by_id("paste-tampered-test", corpus_path=tmp_corpus)
        assert exc.value.code == "DISK_WRITE_FAILED"
        assert "checksum mismatch" in exc.value.cause


class TestRawPathEscapesCorpus:
    """T3 — /review testing specialist: load_card's raw_path-escapes-corpus
    branch was uncovered. Plant a card whose frontmatter raw_path tries to
    traverse outside the corpus root and assert the security guard fires."""

    def test_raw_path_dotdot_rejected(self, tmp_corpus, tmp_path):
        outside = tmp_path / "evil.raw.txt"
        outside.write_bytes(b"pwn")
        md = tmp_corpus / "malicious.md"
        md.write_text(
            "---\n"
            "source_type: paste\n"
            "captured: '2026-04-25T12:00:00+00:00'\n"
            "author: self\n"
            "raw_path: ../evil.raw.txt\n"
            "raw_checksum: sha256:0000000000000000000000000000000000000000000000000000000000000000\n"
            "---\n## Content\n\n"
        )
        with pytest.raises(XSensaiError) as exc:
            corpus.load_card(md, corpus_root=tmp_corpus)
        assert exc.value.code == "DISK_WRITE_FAILED"
        assert "escapes corpus root" in exc.value.cause


class TestV1MutationLog:
    def test_logs_blocked_event(self, tmp_corpus):
        corpus.log_v1_mutation_blocked(tmp_corpus, "old-v1-card", "annotate")
        log_path = tmp_corpus / "_v1-upgraded.jsonl"
        assert log_path.exists()
        line = log_path.read_text().strip()
        entry = json.loads(line)
        assert entry["card_id"] == "old-v1-card"
        assert entry["attempted_op"] == "annotate"
        assert entry["outcome"] == "blocked"
        assert "timestamp" in entry

    def test_appends_multiple_events(self, tmp_corpus):
        corpus.log_v1_mutation_blocked(tmp_corpus, "card-a", "pin")
        corpus.log_v1_mutation_blocked(tmp_corpus, "card-b", "annotate")
        lines = (tmp_corpus / "_v1-upgraded.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["card_id"] == "card-a"
        assert json.loads(lines[1])["card_id"] == "card-b"
