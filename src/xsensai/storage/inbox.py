"""Quick inbox writer for /xpaste abort recovery + tentative crash snapshots.

`/xpaste` mid-flow aborts (user said "no" at confirm, or Ctrl-C between
content and confirm) write content here so it isn't lost. `/xpaste recover`
reads from here to restore the most recent abort entry.

Path resolution (3-level fallback so abort-save NEVER silently fails):
  1. $XSENSAI_VAULT_INBOX (explicit override; must be a writable file path)
  2. {corpus_parent}/00_inbox/quick.md (vault convention; corpus is
     04_areas/x-bookmarks/, parent is the vault root)
  3. {corpus}/_inbox-quick.md (last-resort fallback inside the corpus dir)

Tentative snapshots (PASTE_CRASHED defense per UC11): written immediately
after step 1 (content received) and confirmed-deleted on step 7 success.
Format uses a marker so /xpaste recover can find them.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

from xsensai.errors import XSensaiError
from xsensai.storage import sidecar

log = logging.getLogger(__name__)


MARKER_BEGIN = "<!-- xsensai-abort-begin -->"
MARKER_END = "<!-- xsensai-abort-end -->"
TENTATIVE_PREFIX = "<!-- xsensai-tentative:"  # closes with marker_end-style
TENTATIVE_SUFFIX = " -->"

# UUID regex (8-4-4-4-12 hex with dashes) — caller-supplied snapshot_ids
# MUST match this. Without validation, a snapshot_id like
# `abc -->\n<!-- xsensai-tentative:evil` could inject markers into the
# inbox file and corrupt _split_blocks parsing.
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


def _vault_inbox_path(corpus_path: Path) -> Path:
    """Single source of truth for the vault-convention inbox path.

    Vault layout: corpus is `04_areas/x-bookmarks/`, inbox is `00_inbox/quick.md`
    one level up. Both `resolve_inbox_path` and `_append_or_fallback` use this.
    """
    return corpus_path.parent.parent / "00_inbox" / "quick.md"


def resolve_inbox_path(corpus_path: Path) -> Tuple[Path, int]:
    """Resolve inbox path via the 3-level fallback. Returns (path, level).

    Level 1 = $XSENSAI_VAULT_INBOX. Level 2 = vault/00_inbox/quick.md.
    Level 3 = corpus/_inbox-quick.md. Caller logs the level for debug.
    """
    override = os.environ.get("XSENSAI_VAULT_INBOX")
    if override:
        # /review F25: validate the override stays under $HOME OR the corpus
        # parent root. Without this, a malicious env var (sourced from gbrain
        # sync, a wrapper script, etc.) becomes an arbitrary file-write
        # primitive into the inbox writer. We allow $HOME because users may
        # legitimately want the inbox at ~/inbox.md or similar.
        override_path = Path(override).expanduser()
        try:
            resolved = override_path.resolve()
            home = Path.home().resolve()
            vault_root = corpus_path.parent.parent.resolve()
            ok = False
            for root in (home, vault_root):
                try:
                    resolved.relative_to(root)
                    ok = True
                    break
                except ValueError:
                    continue
            if ok:
                return override_path, 1
            log.warning(
                "XSENSAI_VAULT_INBOX=%s is outside $HOME and the vault root; "
                "rejecting and falling back to vault inbox.",
                override,
            )
        except OSError as e:
            log.warning("XSENSAI_VAULT_INBOX=%s could not be resolved: %s", override, e)
        # Fall through to level 2/3 if override rejected.
    # corpus_path NOT resolved (per Eng review symlink edge case): use the
    # un-resolved input for parent calculation so symlinked corpora don't
    # silently traverse to a different vault. If user wants explicit, set
    # XSENSAI_VAULT_INBOX.
    vault_inbox = _vault_inbox_path(corpus_path)
    if vault_inbox.parent.exists():
        return vault_inbox, 2
    return corpus_path / "_inbox-quick.md", 3


def append_to_quick_inbox(
    content: str,
    corpus_path: Path,
    why_saved_attempt: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Path:
    """Append a confirmed abort (user said "no" or "cancel") to the inbox.

    Returns the path written. Raises XSensaiError(PASTE_CRASHED) if all 3
    fallback paths fail to write — abort-save MUST NOT silently lose data.
    """
    path, level = resolve_inbox_path(corpus_path)
    log.info("inbox path resolved: %s (fallback level: %d)", path, level)

    entry = _format_entry(content, why_saved_attempt, source_url, tentative=False)
    return _append_or_fallback(entry, path, corpus_path)


def write_tentative_snapshot(
    content: str,
    corpus_path: Path,
    snapshot_id: str,
    why_saved_attempt: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Path:
    """Write a tentative snapshot at /xpaste step 1 (content received).

    snapshot_id MUST match the UUID4 format (8-4-4-4-12 hex with dashes).
    Without strict validation, a snapshot_id containing the literal markers
    `<!-- xsensai-tentative:` or newlines could inject metadata into adjacent
    blocks and corrupt _split_blocks parsing — see _UUID_RE.

    On successful step 7 the caller passes snapshot_id to
    clear_tentative_snapshot() to remove this entry. If /xpaste crashes
    (Ctrl-C, network drop) before step 7, the entry survives in the inbox
    and `/xpaste recover` can restore it.
    """
    if not _UUID_RE.match(snapshot_id):
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause=f"Invalid snapshot_id format: {snapshot_id!r}",
            attempted="write_tentative_snapshot",
            next_action="snapshot_id must be a UUID4 string. Use uuid.uuid4().",
            retryable=False,
        )
    path, level = resolve_inbox_path(corpus_path)
    log.info("tentative snapshot path: %s (fallback level: %d, id: %s)", path, level, snapshot_id)
    entry = _format_entry(
        content, why_saved_attempt, source_url, tentative=True, snapshot_id=snapshot_id
    )
    return _append_or_fallback(entry, path, corpus_path)


def clear_tentative_snapshot(snapshot_id: str, corpus_path: Path) -> bool:
    """Remove a tentative snapshot block by its snapshot_id.

    Returns True if removed, False if not found (idempotent — safe to call
    even if the snapshot was never written, e.g., when content reached the
    inbox via append_to_quick_inbox instead).
    """
    path, _ = resolve_inbox_path(corpus_path)
    if not path.exists():
        return False
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        return False
    marker = f"{TENTATIVE_PREFIX}{snapshot_id}{TENTATIVE_SUFFIX}"
    if marker not in existing:
        return False
    blocks = _split_blocks(existing)
    kept = [b for b in blocks if marker not in b]
    new_content = "".join(kept)
    try:
        sidecar.durable_replace(path, new_content.encode("utf-8"))
        return True
    except XSensaiError:
        log.warning("clear_tentative_snapshot: durable_replace failed for %s", path)
        return False


def list_recoverable(corpus_path: Path) -> List[dict]:
    """List recoverable entries (tentative + confirmed aborts) newest-first.

    Each dict: {timestamp, kind ('tentative'|'abort'), content, why_saved,
    source_url, snapshot_id (tentative only)}.
    """
    path, _ = resolve_inbox_path(corpus_path)
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    blocks = _split_blocks(text)
    parsed: List[dict] = []
    for block in blocks:
        if MARKER_BEGIN not in block:
            continue
        parsed.append(_parse_block(block))
    parsed.reverse()  # newest first
    return parsed


def _append_or_fallback(entry: str, primary: Path, corpus_path: Path) -> Path:
    """Try writing to primary; if that fails, walk the fallback chain.

    Raises PASTE_CRASHED only if ALL paths fail.
    """
    paths_to_try: List[Path] = [primary]
    # Build the ordered fallback chain (skip duplicates).
    if "XSENSAI_VAULT_INBOX" in os.environ:
        # primary was the override; add levels 2 + 3 as fallbacks
        l2 = _vault_inbox_path(corpus_path)
        l3 = corpus_path / "_inbox-quick.md"
        paths_to_try.extend([l2, l3])
    elif primary.name == "quick.md" and primary.parent.name == "00_inbox":
        # primary was level 2; level 3 remains
        paths_to_try.append(corpus_path / "_inbox-quick.md")
    # If primary was level 3 already, no further fallback.

    last_err: Optional[Exception] = None
    for path in paths_to_try:
        try:
            return _append_one(entry, path)
        except OSError as e:
            log.warning("inbox write failed for %s: %s", path, e)
            last_err = e
            continue
    raise XSensaiError(
        code="PASTE_CRASHED",
        cause="All inbox fallback paths failed; aborted paste content could not be saved.",
        attempted=f"append_to_quick_inbox(corpus={corpus_path})",
        next_action=(
            "Check filesystem permissions on the vault directory and the corpus directory. "
            "Your pasted content was not preserved — re-run /xpaste with the content "
            "still in your scrollback."
        ),
        retryable=False,
        details=f"last error: {last_err}",
    )


def _append_one(entry: str, path: Path) -> Path:
    """Append a single entry to one path. Reads existing, appends, writes back
    atomically via durable_replace. Raises OSError on filesystem failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = ""
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing and not existing.endswith("\n"):
            existing += "\n"
    new_content = existing + entry
    sidecar.durable_replace(path, new_content.encode("utf-8"))
    return path


