"""P0 cron self-rotation — gh_secrets_updater + GhSecretTokenProvider.

All offline: the `gh` subprocess is mocked at xsensai.sync.gh_secrets_updater's
subprocess.run seam (project rule: no test hits the network). These cover the
write path, the failure path, no-argv/no-newline-corruption guarantees, the
add-mask emission in Actions, the canary preflight, and the provider's
catch-not-raise contract.
"""

from __future__ import annotations

import subprocess
from types import SimpleNamespace

import pytest

from xsensai.errors import XSensaiError
from xsensai.sync import gh_secrets_updater as gsu
from xsensai.sync.auth import GhSecretTokenProvider


# --- helpers ---------------------------------------------------------------

class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_run(monkeypatch, *, returncode=0, stderr="", record=None):
    """Patch gsu's subprocess.run + which('gh'); record each invocation."""
    monkeypatch.setattr(gsu.shutil, "which", lambda _: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        if record is not None:
            record.append({"cmd": cmd, "kwargs": kwargs})
        return _FakeProc(returncode=returncode, stderr=stderr)

    monkeypatch.setattr(gsu.subprocess, "run", fake_run)


# --- update_repo_secret ----------------------------------------------------

def test_update_repo_secret_happy_path(monkeypatch):
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    gsu.update_repo_secret("owner/repo", "MY_SECRET", "tok-value", pat="pat-123")
    assert len(rec) == 1
    cmd = rec[0]["cmd"]
    assert cmd[0] == "/usr/bin/gh"
    assert "secret" in cmd and "set" in cmd and "MY_SECRET" in cmd
    assert "--repo" in cmd and "owner/repo" in cmd
    assert "--app" in cmd and "actions" in cmd


def test_token_passed_via_stdin_not_argv(monkeypatch):
    """The token value must never appear in argv (no ps -ef leak)."""
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    gsu.update_repo_secret("owner/repo", "MY_SECRET", "super-secret-tok", pat="pat-xyz")
    cmd = rec[0]["cmd"]
    assert "super-secret-tok" not in cmd, "token leaked into argv!"
    # value comes through stdin
    assert rec[0]["kwargs"].get("input") == "super-secret-tok"
    # PAT must not be in argv either
    assert "pat-xyz" not in cmd


def test_pat_passed_via_env_not_argv(monkeypatch):
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    gsu.update_repo_secret("owner/repo", "S", "v", pat="pat-secret")
    env = rec[0]["kwargs"].get("env", {})
    assert env.get("GH_TOKEN") == "pat-secret"
    # A stray GITHUB_TOKEN must not shadow GH_TOKEN for gh.
    assert "GITHUB_TOKEN" not in env


def test_no_body_dash_flag_used(monkeypatch):
    """Must NOT use `--body -` (it strips trailing newline / corrupts values)."""
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    gsu.update_repo_secret("owner/repo", "S", "v", pat="p")
    cmd = rec[0]["cmd"]
    assert "--body" not in cmd


def test_value_is_byte_exact_via_stdin(monkeypatch):
    """Whatever we pass is sent verbatim through stdin (no trimming by us)."""
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    gsu.update_repo_secret("owner/repo", "S", "tok-no-newline", pat="p")
    assert rec[0]["kwargs"]["input"] == "tok-no-newline"


def test_nonzero_exit_raises_gh_secret_write_failed(monkeypatch):
    _capture_run(monkeypatch, returncode=1, stderr="HTTP 403: Resource not accessible")
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("owner/repo", "S", "v", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"
    assert ei.value.retryable is True


def test_stderr_redacted_in_error(monkeypatch):
    """The PAT/value must not survive into the error details."""
    secret_pat = "abcdefghijklmnopqrstuvwxyz0123456789ABCD"  # >=32 chars
    _capture_run(monkeypatch, returncode=1, stderr=f"failed with token {secret_pat}")
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("owner/repo", "S", "v", pat=secret_pat)
    rendered = ei.value.format()
    assert secret_pat not in rendered


def test_missing_gh_binary_raises(monkeypatch):
    monkeypatch.setattr(gsu.shutil, "which", lambda _: None)
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("owner/repo", "S", "v", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"


def test_empty_value_refused(monkeypatch):
    _capture_run(monkeypatch, returncode=0)
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("owner/repo", "S", "", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"


def test_empty_repo_refused(monkeypatch):
    _capture_run(monkeypatch, returncode=0)
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("", "S", "v", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"


def test_add_mask_emitted_in_actions(monkeypatch, capsys):
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gsu.update_repo_secret("owner/repo", "S", "rotated-tok", pat="p")
    out = capsys.readouterr().out
    assert "::add-mask::rotated-tok" in out


def test_add_mask_not_emitted_outside_actions(monkeypatch, capsys):
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    gsu.update_repo_secret("owner/repo", "S", "rotated-tok", pat="p")
    out = capsys.readouterr().out
    assert "::add-mask::" not in out


def test_add_mask_escapes_workflow_command_chars(monkeypatch, capsys):
    """A token with %/CR/LF must be escaped so it can't break/inject commands."""
    _capture_run(monkeypatch, returncode=0)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    gsu.update_repo_secret("owner/repo", "S", "tok%val\nfoo\rbar", pat="p")
    out = capsys.readouterr().out
    assert "::add-mask::tok%25val%0Afoo%0Dbar" in out
    # The raw newline must not appear as a literal line break in the command.
    assert "::add-mask::tok%val\nfoo" not in out


def test_run_gh_timeout_becomes_gh_secret_write_failed(monkeypatch):
    """A hung gh (TimeoutExpired) must surface as XSensaiError, not escape raw."""
    monkeypatch.setattr(gsu.shutil, "which", lambda _: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, gsu._GH_TIMEOUT_S)

    monkeypatch.setattr(gsu.subprocess, "run", fake_run)
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("owner/repo", "S", "v", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"
    assert ei.value.retryable is True


def test_run_gh_oserror_becomes_gh_secret_write_failed(monkeypatch):
    monkeypatch.setattr(gsu.shutil, "which", lambda _: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        raise OSError("exec format error")

    monkeypatch.setattr(gsu.subprocess, "run", fake_run)
    with pytest.raises(XSensaiError) as ei:
        gsu.update_repo_secret("owner/repo", "S", "v", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"


def test_verify_secret_write_tolerates_delete_timeout(monkeypatch):
    """Canary delete timing out must NOT fail preflight (write proved perm)."""
    monkeypatch.setattr(gsu.shutil, "which", lambda _: "/usr/bin/gh")
    calls = {"n": 0}

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:  # the canary write succeeds
            return _FakeProc(returncode=0)
        raise subprocess.TimeoutExpired(cmd, gsu._GH_TIMEOUT_S)  # delete hangs

    monkeypatch.setattr(gsu.subprocess, "run", fake_run)
    gsu.verify_secret_write("owner/repo", pat="p")  # must not raise
    assert calls["n"] == 2


def test_provider_store_catches_non_xsensai_error(monkeypatch):
    """Defense in depth: an unexpected (non-XSensaiError) failure must land in
    last_persist_error, never escape mid-fetch and bypass the gate."""
    def boom(repo, name, value, *, pat):
        raise RuntimeError("totally unexpected")

    monkeypatch.setattr(gsu, "update_repo_secret", boom)
    p = GhSecretTokenProvider(repo="o/r", pat="pat-1")
    p.store_refresh_token("new-rt")  # must NOT raise
    assert p.last_persist_error is not None
    assert p.last_persist_error.code == "GH_SECRET_WRITE_FAILED"
    assert "totally unexpected" in p.last_persist_error.cause


# --- verify_secret_write (canary preflight) --------------------------------

def test_verify_secret_write_canary_roundtrip(monkeypatch):
    rec = []
    _capture_run(monkeypatch, returncode=0, record=rec)
    gsu.verify_secret_write("owner/repo", pat="p")
    # one write (set) + one delete
    cmds = [r["cmd"] for r in rec]
    assert any("set" in c and gsu.CANARY_SECRET_NAME in c for c in cmds)
    assert any("delete" in c and gsu.CANARY_SECRET_NAME in c for c in cmds)


def test_verify_secret_write_raises_when_pat_cannot_write(monkeypatch):
    _capture_run(monkeypatch, returncode=1, stderr="HTTP 403")
    with pytest.raises(XSensaiError) as ei:
        gsu.verify_secret_write("owner/repo", pat="p")
    assert ei.value.code == "GH_SECRET_WRITE_FAILED"


def test_verify_secret_write_tolerates_cleanup_failure(monkeypatch):
    """If the canary write succeeds but delete fails, do not raise."""
    calls = {"n": 0}
    monkeypatch.setattr(gsu.shutil, "which", lambda _: "/usr/bin/gh")

    def fake_run(cmd, **kwargs):
        calls["n"] += 1
        # first call (set) succeeds, second (delete) fails
        return _FakeProc(returncode=0 if calls["n"] == 1 else 1)

    monkeypatch.setattr(gsu.subprocess, "run", fake_run)
    gsu.verify_secret_write("owner/repo", pat="p")  # must not raise
    assert calls["n"] == 2


# --- GhSecretTokenProvider -------------------------------------------------

def test_provider_store_calls_updater(monkeypatch):
    seen = {}

    def fake_update(repo, name, value, *, pat):
        seen.update(repo=repo, name=name, value=value, pat=pat)

    monkeypatch.setattr(gsu, "update_repo_secret", fake_update)
    p = GhSecretTokenProvider(repo="o/r", pat="pat-1")
    p.store_refresh_token("new-rt")
    assert seen == {"repo": "o/r", "name": "XSENSAI_X_REFRESH_TOKEN",
                    "value": "new-rt", "pat": "pat-1"}
    assert p.last_persist_error is None


def test_provider_store_catches_failure_does_not_raise(monkeypatch):
    def boom(repo, name, value, *, pat):
        raise XSensaiError(
            code="GH_SECRET_WRITE_FAILED", cause="nope",
            attempted="x", next_action="y", retryable=True,
        )

    monkeypatch.setattr(gsu, "update_repo_secret", boom)
    p = GhSecretTokenProvider(repo="o/r", pat="pat-1")
    # MUST NOT raise — the rotated token is already consumed by X.
    p.store_refresh_token("new-rt")
    assert p.last_persist_error is not None
    assert p.last_persist_error.code == "GH_SECRET_WRITE_FAILED"


def test_provider_store_clears_error_on_success(monkeypatch):
    p = GhSecretTokenProvider(repo="o/r", pat="pat-1")
    p.last_persist_error = XSensaiError(
        code="GH_SECRET_WRITE_FAILED", cause="old", attempted="x",
        next_action="y", retryable=True,
    )
    monkeypatch.setattr(gsu, "update_repo_secret", lambda *a, **k: None)
    p.store_refresh_token("new-rt")
    assert p.last_persist_error is None


def test_provider_get_reads_env(monkeypatch):
    monkeypatch.setenv("XSENSAI_X_REFRESH_TOKEN", "env-rt")
    p = GhSecretTokenProvider(repo="o/r", pat="pat-1")
    assert p.get_refresh_token() == "env-rt"


def test_provider_store_refuses_empty(monkeypatch):
    p = GhSecretTokenProvider(repo="o/r", pat="pat-1")
    with pytest.raises(ValueError):
        p.store_refresh_token("")
