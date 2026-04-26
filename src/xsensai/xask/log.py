"""Question log for /xask — privacy-aware JSONL append + purge.

DX4 (privacy): default mode is `hash_only` — captures q_hash + meta but NOT
question text. Set `XSENSAI_XASK_LOG_MODE=full` to log raw text. `off`
disables logging entirely.

EC7 (Eng review): replaces the v2-draft shell script because flock(1) is
missing on macOS. Uses fcntl.flock for cross-process append safety, json.dumps
for all fields (escapes everything safely), chmod 600 for the log file.

CLI:
    python -m xsensai.xask.log purge       # delete entries older than retention
    python -m xsensai.xask.log show         # tail the log (jq-friendly)

Schema (one JSON line per /xask call):
    ts, q_hash, [question], top3, candidates, web, challenge_used,
    challenge_status, output_sha256, prompt_template_version,
    service_version, duration_ms
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Iterator, Optional

log = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 90
DEFAULT_MODE = "hash_only"  # off | hash_only | full

# File modes (Eng EC7 + EC11 security)
_LOG_FILE_MODE = 0o600
_LOG_DIR_MODE = 0o700

# F3 fix (review): scrub common secret patterns from question text BEFORE
# logging, even in `full` mode. The user might paste an API key into a
# /xask question and would otherwise have it sitting in
# ~/.cache/xsensai/xask-log.jsonl for 90 days at chmod 600. Best-effort —
# not a substitute for not pasting secrets.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),  # OpenAI-style
    re.compile(r"ghp_[A-Za-z0-9]{36}"),  # GitHub PAT
    re.compile(r"gho_[A-Za-z0-9]{36}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{40,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"xox[abps]-[A-Za-z0-9-]+"),  # Slack tokens
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),  # Google API key
    re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"),  # generic long token (last resort)
]


def _scrub_secrets(text: str) -> str:
    """Best-effort secret redaction for question log text."""
    if not text:
        return text
    out = text
    for pat in _SECRET_PATTERNS[:-1]:  # specific patterns first
        out = pat.sub("[REDACTED:secret]", out)
    # The generic 40+ long-token catch-all runs LAST so it doesn't shadow
    # specific patterns that would give better redaction labels.
    out = _SECRET_PATTERNS[-1].sub(
        lambda m: "[REDACTED:long-token]" if len(m.group(0)) > 40 else m.group(0),
        out,
    )
    return out


def _fsync_parent_dir(path: Path) -> None:
    """F6 fix: open parent dir + fsync after rename for crash-safety parity
    with the Slice 2 sidecar pattern. macOS APFS / Linux ext4 / XFS can lose
    a rename across power loss otherwise."""
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
    except OSError as e:
        log.debug("could not open parent dir for fsync: %s", e)
        return
    try:
        os.fsync(dir_fd)
    except OSError as e:
        log.debug("parent dir fsync failed: %s", e)
    finally:
        os.close(dir_fd)


@dataclass(frozen=True)
class LogEntry:
    """One row of the /xask question log.

    F5 fix (review): the `state` field distinguishes "started" (logged
    BEFORE host synthesis, so a crashed synthesis still leaves a bisect
    record) from "completed" (logged AFTER successful emit). Pair entries
    by q_hash + close ts for analysis.
    """

    ts: str
    q_hash: str
    question: Optional[str]  # None when mode == hash_only
    top3: list
    candidates: int
    web: str
    challenge_used: bool
    challenge_status: Optional[str]
    output_sha256: str  # "pending" when state == "started"
    prompt_template_version: str
    service_version: str
    duration_ms: int
    state: str = "completed"  # "started" | "completed"


def resolve_log_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "xsensai"


def resolve_log_file() -> Path:
    return resolve_log_dir() / "xask-log.jsonl"


def resolve_mode() -> str:
    raw = os.environ.get("XSENSAI_XASK_LOG_MODE", DEFAULT_MODE).strip().lower()
    if raw not in {"off", "hash_only", "full"}:
        log.warning(
            "XSENSAI_XASK_LOG_MODE=%r is not one of off|hash_only|full; "
            "defaulting to %s",
            raw,
            DEFAULT_MODE,
        )
        return DEFAULT_MODE
    return raw


# Back-compat aliases (in case anyone reaches into the underscore names)
_resolve_log_dir = resolve_log_dir
_resolve_log_file = resolve_log_file
_resolve_mode = resolve_mode


def resolve_retention_days() -> int:
    raw = os.environ.get("XSENSAI_XASK_LOG_RETENTION_DAYS")
    if raw is None:
        return DEFAULT_RETENTION_DAYS
    try:
        n = int(raw)
        if n < 1:
            return DEFAULT_RETENTION_DAYS
        return n
    except ValueError:
        return DEFAULT_RETENTION_DAYS


def append_log(
    *,
    question: str,
    top3: list,
    candidates: int,
    web: str,
    challenge_used: bool,
    challenge_status: Optional[str],
    output_sha256: str,
    prompt_template_version: str,
    service_version: str,
    duration_ms: int,
    state: str = "completed",
) -> Optional[Path]:
    """Append one entry. Honors XSENSAI_XASK_LOG_MODE.

    Returns the log file path (Path) on write, or None when mode=off.

    state="started" → log BEFORE synthesis (output_sha256 should be "pending").
    state="completed" → log AFTER synthesis with the real output_sha256.
    """
    mode = _resolve_mode()
    if mode == "off":
        return None

    log_dir = resolve_log_dir()
    log_file = resolve_log_file()
    log_dir.mkdir(parents=True, exist_ok=True, mode=_LOG_DIR_MODE)
    # mkdir doesn't always honor mode if the dir already exists; enforce:
    try:
        os.chmod(log_dir, _LOG_DIR_MODE)
    except OSError:
        pass

    # F3 fix: scrub secret patterns from question text before logging.
    safe_question = _scrub_secrets(question) if mode == "full" else None

    entry = LogEntry(
        ts=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        q_hash=sha256(question.encode("utf-8")).hexdigest()[:16],
        question=safe_question,
        top3=list(top3),
        candidates=candidates,
        web=web,
        challenge_used=challenge_used,
        challenge_status=challenge_status,
        output_sha256=output_sha256,
        prompt_template_version=prompt_template_version,
        service_version=service_version,
        duration_ms=duration_ms,
        state=state,
    )

    line = json.dumps(asdict(entry), ensure_ascii=False) + "\n"
    fd = os.open(log_file, os.O_WRONLY | os.O_APPEND | os.O_CREAT, _LOG_FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line.encode("utf-8"))
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
    # Re-chmod in case the file existed before with different perms:
    try:
        os.chmod(log_file, _LOG_FILE_MODE)
    except OSError:
        pass
    return log_file


def iter_entries(log_file: Optional[Path] = None) -> Iterator[dict]:
    """Yield each parsed log entry. Skips malformed lines with a warning."""
    p = log_file or resolve_log_file()
    if not p.exists():
        return
    with open(p, encoding="utf-8") as f:
        for i, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as e:
                log.warning("xask-log line %d malformed: %s", i, e)


def purge(
    retention_days: Optional[int] = None,
    log_file: Optional[Path] = None,
) -> int:
    """Delete entries older than retention_days. Returns count purged.

    F2 fix (review): hold an exclusive flock on the LIVE log file for the
    entire read+rewrite+replace window so a concurrent /xask `append_log`
    doesn't land between read and replace and get silently dropped.

    F6 fix: fsync the parent dir after os.replace so the rename survives
    power loss (matches Slice 2 sidecar durability).
    """
    days = retention_days if retention_days is not None else resolve_retention_days()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    p = log_file or resolve_log_file()
    if not p.exists():
        return 0

    # Open the LIVE log file (for advisory locking) — survivors are computed
    # while the lock is held so concurrent appenders block.
    live_fd = os.open(p, os.O_RDONLY)
    try:
        fcntl.flock(live_fd, fcntl.LOCK_EX)

        survivors = []
        purged = 0
        for entry in iter_entries(p):
            ts = entry.get("ts")
            try:
                entry_ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                survivors.append(entry)  # keep undated rows out of caution
                continue
            if entry_ts >= cutoff:
                survivors.append(entry)
            else:
                purged += 1

        tmp = p.with_suffix(".jsonl.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _LOG_FILE_MODE)
        try:
            for entry in survivors:
                os.write(
                    fd, (json.dumps(entry, ensure_ascii=False) + "\n").encode("utf-8")
                )
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, p)
        try:
            os.chmod(p, _LOG_FILE_MODE)
        except OSError:
            pass
        _fsync_parent_dir(p)
    finally:
        try:
            fcntl.flock(live_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(live_fd)
    return purged


def _cli() -> int:
    """`python -m xsensai.xask.log [purge|show]` entrypoint."""
    args = sys.argv[1:]
    if not args:
        print(
            "usage: python -m xsensai.xask.log [purge | show]",
            file=sys.stderr,
        )
        return 2
    cmd = args[0]
    if cmd == "purge":
        n = purge()
        # Machine-readable JSON for automation parity with `show` (A7 fix).
        print(
            json.dumps(
                {"ok": True, "purged": n, "retention_days": resolve_retention_days()}
            )
        )
        return 0
    if cmd == "show":
        for entry in iter_entries():
            print(json.dumps(entry, ensure_ascii=False))
        return 0
    print(f"Unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli())


__all__ = [
    "LogEntry",
    "append_log",
    "iter_entries",
    "purge",
    "DEFAULT_MODE",
    "DEFAULT_RETENTION_DAYS",
]