def _format_entry(
    content: str,
    why_saved_attempt: Optional[str],
    source_url: Optional[str],
    tentative: bool,
    snapshot_id: Optional[str] = None,
) -> str:
    """Format an inbox entry block, marker-bracketed for clean parsing."""
    ts = datetime.now(timezone.utc).isoformat()
    lines = [MARKER_BEGIN]
    lines.append(f"<!-- timestamp: {ts} -->")
    lines.append(f"<!-- kind: {'tentative' if tentative else 'abort'} -->")
    if tentative and snapshot_id:
        lines.append(f"{TENTATIVE_PREFIX}{snapshot_id}{TENTATIVE_SUFFIX}")
    if why_saved_attempt:
        lines.append(f"<!-- why_saved_attempt: {_escape_html(why_saved_attempt)} -->")
    if source_url:
        lines.append(f"<!-- source_url: {_escape_html(source_url)} -->")
    lines.append("")
    lines.append(content)
    lines.append("")
    lines.append(MARKER_END)
    lines.append("")
    return "\n".join(lines) + "\n"


def _split_blocks(text: str) -> List[str]:
    """Split text into blocks separated by MARKER_END+newline so list_recoverable
    can parse each as a separate entry. Non-marker preamble is preserved as
    its own (non-recoverable) block.
    """
    blocks: List[str] = []
    cursor = 0
    while True:
        idx = text.find(MARKER_END + "\n", cursor)
        if idx == -1:
            tail = text[cursor:]
            if tail:
                blocks.append(tail)
            break
        end = idx + len(MARKER_END) + 1
        blocks.append(text[cursor:end])
        cursor = end
    return blocks


