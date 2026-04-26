"""Minimal one-shot OAuth 2.0 PKCE flow for x-sensai.

Per /autoplan auto-decision #19 + S-12: 200-250 LoC budget; --dry-run mode;
4 dedicated error codes (PORT_COLLISION, BROWSER_NOT_DEFAULT, GRANT_REFUSED,
KEYCHAIN_BLOCKED). Per E-5: 127.0.0.1 random ephemeral port + state parameter
verification. XDK handles the PKCE code_verifier/code_challenge dance
internally; we own the state parameter (CSRF defense).

Usage:
  python -m xsensai.sync.setup_oauth                 # full PKCE flow
  python -m xsensai.sync.setup_oauth --check         # preflight only (no browser)
  python -m xsensai.sync.setup_oauth --dry-run       # runs flow but skips token store
  python -m xsensai.sync.setup_oauth --copy-url      # print URL instead of auto-open
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import secrets
import shutil
import socket
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple
from urllib.parse import parse_qs, urlparse

from xsensai.errors import XSensaiError
from xsensai.sync.auth import (
    KEYCHAIN_ACCOUNT_NAME,
    KEYCHAIN_SERVICE_NAME,
    KeychainTokenProvider,
)


log = logging.getLogger(__name__)


CLIENT_ID_ENV = "XSENSAI_X_CLIENT_ID"
PORT_ENV = "XSENSAI_OAUTH_PORT"
DEFAULT_SCOPES = ["bookmark.read", "tweet.read", "users.read", "offline.access"]
CALLBACK_TIMEOUT_SECONDS = 300  # 5 min — long enough for the user to grant
# Default callback port. X's OAuth 2.0 requires the redirect URI to EXACTLY
# match the one registered in the X dev portal — including the port. So we
# use a fixed default (not a random ephemeral port). The user registers
# `http://127.0.0.1:8765/callback` once in their dev portal and we use this
# port forever after. Override with --port or XSENSAI_OAUTH_PORT if needed.
DEFAULT_CALLBACK_PORT = 8765


class _CallbackHandler(BaseHTTPRequestHandler):
    """One-shot HTTP handler that captures ?code=&state= from the redirect."""

    received_code: Optional[str] = None
    received_state: Optional[str] = None
    error_msg: Optional[str] = None

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        if "error" in params:
            type(self).error_msg = params["error"][0]
            self._respond_html(
                "<h2>x-sensai OAuth — grant refused</h2>"
                f"<p>X returned: <code>{type(self).error_msg}</code></p>"
                "<p>You can close this tab.</p>",
                status=400,
            )
            return
        if "code" in params:
            type(self).received_code = params["code"][0]
            type(self).received_state = (params.get("state") or [None])[0]
            self._respond_html(
                "<h2>x-sensai OAuth — success ✅</h2>"
                "<p>Token captured. You can close this tab and return to your terminal.</p>"
            )
            return
        self._respond_html(
            "<h2>x-sensai OAuth — waiting</h2>"
            "<p>This is the OAuth callback endpoint. Open the URL printed in "
            "your terminal to start the flow.</p>",
            status=400,
        )

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Silence noisy default access-log output.
        pass

    def _respond_html(self, html: str, *, status: int = 200) -> None:
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _bind_loopback_server(port: int = DEFAULT_CALLBACK_PORT) -> Tuple[HTTPServer, int]:
    """Bind on 127.0.0.1 at the given port (default 8765, fixed so the
    redirect URI matches what's registered in the X dev portal).

    On port-in-use, raise OAUTH_PORT_COLLISION with a clear next step
    (kill whatever's using that port, or pass --port to override).
    """
    try:
        server = HTTPServer(("127.0.0.1", port), _CallbackHandler)
    except OSError as e:
        raise XSensaiError(
            code="OAUTH_PORT_COLLISION",
            cause=f"Could not bind 127.0.0.1:{port}: {e}",
            attempted=f"HTTPServer(('127.0.0.1', {port}), ...)",
            next_action=(
                f"Port {port} is in use. Either close the program holding it "
                f"(e.g., `lsof -i :{port}`), or pass --port <number> to use a "
                f"different port (and update the callback URL in your X dev "
                f"portal to match — see X dev portal → your app → User "
                f"authentication settings → Callback URI)."
            ),
            retryable=True,
        )
    return server, server.server_port


def _open_browser(url: str) -> bool:
    """Open the URL in the user's default browser. Returns False on any failure."""
    try:
        return webbrowser.open(url, new=2, autoraise=True)
    except Exception as e:
        log.warning("webbrowser.open failed: %s", e)
        return False


def _wait_for_callback(server: HTTPServer, *, timeout_s: int) -> None:
    """Block until the callback fires OR timeout. Single-shot."""
    server.timeout = timeout_s
    # handle_request() respects server.timeout (as the socket .settimeout())
    server.handle_request()


def _check_preconditions(*, client_id: Optional[str]) -> int:
    """Verify what we'd need to run the full flow. No browser, no token write."""
    issues = []

    if not client_id:
        issues.append(
            f"  ❌ Missing client_id. Set the {CLIENT_ID_ENV} env var, or "
            f"register an X dev app at developer.x.com (~10 min, browser + dev "
            f"portal approval). Also buy ~$10 of API credits at console.x.com."
        )
    else:
        issues.append(f"  ✅ client_id present ({len(client_id)} chars)")

    if shutil.which("security") is None:
        issues.append(
            "  ❌ macOS `security` CLI not found. OAuth setup writes the refresh "
            "token to Keychain via `security` — required for `KeychainTokenProvider`."
        )
    else:
        issues.append("  ✅ macOS `security` CLI available")

    try:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        s.close()
        issues.append("  ✅ 127.0.0.1 ephemeral port binding works")
    except OSError as e:
        issues.append(f"  ❌ Cannot bind 127.0.0.1 ephemeral port: {e}")

    try:
        # `xdk` import — sync.client lazy-imports it but we want to confirm here
        import xdk  # noqa: F401
        issues.append("  ✅ xdk package importable")
    except ImportError:
        issues.append("  ❌ xdk package not installed. Run: pip install xdk")

    print("x-sensai OAuth — precondition check")
    print("=" * 50)
    for line in issues:
        print(line)
    print()
    if any(line.startswith("  ❌") for line in issues):
        print("One or more preconditions FAILED. Fix the ❌ items above, then re-run.")
        return 1
    print("All preconditions OK. Run without --check to actually authorize.")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m xsensai.sync.setup_oauth",
        description=(
            "One-shot OAuth 2.0 PKCE flow. Opens a browser to grant, captures "
            "the redirect on a 127.0.0.1 ephemeral port, exchanges the code, "
            "stores the refresh token in macOS Keychain."
        ),
    )
    parser.add_argument("--check", action="store_true",
                        help="Verify preconditions only — no browser, no token write")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run the full flow but do NOT exchange the code or write to Keychain")
    parser.add_argument("--copy-url", action="store_true",
                        help="Print the auth URL instead of auto-opening the browser")
    parser.add_argument("--client-id", default=None,
                        help=f"X dev app client_id (overrides ${CLIENT_ID_ENV})")
    parser.add_argument("--client-secret", default=None,
                        help=f"X dev app client_secret — REQUIRED for Confidential clients "
                        f"(X dev portal 'Web App' type). Public Clients (Native App / SPA) "
                        f"don't need this. Overrides $XSENSAI_X_CLIENT_SECRET.")
    parser.add_argument("--port", type=int, default=None,
                        help=f"Callback port (overrides ${PORT_ENV}; default {DEFAULT_CALLBACK_PORT}). "
                        f"MUST match what's registered in your X dev portal.")
    args = parser.parse_args(argv)

    client_id = args.client_id or os.environ.get(CLIENT_ID_ENV, "").strip() or None
    # Confidential clients only — Public Clients leave this None.
    from xsensai.sync.auth import CLIENT_SECRET_ENV
    client_secret = args.client_secret or os.environ.get(CLIENT_SECRET_ENV, "").strip() or None

    if args.check:
        return _check_preconditions(client_id=client_id)

    # Real flow — client_id required
    if not client_id:
        err = XSensaiError(
            code="OAUTH_CLIENT_ID_MISSING",
            cause=f"No X dev app client_id provided.",
            attempted="setup_oauth main() — pre-flight",
            next_action=(
                f"Either pass --client-id, or export {CLIENT_ID_ENV}=<your-client-id>. "
                f"If you don't have a client_id yet, register an X dev app at "
                f"https://developer.x.com (free, ~10 min). Also buy ~$10 of API "
                f"credits at https://console.x.com (one-time, lasts years at "
                f"personal volume)."
            ),
            retryable=True,
        )
        print(err.format(), file=sys.stderr)
        return 2

    try:
        import xdk
    except ImportError:
        err = XSensaiError(
            code="OAUTH_SETUP_REQUIRED",
            cause="xdk package not installed.",
            attempted="import xdk",
            next_action="Run: pip install xdk  (or: VIRTUAL_ENV=.venv uv pip install xdk)",
            retryable=False,
        )
        print(err.format(), file=sys.stderr)
        return 2

    # Bind loopback callback server. Port resolution order:
    # 1. --port flag
    # 2. XSENSAI_OAUTH_PORT env var
    # 3. DEFAULT_CALLBACK_PORT (8765)
    port_to_use = args.port
    if port_to_use is None:
        env_port = os.environ.get(PORT_ENV, "").strip()
        if env_port:
            try:
                port_to_use = int(env_port)
            except ValueError:
                print(f"warning: ${PORT_ENV}={env_port!r} is not a valid integer; using {DEFAULT_CALLBACK_PORT}", file=sys.stderr)
                port_to_use = DEFAULT_CALLBACK_PORT
    if port_to_use is None:
        port_to_use = DEFAULT_CALLBACK_PORT

    try:
        server, port = _bind_loopback_server(port_to_use)
    except XSensaiError as e:
        print(e.format(), file=sys.stderr)
        return 3

    redirect_uri = f"http://127.0.0.1:{port}/callback"
    state = secrets.token_urlsafe(32)
    log.info("OAuth callback server bound to %s", redirect_uri)

    # Build XDK kwargs conditionally — only thread client_secret for
    # Confidential clients. Public Clients (Native App / SPA) don't need it.
    xdk_kwargs = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": DEFAULT_SCOPES,
    }
    if client_secret:
        xdk_kwargs["client_secret"] = client_secret
    client = xdk.Client(**xdk_kwargs)
    try:
        auth_url = client.get_authorization_url(state=state)
    except Exception as e:
        err = XSensaiError(
            code="OAUTH_SETUP_REQUIRED",
            cause=f"Could not build authorization URL: {type(e).__name__}: {e}",
            attempted="xdk.Client.get_authorization_url(state=...)",
            next_action="Verify your client_id is for an OAuth 2.0 PKCE app (not OAuth 1.0a).",
            retryable=False,
        )
        print(err.format(), file=sys.stderr)
        return 3

    # Open browser (or print URL on user request / browser failure)
    opened = False
    if not args.copy_url:
        opened = _open_browser(auth_url)

    if args.copy_url or not opened:
        if not opened and not args.copy_url:
            warn = XSensaiError(
                code="OAUTH_BROWSER_NOT_DEFAULT",
                cause="Could not auto-open the default browser.",
                attempted="webbrowser.open(auth_url)",
                next_action="Copy the URL below into any browser to grant access.",
                retryable=True,
            )
            print(warn.format(), file=sys.stderr)
        print()
        print("OAuth URL:")
        print(auth_url)
        print()

    print(f"Waiting for OAuth callback (up to {CALLBACK_TIMEOUT_SECONDS}s)...")
    try:
        _wait_for_callback(server, timeout_s=CALLBACK_TIMEOUT_SECONDS)
    finally:
        try:
            server.server_close()
        except Exception:
            pass

    if _CallbackHandler.error_msg:
        err = XSensaiError(
            code="OAUTH_GRANT_REFUSED",
            cause=f"X returned grant refusal: {_CallbackHandler.error_msg}",
            attempted="OAuth 2.0 PKCE authorization",
            next_action="Re-run setup_oauth and grant access when prompted.",
            retryable=True,
        )
        print(err.format(), file=sys.stderr)
        return 4

    if not _CallbackHandler.received_code:
        err = XSensaiError(
            code="OAUTH_GRANT_REFUSED",
            cause=f"OAuth callback timed out after {CALLBACK_TIMEOUT_SECONDS}s with no code.",
            attempted="OAuth 2.0 PKCE authorization",
            next_action="Re-run and complete the browser grant step within 5 minutes.",
            retryable=True,
        )
        print(err.format(), file=sys.stderr)
        return 4

    # CRITICAL: state-parameter verification (CSRF defense per E-5)
    if _CallbackHandler.received_state != state:
        err = XSensaiError(
            code="OAUTH_GRANT_REFUSED",
            cause="OAuth state parameter mismatch — possible CSRF attempt.",
            attempted="state == received_state assertion",
            next_action="Re-run setup_oauth in a clean browser session.",
            retryable=True,
        )
        print(err.format(), file=sys.stderr)
        return 4

    if args.dry_run:
        print("[--dry-run] Callback received successfully + state verified. ")
        print("[--dry-run] Skipping token exchange. Token NOT written to Keychain.")
        print(f"[--dry-run] Code length: {len(_CallbackHandler.received_code)}; state OK.")
        return 0

    # Exchange code for token
    try:
        token = client.exchange_code(_CallbackHandler.received_code)
    except Exception as e:
        err = XSensaiError(
            code="OAUTH_GRANT_REFUSED",
            cause=f"Code-for-token exchange failed: {type(e).__name__}: {e}",
            attempted="xdk.Client.exchange_code(code, code_verifier)",
            next_action="Re-run setup_oauth — the code may have expired (~30s lifetime).",
            retryable=True,
        )
        print(err.format(), file=sys.stderr)
        return 4

    refresh_token = (token or {}).get("refresh_token")
    if not refresh_token:
        err = XSensaiError(
            code="OAUTH_SETUP_REQUIRED",
            cause="X did not return a refresh_token. Verify offline.access scope is enabled.",
            attempted="token_dict.get('refresh_token')",
            next_action="Check that your X dev app's OAuth 2.0 settings include offline.access.",
            retryable=False,
        )
        print(err.format(), file=sys.stderr)
        return 4

    try:
        provider = KeychainTokenProvider()
        provider.store_refresh_token(refresh_token)
    except XSensaiError as e:
        print(e.format(), file=sys.stderr)
        return 5

    # Also persist the client_id (and client_secret if Confidential) in
    # Keychain so /xsync from Claude Code (which doesn't inherit shell
    # env vars) can find them without the user having to add `export`
    # lines to their shell rc.
    try:
        from xsensai.sync.auth import store_client_id, store_client_secret
        store_client_id(client_id)
        if client_secret:
            store_client_secret(client_secret)
    except XSensaiError as e:
        # Non-fatal — OAuth succeeded; user can fall back to env vars.
        print(e.format(), file=sys.stderr)

    secret_note = " + client_secret" if client_secret else ""
    print()
    print("✅ x-sensai OAuth setup complete.")
    print(f"   Refresh token + client_id{secret_note} stored in Keychain ({KEYCHAIN_SERVICE_NAME}/*).")
    print()
    print("Next: run /xsync in Claude Code to fetch your bookmarks.")
    print("(No shell env vars needed — everything's in Keychain.)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
