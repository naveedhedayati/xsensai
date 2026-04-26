"""Error contract for x-sensai.

Every user-visible error in every slice uses XSensaiError. The format is locked
by the spec's error matrix and rendered via .format():

    [CODE] {one-line cause}
    What was attempted: {action}
    Safe next action: {what to do}
    Retryable: yes | no
    {optional details}

Codes are constrained to a Literal so a typo at the call site fails type-check
and (defensively) raises at construction. New codes are added by extending
ErrorCode below; the spec's error matrix is the source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, get_args

ErrorCode = Literal[
    # Sync / cron
    "AUTH_FAILED",
    "RATE_LIMITED",
    "NETWORK_TIMEOUT",
    "TWEET_DELETED",
    "EXTRACTION_FAILED",
    "REINDEX_PARTIAL",
    "PUSH_REJECTED",
    "REBASE_CONFLICT",
    "DISK_WRITE_FAILED",
    "YAML_PARSE_FAILED",
    "TRANSCRIPT_FAILED",
    "TRANSCRIPT_SKIPPED",
    "VIDEO_UNAVAILABLE",
    # Concurrency
    "LOCK_HELD",
    "STALE_LOCK_RECLAIMED",
    "MID_WRITE_DETECTED",
    # Paste / annotate
    "PASTE_EMPTY",
    "PASTE_CRASHED",
    # Slice 2: v1 mutation refusal + MCP confirmation guard
    "V1_MUTATION_BLOCKED",
    "USER_CONFIRMATION_REQUIRED",
    # Retrieval / fallback
    "FALLBACK_FIRED",
    "NO_RESULTS",
    "CORPUS_UNAVAILABLE",
    # MCP / runtime
    "INTERNAL_ERROR",
]

_VALID_CODES = frozenset(get_args(ErrorCode))


# Not frozen: Python's exception machinery mutates __traceback__ / __cause__ /
# __notes__ during raise/except/contextlib teardown. A frozen dataclass that
# inherits from Exception triggers FrozenInstanceError in async fixture teardown.
# Slice 1 caught this in test_async_concurrency. Don't freeze exception dataclasses.
@dataclass
class XSensaiError(Exception):
    """A user-visible error following the spec's error contract.

    Construct with a code from ErrorCode and the four required fields. Optional
    `details` is a free-form string appended after the four required lines.
    Call .format() to render the canonical message.

    Construction with an unknown code raises ValueError immediately so typos
    cannot silently propagate.
    """

    code: ErrorCode
    cause: str
    attempted: str
    next_action: str
    retryable: bool
    details: str | None = field(default=None)

    def __post_init__(self) -> None:
        if self.code not in _VALID_CODES:
            raise ValueError(
                f"Unknown error code: {self.code!r}. "
                f"Add it to ErrorCode in xsensai/errors.py if it's new."
            )
        if not isinstance(self.retryable, bool):
            raise TypeError(
                f"retryable must be a bool, got {type(self.retryable).__name__}"
            )

    def format(self) -> str:
        """Render the canonical user-visible message per the spec contract."""
        retryable_str = "yes" if self.retryable else "no"
        lines = [
            f"[{self.code}] {self.cause}",
            f"What was attempted: {self.attempted}",
            f"Safe next action: {self.next_action}",
            f"Retryable: {retryable_str}",
        ]
        if self.details:
            lines.append(self.details)
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.format()


__all__ = ["ErrorCode", "XSensaiError"]