def _extract_comment_value(line: str, key: str) -> str:
    """Extract VALUE from a `<!-- KEY: VALUE -->` line.

    Uses removeprefix/removesuffix instead of `.rstrip(' -->')` — the old
    version was a multi-character set strip that silently corrupted values
    legitimately ending in space, dash, or `>`.
    """
    return line.removeprefix(f"<!-- {key}:").removesuffix(" -->").strip()


def _parse_block(block: str) -> dict:
    """Extract metadata + content from a marker-bracketed block."""
    out: dict = {"kind": "abort", "content": "", "why_saved_attempt": None, "source_url": None,
                 "timestamp": None, "snapshot_id": None}
    lines = block.splitlines()
    content_lines: List[str] = []
    in_content = False
    for line in lines:
        if line.startswith(MARKER_BEGIN) or line.startswith(MARKER_END):
            in_content = False
            continue
        if line.startswith("<!-- timestamp:"):
            out["timestamp"] = _extract_comment_value(line, "timestamp")
            continue
        if line.startswith("<!-- kind:"):
            out["kind"] = _extract_comment_value(line, "kind")
            continue
        if line.startswith(TENTATIVE_PREFIX):
            out["snapshot_id"] = line[len(TENTATIVE_PREFIX):-len(TENTATIVE_SUFFIX)]
            continue
        if line.startswith("<!-- why_saved_attempt:"):
            out["why_saved_attempt"] = _extract_comment_value(line, "why_saved_attempt")
            continue
        if line.startswith("<!-- source_url:"):
            out["source_url"] = _extract_comment_value(line, "source_url")
            continue
        if line == "" and not in_content:
            in_content = True
            continue
        if in_content:
            content_lines.append(line)
    out["content"] = "\n".join(content_lines).strip()
    return out


def _escape_html(s: str) -> str:
    """Escape HTML-comment-breaking sequences AND newlines.

    `-->` in user content would close the comment block prematurely.
    Newlines/CRs would break out of the single-line comment metadata format
    and could inject fake `<!-- kind: -->` headers into adjacent blocks
    (see /review F24 — paste content marker-injection class).
    """
    return (
        s.replace("-->", "--&gt;")
         .replace("\r", " ")
         .replace("\n", " ")
    )


__all__ = [
    "resolve_inbox_path",
    "append_to_quick_inbox",
    "write_tentative_snapshot",
    "clear_tentative_snapshot",
    "list_recoverable",
    "MARKER_BEGIN",
    "MARKER_END",
]
