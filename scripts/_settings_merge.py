"""Merge `permissions.ask` entries into ~/.claude/settings.json.

Used by `scripts/install_commands.sh` to auto-wire the cryptographic gate for
xsensai's destructive MCP tools (`delete_bookmark`, `restore_bookmark`).

Slice 7.5 (v0.9.0.0): per AE1 / AE2 / TD-ENG-1, this helper
- writes to USER-global `~/.claude/settings.json` (matches install_commands.sh
  per-user precedent)
- uses stdlib json only (no jq dep)
- on malformed JSON: backs up to `{path}.bak.{ts}` and skips the merge,
  exiting 0 so install_commands.sh continues
- detects pre-existing `permissions.allow` wildcards that would subsume our
  literal `ask` entries and prints a loud `[PERMISSIONS_WILDCARD_OVERRIDE]`
  warning to stdout
- announces every change via stdout (silent mutation = hostile DX, AD1)
- idempotent (running twice is a no-op)

Exit codes:
  0  — merged successfully OR no-op (already present) OR safe-skipped on
       malformed JSON.
  Anything else — unexpected failure (caller may abort install).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
from pathlib import Path
from typing import Iterable

# Slice 7.5 destructive MCP tool entries to ensure are in `permissions.ask`.
ASK_ENTRIES = [
    "mcp__xsensai__delete_bookmark",
    "mcp__xsensai__restore_bookmark",
]

# Cap the number of `{path}.bak.{ts}` backup files we keep alongside the
# settings file. Settings can contain API keys and other secrets, so unbounded
# accumulation is a privacy concern (per /review F9).
MAX_BACKUPS = 3


def _wildcard_subsumes(entry: str, allow_list: Iterable) -> bool:
    """Return True if any item in `allow_list` matches `entry` literally or as
    a `*`-suffixed prefix wildcard (e.g., `mcp__*` covers `mcp__xsensai__delete_bookmark`).

    Non-string entries (per /review F4: a hostile or accidentally malformed
    `permissions.allow` entry of type int / dict / null would raise
    AttributeError on `.endswith()`) are skipped silently — they cannot
    match string MCP tool names anyway.
    """
    for pattern in allow_list:
        if not isinstance(pattern, str):
            continue
        if pattern == entry:
            return True
        if pattern.endswith("*"):
            prefix = pattern[:-1]
            if entry.startswith(prefix):
                return True
    return False


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write JSON to `path` atomically with mode 0600 (per /review F3 + F9).

    1. Write to `{path}.tmp.{pid}` first, fsync, then `os.replace`. POSIX
       guarantees the rename is atomic — readers see either the old file or
       the new one, never a torn write.
    2. chmod 0600 BEFORE the replace so secrets in the new file are never
       world-readable, even briefly.
    """
    tmp = path.with_suffix(path.suffix + f".tmp.{os.getpid()}")
    payload = json.dumps(data, indent=2) + "\n"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, payload.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _write_backup(path: Path, raw: str, ts: str) -> Path:
    """Write a `{path}.bak.{ts}` file with mode 0600 (secrets-safe per /review
    F9). Uses O_EXCL so an attacker pre-planting a symlink at the backup path
    causes a write failure, not a follow-the-symlink data leak.
    """
    backup = path.with_suffix(path.suffix + f".bak.{ts}")
    fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, raw.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    return backup


def _gc_backups(path: Path, keep: int = MAX_BACKUPS) -> None:
    """Keep only the N most recent backups; unlink the rest. Settings files
    contain secrets (API keys, tokens) — unbounded backup accumulation is a
    privacy concern (per /review F9).
    """
    pattern = f"{path.name}.bak.*"
    backups = sorted(path.parent.glob(pattern), reverse=True)
    for old in backups[keep:]:
        try:
            old.unlink()
        except OSError:
            pass


