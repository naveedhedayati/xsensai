"""Error and info contract for x-sensai.

Every user-visible error uses XSensaiError. The format is locked by the spec's
error matrix and rendered via .format():

    [CODE] {one-line cause}
    What was attempted: {action}
    Safe next action: {what to do}
    Retryable: yes | no
    {optional details}

XSensaiInfo provides structured non-error status lines (web miss, no_results,
challenge dup, etc.) so branch outcomes stay contract-compliant instead of
emitting raw English. Format:

    [INFO/CODE] {one-line cause}
    {action_or_note}
    Source: {source}

Prefix taxonomy: error lines start with `[A-Z_]+]`; info lines start with
`[INFO/...]`. Scripts grepping for errors must exclude `^\\[INFO/`.

Codes are constrained to Literals so typos fail type-check and raise at
construction. New codes extend ErrorCode / InfoCode below.
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
    # v1 mutation refusal + MCP confirmation guard
    "V1_MUTATION_BLOCKED",
    "USER_CONFIRMATION_REQUIRED",
    # Retrieval / fallback
    "FALLBACK_FIRED",
    "NO_RESULTS",
    "CORPUS_UNAVAILABLE",
    "QMD_NOT_FOUND",
    # Platform guard (macOS-only)
    "UNSUPPORTED_PLATFORM",
    # /xask error states
    "WEB_FORK_FAILED",
    "EMPTY_CORPUS",
    "TEMPLATE_VALIDATION_FAILED",
    # Slice 4 — sync errors
    "OAUTH_SETUP_REQUIRED",
    "OAUTH_PORT_COLLISION",
    "OAUTH_BROWSER_NOT_DEFAULT",
    "OAUTH_GRANT_REFUSED",
    "OAUTH_KEYCHAIN_BLOCKED",
    "OAUTH_CLIENT_ID_MISSING",
    "X_API_RATE_LIMITED",
    "X_API_NETWORK_ERROR",
    "SYNC_LOCK_HELD",
    "CORPUS_UNREACHABLE",
    "INVALID_FLAGS",
    # Slice 5 — cron-specific failure modes (distinct from manual /xsync surfaces)
    "COST_LIMIT_REACHED",
    "CRON_CONFLICT_UNRESOLVED",
    "SYNC_PUSH_REJECTED",
    "SYNC_AUTH_FAILED",
    # Cron self-rotating refresh token (P0 — fine-grained PAT writeback)
    "GH_SECRET_WRITE_FAILED",
    "TOKEN_PERSIST_FAILED",
    # Slice 6 — tombstone + setup wizard
    "TOMBSTONE_BLOCKED",
    "NO_ROLLBACK_JOURNAL",
    "SETUP_GH_AUTH_REQUIRED",
    "SETUP_DEPLOY_KEY_REJECTED",
    "SETUP_FIRST_RUN_FAILED",
    # Slice 7 — confirmation nonce/handshake for destructive MCP tools
    "NONCE_REQUIRED",
    "NONCE_INVALID",
    "NONCE_EXPIRED",
    "NONCE_OPERATION_MISMATCH",
    "NONCE_ALREADY_REDEEMED",
    # MCP / runtime
    "INTERNAL_ERROR",
]

InfoCode = Literal[
    # /xask non-error status lines (XSensaiInfo envelope)
    "NO_CORPUS_MATCH",
    "WEB_NO_FRESH",
    "WEB_TIMEOUT",
    "WEB_PARSE",
    "WEB_NOT_INSTALLED",  # last30days script missing or not owned by user
    "CHALLENGE_NO_DISSENT",
    # Slice 4 — sync info envelopes (non-error status)
    "CHECKPOINT_RESUME",
    "EXTRACTION_DEFERRED",
    "THREAD_FETCH_FAILED",
    "THREAD_OUTSIDE_7DAY_WINDOW",
    "THREAD_FETCH_UNKNOWN_EMPTY",
    "SEARCH_ALL_UNAVAILABLE",
    "SYNC_DONE",
    "SYNC_PARTIAL",
    "SYNC_PROGRESS",
    "SYNC_STARTING",
    "SYNC_STALE",
    "IDEMPOTENT_SKIP",
    "VAULT_DIRTY_FIRST_RUN",
    "VAULT_NOT_GIT",
    "GIT_LOCKED",
    "THREADS_PERMANENTLY_UNFETCHED",
    "NO_PENDING_EXTRACTIONS",
    "EXTRACT_DONE",
    # Slice 5 — cron status envelopes
    "CRON_RECOVERED_FROM_CONFLICT",
    "CRON_NO_NEW_BOOKMARKS",
    "CRON_PARTIAL_DUE_TO_COST",
    "EXTRACTION_BACKLOG_GROWING",
    "LAZY_EXTRACT_TRIGGERED",
    # Agent-driven setup: a step only a human can do (X dev app / OAuth / pay)
    "HUMAN_ACTION_REQUIRED",
]

_VALID_CODES = frozenset(get_args(ErrorCode))
_VALID_INFO_CODES = frozenset(get_args(InfoCode))


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


@dataclass(frozen=True)
class XSensaiInfo:
    """Non-error status line for /xask branch outcomes (Slice 3, DX2 fix).

    The error contract requires every user-visible diagnostic to flow through
    a structured envelope, not raw English strings. XSensaiInfo is the
    sibling of XSensaiError for cases that aren't errors but ARE status
    worth surfacing in a uniform shape (web miss, no_results, challenge
    dup, etc.).

    Format renders as:

        [INFO/CODE] {one-line cause}
        {action_or_note}
        Source: {source}

    Frozen because XSensaiInfo is not an Exception (no traceback mutation).
    """

    code: InfoCode
    cause: str
    action_or_note: str
    source: str

    def __post_init__(self) -> None:
        if self.code not in _VALID_INFO_CODES:
            raise ValueError(
                f"Unknown info code: {self.code!r}. "
                f"Add it to InfoCode in xsensai/errors.py if it's new."
            )

    def format(self) -> str:
        """Render the canonical user-visible status message."""
        return "\n".join(
            [
                f"[INFO/{self.code}] {self.cause}",
                self.action_or_note,
                f"Source: {self.source}",
            ]
        )

    def __str__(self) -> str:
        return self.format()


__all__ = ["ErrorCode", "InfoCode", "XSensaiError", "XSensaiInfo"]
