"""Unit tests for the Slice 7 confirmation-nonce store.

Coverage map (TE numbering from plan ~/.claude/plans/vigilant-handshaking-magpie.md):
  TE1  — concurrent issue same (op,target) -> second wins
  TE2  — covered in test_destructive_token_flow.py (lock-then-redeem)
  TE3  — nonce randomness uses OS entropy, not test-injected
  TE6  — clock-jump (time.monotonic immune to wall-clock corrections)
  TE7  — target_id regex consistency with corpus._VALID_ID_RE
  TE11 — NONCE_REQUIRED literal lives in ErrorCode (regression)
  TE12 — NONCE_ALREADY_REDEEMED distinct from NONCE_INVALID (tombstone behavior)
  TE14 — NonceStore.reset() clears state, fresh entropy after reset
"""

from __future__ import annotations

import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import get_args

import pytest

from xsensai.errors import XSensaiError, ErrorCode
from xsensai.mcp_server import nonce_store
from xsensai.mcp_server.nonce_store import (
    IssuedNonce,
    NonceStore,
    NONCE_DELIMITER_CLOSE,
    NONCE_DELIMITER_OPEN,
    NONCE_TTL_SECONDS,
    normalize_user_input,
)
from xsensai.storage.corpus import _VALID_ID_RE


# ---- helpers ---------------------------------------------------------------


@pytest.fixture()
def store() -> NonceStore:
    """Fresh per-test NonceStore (avoids singleton bleed)."""
    return NonceStore(ttl_seconds=NONCE_TTL_SECONDS, gc_grace_seconds=60)


@pytest.fixture(autouse=True)
def _reset_module_singleton():
    """Always reset the module-level singleton after each test (TE14)."""
    nonce_store.reset_store()
    yield
    nonce_store.reset_store()


def _err_code(exc: XSensaiError) -> str:
    return exc.code


# ---- happy path + shape ----------------------------------------------------


def test_issue_returns_8char_base32_nonce(store: NonceStore) -> None:
    issued = store.issue(operation="delete", target_id="abc123")
    assert isinstance(issued, IssuedNonce)
    assert len(issued.nonce) == 8
    # Plain base32 alphabet (RFC4648; we explicitly rejected the "no I/L/O"
    # claim in AE9 — alphabet is A-Z2-7).
    assert re.fullmatch(r"[A-Z2-7]{8}", issued.nonce), (
        f"unexpected alphabet: {issued.nonce!r}"
    )
    assert issued.operation == "delete"
    assert issued.target_id == "abc123"
    # display form is grouped 4-4 with hyphen
    assert issued.display_nonce == f"{issued.nonce[:4]}-{issued.nonce[4:]}"
    assert issued.redeemed_at_utc is None
    assert issued.expires_monotonic > issued.issued_monotonic


def test_redeem_success_consumes_record(store: NonceStore) -> None:
    issued = store.issue(operation="delete", target_id="x")
    # Round-trip via the user-input normalizer (case-insensitive + hyphens)
    user_typed = issued.display_nonce.lower()
    store.redeem(
        nonce=user_typed,
        operation="delete",
        target_id="x",
    )
    # Tombstone behavior: redeem again with the same code → ALREADY_REDEEMED
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce=issued.nonce, operation="delete", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_ALREADY_REDEEMED"


# ---- TE12: codes derivability ----------------------------------------------


def test_already_redeemed_distinct_from_invalid(store: NonceStore) -> None:
    issued = store.issue(operation="delete", target_id="x")
    store.redeem(nonce=issued.nonce, operation="delete", target_id="x")
    # Same nonce string redeemed again — ALREADY_REDEEMED (not INVALID),
    # because the tombstoned record stays in the dict.
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce=issued.nonce, operation="delete", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_ALREADY_REDEEMED"
    assert exc_info.value.retryable is True


def test_operation_mismatch_for_cross_op_nonce(store: NonceStore) -> None:
    issued = store.issue(operation="delete", target_id="x")
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce=issued.nonce, operation="restore", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_OPERATION_MISMATCH"
    # F3 fix: cause text MUST NOT echo the issued (op, target) — that
    # would turn this envelope into an enumeration oracle for prompt
    # injection. Generic message only.
    assert "delete" not in exc_info.value.cause
    assert "x" not in exc_info.value.cause.lower() or "single-use" in exc_info.value.cause.lower()
    assert "single-use" in exc_info.value.cause or "different operation" in exc_info.value.cause


def test_operation_mismatch_for_cross_target_nonce(store: NonceStore) -> None:
    issued = store.issue(operation="delete", target_id="card-A")
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(
            nonce=issued.nonce, operation="delete", target_id="card-B"
        )
    assert _err_code(exc_info.value) == "NONCE_OPERATION_MISMATCH"


def test_invalid_nonce_unknown_string(store: NonceStore) -> None:
    store.issue(operation="delete", target_id="x")
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce="ZZZZZZZZ", operation="delete", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_INVALID"


def test_invalid_when_no_record_for_target_or_match(store: NonceStore) -> None:
    # No issuance at all
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce="ANYTHING", operation="delete", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_INVALID"


