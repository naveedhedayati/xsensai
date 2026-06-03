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


@pytest.fixture(autouse=True)
def _neutralize_ci_env(monkeypatch):
    """Make this module hermetic: run identically locally and inside GitHub
    Actions. The cron self-rotation logic branches on GITHUB_ACTIONS +
    GITHUB_REPOSITORY + XSENSAI_SECRETS_PAT, all of which CI sets for real.
    Default each test to a clean slate; tests that exercise the rotation/
    fatal-in-CI paths set these env vars explicitly (which runs after this
    autouse fixture and overrides it)."""
    for _v in (
        "GITHUB_ACTIONS",
        "GITHUB_REPOSITORY",
        "XSENSAI_SECRETS_PAT",
        "XSENSAI_ALLOW_NO_PERSIST",
    ):
        monkeypatch.delenv(_v, raising=False)


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


# --------------------------------------------------------------------------
# P0 cron self-rotation — provider selection, persist-failure gate, canary
# --------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402
from xsensai.errors import XSensaiError  # noqa: E402
from xsensai.sync.auth import GhSecretTokenProvider, EnvSecretTokenProvider  # noqa: E402


def _stub_corpus(monkeypatch, tmp_path):
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    monkeypatch.setenv("XSENSAI_CORPUS_PATH", str(corpus))
    return corpus


def _base_env(monkeypatch):
    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "test-rt")
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "test-ci")


def test_provider_selection_gh_when_pat_and_repo(monkeypatch, tmp_path):
    """PAT + GITHUB_REPOSITORY present -> GhSecretTokenProvider selected."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    seen = {}

    def fake_run(**kwargs):
        seen["provider"] = kwargs["token_provider"]
        return _service.RunResult(
            run_id="r", status="empty", extraction_strategy="none",
            rendered_message="[INFO/SYNC_DONE] none\nx\nSource: y",
            threads_unfetched_this_run=0, duration_ms=1,
        )

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run", lambda **k: None)

    rc = headless.run()
    assert rc == 0
    assert isinstance(seen["provider"], GhSecretTokenProvider)


def test_provider_selection_fatal_in_actions_without_pat(monkeypatch, tmp_path, capsys):
    """In GitHub Actions, missing PAT is FATAL (would resurrect the P0 bug)."""
    _base_env(monkeypatch)
    _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.delenv("XSENSAI_SECRETS_PAT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.delenv("XSENSAI_ALLOW_NO_PERSIST", raising=False)

    rc = headless.run()
    assert rc == 2
    assert "TOKEN_PERSIST_FAILED" in capsys.readouterr().err


def test_provider_selection_actions_optout(monkeypatch, tmp_path, capsys):
    """XSENSAI_ALLOW_NO_PERSIST=1 makes missing-PAT-in-Actions non-fatal."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.delenv("XSENSAI_SECRETS_PAT", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("XSENSAI_ALLOW_NO_PERSIST", "1")

    seen = {}

    def fake_run(**kwargs):
        seen["provider"] = kwargs["token_provider"]
        return _service.RunResult(
            run_id="r", status="empty", extraction_strategy="none",
            rendered_message="x", threads_unfetched_this_run=0, duration_ms=1,
        )

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run", lambda **k: None)

    rc = headless.run()
    assert rc == 0
    assert isinstance(seen["provider"], EnvSecretTokenProvider)
    assert not isinstance(seen["provider"], GhSecretTokenProvider)


def test_provider_selection_env_when_no_pat_outside_actions(monkeypatch, tmp_path):
    """No PAT, not in Actions -> Env provider (backward compatible)."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.delenv("XSENSAI_SECRETS_PAT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)

    seen = {}

    def fake_run(**kwargs):
        seen["provider"] = kwargs["token_provider"]
        return _service.RunResult(
            run_id="r", status="empty", extraction_strategy="none",
            rendered_message="x", threads_unfetched_this_run=0, duration_ms=1,
        )

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run", lambda **k: None)

    rc = headless.run()
    assert rc == 0
    assert isinstance(seen["provider"], EnvSecretTokenProvider)
    assert not isinstance(seen["provider"], GhSecretTokenProvider)


def test_persist_failure_on_empty_path_exits_1_and_flags(monkeypatch, tmp_path):
    """Empty-bookmarks run + persist failure -> exit 1, flag, heartbeat fail."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    corpus = _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    finalize_calls = []

    def fake_run(**kwargs):
        # Simulate a rotation whose writeback failed during the run.
        kwargs["token_provider"].last_persist_error = XSensaiError(
            code="GH_SECRET_WRITE_FAILED", cause="403", attempted="x",
            next_action="y", retryable=True,
        )
        return _service.RunResult(
            run_id="r", status="empty", extraction_strategy="none",
            rendered_message="x", threads_unfetched_this_run=0, duration_ms=1,
        )

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run",
                        lambda **k: finalize_calls.append(k))
    monkeypatch.setattr(headless.git_push, "commit_and_push",
                        lambda *a, **k: None)
    monkeypatch.setattr(headless, "read_status", lambda *a, **k: object())

    rc = headless.run()
    assert rc == 1
    assert (corpus / "SYNC_TOKEN_PERSIST_FAILED.md").exists()
    assert finalize_calls and finalize_calls[0]["success"] is False
    assert finalize_calls[0]["last_error"] == "TOKEN_PERSIST_FAILED"


