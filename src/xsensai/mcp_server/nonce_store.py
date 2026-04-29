"""In-memory confirmation-nonce store for destructive MCP tools (Slice 7).

The store backs the two-call handshake on `delete_bookmark` and
`restore_bookmark`. Replaces the host-attestable `user_confirmed: bool`
gate from Slice 6 with a flow that requires the user to echo a one-time
code: server issues code, host displays code, user types code, server
redeems code.

The host LLM can still mint and redeem in a single tool-use chain — the
nonce alone does not prove user attestation (acknowledged limitation,
captured in TROUBLESHOOTING.md). The point is to raise the
social-engineering bar from "host sets a bool" to "user manually echoes
an 8-character string." See plan ~/.claude/plans/vigilant-handshaking-magpie.md
for the dual-voice review.

Design choices (per /autoplan):
- Tombstone-on-redeem: the IssuedNonce record stays in the dict after a
  successful redeem with `redeemed_at` set, so the redeem path can
  distinguish ALREADY_REDEEMED from INVALID. GC removes after expiry +
  grace.
- TTL via `time.monotonic()` so NTP / DST clock corrections cannot
  falsely expire or preserve a live nonce; wall-clock UTC is kept for
  display only.
- Single key = `(operation, target_id)`; reissuance overwrites
  (invalidates the prior nonce for that key). A secondary scan on the
  nonce string lets redeem return OPERATION_MISMATCH when the user
  echoed a code that exists for a different (op, target) pair.
- Always consume on redeem regardless of subsequent op outcome
  (LOCK_HELD, v1 refusal, no-op, etc. all consume the nonce). Single
  rule, no special cases.
- threading.Lock around the dict — FastMCP can multi-thread.

NOT in scope: persistence across MCP server restart (intentional —
short-lived in-memory only); cross-process coordination (single MCP
server per Claude Desktop instance).
"""

from __future__ import annotations

import os
import secrets
import threading
import time
from base64 import b32encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, Final, Literal, Optional, Tuple

from xsensai.errors import XSensaiError

DestructiveOperation = Literal["delete", "restore"]

NONCE_TTL_SECONDS: Final[int] = 90
NONCE_GC_GRACE_SECONDS: Final[int] = 60  # tombstones live this long past expiry
NONCE_BYTES: Final[int] = 5  # 5 random bytes -> 8 base32 chars (40 bits)
NONCE_DELIMITER_OPEN: Final[str] = "<<<NONCE: "
NONCE_DELIMITER_CLOSE: Final[str] = ">>>"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_nonce_for_display(raw: str) -> str:
    """Group as ABCD-EFGH for readability. Not stored — derived on render."""
    return f"{raw[:4]}-{raw[4:]}"


def normalize_user_input(echoed: str) -> str:
    """User-side parser: strip whitespace + hyphens, uppercase. Tolerant
    input matches the case-insensitive + hyphens-optional contract from
    `commands/xrestore.md`. Empty string returned if input is malformed
    in shape (caller treats as cancel).
    """
    if not isinstance(echoed, str):
        return ""
    cleaned = echoed.strip().replace("-", "").replace(" ", "").upper()
    return cleaned


@dataclass
class IssuedNonce:
    nonce: str  # 8-char base32, uppercase, no padding (server canonical form)
    operation: DestructiveOperation
    target_id: str
    issued_at_utc: datetime  # for display + logging only
    issued_monotonic: float
    expires_monotonic: float
    redeemed_at_utc: Optional[datetime] = None  # tombstone marker; None = active

    @property
    def display_nonce(self) -> str:
        return _format_nonce_for_display(self.nonce)