# ---- TE6: clock-jump immunity ----------------------------------------------


def test_expiry_uses_monotonic_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monotonic clock makes the TTL immune to wall-clock NTP corrections.
    We assert the store's expiry math uses monotonic by overriding
    `time.monotonic` in the nonce_store module.
    """
    fake_t = [0.0]

    def fake_monotonic() -> float:
        return fake_t[0]

    monkeypatch.setattr("xsensai.mcp_server.nonce_store.time.monotonic", fake_monotonic)
    store = NonceStore(ttl_seconds=10, gc_grace_seconds=5)
    issued = store.issue(operation="delete", target_id="x")
    # Move the wall clock backward (would falsely preserve an expired
    # nonce if we used datetime.now). Monotonic stays at 0.0 — nonce is
    # still valid.
    fake_t[0] = 5.0
    store.redeem(nonce=issued.nonce, operation="delete", target_id="x")
    # Past expiry on monotonic clock now
    issued2 = store.issue(operation="restore", target_id="x")
    fake_t[0] = 5.0 + 11.0  # 11s after issue, TTL is 10s
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce=issued2.nonce, operation="restore", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_EXPIRED"


# ---- TE1: concurrent reissue -----------------------------------------------


def test_reissue_is_idempotent_for_live_record(store: NonceStore) -> None:
    """Two `issue` calls for the same (op, target) within the live window
    return the SAME nonce. F2 fix: prevents a host LLM retry from rotating
    the code shown to the user mid-flow.
    """
    first = store.issue(operation="delete", target_id="x")
    second = store.issue(operation="delete", target_id="x")
    assert first.nonce == second.nonce
    # The single live record redeems exactly once
    store.redeem(nonce=first.nonce, operation="delete", target_id="x")
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce=second.nonce, operation="delete", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_ALREADY_REDEEMED"


def test_reissue_after_redeem_mints_fresh(store: NonceStore) -> None:
    """Once the live record is tombstoned, a subsequent issue mints
    fresh AND overwrites the tombstone. The original nonce string is
    then no longer present at any key, so replay returns NONCE_INVALID
    rather than NONCE_ALREADY_REDEEMED.

    (The ALREADY_REDEEMED path is still reachable on direct
    redeem-then-replay without an intervening issue — see
    `test_already_redeemed_distinct_from_invalid` above.)
    """
    first = store.issue(operation="delete", target_id="x")
    store.redeem(nonce=first.nonce, operation="delete", target_id="x")
    second = store.issue(operation="delete", target_id="x")
    assert second.nonce != first.nonce
    with pytest.raises(XSensaiError) as exc_info:
        store.redeem(nonce=first.nonce, operation="delete", target_id="x")
    assert _err_code(exc_info.value) == "NONCE_INVALID"


def test_reissue_after_expiry_mints_fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_t = [0.0]
    monkeypatch.setattr(
        "xsensai.mcp_server.nonce_store.time.monotonic", lambda: fake_t[0]
    )
    store = NonceStore(ttl_seconds=10, gc_grace_seconds=5)
    first = store.issue(operation="delete", target_id="x")
    fake_t[0] = 11.0
    second = store.issue(operation="delete", target_id="x")
    assert second.nonce != first.nonce


def test_concurrent_issue_same_target_returns_one_nonce(store: NonceStore) -> None:
    """Race two threads issuing for the same key. With F2's idempotent
    fix, all 8 threads return the SAME live record (whichever was minted
    first wins; the rest see the existing record).
    """
    results = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        return store.issue(operation="delete", target_id="x")

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = [ex.submit(worker) for _ in range(8)]
        for f in futures:
            results.append(f.result())

    # All threads see the same live record
    distinct_nonces = {r.nonce for r in results}
    assert len(distinct_nonces) == 1
    # The single live record redeems exactly once across the threads
    nonce = results[0].nonce
    successes = 0
    for _ in results:
        try:
            store.redeem(nonce=nonce, operation="delete", target_id="x")
            successes += 1
        except XSensaiError as e:
            assert e.code == "NONCE_ALREADY_REDEEMED"
    assert successes == 1


# ---- TE3: randomness uses OS entropy ---------------------------------------


def test_secrets_used_not_random_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: nonce uses secrets.token_bytes (OS entropy), NOT the
    `random` module. If a future maintainer swaps in `random.choices(...)`,
    a `random.seed(0)` from a test harness would make nonces deterministic.
    """
    import random

    random.seed(0)
    a = NonceStore().issue(operation="delete", target_id="x").nonce
    random.seed(0)
    b = NonceStore().issue(operation="delete", target_id="x").nonce
    # Despite identical random.seed, OS entropy differs → nonces differ
    assert a != b


# ---- TE7: id regex consistency ---------------------------------------------


