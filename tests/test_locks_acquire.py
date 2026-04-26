"""Tests for locks/filelock.py — fcntl.flock acquire + UUID fencing.

Slice 2 unit-test surface. Subprocess contention tests live in
test_concurrency_paste.py (gated XSENSAI_RUN_INTEGRATION=1).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.locks import filelock


@pytest.fixture
def corpus(tmp_path):
    """Empty corpus dir for lock tests."""
    c = tmp_path / "corpus"
    c.mkdir()
    return c


class TestAcquireRelease:
    def test_happy_path_creates_lock_files(self, corpus):
        with filelock.with_card_write_lock(corpus, "xpaste") as handle:
            assert handle.metadata.writer_kind == "xpaste"
            assert handle.lock_path.exists()
            assert handle.json_path.exists()
            # JSON contains expected metadata
            data = json.loads(handle.json_path.read_text())
            assert data["writer_kind"] == "xpaste"
            assert data["pid"] == os.getpid()
            assert "fencing_token" in data
            assert len(data["fencing_token"]) == 36  # UUID4

    def test_release_unlinks_json_sidecar(self, corpus):
        json_path = corpus / filelock.LOCKS_DIR_NAME / filelock.CARD_WRITE_JSON_NAME
        with filelock.with_card_write_lock(corpus, "xnote"):
            assert json_path.exists()
        assert not json_path.exists()

    def test_release_keeps_lock_file_for_reuse(self, corpus):
        """Lock file (.lock) stays around as the flock target — only JSON
        metadata is unlinked. Re-acquire should work seamlessly."""
        with filelock.with_card_write_lock(corpus, "xpaste"):
            pass
        with filelock.with_card_write_lock(corpus, "xpaste"):
            pass  # No exception

    def test_fencing_token_unique_per_acquire(self, corpus):
        tokens = []
        for _ in range(3):
            with filelock.with_card_write_lock(corpus, "xpaste") as h:
                tokens.append(h.token)
        assert len(set(tokens)) == 3

    def test_creates_locks_dir_if_missing(self, corpus):
        locks_dir = corpus / filelock.LOCKS_DIR_NAME
        assert not locks_dir.exists()
        with filelock.with_card_write_lock(corpus, "xpaste"):
            assert locks_dir.exists()


class TestContention:
    def test_nested_acquire_raises_lock_held(self, corpus):
        """Acquiring twice from the same process: second call raises LOCK_HELD.

        fcntl.flock is per-fd so two open() + flock() in the same process do
        contend (this is the within-process serialization our writers rely on).
        """
        with filelock.with_card_write_lock(corpus, "xpaste"):
            with pytest.raises(XSensaiError) as exc:
                with filelock.with_card_write_lock(corpus, "xpaste"):
                    pass
            assert exc.value.code == "LOCK_HELD"

    def test_lock_held_error_includes_holder_metadata(self, corpus):
        with filelock.with_card_write_lock(corpus, "xpaste"):
            with pytest.raises(XSensaiError) as exc:
                with filelock.with_card_write_lock(corpus, "xnote"):
                    pass
            err = exc.value
            assert err.code == "LOCK_HELD"
            assert "xpaste" in err.cause  # original holder's writer_kind
            assert str(os.getpid()) in err.cause
            assert "rm " in err.next_action  # manual escape hatch

    def test_lock_held_error_when_no_metadata(self, corpus):
        """Lock file exists but JSON sidecar is missing/corrupt — still raise
        LOCK_HELD with degraded diagnostic."""
        # Manually create a held lock without JSON
        locks_dir = corpus / filelock.LOCKS_DIR_NAME
        locks_dir.mkdir()
        lock_path = locks_dir / filelock.CARD_WRITE_LOCK_NAME
        fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT)
        try:
            import fcntl as _fcntl
            _fcntl.flock(fd, _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            with pytest.raises(XSensaiError) as exc:
                with filelock.with_card_write_lock(corpus, "xpaste"):
                    pass
            assert exc.value.code == "LOCK_HELD"
            assert "no metadata available" in exc.value.cause
        finally:
            os.close(fd)


class TestFencingToken:
    def test_verify_returns_true_inside_context(self, corpus):
        with filelock.with_card_write_lock(corpus, "xpaste") as handle:
            assert filelock.verify_fencing_token(corpus, handle.token) is True

    def test_verify_returns_false_after_release(self, corpus):
        captured_token = None
        with filelock.with_card_write_lock(corpus, "xpaste") as handle:
            captured_token = handle.token
        # After context exit, JSON unlinked → verify returns False
        assert filelock.verify_fencing_token(corpus, captured_token) is False

    def test_verify_returns_false_for_wrong_token(self, corpus):
        with filelock.with_card_write_lock(corpus, "xpaste"):
            assert filelock.verify_fencing_token(corpus, "wrong-token-uuid") is False

    def test_verify_returns_false_when_no_lock(self, corpus):
        assert filelock.verify_fencing_token(corpus, "any-token") is False

    def test_verify_returns_false_on_corrupt_json(self, corpus):
        locks_dir = corpus / filelock.LOCKS_DIR_NAME
        locks_dir.mkdir()
        json_path = locks_dir / filelock.CARD_WRITE_JSON_NAME
        json_path.write_text("not-valid-json{")
        assert filelock.verify_fencing_token(corpus, "any") is False


class TestLockMetadata:
    def test_round_trip_json(self):
        m = filelock.LockMetadata(
            pid=12345,
            hostname="test-host",
            started_at="2026-04-25T18:23:11+00:00",
            writer_kind="xpaste",
            fencing_token="abcd-1234",
        )
        roundtrip = filelock.LockMetadata.from_json(m.to_json())
        assert roundtrip == m
