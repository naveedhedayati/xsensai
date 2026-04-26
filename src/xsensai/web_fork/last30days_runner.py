"""Subprocess wrapper for the last30days Claude Code skill.

Eng review EC6: env-scrubbed (no Anthropic / X tokens passed through) +
executable-path validation (rejects executables not owned by the user).
Eng review EC2: real asyncio so callers can fan out retrieval + web fork
in parallel.

Outcomes:
    {"status": "ok",      "payload": <json>}
    {"status": "empty",   "payload": <json>}    # ok but no findings
    {"status": "missed",  "reason": "timeout"}
    {"status": "failed",  "reason": "<short>"}
    {"status": "skipped", "reason": "last30days_not_installed" | "executable_not_owned_by_user" | "user_opted_out"}
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, Dict

log = logging.getLogger(__name__)

DEFAULT_PATH = "~/.claude/skills/last30days/scripts/last30days.py"
DEFAULT_TIMEOUT_S = 20.0

# Cap question length so a runaway prompt-injected card body can't blow
# POSIX ARG_MAX (~256KB on macOS) or balloon the question log.
MAX_QUESTION_CHARS = 8192

# Allowlisted env vars passed to the subprocess. NOTHING with secrets.
_ALLOWED_PASSTHROUGH = (
    "PATH",
    "HOME",
    "XDG_CACHE_HOME",
    "LANG",
    "LC_ALL",
    "TERM",
    # Project-scoped XSENSAI vars that don't carry secrets:
    "XSENSAI_CORPUS_PATH",
    "XSENSAI_QMD_PATH",
)

# Belt-and-suspenders deny pattern: if a future maintainer adds a name like
# "AWS_PROFILE" to _ALLOWED_PASSTHROUGH, this regex catches the obviously
# secret-shaped names so the error fires loudly at module load (via the
# _validate_allowlist call below) rather than silently leaking.
_SECRET_NAME_RE = re.compile(
    r"(?i)(token|key|secret|password|credential|api_?key|"
    r"aws_(?:secret|session|access)|gcp_|google_|github_|gh_|"
    r"anthropic|openai|npm_token)"
)


def _validate_allowlist() -> None:
    """Raise at module load if anyone adds a secret-shaped name to the allowlist."""
    for name in _ALLOWED_PASSTHROUGH:
        if _SECRET_NAME_RE.search(name):
            raise RuntimeError(
                f"_ALLOWED_PASSTHROUGH contains a secret-shaped name: {name!r}. "
                "Refusing to forward potentially-sensitive env to last30days. "
                "Either rename the var or remove it from the allowlist."
            )


_validate_allowlist()


def resolve_path() -> Path:
    raw = os.environ.get("XSENSAI_LAST30DAYS_PATH", DEFAULT_PATH)
    return Path(raw).expanduser()


# Back-compat alias (callers used to reach the underscore name)
_resolve_path = resolve_path


def _scrubbed_env() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in _ALLOWED_PASSTHROUGH:
        v = os.environ.get(k)
        if v is not None:
            out[k] = v
    out.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    return out


def _is_empty_payload(payload: Any) -> bool:
    """Heuristic: last30days success-empty (Eng EC5 branch table)."""
    if payload is None:
        return True
    if isinstance(payload, list):
        return len(payload) == 0
    if isinstance(payload, dict):
        # Common shape: {"results": [...]} or {"items": [...]}
        for k in ("results", "items", "findings", "data"):
            if k in payload:
                v = payload[k]
                if isinstance(v, list):
                    return len(v) == 0
        return len(payload) == 0
    return False


async def run_last30days(
    question: str,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Invoke the last30days skill subprocess with hard timeout + env scrub."""
    if len(question) > MAX_QUESTION_CHARS:
        return {
            "status": "failed",
            "reason": f"question_too_long:{len(question)}>{MAX_QUESTION_CHARS}",
        }
    path = resolve_path()
    if not path.exists() or not path.is_file():
        return {"status": "skipped", "reason": "last30days_not_installed"}
    try:
        # S2 fix: lstat() not stat() so we see the SYMLINK's ownership, not
        # the target's. An attacker who can drop a symlink at the
        # XSENSAI_LAST30DAYS_PATH location could otherwise point it at a
        # user-owned binary and bypass the uid check.
        lst = path.lstat()
        if stat.S_ISLNK(lst.st_mode):
            return {
                "status": "skipped",
                "reason": "executable_is_symlink_refused",
            }
        if lst.st_uid != os.getuid():
            return {
                "status": "skipped",
                "reason": "executable_not_owned_by_user",
            }
    except OSError as e:
        return {"status": "failed", "reason": f"stat_error:{e.errno}"}

    env = _scrubbed_env()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            str(path),
            question,
            "--quick",
            "--emit=compact",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
    except (OSError, FileNotFoundError) as e:
        return {"status": "failed", "reason": f"spawn_error:{e}"}

    # Adversarial-review HIGH: cap stdout/stderr buffer to avoid OOM if a
    # buggy or hostile last30days install emits gigabytes of JSON.
    try:
        stdout, stderr = await asyncio.wait_for(
            _bounded_communicate(proc, max_bytes=1024 * 1024),  # 1 MiB cap
            timeout=timeout_s,
        )
    except _StdoutTooLarge as e:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        return {"status": "failed", "reason": f"stdout_too_large:{e.size}>{e.cap}"}
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("last30days subprocess refused to die after kill()")
        return {"status": "missed", "reason": "timeout"}
    except asyncio.CancelledError:
        # Caller (e.g. /xask early-return) cancelled us; clean up child.
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            log.warning("last30days subprocess refused to die after cancel")
        raise

    if proc.returncode != 0:
        first_line = (stderr.decode("utf-8", errors="replace").splitlines() or [""])[0]
        return {"status": "failed", "reason": first_line[:200] or f"exit_{proc.returncode}"}

    try:
        payload = json.loads(stdout.decode("utf-8", errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"status": "failed", "reason": f"parse_error:{type(e).__name__}"}

    if _is_empty_payload(payload):
        return {"status": "empty", "payload": payload}
    return {"status": "ok", "payload": payload}


class _StdoutTooLarge(Exception):
    def __init__(self, size: int, cap: int):
        self.size = size
        self.cap = cap


async def _bounded_communicate(proc, *, max_bytes: int):
    """proc.communicate() with a hard byte cap on stdout to prevent OOM.

    A buggy or hostile last30days install that emits unbounded output would
    otherwise be buffered in memory until either the timeout fires or RAM is
    exhausted. We read in chunks and bail past the cap.
    """
    stdout_chunks = []
    stderr_chunks = []
    total = 0

    async def _drain(stream, sink, count_total: bool):
        nonlocal total
        while True:
            chunk = await stream.read(64 * 1024)
            if not chunk:
                break
            sink.append(chunk)
            if count_total:
                total += len(chunk)
                if total > max_bytes:
                    raise _StdoutTooLarge(size=total, cap=max_bytes)

    await asyncio.gather(
        _drain(proc.stdout, stdout_chunks, count_total=True),
        _drain(proc.stderr, stderr_chunks, count_total=False),
    )
    await proc.wait()
    return b"".join(stdout_chunks), b"".join(stderr_chunks)


__all__ = ["run_last30days", "DEFAULT_TIMEOUT_S", "DEFAULT_PATH", "MAX_QUESTION_CHARS"]
