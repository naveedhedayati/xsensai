"""Slice 4 — index_rebuild lock domain + heartbeat thread (diagnostics-only).

Per /autoplan E-2 fix:
  - flock is the truth (not TTL)
  - heartbeat is diagnostics only — rewrites JSON's `heartbeat` field
  - threading.excepthook surfaces daemon-thread failures to main thread
  - heartbeat preserves fencing_token across rewrites (verify_fencing_token stable)
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from xsensai.errors import XSensaiError
from xsensai.locks import (
    HEARTBEAT_INTERVAL_SECONDS,
    LockDomain,
    with_card_write_lock,
    with_index_rebuild_lock,
)
from xsensai.locks.filelock import (
    INDEX_REBUILD_JSON_NAME,
    INDEX_REBUILD_LOCK_NAME,
    LOCKS_DIR_NAME,
)


def test_index_rebuild_lock_acquires_and_releases(tmp_path):
    with with_index_rebuild_lock(tmp_path, "xsync", heartbeat=False) as h:
        assert (tmp_path / LOCKS_DIR_NAME / INDEX_REBUILD_LOCK_NAME).exists()
        assert (tmp_path / LOCKS_DIR_NAME / INDEX_REBUILD_JSON_NAME).exists()
        assert h.token  # UUID
    # JSON sidecar gone after release; lock file persists (kernel resource).
    assert not (tmp_path / LOCKS_DIR_NAME / INDEX_REBUILD_JSON_NAME).exists()


def test_index_rebuild_lock_held_raises_lock_held(tmp_path):
    with with_index_rebuild_lock(tmp_path, "xsync", heartbeat=False):
        with pytest.raises(XSensaiError) as exc:
            with with_index_rebuild_lock(tmp_path, "cron", heartbeat=False):
                pass
        assert exc.value.code == "LOCK_HELD"
        assert "index_rebuild" in exc.value.cause


def test_index_rebuild_separate_from_card_write(tmp_path):
    """The two domains are independent locks."""
    with with_card_write_lock(tmp_path, "xpaste"):
        with with_index_rebuild_lock(tmp_path, "xsync", heartbeat=False):
            assert (tmp_path / LOCKS_DIR_NAME / "card_write.json").exists()
            assert (tmp_path / LOCKS_DIR_NAME / INDEX_REBUILD_JSON_NAME).exists()


def test_heartbeat_writes_into_json(tmp_path):
    """Heartbeat thread updates the `heartbeat` field; fencing_token stable."""
    json_path = tmp_path / LOCKS_DIR_NAME / INDEX_REBUILD_JSON_NAME
    with with_index_rebuild_lock(
        tmp_path, "xsync", heartbeat=True, heartbeat_interval_s=0.1,
    ) as h:
        original_token = h.token
        # Wait for at least one heartbeat tick (we set interval to 0.1s)
        time.sleep(0.4)
        data = json.loads(json_path.read_text())
        assert data["heartbeat"] is not None, "Heartbeat thread didn't write"
        assert data["fencing_token"] == original_token, (
            "Heartbeat must preserve the fencing_token"
        )


def test_heartbeat_disabled_when_heartbeat_false(tmp_path):
    """`heartbeat=False` doesn't spawn the daemon — JSON's heartbeat stays None."""
    json_path = tmp_path / LOCKS_DIR_NAME / INDEX_REBUILD_JSON_NAME
    with with_index_rebuild_lock(tmp_path, "xsync", heartbeat=False):
        time.sleep(0.2)
        data = json.loads(json_path.read_text())
        assert data["heartbeat"] is None


def test_excepthook_installed_globally():
    """Module-level threading.excepthook is patched to capture heartbeat errors."""
    import threading
    # Just verify the hook isn't the default — full failure-propagation is
    # exercised in integration tests where the daemon thread actually runs.
    assert threading.excepthook.__name__ == "_heartbeat_excepthook"
