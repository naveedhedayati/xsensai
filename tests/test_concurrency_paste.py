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
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


pytestmark = pytest.mark.skipif(
    os.environ.get("XSENSAI_RUN_INTEGRATION") != "1",
    reason="Set XSENSAI_RUN_INTEGRATION=1 to run subprocess concurrency tests",
)


# Subprocess worker — runs in a fresh Python interpreter via -c so we
# exercise real cross-process flock semantics, not within-process.
#
# Readiness barrier (see _spawn_workers): each cold interpreter writes a
# ready-marker AFTER its imports finish, then blocks until the parent drops a
# `go` file. Without the barrier, slow+variable interpreter startup (importing
# pydantic/mcp/etc.) staggers the workers — an early winner can acquire AND
# release the briefly-held lock before a late starter even reaches the flock
# call. That produces multiple "winners" that never actually held the lock at
# the same instant: a false failure of a sound lock. The barrier forces all
# workers to contend inside the same tiny window, which is the real thing under
# test. _spawn_workers fails loud if the barrier can't synchronize rather than
# silently running a degraded (staggered) round that could false-fail.

# Timing tunables, shared by the parent and the worker template so the two
# deadlines can't drift. GATE_DEADLINE_S is generous on purpose: it must exceed
# worst-case cold-interpreter startup for `count` workers on a loaded runner.
GATE_DEADLINE_S = 15.0
LOCK_HOLD_S = 0.2
GATE_POLL_S = 0.002
READY_POLL_S = 0.005
COMMUNICATE_TIMEOUT_S = 20.0

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
gate = Path({gate!r})
result = {{"pid": os.getpid(), "acquired": False, "error_code": None, "gate_timeout": False}}

# Signal "imports done, parked at the gate", then wait for the release.
(gate / ("ready.%d" % os.getpid())).write_text("1")
_deadline = time.monotonic() + {gate_deadline}
while not (gate / "go").exists():
    if time.monotonic() > _deadline:
        result["gate_timeout"] = True
        break
    time.sleep({gate_poll})

try:
    with filelock.with_card_write_lock(corpus, "xpaste") as h:
        result["acquired"] = True
        result["token"] = h.token
        # Hold long enough that every co-released worker attempts (and fails)
        # while we still hold. All workers cleared the gate microseconds apart.
        time.sleep({hold})
except XSensaiError as e:
    result["error_code"] = e.code

print(json.dumps(result))
"""


def _spawn_workers(corpus: Path, count: int) -> list[dict]:
    """Spawn `count` subprocesses that all contend for the lock simultaneously.

    Uses a readiness barrier so the race is real: every worker finishes its
    (slow, variable) interpreter startup and parks at a shared gate; once all
    `count` workers are parked, the parent releases them at once. This removes
    the startup-stagger artifact that made this test flaky (multiple non-
    overlapping "winners"). Returns parsed result dicts (one per subprocess).
    """
    repo_root = str(Path(__file__).resolve().parent.parent / "src")
    gate = Path(tempfile.mkdtemp(prefix="xsensai-gate-"))
    go_file = gate / "go"
    script = WORKER_SCRIPT.format(
        repo_root=repo_root,
        corpus=str(corpus),
        gate=str(gate),
        gate_deadline=GATE_DEADLINE_S,
        gate_poll=GATE_POLL_S,
        hold=LOCK_HOLD_S,
    )

    procs = []
    for _ in range(count):
        p = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        procs.append(p)

    try:
        # Wait until every worker has cleared imports and is parked at the gate.
        deadline = time.monotonic() + GATE_DEADLINE_S
        parked = 0
        while time.monotonic() < deadline:
            parked = len(list(gate.glob("ready.*")))
            if parked >= count:
                break
            time.sleep(READY_POLL_S)
        # Fail loud if the barrier never synchronized. Touching `go` anyway would
        # silently degrade to the startup-stagger race this barrier exists to
        # remove, yielding a FALSE "multiple winners" failure that reads as a
        # lock bug. A clear message here is correctly attributed to the harness.
        if parked < count:
            raise AssertionError(
                f"lock barrier failed to synchronize: only {parked}/{count} "
                f"workers parked within {GATE_DEADLINE_S}s (loaded runner?). "
                "Rerun — this is a harness setup issue, not a lock failure."
            )
        # Release all workers at the same instant.
        go_file.touch()

        results = []
        for p in procs:
            try:
                stdout, stderr = p.communicate(timeout=COMMUNICATE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                p.kill()
                stdout, stderr = p.communicate()
            text = stdout.decode().strip()
            if not text:
                # Empty stdout = the worker crashed before printing (e.g. an
                # import/lock regression). Surface its stderr instead of letting
                # json.loads raise a cryptic JSONDecodeError that hides the cause.
                raise AssertionError(
                    f"worker pid={p.pid} produced no output "
                    f"(returncode={p.returncode}). stderr:\n{stderr.decode()}"
                )
            results.append(json.loads(text))
        # A worker that gave up waiting for `go` ran outside the barrier, so its
        # result can't prove the uniqueness property — fail rather than trust it.
        timed_out = [r["pid"] for r in results if r.get("gate_timeout")]
        if timed_out:
            raise AssertionError(
                f"workers {timed_out} hit the {GATE_DEADLINE_S}s gate deadline "
                "and contended unsynchronized. Rerun — harness setup issue."
            )
        return results
    finally:
        # Reap any still-running workers before deleting the gate so a timeout
        # or an assertion above can't leak subprocesses that still hold the flock.
        for p in procs:
            if p.poll() is None:
                p.kill()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        shutil.rmtree(gate, ignore_errors=True)


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
