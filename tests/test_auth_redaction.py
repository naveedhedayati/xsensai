"""Slice 5 — redaction helper tests (autoplan E7)."""

from __future__ import annotations

from xsensai.sync.auth import redact_token_strings


def test_redacts_bearer_prefix():
    text = "Authorization: Bearer ya29.A0ARrdaM-very-long-secret-token-here-12345"
    out = redact_token_strings(text)
    assert "ya29.A0ARrdaM" not in out
    assert "<REDACTED>" in out


def test_redacts_long_opaque_token():
    """Any 32+ char run of url-safe base64 chars is treated as suspicious."""
    text = "got token: AbCdEf123456_-AbCdEf123456_-AbCdEf12 then next."
    out = redact_token_strings(text)
    assert "AbCdEf123456_-AbCdEf123456_-AbCdEf12" not in out
    assert "<REDACTED:32+>" in out


def test_short_strings_not_redacted():
    text = "user_id is 12345 and key is abc"
    out = redact_token_strings(text)
    assert out == text


def test_redacts_extra_secrets():
    """Caller can pass live env values to redact verbatim."""
    text = "OAuth refresh_token=my-refresh-9876 expired"
    out = redact_token_strings(text, extra_secrets=["my-refresh-9876"])
    assert "my-refresh-9876" not in out
    assert "<REDACTED>" in out


def test_extra_secret_too_short_not_redacted():
    """Don't redact extra_secrets shorter than 8 chars (false positive risk)."""
    text = "the username is jay and that's it"
    out = redact_token_strings(text, extra_secrets=["jay"])
    assert "jay" in out  # too short to redact


def test_empty_input():
    assert redact_token_strings("") == ""
    assert redact_token_strings("", extra_secrets=["x" * 40]) == ""


def test_redacts_multiple_bearer_tokens():
    text = "first: Bearer abc123def456ghi789jkl012mno345 second: Bearer xyz789uvw456rst123"
    out = redact_token_strings(text)
    assert "abc123def456" not in out
    assert "xyz789uvw456" not in out
    assert out.count("<REDACTED>") >= 2


def test_realistic_log_line_redacted():
    """Realistic log-line: traceback + raw token. Must scrub."""
    text = (
        "ERROR auth.py:144 OAuth exchange failed: HTTP 401 — "
        "tried with token=ya29.aBCdef1234567890ABCDEFhijklmnopqrstuvwxyz, "
        "client_id=test-client. Response body: 'invalid_grant'"
    )
    out = redact_token_strings(text)
    assert "ya29.aBCdef1234567890" not in out
    # Diagnostic context preserved
    assert "OAuth exchange failed" in out
    assert "HTTP 401" in out
