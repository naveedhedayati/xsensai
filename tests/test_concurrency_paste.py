"""Subprocess-based concurrency tests — gated on XSENSAI_RUN_INTEGRATION=1.

Tests real OS-level file locking by spawning multiple Python subprocesses
that all attempt to acquire the same card_write lock. Property: exactly one
holder per round. The fcntl.flock contract guarantees this; the test proves
our wrapper preserves the contract.

Why subprocess and not threads: fcntl.flock is per-fd within a process but
also per-process across fds. We need cross-process to exercise the real
contention surface that Slice 4 cron will hit.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("XSENSAI_RUN_INTEGRATION") != "1",
    reason="Set XSENSAI_RUN_INTEGRATION=1 to run subprocess concurrency tests",
)


# Subprocess worker — runs in a fresh Python interpreter via -c so we
# exercise real cross-process flock semantics, not within-process.
WORKER_SCRIPT = """
import json
import os
import sys
import time
sys.path.insert(0, {repo_root!r})
from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from pathlib import Path

corpus = Path({corpus!r})
result = {{"pid": os.getpid(), "acquired": False, "error_code": None}}
try:
    with filelock.with_card_write_lock(corpus, "xpaste") as h:
        result["acquired"] = True
        result["token"] = h.token
        # Hold the lock briefly so other workers contend
        time.sleep(0.1)
except XSensaiError as e:
    result["error_code"] = e.code

print(json.dumps(result))
"""


def _spawn_workers(corpus: Path, count: int) -> list[dict]:
    """Spawn `count` subprocesses simultaneously, all racing for the lock.
    Returns parsed result dicts (one per subprocess).
    """
    repo_root = str(Path(__file__).resolve().parent.parent / "src")
    script = WORKER_SCRIPT.format(repo_root=repo_root, corpus=str(corpus))

    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(p)

    results = []
    for p in procs:
        stdout, stderr = p.communicate(timeout=10)
        if p.returncode != 0:
            print(f"worker stderr: {stderr.decode()}", file=sys.stderr)
        results.append(json.loads(stdout.decode().strip()))
    return results


@pytest.fixture
def corpus(tmp_path):
    c = tmp_path / "corpus"
    c.mkdir()
    return c


class TestLockUniqueness:
    def test_two_concurrent_workers_one_wins(self, corpus):
        """Property: with 2 simultaneous workers, exactly one acquires."""
        results = _spawn_workers(corpus, count=2)
        acquired = [r for r in results if r["acquired"]]
        rejected = [r for r in results if not r["acquired"]]
        assert len(acquired) == 1, f"expected 1 acquirer, got {len(acquired)}: {results}"
        assert len(rejected) == 1
        assert rejected[0]["error_code"] == "LOCK_HELD"

    def test_ten_concurrent_workers_one_wins(self, corpus):
        """Property: with 10 simultaneous workers, exactly one acquires.

        This is the critical test from Eng UC6 — proves the dual-acquire
        bug class is closed. The fcntl-based implementation should have
        exactly one winner per round regardless of N.
        """
        results = _spawn_workers(corpus, count=10)
        acquired = [r for r in results if r["acquired"]]
        rejected = [r for r in results if not r["acquired"]]
        assert len(acquired) == 1, f"expected 1 acquirer, got {len(acquired)}"
        assert len(rejected) == 9
        assert all(r["error_code"] == "LOCK_HELD" for r in rejected)

    def test_sequential_acquire_works(self, corpus):
        """After a round completes, next round can acquire cleanly."""
        # First batch
        results1 = _spawn_workers(corpus, count=3)
        acquired1 = [r for r in results1 if r["acquired"]]
        assert len(acquired1) == 1
        # Second batch (lock released between batches)
        results2 = _spawn_workers(corpus, count=3)
        acquired2 = [r for r in results2 if r["acquired"]]
        assert len(acquired2) == 1
        # Different acquirer (different PID — almost certain across rounds)
        assert acquired1[0]["pid"] != acquired2[0]["pid"]

    def test_token_unique_across_rounds(self, corpus):
        """Each acquire round produces a fresh fencing token (no reuse)."""
        results1 = _spawn_workers(corpus, count=2)
        results2 = _spawn_workers(corpus, count=2)
        token1 = next(r["token"] for r in results1 if r["acquired"])
        token2 = next(r["token"] for r in results2 if r["acquired"])
        assert token1 != token2
