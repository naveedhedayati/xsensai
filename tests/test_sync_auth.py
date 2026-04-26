"""Slice 4 — TokenProvider implementations.

KeychainTokenProvider tests mock the `keyring` library (which uses macOS
Security.framework directly via PyObjC — no subprocess). Per /review F10
fix, the implementation no longer goes through the `security` CLI.

EnvSecretTokenProvider tests use monkeypatch on os.environ.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

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


def _patch_keyring(monkeypatch, fake_keyring):
    """Helper: inject a fake keyring module so tests don't touch real Keychain."""
    monkeypatch.setitem(sys.modules, "keyring", fake_keyring)


def test_keychain_provider_returns_token(monkeypatch):
    """keyring.get_password returns a token → provider returns it."""
    fake = MagicMock()
    fake.get_password.return_value = "kc-token-xyz"
    _patch_keyring(monkeypatch, fake)
    p = KeychainTokenProvider()
    assert p.get_refresh_token() == "kc-token-xyz"
    fake.get_password.assert_called_once_with("x-sensai", "x-api-refresh-token")


def test_keychain_provider_raises_setup_required_when_missing(monkeypatch):
    """keyring.get_password returns None → OAUTH_SETUP_REQUIRED."""
    fake = MagicMock()
    fake.get_password.return_value = None
    _patch_keyring(monkeypatch, fake)
    p = KeychainTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.get_refresh_token()
    assert exc.value.code == "OAUTH_SETUP_REQUIRED"


def test_keychain_provider_raises_blocked_on_keyring_error(monkeypatch):
    """keyring.get_password raises → OAUTH_KEYCHAIN_BLOCKED."""
    fake = MagicMock()
    fake.get_password.side_effect = RuntimeError("Keychain ACL denied")
    _patch_keyring(monkeypatch, fake)
    p = KeychainTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.get_refresh_token()
    assert exc.value.code == "OAUTH_KEYCHAIN_BLOCKED"


def test_keychain_provider_store_writes_via_keyring(monkeypatch):
    """store_refresh_token calls keyring.set_password (no subprocess)."""
    fake = MagicMock()
    _patch_keyring(monkeypatch, fake)
    p = KeychainTokenProvider()
    p.store_refresh_token("new-token-123")
    fake.set_password.assert_called_once_with("x-sensai", "x-api-refresh-token", "new-token-123")


def test_keychain_provider_store_no_argv_exposure(monkeypatch):
    """F10 regression guard: store path must not invoke subprocess.run.

    The bug it prevents: previous impl ran `security add-generic-password
    -w <token>`, putting the token in argv (visible via `ps -ef` to any
    local user for ~50ms). Asserting subprocess.run is never called from
    store_refresh_token catches a regression to the CLI-based impl.
    """
    import subprocess as _sp
    fake = MagicMock()
    _patch_keyring(monkeypatch, fake)
    called = []
    monkeypatch.setattr(_sp, "run", lambda *a, **kw: called.append(a) or pytest.fail(
        "subprocess.run should not be called by KeychainTokenProvider — F10 regression"
    ))
    p = KeychainTokenProvider()
    p.store_refresh_token("token-abc")
    assert called == []


def test_keychain_provider_raises_blocked_on_set_error(monkeypatch):
    fake = MagicMock()
    fake.set_password.side_effect = RuntimeError("Keychain locked")
    _patch_keyring(monkeypatch, fake)
    p = KeychainTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.store_refresh_token("token")
    assert exc.value.code == "OAUTH_KEYCHAIN_BLOCKED"


def test_keychain_provider_store_rejects_empty():
    p = KeychainTokenProvider()
    with pytest.raises(ValueError):
        p.store_refresh_token("")


def test_keychain_provider_raises_setup_required_when_keyring_missing(monkeypatch):
    """If `keyring` package isn't installed, fail clearly (not ImportError)."""
    # Remove keyring from sys.modules + stub a failing import
    monkeypatch.setitem(sys.modules, "keyring", None)  # makes 'import keyring' raise
    p = KeychainTokenProvider()
    with pytest.raises(XSensaiError) as exc:
        p.get_refresh_token()
    assert exc.value.code == "OAUTH_SETUP_REQUIRED"
    assert "keyring" in exc.value.cause.lower()


def test_get_stored_client_id_prefers_env_var(monkeypatch):
    """Env var wins over Keychain (lets cron + tests override)."""
    from xsensai.sync.auth import CLIENT_ID_ENV, get_stored_client_id
    monkeypatch.setenv(CLIENT_ID_ENV, "from-env-123")
    fake = MagicMock()
    fake.get_password.return_value = "from-keychain-456"
    _patch_keyring(monkeypatch, fake)
    assert get_stored_client_id() == "from-env-123"


def test_get_stored_client_id_falls_back_to_keychain(monkeypatch):
    """When env var is unset, read from Keychain. This is the load-bearing
    path for /xsync from a fresh Claude Code session — that process
    doesn't inherit env vars from the terminal that ran setup_oauth.
    """
    from xsensai.sync.auth import CLIENT_ID_ENV, get_stored_client_id
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    fake = MagicMock()
    fake.get_password.return_value = "from-keychain-456"
    _patch_keyring(monkeypatch, fake)
    assert get_stored_client_id() == "from-keychain-456"


def test_get_stored_client_id_returns_none_when_neither_set(monkeypatch):
    """Both sources empty → None (caller surfaces OAUTH_CLIENT_ID_MISSING)."""
    from xsensai.sync.auth import CLIENT_ID_ENV, get_stored_client_id
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    fake = MagicMock()
    fake.get_password.return_value = None
    _patch_keyring(monkeypatch, fake)
    assert get_stored_client_id() is None


def test_store_client_id_writes_to_keychain(monkeypatch):
    from xsensai.sync.auth import KEYCHAIN_CLIENT_ID_ACCOUNT, KEYCHAIN_SERVICE_NAME, store_client_id
    fake = MagicMock()
    _patch_keyring(monkeypatch, fake)
    store_client_id("my-client-id")
    fake.set_password.assert_called_once_with(
        KEYCHAIN_SERVICE_NAME, KEYCHAIN_CLIENT_ID_ACCOUNT, "my-client-id"
    )


def test_store_client_id_rejects_empty(monkeypatch):
    from xsensai.sync.auth import store_client_id
    fake = MagicMock()
    _patch_keyring(monkeypatch, fake)
    with pytest.raises(ValueError):
        store_client_id("")