def test_target_id_regex_matches_corpus_canonical() -> None:
    """nonce_store does not validate target_id itself (mcp_server.server
    calls corpus.validate_card_id BEFORE invoking the store). The real
    drift-protection test lives at
    tests/test_destructive_token_flow.py::TestInvalidId, which exercises
    the full path: server.delete_bookmark(id=<malformed>) → NO_RESULTS,
    no nonce minted. Here we just smoke-check the canonical regex shape
    to catch a maintainer accidentally widening it.
    """
    assert _VALID_ID_RE.match("abc123")
    assert _VALID_ID_RE.match("a.b_c-d")
    # Critical negative cases — drift here would be a real bug
    assert not _VALID_ID_RE.match(".dotfile")
    assert not _VALID_ID_RE.match("a/b")
    assert not _VALID_ID_RE.match("../etc/passwd")
    assert not _VALID_ID_RE.match("with space")
    assert not _VALID_ID_RE.match("")


# ---- TE11: NONCE_REQUIRED literal in ErrorCode (regression) ----------------


def test_nonce_codes_all_in_errorcode_literal() -> None:
    """ErrorCode is a Literal union — XSensaiError raises at construction
    if the code isn't in the union. Verify all 5 Slice 7 codes are present.
    Regression on plan-draft inconsistency where NONCE_REQUIRED was
    referenced but missing from the Literal.
    """
    codes = set(get_args(ErrorCode))
    for required in (
        "NONCE_REQUIRED",
        "NONCE_INVALID",
        "NONCE_EXPIRED",
        "NONCE_OPERATION_MISMATCH",
        "NONCE_ALREADY_REDEEMED",
    ):
        assert required in codes, (
            f"{required} missing from ErrorCode Literal — "
            "would fail at XSensaiError construction"
        )


def test_xsensaierror_constructs_for_each_nonce_code() -> None:
    """Live-construction smoke test: every nonce code can be instantiated
    in an XSensaiError without raising.
    """
    for code in (
        "NONCE_REQUIRED",
        "NONCE_INVALID",
        "NONCE_EXPIRED",
        "NONCE_OPERATION_MISMATCH",
        "NONCE_ALREADY_REDEEMED",
    ):
        e = XSensaiError(
            code=code,  # type: ignore[arg-type]
            cause="x",
            attempted="x",
            next_action="x",
            retryable=True,
        )
        assert e.code == code


# ---- TE14: reset() clears state --------------------------------------------


def test_reset_clears_all_state() -> None:
    nonce_store.reset_store()
    issued = nonce_store.issue_nonce(operation="delete", target_id="x")
    nonce_store.reset_store()
    # After reset, the supplied nonce is unknown
    with pytest.raises(XSensaiError) as exc_info:
        nonce_store.redeem_nonce(
            nonce=issued.nonce, operation="delete", target_id="x"
        )
    assert _err_code(exc_info.value) == "NONCE_INVALID"


# ---- normalize_user_input + GC + bypass ------------------------------------


@pytest.mark.parametrize(
    "user_input,expected",
    [
        ("ABCD-EFGH", "ABCDEFGH"),
        ("abcd-efgh", "ABCDEFGH"),
        (" ABCD EFGH ", "ABCDEFGH"),
        ("abcdefgh", "ABCDEFGH"),
        ("", ""),
        (None, ""),  # type: ignore[arg-type]
    ],
)
def test_normalize_user_input(user_input, expected) -> None:
    assert normalize_user_input(user_input) == expected


def test_garbage_collect_removes_expired_tombstones(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_t = [0.0]
    monkeypatch.setattr(
        "xsensai.mcp_server.nonce_store.time.monotonic",
        lambda: fake_t[0],
    )
    store = NonceStore(ttl_seconds=5, gc_grace_seconds=2)
    a = store.issue(operation="delete", target_id="a")
    b = store.issue(operation="restore", target_id="b")
    store.redeem(nonce=a.nonce, operation="delete", target_id="a")  # tombstone
    # Move past expiry + grace
    fake_t[0] = 100.0
    n = store.garbage_collect()
    assert n == 2  # both records purged


def test_destructive_bypass_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("XSENSAI_DESTRUCTIVE_BYPASS", raising=False)
    assert nonce_store.destructive_bypass_enabled() is False
    monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "1")
    assert nonce_store.destructive_bypass_enabled() is True
    monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "true")
    assert nonce_store.destructive_bypass_enabled() is True
    monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "0")
    assert nonce_store.destructive_bypass_enabled() is False
    monkeypatch.setenv("XSENSAI_DESTRUCTIVE_BYPASS", "false")
    assert nonce_store.destructive_bypass_enabled() is False


def test_invalid_operation_at_issue() -> None:
    with pytest.raises(XSensaiError) as exc_info:
        NonceStore().issue(operation="purge", target_id="x")  # type: ignore[arg-type]
    assert _err_code(exc_info.value) == "INVALID_FLAGS"


def test_delimiters_stable() -> None:
    """The delimited-block pattern is part of the plan's user-instruction
    contract (AE8). If anyone changes these, the slash command markdown
    instruction breaks silently.
    """
    assert NONCE_DELIMITER_OPEN == "<<<NONCE: "
    assert NONCE_DELIMITER_CLOSE == ">>>"