class NonceStore:
    """Thread-safe per-process registry. Single MCP server per host;
    one shared module-level instance is enough.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int = NONCE_TTL_SECONDS,
        gc_grace_seconds: int = NONCE_GC_GRACE_SECONDS,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._gc_grace_seconds = gc_grace_seconds
        self._lock = threading.Lock()
        # Primary index: (operation, target_id) -> IssuedNonce
        self._by_key: Dict[Tuple[str, str], IssuedNonce] = {}

    def issue(
        self,
        *,
        operation: DestructiveOperation,
        target_id: str,
    ) -> IssuedNonce:
        """Issue a nonce for (operation, target_id). Idempotent on host
        retry: if a non-expired non-redeemed record already exists for
        this key, return the SAME record so the user doesn't see a code
        rotate underneath them mid-flow. A fresh nonce is minted only
        when there's no live record (absent, expired, or tombstoned).

        Opportunistically garbage-collects expired tombstones to bound
        dict growth.
        """
        if operation not in ("delete", "restore"):
            raise XSensaiError(
                code="INVALID_FLAGS",
                cause=f"Unsupported destructive operation {operation!r}",
                attempted=f"NonceStore.issue(operation={operation!r}, ...)",
                next_action="operation must be 'delete' or 'restore'",
                retryable=False,
            )

        now_mono = time.monotonic()
        with self._lock:
            self._gc_locked(now_mono)
            existing = self._by_key.get((operation, target_id))
            if (
                existing is not None
                and existing.redeemed_at_utc is None
                and existing.expires_monotonic > now_mono
            ):
                # Live record exists — return it so retried first-call
                # invocations don't rotate the code on the user.
                return existing

            nonce_bytes = secrets.token_bytes(NONCE_BYTES)
            # b32encode pads to multiples of 8 chars; 5 bytes -> 8 chars exactly,
            # but we strip any '=' defensively for shorter inputs.
            nonce = b32encode(nonce_bytes).decode("ascii").rstrip("=")
            record = IssuedNonce(
                nonce=nonce,
                operation=operation,
                target_id=target_id,
                issued_at_utc=_utc_now(),
                issued_monotonic=now_mono,
                expires_monotonic=now_mono + self._ttl_seconds,
            )
            self._by_key[(operation, target_id)] = record
        return record

    def redeem(
        self,
        *,
        nonce: str,
        operation: DestructiveOperation,
        target_id: str,
    ) -> None:
        """Validate and consume a nonce. Always tombstones the record on
        successful redeem (record stays with redeemed_at set; GC purges
        later). Raises XSensaiError on failure with the canonical
        Slice 7 envelopes.

        Failure precedence (deterministic order):
          1. Record present at (op, target) but already redeemed →
             NONCE_ALREADY_REDEEMED
          2. Record present at (op, target) but expired →
             NONCE_EXPIRED
          3. Record present at (op, target) but nonce string differs
             → NONCE_INVALID
          4. Record absent at (op, target) but nonce string matches
             ANY other record → NONCE_OPERATION_MISMATCH
          5. Otherwise → NONCE_INVALID
        """
        normalized = normalize_user_input(nonce)
        now_mono = time.monotonic()

        with self._lock:
            self._gc_locked(now_mono)
            record = self._by_key.get((operation, target_id))
            if record is not None:
                if record.redeemed_at_utc is not None:
                    raise self._err_already_redeemed(operation, target_id)
                if record.expires_monotonic <= now_mono:
                    raise self._err_expired(operation, target_id)
                if record.nonce != normalized:
                    raise self._err_invalid(operation, target_id)
                # Success: tombstone (don't delete — keeps ALREADY_REDEEMED
                # distinguishable from INVALID for repeated calls).
                record.redeemed_at_utc = _utc_now()
                return

            # No record at the (op, target) key. Check whether the
            # supplied nonce matches a record bound to a different
            # (op, target) — that's OPERATION_MISMATCH. Otherwise
            # INVALID.
            if normalized:
                for other in self._by_key.values():
                    if other.nonce == normalized and other.redeemed_at_utc is None:
                        raise self._err_operation_mismatch(
                            attempted_op=operation,
                            attempted_target=target_id,
                            issued_op=other.operation,
                            issued_target=other.target_id,
                        )
            raise self._err_invalid(operation, target_id)

    def garbage_collect(self) -> int:
        """Remove records whose tombstone-grace window has elapsed.
        Returns count removed. Public for explicit invocation in
        long-running tests.
        """
        with self._lock:
            return self._gc_locked(time.monotonic())

    def reset(self) -> None:
        """Test helper: drop all state. NEVER call from production code."""
        with self._lock:
            self._by_key.clear()

    def _gc_locked(self, now_mono: float) -> int:
        # Grace window for tombstones: redeemed records stay until
        # expires_monotonic + grace so a double-redeem within the grace
        # returns ALREADY_REDEEMED rather than INVALID.
        cutoff = now_mono - self._gc_grace_seconds
        stale = [
            key
            for key, rec in self._by_key.items()
            if rec.expires_monotonic <= cutoff
        ]
        for key in stale:
            del self._by_key[key]
        return len(stale)

    # --- error envelopes (slash-command-first next_action wording per AD3) ---

    @staticmethod
    def _user_facing_op(op: str) -> str:
        return {"delete": "/xdelete", "restore": "/xrestore"}.get(op, f"({op})")

    def _err_invalid(
        self, operation: DestructiveOperation, target_id: str
    ) -> XSensaiError:
        cmd = self._user_facing_op(operation)
        return XSensaiError(
            code="NONCE_INVALID",
            cause="The confirmation code did not match any pending request.",
            attempted=f"redeem(op={operation!r}, target={target_id!r})",
            next_action=(
                f"Re-run {cmd} to issue a fresh code. Codes are 8 characters "
                "between the <<<NONCE: ...>>> markers, case-insensitive, "
                "hyphens optional. (see TROUBLESHOOTING.md#nonce-invalid)"
            ),
            retryable=True,
        )

    def _err_expired(
        self, operation: DestructiveOperation, target_id: str
    ) -> XSensaiError:
        cmd = self._user_facing_op(operation)
        return XSensaiError(
            code="NONCE_EXPIRED",
            cause=f"The confirmation code expired ({self._ttl_seconds}s window).",
            attempted=f"redeem(op={operation!r}, target={target_id!r})",
            next_action=(
                f"Re-run {cmd} now — a new code will be issued and you have "
                f"{self._ttl_seconds}s to echo it. "
                "(see TROUBLESHOOTING.md#nonce-expired)"
            ),
            retryable=True,
        )

    def _err_operation_mismatch(
        self,
        *,
        attempted_op: str,
        attempted_target: str,
        issued_op: str,
        issued_target: str,
    ) -> XSensaiError:
        # F3 fix: do NOT echo `issued_op` / `issued_target` back to the
        # caller. The error shape is delivered to the host LLM and a
        # prompt-injected card body could read it; echoing the issued
        # binding would turn this envelope into an enumeration oracle
        # for "which (op, target) does this nonce belong to?". Keep the
        # cause generic; the user already knows what they attempted.
        attempted_cmd = self._user_facing_op(attempted_op)
        return XSensaiError(
            code="NONCE_OPERATION_MISMATCH",
            cause=(
                "The confirmation code was issued for a different operation "
                "or card. Codes are single-use and per-(operation, target)."
            ),
            attempted=f"redeem(op={attempted_op!r}, target={attempted_target!r})",
            next_action=(
                f"Re-run {attempted_cmd} to issue a fresh code bound to this "
                "operation and card. "
                "(see TROUBLESHOOTING.md#nonce-operation-mismatch)"
            ),
            retryable=True,
        )

    def _err_already_redeemed(
        self, operation: DestructiveOperation, target_id: str
    ) -> XSensaiError:
        cmd = self._user_facing_op(operation)
        return XSensaiError(
            code="NONCE_ALREADY_REDEEMED",
            cause="That confirmation code was already used.",
            attempted=f"redeem(op={operation!r}, target={target_id!r})",
            next_action=(
                f"Re-run {cmd} to issue a fresh code. Each code is single-use. "
                "(see TROUBLESHOOTING.md#nonce-already-redeemed)"
            ),
            retryable=True,
        )


# Module-level singleton. Tests use `reset_store()` for isolation.
_STORE: Final[NonceStore] = NonceStore()


def issue_nonce(
    *, operation: DestructiveOperation, target_id: str
) -> IssuedNonce:
    """Public helper used by mcp_server.server."""
    return _STORE.issue(operation=operation, target_id=target_id)


def redeem_nonce(
    *,
    nonce: str,
    operation: DestructiveOperation,
    target_id: str,
) -> None:
    """Public helper used by mcp_server.server. Always consumes (tombstones)
    on success. Raises XSensaiError on any failure path.
    """
    _STORE.redeem(nonce=nonce, operation=operation, target_id=target_id)


def reset_store() -> None:
    """Test helper. Resets the module-level singleton."""
    _STORE.reset()


# --- env-var bypass (AD7) ----------------------------------------------------

_BYPASS_ENV_VAR: Final[str] = "XSENSAI_DESTRUCTIVE_BYPASS"


def destructive_bypass_enabled() -> bool:
    """Read at call time. The bypass is intended to be set in the parent
    shell that spawns the MCP server; the MCP process inherits the env
    var on launch. Within a single Python process, any in-process call
    to `os.environ["XSENSAI_DESTRUCTIVE_BYPASS"]="1"` would also flip
    this — there is no MCP tool that exposes env mutation, but the env
    var itself is process-mutable, not a hardware boundary. Trust this
    flag at the granularity of "the local user controls the spawning
    shell," not "the host LLM cannot ever set it."

    Used by `delete_bookmark` and `restore_bookmark` to skip the nonce
    handshake for scripted maintenance (cron-side bulk cleanup, test
    fixtures). Loud audit log marker is the caller's responsibility.
    """
    raw = os.environ.get(_BYPASS_ENV_VAR, "")
    return raw.lower() in ("1", "true", "yes")