def test_no_persist_failure_empty_path_exits_0(monkeypatch, tmp_path):
    """Empty run with successful persistence still exits 0 (no false alarm)."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    corpus = _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    finalize_calls = []

    def fake_run(**kwargs):
        # No last_persist_error set -> clean.
        return _service.RunResult(
            run_id="r", status="empty", extraction_strategy="none",
            rendered_message="x", threads_unfetched_this_run=0, duration_ms=1,
        )

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run",
                        lambda **k: finalize_calls.append(k))

    rc = headless.run()
    assert rc == 0
    assert not (corpus / "SYNC_TOKEN_PERSIST_FAILED.md").exists()
    assert finalize_calls[0]["success"] is True


def test_persist_failure_on_cards_path_exits_1_cards_still_pushed(monkeypatch, tmp_path):
    """Cards synced + persist failure -> cards pushed, flag written, exit 1."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    corpus = _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    pushes = []

    def fake_run(**kwargs):
        kwargs["token_provider"].last_persist_error = XSensaiError(
            code="GH_SECRET_WRITE_FAILED", cause="403", attempted="x",
            next_action="y", retryable=True,
        )
        return _service.RunResult(
            run_id="r", status="ok", extraction_strategy="deferred",
            rendered_message="ok", threads_unfetched_this_run=0, duration_ms=1,
            cards_written=["card-1", "card-2"],
        )

    def fake_push(*a, **k):
        pushes.append(k.get("message"))
        return SimpleNamespace(success=True, conflict_unresolved=False,
                               flag_written=False, error=None)

    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(_service, "finalize_run", lambda **k: None)
    monkeypatch.setattr(headless.git_push, "commit_and_push", fake_push)
    monkeypatch.setattr(headless, "read_status", lambda *a, **k: object())

    rc = headless.run()
    assert rc == 1, "persist failure on cards path must be partial (exit 1)"
    assert pushes, "cards must still be committed/pushed"
    assert (corpus / "SYNC_TOKEN_PERSIST_FAILED.md").exists()


def test_persist_failure_on_non_ok_status_writes_flag(monkeypatch, tmp_path, capsys):
    """Rotation consumed the token + writeback failed, then run returns non-ok:
    the persist flag must still be written (review F6 — was silently dropped)."""
    from xsensai.sync import service as _service

    _base_env(monkeypatch)
    corpus = _stub_corpus(monkeypatch, tmp_path)
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    def fake_run(**kwargs):
        kwargs["token_provider"].last_persist_error = XSensaiError(
            code="GH_SECRET_WRITE_FAILED", cause="403", attempted="x",
            next_action="y", retryable=True,
        )
        return _service.RunResult(
            run_id="r", status="partial", extraction_strategy="none",
            rendered_message="[SYNC_PARTIAL] something went sideways",
            threads_unfetched_this_run=0, duration_ms=1,
        )

    updates = []
    status_obj = SimpleNamespace(extraction_pending_count=0, total_cards=0)
    monkeypatch.setattr(_service, "run", fake_run)
    monkeypatch.setattr(headless, "update_after_run",
                        lambda *a, **k: updates.append(k) or status_obj)
    monkeypatch.setattr(headless, "read_status", lambda *a, **k: status_obj)
    monkeypatch.setattr(headless.git_push, "commit_and_push", lambda *a, **k: None)

    rc = headless.run()
    assert rc == 2, "non-ok status is still fatal"
    assert (corpus / "SYNC_TOKEN_PERSIST_FAILED.md").exists(), \
        "persist flag must be written even on the non-ok path"
    assert updates and updates[-1]["last_error"] == "TOKEN_PERSIST_FAILED"
    assert "TOKEN_PERSIST_FAILED" in capsys.readouterr().err


def test_preflight_canary_invoked_when_pat_present(monkeypatch):
    """--check runs the canary write/delete when a PAT is configured."""
    from xsensai.sync import gh_secrets_updater as gsu

    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "ci")
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")

    called = {"canary": False}
    monkeypatch.setattr(gsu, "gh_diagnostics", lambda **k: "gh 2.x")

    def fake_verify(repo, *, pat):
        called["canary"] = True

    monkeypatch.setattr(gsu, "verify_secret_write", fake_verify)
    # xdk may or may not be installed; we only assert the canary ran.
    headless._check_preflight()
    assert called["canary"] is True


def test_preflight_fails_when_canary_fails(monkeypatch, capsys):
    """A PAT that cannot write secrets -> PREFLIGHT FAIL (exit 2)."""
    from xsensai.sync import gh_secrets_updater as gsu

    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "rt")
    monkeypatch.setenv("XSENSAI_X_CLIENT_ID", "ci")
    monkeypatch.setenv("XSENSAI_SECRETS_PAT", "pat-abc")
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setattr(gsu, "gh_diagnostics", lambda **k: "gh 2.x")

    def boom(repo, *, pat):
        raise XSensaiError(code="GH_SECRET_WRITE_FAILED", cause="403",
                           attempted="x", next_action="y", retryable=True)

    monkeypatch.setattr(gsu, "verify_secret_write", boom)
    rc = headless._check_preflight()
    assert rc == 2
    assert "PAT cannot write repo secrets" in capsys.readouterr().err


def test_token_persist_failed_text_no_secrets():
    text = headless._token_persist_failed_text("run-xyz")
    assert "run-xyz" in text
    for bad in ("Bearer ", "ghp_", "ghs_", "ya29."):
        assert bad not in text
