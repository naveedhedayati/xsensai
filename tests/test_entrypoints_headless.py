"""Slice 5 — headless entrypoint tests.

Most heavy testing happens at integration level (test_headless_e2e —
gated). These unit tests cover:
  - --check preflight
  - --emit-secrets-stdin output shape (no secret VALUES, just commands)
  - missing-env path returns 2
  - auth recovery template has no secret-bearing strings
"""

from __future__ import annotations

import io
import os
import re
import sys
from pathlib import Path

import pytest

from xsensai.entrypoints import headless


def test_check_preflight_missing_env(monkeypatch, capsys):
    monkeypatch.delenv("XSENSAI_X_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("XSENSAI_X_CLIENT_ID", raising=False)
    rc = headless._check_preflight()
    captured = capsys.readouterr()
    assert rc == 2
    assert "PREFLIGHT FAIL" in captured.err
    assert "XSENSAI_X_REFRESH_TOKEN" in captured.err
    assert "XSENSAI_X_CLIENT_ID" in captured.err


def test_check_preflight_env_set(monkeypatch, capsys):
    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "test-refresh-token")
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "test-client-id")
    rc = headless._check_preflight()
    captured = capsys.readouterr()
    assert rc == 0
    assert "PREFLIGHT OK" in captured.err


def test_emit_secrets_stdin_output_shape(capsys):
    rc = headless._emit_secrets_stdin()
    captured = capsys.readouterr()
    assert rc == 0
    out = captured.out
    # Should contain command templates, not actual secret values.
    assert "security find-generic-password" in out
    assert "gh secret set XSENSAI_X_REFRESH_TOKEN" in out
    assert "gh secret set XSENSAI_X_CLIENT_ID" in out
    # CLIENT_SECRET is commented out (only-if-confidential).
    assert "# security find-generic-password" in out
    assert "VAULT_DEPLOY_KEY" in out


def test_emit_secrets_stdin_no_actual_secret_values(capsys, monkeypatch):
    """Even if env vars are set, --emit-secrets-stdin must not echo them."""
    monkeypatch.setenv(
        "XSENSAI_X_REFRESH_TOKEN", "Bearer-abcdef123456-secret-value"
    )
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "leak-test-client")

    headless._emit_secrets_stdin()
    captured = capsys.readouterr()
    out = captured.out
    # The actual values must NOT appear in stdout.
    assert "Bearer-abcdef" not in out
    assert "leak-test-client" not in out
    # The variable NAME is fine to appear (it's not secret).
    assert "XSENSAI_X_REFRESH_TOKEN" in out


def test_run_missing_env_returns_2(monkeypatch, capsys):
    monkeypatch.delenv("XSENSAI_X_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("XSENSAI_X_CLIENT_ID", raising=False)
    # Need an existing corpus path to avoid CORPUS_UNAVAILABLE distraction
    monkeypatch.setenv(
        "XSENSAI_CORPUS_PATH",
        str(Path(__file__).parent / "fixtures" / "cards"),
    )
    rc = headless.run()
    assert rc == 2
    captured = capsys.readouterr()
    assert "OAUTH_SETUP_REQUIRED" in captured.err


def test_auth_failed_recovery_text_no_secrets():
    text = headless._auth_failed_recovery_text("test-run-001")
    # Static template assertions
    assert "test-run-001" in text  # run_id is fine
    assert "setup_oauth --reauth" in text
    assert "gh secret set XSENSAI_X_REFRESH_TOKEN" in text
    assert "git rm SYNC_AUTH_FAILED.md" in text

    # Sentinel: no env-var values, no Bearer prefix, no random secret
    # interpolation
    bad_patterns = [
        "Bearer ",
        "ya29.",  # google bearer prefix
        "ghp_",   # github classic PAT prefix
        "ghs_",   # github fine-grained prefix
    ]
    for bad in bad_patterns:
        assert bad not in text, f"flag template leaked pattern: {bad}"


def test_auth_failed_recovery_text_no_env_var_interpolation(monkeypatch):
    """Auth recovery template must not interpolate env var values."""
    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "this-should-not-leak")
    text = headless._auth_failed_recovery_text("run-leak-test")
    assert "this-should-not-leak" not in text


def test_emit_secrets_stdin_via_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv",
                        ["headless", "--emit-secrets-stdin"])
    rc = headless._cli()
    assert rc == 0
    captured = capsys.readouterr()
    assert "gh secret set" in captured.out


def test_check_via_cli(monkeypatch, capsys):
    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "x")
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "x")
    monkeypatch.setattr(sys, "argv", ["headless", "--check"])
    rc = headless._cli()
    captured = capsys.readouterr()
    # If xdk is installed in the dev venv, PREFLIGHT OK; otherwise FAIL.
    # Either way, return code should match.
    assert rc in (0, 2)
    assert ("PREFLIGHT OK" in captured.err) or ("PREFLIGHT FAIL" in captured.err)


def test_run_empty_status_returns_zero_and_resets_failure_counter(
    monkeypatch, tmp_path, capsys
):
    """No-new-bookmarks path must exit 0 (per CLAUDE.md spec) and mark
    heartbeat success=True so consecutive_cron_failures resets.

    Regression: pre-fix, headless treated `status="empty"` as a generic
    failure (return 2 + heartbeat success=False), violating the spec
    "0 full / 0 no-new / 1 partial / 2 fatal" and producing false-alarm
    cron-stale banners every time the user had no new X bookmarks since
    the last sync. Surfaced by 2026-04-29 manual QA Phase D7.
    """
    from xsensai.sync import service as _service
    from xsensai.sync.heartbeat import read_status

    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "test-rt")
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "test-ci")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(corpus))

    empty_result = _service.RunResult(
        run_id="test-empty-run",
        status="empty",
        extraction_strategy="none",
        rendered_message="[INFO/SYNC_DONE] No new bookmarks since last sync.\n"
                         "Nothing to do.\nSource: sync.service.run(mode=headless)",
        threads_unfetched_this_run=0,
        duration_ms=42,
    )

    finalize_calls = []

    def fake_run(**kwargs):
        return empty_result

    def fake_finalize_run(**kwargs):
        finalize_calls.append(kwargs)

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run", fake_finalize_run)

    rc = headless.run()

    assert rc == 0, "no-new-bookmarks must exit 0 per CLAUDE.md spec"
    assert len(finalize_calls) == 1, "finalize_run must run for empty path so heartbeat marks success"
    fc = finalize_calls[0]
    assert fc["success"] is True, "empty path must mark heartbeat success=True"
    assert fc["n_new_cards"] == 0
    assert fc["extraction_inline"] == 0
    assert fc["extraction_pending"] == 0
    assert fc["mode"] == "headless"

    captured = capsys.readouterr()
    assert "CRON_NO_NEW_BOOKMARKS" in captured.err
    # The success info must NOT be re-printed as if it were an error.
    assert "INFO/SYNC_DONE" not in captured.err
