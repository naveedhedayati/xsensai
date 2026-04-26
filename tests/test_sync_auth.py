"""Slice 4 — TokenProvider implementations.

KeychainTokenProvider tests mock the `security` CLI via subprocess monkeypatching.
EnvSecretTokenProvider tests use monkeypatch on os.environ.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from xsensai.errors import XSensaiError
from xsensai.sync.auth import (
    ENV_VAR_NAME,
    EnvSecretTokenProvider,
    KeychainTokenProvider,
    TokenProvider,
)


def test_provider_protocol_runtime_check():
    """Both implementations satisfy the runtime-checkable Protocol."""
    assert isinstance(KeychainTokenProvider(), TokenProvider)
    assert isinstance(EnvSecretTokenProvider(), TokenProvider)


def test_env_provider_returns_token(monkeypatch):
    monkeypatch.setenv(ENV_VAR_NAME, "test-refresh-token-abc")
    p = EnvSecretTokenProvider()
    assert p.get_refresh_token() == "test-refresh-token-abc"


def test_env_provider_raises_setup_required_when_unset(monkeypatch):
    monkeypatch.delenv(ENV_VAR_NAME, raising=False)
    p = EnvSecretTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.get_refresh_token()
    assert exc.value.code == "OAUTH_SETUP_REQUIRED"


def test_env_provider_store_is_no_op(monkeypatch):
    """Env-backed provider can't write back; store() silently no-ops."""
    p = EnvSecretTokenProvider()
    p.store_refresh_token("anything")  # must not raise


def test_keychain_provider_returns_token(monkeypatch):
    """Mock `security` CLI to return a token; provider returns it."""
    def fake_run(cmd, **kw):
        # Return mock CompletedProcess
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="kc-token-xyz\n", stderr="",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    p = KeychainTokenProvider()
    assert p.get_refresh_token() == "kc-token-xyz"


def test_keychain_provider_raises_setup_required_when_missing(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=44,
            stdout="", stderr="security: SecKeychainSearchCopyNext: The specified item could not be found",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    p = KeychainTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.get_refresh_token()
    assert exc.value.code == "OAUTH_SETUP_REQUIRED"


def test_keychain_provider_raises_blocked_on_keychain_error(monkeypatch):
    def fake_run(cmd, **kw):
        return subprocess.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="some other Keychain error",
        )
    monkeypatch.setattr(subprocess, "run", fake_run)
    p = KeychainTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.get_refresh_token()
    assert exc.value.code == "OAUTH_KEYCHAIN_BLOCKED"


def test_keychain_provider_store_writes_via_security_cli(monkeypatch):
    captured_cmd = []

    def fake_run(cmd, **kw):
        captured_cmd.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    p = KeychainTokenProvider()
    p.store_refresh_token("new-token-123")
    assert any("add-generic-password" in c for c in captured_cmd[0])
    assert "new-token-123" in captured_cmd[0]


def test_keychain_provider_store_rejects_empty():
    p = KeychainTokenProvider()
    with pytest.raises(ValueError):
        p.store_refresh_token("")