def _safe_skip(message: str) -> int:
    """Print [SETTINGS_MALFORMED] envelope to stdout and exit 0 (skip merge,
    don't kill install_commands.sh). Per AE2 — malformed JSON is non-fatal.
    """
    print(f"[SETTINGS_MALFORMED] {message} Permissions wiring SKIPPED.")
    return 0


def _merge_locked(settings_path: Path) -> int:
    """Read-modify-write the settings file. Caller has already acquired the
    flock on the parent directory (see `merge`).
    """
    ts = time.strftime("%Y%m%d-%H%M%S")

    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"permissions": {"ask": list(ASK_ENTRIES)}}
        _atomic_write_json(settings_path, data)
        print(
            f"Created {settings_path} with permissions.ask entries: "
            f"{', '.join(ASK_ENTRIES)}"
        )
        return 0

    # /review F8: settings.json is in the user's own ~/.claude/ — but the
    # `|| true` in install_commands.sh meant a 1 GB JSON-bomb file would
    # silently kill install_commands.sh's settings step via OOM. Cap the
    # read at 1 MB and safe-skip beyond that.
    if settings_path.is_file() and settings_path.stat().st_size > 1_000_000:
        return _safe_skip(
            f"{settings_path} is larger than 1 MB; refusing to parse."
        )

    try:
        raw = settings_path.read_text(encoding="utf-8")
    except (IsADirectoryError, OSError) as e:
        return _safe_skip(f"{settings_path} could not be read ({e}).")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        backup = _write_backup(settings_path, raw, ts)
        print(
            f"[SETTINGS_MALFORMED] {settings_path} is not valid JSON ({e}). "
            f"Backed up to {backup}. Permissions wiring SKIPPED — fix the JSON "
            f"and re-run ./scripts/install_commands.sh."
        )
        return 0

    if not isinstance(data, dict):
        return _safe_skip(f"{settings_path} top-level is not an object.")

    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        return _safe_skip(f"{settings_path} `permissions` is not an object.")

    ask = perms.setdefault("ask", [])
    if not isinstance(ask, list):
        return _safe_skip(f"{settings_path} `permissions.ask` is not an array.")

    allow_list = perms.get("allow", [])
    if not isinstance(allow_list, list):
        allow_list = []

    overrides = [e for e in ASK_ENTRIES if _wildcard_subsumes(e, allow_list)]
    if overrides:
        # Privacy contract (per /review): only echo the SUBSUMED entries
        # (xsensai's own tools). Never include the matching pattern from
        # `allow_list` in the warning — that would leak which other tools
        # the user has configured.
        print(
            f"[PERMISSIONS_WILDCARD_OVERRIDE] WARNING: {settings_path} has "
            f"`permissions.allow` entries that subsume the new `ask` entries: "
            f"{overrides}. The Claude Code permission prompt will NOT fire for "
            f"these tools. To enable the gate, remove or narrow the matching "
            f"`allow` entries. (See docs/PERMISSIONS_ASK.md.)"
        )

    added = []
    for entry in ASK_ENTRIES:
        if entry not in ask:
            ask.append(entry)
            added.append(entry)

    if not added:
        print(
            f"permissions.ask entries already present in {settings_path} "
            f"(no changes)."
        )
        return 0

    backup = _write_backup(settings_path, raw, ts)
    _atomic_write_json(settings_path, data)
    _gc_backups(settings_path)
    print(
        f"Added permissions.ask entries to {settings_path}: {', '.join(added)} "
        f"(backup: {backup}). See docs/PERMISSIONS_ASK.md."
    )
    return 0


def merge(settings_path: Path) -> int:
    """Read-modify-write the settings file under a per-directory flock so
    concurrent `install_commands.sh` runs don't lose updates (per /review F3).
    """
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = settings_path.parent / ".xsensai-settings.lock"
    fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _merge_locked(settings_path)
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        target = Path(argv[1]).expanduser()
    else:
        target = Path.home() / ".claude" / "settings.json"
    return merge(target)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
