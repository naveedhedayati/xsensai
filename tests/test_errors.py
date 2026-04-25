"""Tests for the error contract module.

Locks the spec's error message format from import-time so every later slice
inherits the same shape.
"""

from __future__ import annotations

import pytest

from xsensai.errors import XSensaiError


def test_format_renders_all_required_lines() -> None:
    err = XSensaiError(
        code="LOCK_HELD",
        cause="x-sensai is currently syncing.",
        attempted="/xpaste tried to write a new card.",
        next_action="Wait ~3 minutes and re-run /xpaste.",
        retryable=True,
    )
    out = err.format()
    assert out.startswith("[LOCK_HELD] x-sensai is currently syncing.")
    assert "What was attempted: /xpaste tried to write a new card." in out
    assert "Safe next action: Wait ~3 minutes and re-run /xpaste." in out
    assert "Retryable: yes" in out


def test_format_includes_optional_details_line() -> None:
    err = XSensaiError(
        code="LOCK_HELD",
        cause="x-sensai is currently syncing.",
        attempted="/xpaste tried to write a new card.",
        next_action="Wait ~3 minutes and re-run /xpaste.",
        retryable=True,
        details="Lock holder: cron on github-actions, started 2026-04-25T14:21:00Z",
    )
    out = err.format()
    assert out.endswith(
        "Lock holder: cron on github-actions, started 2026-04-25T14:21:00Z"
    )
    assert out.count("\n") == 4  # 4 required lines + 1 details line, joined by 4 newlines


def test_retryable_renders_no_when_false() -> None:
    err = XSensaiError(
        code="VIDEO_UNAVAILABLE",
        cause="Video unavailable or age-restricted.",
        attempted="yt-dlp fetch.",
        next_action="No retry available; manual review.",
        retryable=False,
    )
    assert "Retryable: no" in err.format()


def test_unknown_code_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="Unknown error code"):
        XSensaiError(  # type: ignore[arg-type]
            code="NOT_A_REAL_CODE",
            cause="x",
            attempted="y",
            next_action="z",
            retryable=True,
        )


def test_retryable_must_be_strict_bool() -> None:
    with pytest.raises(TypeError, match="retryable must be a bool"):
        XSensaiError(
            code="LOCK_HELD",
            cause="x",
            attempted="y",
            next_action="z",
            retryable=1,  # type: ignore[arg-type]
        )


def test_str_returns_format() -> None:
    err = XSensaiError(
        code="NO_RESULTS",
        cause="No matching cards.",
        attempted="search_bookmarks.",
        next_action="Try a different query.",
        retryable=False,
    )
    assert str(err) == err.format()


def test_xsensai_error_is_an_exception() -> None:
    """Should be raisable so call sites can use try/except naturally."""
    err = XSensaiError(
        code="INTERNAL_ERROR",
        cause="Something went wrong.",
        attempted="Anything.",
        next_action="Report a bug.",
        retryable=False,
    )
    with pytest.raises(XSensaiError):
        raise err
