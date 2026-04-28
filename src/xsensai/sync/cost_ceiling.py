"""Per-attempt X API budget tracking for headless cron sync.

Slice 5 / autoplan E9: cap is per-process-attempt, not per-day. Documented
limitation — workflow_dispatch retried after a cron failure starts a fresh
counter. GitHub Actions retry policy MUST be 0 (set in sync.yml) to make
this safe; multiplicative cost amplification only happens if the user
manually retries failed runs aggressively.

Default cap (`XSENSAI_CRON_API_CAP=200`) gives ~30-60x headroom over
expected 3-6 calls/run. The cap exists to prevent runaway loops, not to
ration normal use.

Usage:

    tracker = BudgetTracker.from_env()
    tracker.record_api_call("bookmark_fetch")
    if tracker.should_bail():
        raise tracker.cost_limit_error()
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal

from xsensai.errors import XSensaiError

ApiCallKind = Literal["bookmark_fetch", "thread_search"]

DEFAULT_CAP = 200
CAP_ENV_VAR = "XSENSAI_CRON_API_CAP"


@dataclass
class BudgetTracker:
    """Tracks X API call count per run; raises when cap is exceeded.

    Per-process state. Persisted nowhere — by design (autoplan E9). If the
    process crashes mid-run, restarted run starts fresh; checkpoint dedup
    prevents redundant card writes but API calls within the cap window
    are not amortized across restarts.
    """

    cap: int = DEFAULT_CAP
    bookmark_fetch_count: int = 0
    thread_search_count: int = 0

    @classmethod
    def from_env(cls) -> "BudgetTracker":
        raw = os.environ.get(CAP_ENV_VAR)
        if raw is None:
            return cls(cap=DEFAULT_CAP)
        try:
            cap = int(raw)
        except ValueError:
            raise XSensaiError(
                code="INVALID_FLAGS",
                cause=f"{CAP_ENV_VAR} must be an integer, got {raw!r}",
                attempted=f"BudgetTracker.from_env reading {CAP_ENV_VAR}",
                next_action=f"Unset {CAP_ENV_VAR} or set it to a positive integer.",
                retryable=False,
            )
        if cap < 1:
            raise XSensaiError(
                code="INVALID_FLAGS",
                cause=f"{CAP_ENV_VAR} must be a positive integer, got {cap}",
                attempted=f"BudgetTracker.from_env",
                next_action=f"Set {CAP_ENV_VAR} to >= 1.",
                retryable=False,
            )
        return cls(cap=cap)

    @property
    def total(self) -> int:
        return self.bookmark_fetch_count + self.thread_search_count

    def record_api_call(self, kind: ApiCallKind) -> None:
        if kind == "bookmark_fetch":
            self.bookmark_fetch_count += 1
        elif kind == "thread_search":
            self.thread_search_count += 1
        else:
            raise ValueError(f"Unknown api call kind: {kind!r}")

    def should_bail(self) -> bool:
        return self.total >= self.cap

    def cost_limit_error(self, n_committed: int = 0) -> XSensaiError:
        return XSensaiError(
            code="COST_LIMIT_REACHED",
            cause=(
                f"x-sensai cron hit the API call cap ({self.cap}) before "
                f"completing the run."
            ),
            attempted=(
                f"Cron sync. {self.bookmark_fetch_count} bookmark fetches + "
                f"{self.thread_search_count} thread searches = {self.total} total."
            ),
            next_action=(
                "Run /xsync from Mac to finish the backlog, OR raise "
                f"{CAP_ENV_VAR} (currently {self.cap}) and re-trigger the workflow. "
                "Next scheduled run resumes from checkpoint."
            ),
            retryable=True,
            details=f"Cards committed this run: {n_committed}",
        )


__all__ = ["BudgetTracker", "ApiCallKind", "DEFAULT_CAP", "CAP_ENV_VAR"]
