"""Token providers — abstract the X API refresh-token source.

Per /autoplan D-S2 fix: UC-1=C made the orchestrator headless-ready but auth
was still desktop/Keychain-centric. TokenProviderProtocol decouples the
sync.client.XClient from how the token is sourced. Slice 4 ships:

  - KeychainTokenProvider  — reads/writes via the `keyring` library, which
    on macOS talks to Security.framework directly (no subprocess argv
    exposure). Used in manual mode.
  - EnvSecretTokenProvider — reads from environment (used by tests + Slice 5 cron)

Per /review F10 fix: the original implementation used `security` CLI via
subprocess, which leaked the token via argv (visible to `ps -ef` for ~50ms
per write). The keyring library uses Security.framework directly via
PyObjC bindings — no subprocess, no argv leak.

Slice 5 cron will instantiate EnvSecretTokenProvider with the GitHub Actions
encrypted secret. No XClient code changes needed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from typing import Iterable, Optional, Protocol, runtime_checkable

from xsensai.errors import XSensaiError


# Slice 5 / autoplan E7 — redaction helper for any text persisted to
# non-committed logs (heartbeat is committed; flag files use static
# templates). Never use this output for committed flags — those must be
# fully static (autoplan E7).
_BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9_\-\.~+/=]{8,}", re.IGNORECASE)
_LONG_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9_\-]{32,}\b")


def redact_token_strings(
    text: str,
    *,
    extra_secrets: Iterable[str] = (),
) -> str:
    """Best-effort redaction of refresh-token / bearer-token shapes.

    For non-committed log output ONLY. Replaces:
      - `Bearer <token>` → `Bearer <REDACTED>`
      - any continuous run of >=32 url-safe-base64 chars → `<REDACTED:32+>`
      - exact matches of any string in `extra_secrets` (e.g., the live
        env-var value of XSENSAI_X_REFRESH_TOKEN at call time)

    Conservative — false positives on long opaque ids are acceptable;
    false negatives leak tokens.
    """
    if not text:
        return text
    out = _BEARER_PATTERN.sub("Bearer <REDACTED>", text)
    out = _LONG_TOKEN_PATTERN.sub("<REDACTED:32+>", out)
    for s in extra_secrets:
        if s and len(s) >= 8:
            out = out.replace(s, "<REDACTED>")
    return out


KEYCHAIN_SERVICE_NAME = "x-sensai"
KEYCHAIN_ACCOUNT_NAME = "x-api-refresh-token"
KEYCHAIN_CLIENT_ID_ACCOUNT = "x-api-client-id"
KEYCHAIN_CLIENT_SECRET_ACCOUNT = "x-api-client-secret"
ENV_VAR_NAME = "XSENSAI_X_REFRESH_TOKEN"
CLIENT_ID_ENV = "XSENSAI_X_CLIENT_ID"
CLIENT_SECRET_ENV = "XSENSAI_X_CLIENT_SECRET"


@runtime_checkable
class TokenProvider(Protocol):
    """Source for the X API OAuth 2.0 refresh token.

    Implementations must NOT cache the token across calls — the access token
    is short-lived and refresh-token rotation can happen at any time. Each
    `get_refresh_token()` call should re-read the canonical source.
    """

    def get_refresh_token(self) -> str:
        """Return the current refresh token. Raise XSensaiError on failure."""
        ...

    def store_refresh_token(self, token: str) -> None:
        """Persist a (possibly rotated) refresh token to the source."""
        ...


class KeychainTokenProvider:
    """macOS Keychain-backed token provider via the `keyring` library.

    Per /review F10 fix: uses keyring (which on macOS talks to
    Security.framework directly via PyObjC) instead of the `security` CLI.
    The token is never exposed via argv — the original `security
    add-generic-password ... -w <token>` design leaked the token to
    `ps -ef` for ~50ms per write. keyring's set_password() goes through
    the Security framework with no subprocess.

    Stores under service=x-sensai, account=x-api-refresh-token.
    """

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE_NAME,
        account: str = KEYCHAIN_ACCOUNT_NAME,
    ) -> None:
        self._service = service
        self._account = account

    def _get_keyring(self) -> object:
        """Lazy-import keyring so test environments without it can still
        import this module (tests that mock the provider don't need keyring)."""
        try:
            import keyring  # type: ignore[import-untyped]
        except ImportError:
            raise XSensaiError(
                code="OAUTH_SETUP_REQUIRED",
                cause="`keyring` package is not installed.",
                attempted="import keyring",
                next_action="Run: pip install keyring  (or: VIRTUAL_ENV=.venv uv pip install keyring)",
                retryable=False,
            )
        return keyring

    def get_refresh_token(self) -> str:
        keyring = self._get_keyring()
        try:
            token: Optional[str] = keyring.get_password(self._service, self._account)
        except Exception as e:
            raise XSensaiError(
                code="OAUTH_KEYCHAIN_BLOCKED",
                cause=f"Keychain lookup failed: {type(e).__name__}: {e}",
                attempted=f"keyring.get_password({self._service!r}, {self._account!r})",
                next_action=(
                    "Open Keychain Access and verify ACL on the x-sensai entry. "
                    "If unsolvable, re-run `python -m xsensai.sync.setup_oauth` "
                    "to recreate the entry."
                ),
                retryable=True,
            )

        if not token:
            raise XSensaiError(
                code="OAUTH_SETUP_REQUIRED",
                cause="X API refresh token not found in macOS Keychain.",
                attempted=f"keyring.get_password({self._service!r}, {self._account!r})",
                next_action=(
                    "Run `python -m xsensai.sync.setup_oauth` to authorize "
                    "x-sensai with your X developer app."
                ),
                retryable=True,
            )
        return token

    def store_refresh_token(self, token: str) -> None:
        if not token:
            raise ValueError("Refusing to store an empty refresh token.")
        keyring = self._get_keyring()
        try:
            keyring.set_password(self._service, self._account, token)
        except Exception as e:
            raise XSensaiError(
                code="OAUTH_KEYCHAIN_BLOCKED",
                cause=f"Keychain write failed: {type(e).__name__}: {e}",
                attempted=f"keyring.set_password({self._service!r}, {self._account!r}, ...)",
                next_action=(
                    "Open Keychain Access and grant write permission, or "
                    "verify the keyring backend (`python -c 'import keyring; "
                    "print(keyring.get_keyring())'`)."
                ),
                retryable=True,
            )


class EnvSecretTokenProvider:
    """Environment-variable-backed token provider.

    Used by tests (avoids Keychain prompts) and by Slice 5 cron (where the
    GitHub Actions encrypted secret is exported as an env var). Read-only:
    `store_refresh_token` is a soft no-op (cron can't write back to GitHub
    secrets without a PAT, and we deliberately avoid that blast radius —
    rotation handling becomes manual re-auth via setup_oauth.py).
    """

    def __init__(self, env_var: str = ENV_VAR_NAME) -> None:
        self._env_var = env_var

    def get_refresh_token(self) -> str:
        token = os.environ.get(self._env_var, "").strip()
        if not token:
            raise XSensaiError(
                code="OAUTH_SETUP_REQUIRED",
                cause=f"Environment variable {self._env_var} is unset or empty.",
                attempted=f"os.environ[{self._env_var!r}]",
                next_action=(
                    f"Export {self._env_var} with the X API refresh token, "
                    "or use KeychainTokenProvider in manual mode."
                ),
                retryable=True,
            )
        return token

    def store_refresh_token(self, token: str) -> None:
        # Intentional no-op: env vars are external-managed in cron; rotation
        # surfaces as the next get_refresh_token() returning the rotated value
        # (or AUTH_FAILED if the secret store wasn't updated).
        pass


def get_stored_client_id() -> Optional[str]:
    """Resolve the X dev app client_id from (in order):
      1. ${XSENSAI_X_CLIENT_ID} env var
      2. macOS Keychain at service=x-sensai, account=x-api-client-id

    Returns None if neither source has a value. The CLI surfaces
    OAUTH_CLIENT_ID_MISSING if both miss.

    Why store client_id in Keychain too? It's not secret, but it IS
    config the user only sets once via setup_oauth. Storing it next to
    the refresh token means /xsync from a fresh Claude Code session
    (which doesn't inherit env vars from the terminal that ran setup_oauth)
    just works without env-var plumbing.
    """
    # Env var wins (cron + tests use it; explicit override).
    env_val = os.environ.get(CLIENT_ID_ENV, "").strip()
    if env_val:
        return env_val
    # Keychain fallback (set by setup_oauth on successful auth).
    try:
        import keyring  # type: ignore[import-untyped]
        kc = keyring.get_password(KEYCHAIN_SERVICE_NAME, KEYCHAIN_CLIENT_ID_ACCOUNT)
        if kc and kc.strip():
            return kc.strip()
    except Exception:
        pass
    return None


def store_client_id(client_id: str) -> None:
    """Persist the X dev app client_id to Keychain so future invocations
    don't need the env var. Called by setup_oauth on successful auth."""
    if not client_id:
        raise ValueError("Refusing to store an empty client_id.")
    try:
        import keyring  # type: ignore[import-untyped]
        keyring.set_password(KEYCHAIN_SERVICE_NAME, KEYCHAIN_CLIENT_ID_ACCOUNT, client_id)
    except Exception as e:
        raise XSensaiError(
            code="OAUTH_KEYCHAIN_BLOCKED",
            cause=f"Could not persist client_id to Keychain: {type(e).__name__}: {e}",
            attempted=f"keyring.set_password({KEYCHAIN_SERVICE_NAME!r}, {KEYCHAIN_CLIENT_ID_ACCOUNT!r}, ...)",
            next_action=(
                "OAuth still succeeded; you can use /xsync by exporting "
                f"{CLIENT_ID_ENV} in your shell instead."
            ),
            retryable=True,
        )


def get_stored_client_secret() -> Optional[str]:
    """Resolve the X dev app client_secret (only required for Confidential
    OAuth 2.0 clients — Public Clients don't need a secret per PKCE).

    Resolution order:
      1. ${XSENSAI_X_CLIENT_SECRET} env var
      2. macOS Keychain at service=x-sensai, account=x-api-client-secret
      3. None (Public Client; PKCE without secret is fine)

    Most personal-tool X dev apps end up Confidential because the X dev
    portal defaults to "Web App" type. Switching to Native App / Single
    Page App (Public Client) avoids needing a secret, but most users
    won't realize that until they hit the auth failure.
    """
    env_val = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if env_val:
        return env_val
    try:
        import keyring  # type: ignore[import-untyped]
        kc = keyring.get_password(KEYCHAIN_SERVICE_NAME, KEYCHAIN_CLIENT_SECRET_ACCOUNT)
        if kc and kc.strip():
            return kc.strip()
    except Exception:
        pass
    return None


def store_client_secret(client_secret: str) -> None:
    """Persist the X dev app client_secret to Keychain (Confidential clients only).

    Public Clients (Native App / Single Page App) don't need this. Called
    by setup_oauth when the user provides --client-secret or the env var
    is set.
    """
    if not client_secret:
        raise ValueError("Refusing to store an empty client_secret.")
    try:
        import keyring  # type: ignore[import-untyped]
        keyring.set_password(KEYCHAIN_SERVICE_NAME, KEYCHAIN_CLIENT_SECRET_ACCOUNT, client_secret)
    except Exception as e:
        raise XSensaiError(
            code="OAUTH_KEYCHAIN_BLOCKED",
            cause=f"Could not persist client_secret to Keychain: {type(e).__name__}: {e}",
            attempted=f"keyring.set_password({KEYCHAIN_SERVICE_NAME!r}, {KEYCHAIN_CLIENT_SECRET_ACCOUNT!r}, ...)",
            next_action=(
                "OAuth still succeeded; you can use /xsync by exporting "
                f"{CLIENT_SECRET_ENV} in your shell instead."
            ),
            retryable=True,
        )


__all__ = [
    "TokenProvider",
    "KeychainTokenProvider",
    "EnvSecretTokenProvider",
    "get_stored_client_id",
    "store_client_id",
    "get_stored_client_secret",
    "store_client_secret",
    "KEYCHAIN_SERVICE_NAME",
    "KEYCHAIN_ACCOUNT_NAME",
    "KEYCHAIN_CLIENT_ID_ACCOUNT",
    "KEYCHAIN_CLIENT_SECRET_ACCOUNT",
    "ENV_VAR_NAME",
    "CLIENT_ID_ENV",
    "CLIENT_SECRET_ENV",
    "redact_token_strings",
]
