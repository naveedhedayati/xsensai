"""Tests for storage/sidecar.py atomic-write surface (durable_replace +
write_sidecar_atomic + crash injection + iCloud detection).

Crash-injection uses XSENSAI_CRASH_AFTER_STEP=N (NOT timing-based SIGKILL —
that pattern is flaky on APFS per Eng review). Step numbering matches the
inline _crash_check() calls in durable_replace.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.storage import sidecar


class TestDurableReplace:
    def test_writes_content_atomically(self, tmp_path):
        target = tmp_path / "card.raw.txt"
        sidecar.durable_replace(target, b"hello world")
        assert target.read_bytes() == b"hello world"

    def test_overwrites_existing_file(self, tmp_path):
        target = tmp_path / "card.raw.txt"
        target.write_bytes(b"old content")
        sidecar.durable_replace(target, b"new content")
        assert target.read_bytes() == b"new content"

    def test_no_orphan_tmp_after_success(self, tmp_path):
        target = tmp_path / "card.raw.txt"
        sidecar.durable_replace(target, b"hello")
        # No .raw.txt.tmp left around
        assert list(tmp_path.glob("*.tmp")) == []

    def test_byte_exact_with_unicode(self, tmp_path):
        target = tmp_path / "card.raw.txt"
        content = "café 🎉 résumé".encode("utf-8")
        sidecar.durable_replace(target, content)
        assert target.read_bytes() == content

    def test_byte_exact_with_binary(self, tmp_path):
        target = tmp_path / "card.raw.txt"
        content = bytes(range(256))  # all byte values
        sidecar.durable_replace(target, content)
        assert target.read_bytes() == content


class TestCrashInjection:
    def test_crash_step_1_no_target_yet(self, tmp_path, monkeypatch):
        """Crash after step 1 (tmp written, fsync'd) — target not created."""
        target = tmp_path / "card.raw.txt"
        monkeypatch.setenv("XSENSAI_CRASH_AFTER_STEP", "1")
        with pytest.raises(XSensaiError) as exc:
            sidecar.durable_replace(target, b"hello")
        assert exc.value.code == "DISK_WRITE_FAILED"
        assert "step 1" in exc.value.cause
        # Target file does NOT exist
        assert not target.exists()

    def test_crash_step_2_no_target_yet(self, tmp_path, monkeypatch):
        """Crash after step 2 (F_FULLFSYNC done, before rename)."""
        target = tmp_path / "card.raw.txt"
        monkeypatch.setenv("XSENSAI_CRASH_AFTER_STEP", "2")
        with pytest.raises(XSensaiError):
            sidecar.durable_replace(target, b"hello")
        assert not target.exists()

    def test_crash_step_3_target_exists(self, tmp_path, monkeypatch):
        """Crash after step 3 (rename done, before parent dir fsync) — target IS there."""
        target = tmp_path / "card.raw.txt"
        monkeypatch.setenv("XSENSAI_CRASH_AFTER_STEP", "3")
        with pytest.raises(XSensaiError):
            sidecar.durable_replace(target, b"hello")
        # Target IS there (rename succeeded); just durability not flushed
        assert target.exists()
        assert target.read_bytes() == b"hello"

    def test_crash_step_4_complete_state(self, tmp_path, monkeypatch):
        """Crash after step 4 (parent fsync done) — fully written."""
        target = tmp_path / "card.raw.txt"
        monkeypatch.setenv("XSENSAI_CRASH_AFTER_STEP", "4")
        with pytest.raises(XSensaiError):
            sidecar.durable_replace(target, b"hello")
        assert target.exists()
        assert target.read_bytes() == b"hello"

    def test_no_injection_when_env_unset(self, tmp_path, monkeypatch):
        target = tmp_path / "card.raw.txt"
        monkeypatch.delenv("XSENSAI_CRASH_AFTER_STEP", raising=False)
        sidecar.durable_replace(target, b"hello")
        assert target.read_bytes() == b"hello"

    def test_invalid_env_value_no_injection(self, tmp_path, monkeypatch):
        target = tmp_path / "card.raw.txt"
        monkeypatch.setenv("XSENSAI_CRASH_AFTER_STEP", "not_a_number")
        sidecar.durable_replace(target, b"hello")
        assert target.read_bytes() == b"hello"


class TestWriteSidecarAtomic:
    def test_returns_correct_checksum(self, tmp_path):
        path = tmp_path / "card.raw.txt"
        content = b"hello world"
        result = sidecar.write_sidecar_atomic(path, content)
        expected = "sha256:" + hashlib.sha256(content).hexdigest()
        assert result == expected

    def test_round_trip_with_read_sidecar(self, tmp_path):
        path = tmp_path / "card.raw.txt"
        content = b"verbatim content"
        checksum = sidecar.write_sidecar_atomic(path, content)
        read_bytes, read_checksum = sidecar.read_sidecar(path)
        assert read_bytes == content
        assert read_checksum == checksum


class TestComputeChecksum:
    def test_empty(self):
        # sha256("") = e3b0...
        assert sidecar.compute_checksum(b"") == "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    def test_known_vector(self):
        assert sidecar.compute_checksum(b"hello") == "sha256:2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"


class TestCrossDeviceRename:
    """T1 — /review testing specialist: EXDEV branch (cross-device rename)
    in durable_replace was uncovered. Monkeypatch os.replace to raise EXDEV
    and assert the typed XSensaiError surfaces with the iCloud diagnostic.
    """

    def test_exdev_cross_device_rename_raises(self, tmp_path, monkeypatch):
        import errno
        target = tmp_path / "card.raw.txt"
        original_replace = os.replace
        def fake_replace(src, dst):
            raise OSError(errno.EXDEV, "Cross-device link")
        monkeypatch.setattr("os.replace", fake_replace)
        with pytest.raises(XSensaiError) as exc:
            sidecar.durable_replace(target, b"hello")
        assert exc.value.code == "DISK_WRITE_FAILED"
        assert "Cross-device" in exc.value.cause


class TestICloudDetection:
    def test_normal_path_is_not_icloud(self, tmp_path):
        assert sidecar.is_likely_icloud_path(tmp_path) is False

    def test_mobile_documents_is_icloud(self):
        # Synthetic path with the known iCloud container name
        path = Path("/Users/foo/Library/Mobile Documents/com~apple~CloudDocs/test")
        assert sidecar.is_likely_icloud_path(path) is True

    def test_clouddocs_is_icloud(self):
        path = Path("/Users/foo/CloudDocs/test")
        assert sidecar.is_likely_icloud_path(path) is True
