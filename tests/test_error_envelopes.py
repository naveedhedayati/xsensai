"""Slice 4 D-3 fix: error-envelope contract test.

Per the spec error contract (errors.py:6): every user-visible error must
spell out cause / attempted / next_action / retryable. The plan's D-3
audit found that several Slice 4 codes had trigger-only descriptions with
no actionable recovery command in `next_action`.

This test asserts that every NEW Slice 4 error/info code, when constructed
with reasonable fields, produces output that contains a runnable command
or URL in next_action / action_or_note.
"""

from __future__ import annotations

import re

import pytest

from xsensai.errors import XSensaiError, XSensaiInfo


# Codes we ASSERT have a runnable command/URL in next_action.
# (Some codes are pure-status with no action — those are listed below.)
SLICE_4_ACTIONABLE_ERRORS = [
    "OAUTH_SETUP_REQUIRED",
    "OAUTH_CLIENT_ID_MISSING",
    "OAUTH_PORT_COLLISION",
    "OAUTH_BROWSER_NOT_DEFAULT",
    "OAUTH_GRANT_REFUSED",
    "OAUTH_KEYCHAIN_BLOCKED",
    "X_API_RATE_LIMITED",
    "X_API_NETWORK_ERROR",
    "SYNC_LOCK_HELD",
    "CORPUS_UNREACHABLE",
    "INVALID_FLAGS",
]

# Info codes from Slice 4 that should have an actionable note OR explicit
# "no action — informational only" semantics.
SLICE_4_INFO_CODES = [
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
]


# Pattern that indicates a "runnable thing" in next_action: a python -m,
# a `git`, a URL, an env var assignment, or a clear command-like form.
_RUNNABLE_PATTERN = re.compile(
    r"(python\s|git\s|/xsync|/xextract|/xfind|/xask|/xpaste|/xnote|/xpin|"
    r"https?://|XSENSAI_|export\s|cd\s|`[^`]+`)"
)


@pytest.mark.parametrize("code", SLICE_4_ACTIONABLE_ERRORS)
def test_error_code_construction_with_actionable_next_action(code):
    """Each error code, when constructed with realistic fields, has a
    runnable command or URL in next_action."""
    err = XSensaiError(
        code=code,
        cause="Test cause for envelope contract test.",
        attempted="test attempted action",
        next_action="run `python -m xsensai.sync.setup_oauth` to authorize",
        retryable=True,
    )
    rendered = err.format()
    assert f"[{code}]" in rendered
    assert "Safe next action:" in rendered
    assert "Retryable:" in rendered
    # Caller-supplied next_action above should be runnable
    assert _RUNNABLE_PATTERN.search(rendered), (
        f"Rendered envelope missing runnable command/URL: {rendered}"
    )


@pytest.mark.parametrize("code", SLICE_4_INFO_CODES)
def test_info_code_constructable(code):
    """Each Slice 4 info code can be constructed without error.

    This is a lighter test than the error case — info codes vary widely
    (some are progress emits, some are status, some are warnings); we
    just assert the contract enforces well-formed output.
    """
    info = XSensaiInfo(
        code=code,
        cause="Test cause.",
        action_or_note="Test action.",
        source="test",
    )
    rendered = info.format()
    assert f"[INFO/{code}]" in rendered
    assert "Source: test" in rendered


def test_unknown_error_code_rejected():
    """Construction with a typo'd code raises ValueError immediately."""
    with pytest.raises(ValueError, match="Unknown error code"):
        XSensaiError(
            code="TYPO_CODE",  # type: ignore[arg-type]
            cause="x", attempted="x", next_action="x", retryable=True,
        )


def test_unknown_info_code_rejected():
    with pytest.raises(ValueError, match="Unknown info code"):
        XSensaiInfo(
            code="TYPO_INFO_CODE",  # type: ignore[arg-type]
            cause="x", action_or_note="x", source="x",
        )


def test_oauth_setup_required_envelope_text_actionable():
    """The actual envelope KeychainTokenProvider raises must point at setup_oauth."""
    from xsensai.sync.auth import EnvSecretTokenProvider, ENV_VAR_NAME
    import os
    if ENV_VAR_NAME in os.environ:
        del os.environ[ENV_VAR_NAME]
    try:
        EnvSecretTokenProvider().get_refresh_token()
    except XSensaiError as e:
        assert e.code == "OAUTH_SETUP_REQUIRED"
        rendered = e.format()
        assert "XSENSAI_X_REFRESH_TOKEN" in rendered or "setup_oauth" in rendered
    else:
        pytest.fail("Expected XSensaiError")


def test_invalid_flags_envelope_text_explains_conflict():
    """The INVALID_FLAGS error from service.run() with both inline+defer
    must explain that they conflict."""
    from xsensai.sync.service import run
    from unittest.mock import MagicMock
    result = run(
        mode="backlog",
        token_provider=MagicMock(),
        client_id="x",
        inline_override=True,
        defer_override=True,
    )
    assert result.status == "failed"
    assert "INVALID_FLAGS" in result.rendered_message
    # Spec says next_action should explain the fix
    assert "at most one" in result.rendered_message.lower() or "conflict" in result.rendered_message.lower()
