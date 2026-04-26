"""Token providers — abstract the X API refresh-token source.

Per /autoplan D-S2 fix: UC-1=C made the orchestrator headless-ready but auth
was still desktop/Keychain-centric. TokenProviderProtocol decouples the
sync.client.XClient from how the token is sourced. Slice 4 ships:

  - KeychainTokenProvider  — reads/writes via macOS `security` CLI (manual mode)
  - EnvSecretTokenProvider — reads from environment (used by tests + Slice 5 cron)

Slice 5 cron will instantiate EnvSecretTokenProvider with the GitHub Actions
encrypted secret. No XClient code changes needed.
"""

from __future__ import annotations

import os
import subprocess
from typing import Protocol, runtime_checkable

from xsensai.errors import XSensaiError


KEYCHAIN_SERVICE_NAME = "x-sensai"
KEYCHAIN_ACCOUNT_NAME = "x-api-refresh-token"
ENV_VAR_NAME = "XSENSAI_X_REFRESH_TOKEN"


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
    """macOS Keychain-backed token provider via the `security` CLI.

    Stores under service=x-sensai, account=x-api-refresh-token. ACL defaults
    to "only the calling app" — if the user runs /xsync from `python` and
    later from `uv run python`, they may get a Keychain prompt the first
    time the new identity tries to read.
    """

    def __init__(
        self,
        service: str = KEYCHAIN_SERVICE_NAME,
        account: str = KEYCHAIN_ACCOUNT_NAME,
    ) -> None:
        self._service = service
        self._account = account

    def get_refresh_token(self) -> str:
        try:
            result = subprocess.run(
                [
                    "security", "find-generic-password",
                    "-s", self._service,
                    "-a", self._account,
                    "-w",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
        except subprocess.TimeoutExpired:
            raise XSensaiError(
                code="OAUTH_KEYCHAIN_BLOCKED",
                cause="Keychain `security` lookup timed out (likely a blocking permission prompt).",
                attempted=f"security find-generic-password -s {self._service} -a {self._account}",
                next_action=(
                    "Open Keychain Access, grant the calling Python access to "
                    f"`{self._service}/{self._account}`, then re-run /xsync."
                ),
                retryable=True,
            )
        except FileNotFoundError:
            raise XSensaiError(
                code="OAUTH_SETUP_REQUIRED",
                cause="`security` CLI not found — Keychain unavailable on this host.",
                attempted="security find-generic-password (macOS Keychain CLI)",
                next_action=(
                    "Either run on macOS, or use EnvSecretTokenProvider with "
                    f"the {ENV_VAR_NAME} environment variable set."
                ),
                retryable=False,
            )

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            if "could not be found" in stderr.lower() or result.returncode == 44:
                raise XSensaiError(
                    code="OAUTH_SETUP_REQUIRED",
                    cause="X API refresh token not found in macOS Keychain.",
                    attempted=f"security find-generic-password -s {self._service} -a {self._account}",
                    next_action=(
                        "Run `python -m xsensai.sync.setup_oauth` to authorize "
                        "x-sensai with your X developer app."
                    ),
                    retryable=True,
                )
            raise XSensaiError(
                code="OAUTH_KEYCHAIN_BLOCKED",
                cause=f"Keychain lookup failed (rc={result.returncode}): {stderr or '<no stderr>'}",
                attempted=f"security find-generic-password -s {self._service} -a {self._account}",
                next_action=(
                    "Check Keychain Access for ACL issues on the entry. If unsolvable, "
                    "re-run `python -m xsensai.sync.setup_oauth` to recreate the entry."
                ),
                retryable=True,
            )

        token = (result.stdout or "").strip()
        if not token:
            raise XSensaiError(
                code="OAUTH_SETUP_REQUIRED",
                cause="Keychain returned an empty refresh token.",
                attempted=f"security find-generic-password -s {self._service} -a {self._account}",
                next_action=(
                    "Re-run `python -m xsensai.sync.setup_oauth` to write a fresh token."
                ),
                retryable=True,
            )
        return token

    def store_refresh_token(self, token: str) -> None:
        if not token:
            raise ValueError("Refusing to store an empty refresh token.")
        # Use -U to update if the entry already exists; -w sets the secret.
        try:
            result = subprocess.run(
                [
                    "security", "add-generic-password",
                    "-s", self._service,
                    "-a", self._account,
                    "-w", token,
                    "-U",
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=15.0,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            raise XSensaiError(
                code="OAUTH_KEYCHAIN_BLOCKED",
                cause=f"Keychain `security add-generic-password` failed: {type(e).__name__}",
                attempted=f"security add-generic-password -s {self._service} -a {self._account}",
                next_action="Open Keychain Access and grant write permission, then retry.",
                retryable=True,
            )
        if result.returncode != 0:
            raise XSensaiError(
                code="OAUTH_KEYCHAIN_BLOCKED",
                cause=f"Keychain write failed (rc={result.returncode}): {(result.stderr or '').strip()}",
                attempted=f"security add-generic-password -s {self._service} -a {self._account}",
                next_action="Check Keychain Access permissions and retry.",
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


__all__ = [
    "TokenProvider",
    "KeychainTokenProvider",
    "EnvSecretTokenProvider",
    "KEYCHAIN_SERVICE_NAME",
    "KEYCHAIN_ACCOUNT_NAME",
    "ENV_VAR_NAME",
]
