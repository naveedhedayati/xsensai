"""Slice 4 — setup_oauth CLI: --check mode + state-param defense.

The full PKCE flow + browser callback can't be tested without a real X dev
app — that's the live integration test (gated env var). These tests cover
the precondition check + argument parsing + the parts that don't require
a real browser.
"""

from __future__ import annotations

import socket
import subprocess

import pytest

from xsensai.sync.setup_oauth import (
    CALLBACK_TIMEOUT_SECONDS,
    CLIENT_ID_ENV,
    DEFAULT_SCOPES,
    _bind_loopback_server,
    _CallbackHandler,
    _check_preconditions,
    main,
)


def test_default_scopes_include_offline_access():
    """`offline.access` is required to receive a refresh_token."""
    assert "offline.access" in DEFAULT_SCOPES
    assert "bookmark.read" in DEFAULT_SCOPES


def test_check_mode_succeeds_with_client_id(monkeypatch, capsys):
    monkeypatch.setenv(CLIENT_ID_ENV, "fake-client-id-123")
    rc = _check_preconditions(client_id="fake-client-id-123")
    out = capsys.readouterr().out
    assert "client_id present" in out
    assert "security`" in out  # macOS Keychain CLI check
    assert rc == 0


def test_check_mode_fails_without_client_id(monkeypatch, capsys):
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    rc = _check_preconditions(client_id=None)
    out = capsys.readouterr().out
    assert "Missing client_id" in out
    assert rc == 1


def test_main_fails_without_client_id(monkeypatch, capsys):
    monkeypatch.delenv(CLIENT_ID_ENV, raising=False)
    rc = main([])
    err = capsys.readouterr().err
    assert "OAUTH_CLIENT_ID_MISSING" in err
    assert rc == 2


def test_bind_loopback_server_uses_ephemeral_port():
    """Per E-5: 127.0.0.1 + ephemeral port (kernel-assigned, not hardcoded)."""
    server, port = _bind_loopback_server()
    try:
        # Server bound on 127.0.0.1 specifically (not 0.0.0.0)
        assert server.server_address[0] == "127.0.0.1"
        # Port is in ephemeral range
        assert port > 0
        # Confirm it's actually listening
        sock = socket.socket()
        sock.settimeout(1.0)
        try:
            sock.connect(("127.0.0.1", port))
            sock.close()
        except OSError:
            pytest.fail("Server not actually listening")
    finally:
        server.server_close()


def test_callback_handler_extracts_code_and_state():
    """Hand-build the request line + verify the handler captures code + state."""
    # Reset class-level state from any prior test
    _CallbackHandler.received_code = None
    _CallbackHandler.received_state = None
    _CallbackHandler.error_msg = None

    # Use http.server's request handling against a real socket pair
    server, port = _bind_loopback_server()
    try:
        # Send a callback request manually
        import threading
        import urllib.request

        def call():
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/callback?code=THE_CODE&state=THE_STATE",
                    timeout=2.0,
                )
            except Exception:
                pass  # response captured already

        t = threading.Thread(target=call, daemon=True)
        t.start()
        server.timeout = 2.0
        server.handle_request()
        t.join(timeout=3.0)

        assert _CallbackHandler.received_code == "THE_CODE"
        assert _CallbackHandler.received_state == "THE_STATE"
    finally:
        server.server_close()


def test_callback_handler_captures_error_msg():
    """When X returns ?error=..., the handler captures it for OAUTH_GRANT_REFUSED."""
    _CallbackHandler.received_code = None
    _CallbackHandler.received_state = None
    _CallbackHandler.error_msg = None

    server, port = _bind_loopback_server()
    try:
        import threading
        import urllib.request

        def call():
            try:
                urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/callback?error=access_denied",
                    timeout=2.0,
                )
            except Exception:
                pass

        t = threading.Thread(target=call, daemon=True)
        t.start()
        server.timeout = 2.0
        server.handle_request()
        t.join(timeout=3.0)

        assert _CallbackHandler.error_msg == "access_denied"
    finally:
        server.server_close()


def test_callback_timeout_constant_is_reasonable():
    """5 minutes — long enough for the user to grant, short enough to not hang forever."""
    assert 60 <= CALLBACK_TIMEOUT_SECONDS <= 600
