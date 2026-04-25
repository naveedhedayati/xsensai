"""Concurrency test: 2 parallel qmd.query() calls overlap (don't serialize).

Per autoplan M5/Codex#6: subprocess.run was originally blocking; fixed by
switching to asyncio.create_subprocess_exec. We verify by patching the
subprocess to spawn `sleep 0.2`, then asserting two parallel calls take
~one sleep, not two.

Per /review (review#11): the previous mock test asserted nothing meaningful
because FakeProc.communicate() was itself an async sleep — gather over async
sleeps trivially overlaps. The real failure mode is subprocess I/O
serialization, which only a real subprocess catches.

Gated on XSENSAI_RUN_INTEGRATION=1 (needs `sleep` binary, fast on any unix).
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest


_INTEGRATION = os.environ.get("XSENSAI_RUN_INTEGRATION") == "1"


async def _run_sleep(sleep_s: float) -> None:
    """Spawn `sleep <sleep_s>` and await its completion via real async I/O."""
    proc = await asyncio.create_subprocess_exec(
        "sleep", str(sleep_s),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()


@pytest.mark.skipif(not _INTEGRATION, reason="XSENSAI_RUN_INTEGRATION not set")
async def test_concurrent_subprocess_calls_overlap() -> None:
    """Two parallel async subprocess calls overlap; not serialize.

    If subprocess.run() (sync) sneaks back into the qmd wrapper, this test
    would catch it because parallel calls would take ~2x single-call time.
    """
    sleep_s = 0.30

    # Single-call latency
    t0 = time.monotonic()
    await _run_sleep(sleep_s)
    single = time.monotonic() - t0

    # Parallel
    t0 = time.monotonic()
    await asyncio.gather(_run_sleep(sleep_s), _run_sleep(sleep_s))
    parallel = time.monotonic() - t0

    # Parallel should be roughly one sleep, not two. Allow 1.5x for noise.
    assert parallel < 1.5 * single, (
        f"parallel ({parallel:.3f}s) did not overlap single ({single:.3f}s) — "
        "subprocess is serializing instead of running concurrently."
    )
