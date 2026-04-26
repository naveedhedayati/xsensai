"""Slug + idempotency helpers for paste card filenames.

slugify() turns paste content into a filesystem-safe stem. disambiguate_slug()
appends -2/-3/... until the filename is unique on disk. content_fingerprint()
hashes the content for the 24h idempotency window — two `/xpaste` of the same
content within 24h surface as "duplicate of {id}" instead of writing a second
card.

Pure functions. No I/O except disambiguate_slug() (which checks file existence).
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from pathlib import Path

from xsensai.errors import XSensaiError


_NON_ALPHANUM = re.compile(r"[^a-z0-9]+")
_LEADING_TRAILING_DASH = re.compile(r"^-+|-+$")
MAX_DISAMBIGUATION_ATTEMPTS = 1000  # pathological cap; raises INTERNAL_ERROR past this


def slugify(content: str, max_len: int = 40) -> str:
    """Generate a filename-safe slug from paste content.

    1. NFKD-normalize, strip combining marks (decomposes accented characters).
    2. Lowercase.
    3. Replace non-[a-z0-9] runs with a single dash.
    4. Strip leading/trailing dashes.
    5. Truncate to max_len, then strip trailing dash again so we don't end on -.
    6. Empty after all that (whitespace, emoji-only) → "untitled".
    """
    if not content:
        return "untitled"
    normalized = unicodedata.normalize("NFKD", content)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    lowered = ascii_only.lower()
    dashed = _NON_ALPHANUM.sub("-", lowered)
    stripped = _LEADING_TRAILING_DASH.sub("", dashed)
    if not stripped:
        return "untitled"
    truncated = stripped[:max_len]
    truncated = _LEADING_TRAILING_DASH.sub("", truncated)
    return truncated or "untitled"


def disambiguate_slug(corpus_path: Path, base_filename: str) -> str:
    """If `{corpus}/{base_filename}.md` exists, append -2, -3, ... until unique.

    Returns the disambiguated stem (no .md suffix). Raises
    XSensaiError(INTERNAL_ERROR) past MAX_DISAMBIGUATION_ATTEMPTS as a
    pathological-loop guard (the user shouldn't hit this in real life; if
    they do, something is genuinely wrong).
    """
    candidate = base_filename
    if not (corpus_path / f"{candidate}.md").exists():
        return candidate
    for attempt in range(2, MAX_DISAMBIGUATION_ATTEMPTS + 1):
        candidate = f"{base_filename}-{attempt}"
        if not (corpus_path / f"{candidate}.md").exists():
            return candidate
    raise XSensaiError(
        code="INTERNAL_ERROR",
        cause=f"Slug disambiguation exhausted {MAX_DISAMBIGUATION_ATTEMPTS} attempts for {base_filename!r}",
        attempted=f"disambiguate_slug({corpus_path}, {base_filename!r})",
        next_action=(
            "Something is wrong — either the corpus has thousands of same-day pastes "
            "with identical content prefixes, or the filesystem is misbehaving. "
            "Investigate manually."
        ),
        retryable=False,
    )


def content_fingerprint(content: str) -> str:
    """sha256 of the paste content, used for the 24h idempotency window.

    Returns 'sha256:<hex>'. Caller compares against recently-written cards'
    fingerprints to detect duplicate-paste accidents.
    """
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


__all__ = [
    "slugify",
    "disambiguate_slug",
    "content_fingerprint",
    "MAX_DISAMBIGUATION_ATTEMPTS",
]
