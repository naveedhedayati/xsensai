"""Card writer — XDK bookmark dict → v2 LoadedCard, written through the lock.

Per /autoplan E-4 fix: the card-write transaction order is **always**:
  (a) write .md + .raw.txt with extraction_pending=True
  (b) append source_id to checkpoint
  (c) extraction (later — inline or deferred per UC-2=C)

This ordering is crash-safe: a crash between (b) and (c) leaves cards with
extraction_pending=True that `/xextract retry-failed` picks up. A crash
between (a) and (b) is recoverable via dedup on the next run.

Per /autoplan E-5 fix: author handle is sanitized via _safe_handle() to
prevent any path-traversal or subprocess injection attacks via crafted
X usernames (X handles are already constrained to [A-Za-z0-9_], but verify).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from xsensai.errors import XSensaiError
from xsensai.model.card import CardFrontmatter, CardMedia, LoadedCard
from xsensai.storage.corpus import resolve_corpus_path, write_card
from xsensai.sync.client import ThreadFetchResult


log = logging.getLogger(__name__)


_HANDLE_SAFE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_HANDLE_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_]")


@dataclass(frozen=True)
class CardWriteResult:
    """Outcome of writing one card."""

    card_id: str
    md_path: Path
    extraction_pending: bool
    thread_fetch_status: str  # complete / outside_window / unknown_empty / failed / not_applicable


def write_one(
    *,
    bookmark: Dict[str, Any],
    thread: ThreadFetchResult,
    corpus_path: Path,
    lock_token: str,
    run_id: str,
    captured: Optional[datetime] = None,
) -> CardWriteResult:
    """Convert a single XDK bookmark + thread result into a v2 card on disk.

    Caller MUST already hold the card_write lock and pass its fencing token.
    """
    captured = captured or datetime.now(timezone.utc)
    corpus = resolve_corpus_path(corpus_path)

    fm = build_frontmatter(
        bookmark=bookmark, thread=thread, captured=captured, run_id=run_id,
    )
    body = build_body(bookmark=bookmark, thread=thread)
    raw_bytes = build_raw_bytes(bookmark=bookmark)
    md_path = corpus / build_filename(fm)

    card = LoadedCard(fm=fm, body=body, raw_bytes=raw_bytes, md_path=md_path)
    written = write_card(card, lock_token=lock_token, corpus_path=corpus)

    return CardWriteResult(
        card_id=written.id,
        md_path=written.md_path,
        extraction_pending=written.fm.extraction_pending,
        thread_fetch_status=str(thread.status),
    )


# ---------------------------------------------------------------------------
# Frontmatter / body / raw construction.
# ---------------------------------------------------------------------------


def build_frontmatter(
    *,
    bookmark: Dict[str, Any],
    thread: ThreadFetchResult,
    captured: datetime,
    run_id: str,
) -> CardFrontmatter:
    source_id = str(bookmark.get("id", "")).strip()
    if not source_id:
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause="XDK bookmark dict has no `id` field — cannot construct card.",
            attempted="card_writer.build_frontmatter()",
            next_action="Inspect the XDK response shape; this is a contract bug.",
            retryable=False,
        )

    author = bookmark.get("_author") or {}
    handle_raw = str(author.get("username", "")).strip()
    handle = _safe_handle(handle_raw) if handle_raw else "unknown"
    permalink = f"https://x.com/{handle}/status/{source_id}" if handle != "unknown" else None

    created_at = _parse_tweet_datetime(bookmark.get("created_at"))
    media = _build_media_from_bookmark(bookmark)

    return CardFrontmatter(
        source_type="bookmark",
        captured=captured,
        source=permalink,
        source_id=source_id,
        source_status=("deleted" if bookmark.get("_deleted") else "live"),
        author=f"@{handle}",
        date=created_at,
        media=media,
        extraction_pending=True,  # E-4 invariant: always True at write
        thread_fetch_status=thread.status,
        xsync_run_id=run_id,
    )


def build_body(*, bookmark: Dict[str, Any], thread: ThreadFetchResult) -> str:
    """Render the card body markdown — Content / Thread / External Links."""
    parts: List[str] = []

    text = (bookmark.get("text") or "").rstrip()
    parts.append("## Content\n")
    parts.append(text + "\n" if text else "_(no text — likely media-only or deleted)_\n")

    if thread.status == "complete" and thread.replies:
        parts.append("\n## Thread\n")
        for reply in thread.replies:
            reply_text = (reply.get("text") or "").rstrip()
            reply_author = (reply.get("_author") or {}).get("username", "?")
            parts.append(f"**@{reply_author}:** {reply_text}\n\n")

    external = _extract_external_urls(bookmark)
    if external:
        parts.append("\n## External Links\n")
        for url in external:
            parts.append(f"- {url}\n")

    return "".join(parts)


def build_raw_bytes(*, bookmark: Dict[str, Any]) -> bytes:
    """Byte-exact tweet text. The verbatim guarantee (sidecar pattern) lives here."""
    text = bookmark.get("text") or ""
    if not isinstance(text, str):
        text = str(text)
    return text.encode("utf-8")


def build_filename(fm: CardFrontmatter) -> str:
    """Spec line 123: `YYYY-MM-DD-{author}-{tweet-id}.md`.

    author is `@handle` — strip the `@` for the filename so it stays
    filesystem-friendly. tweet-id is the source_id. Date is `captured`
    truncated to YYYY-MM-DD UTC.
    """
    if not fm.source_id or not fm.author:
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause="Cannot build filename — source_id or author missing on bookmark frontmatter.",
            attempted="card_writer.build_filename()",
            next_action="This is a bug in build_frontmatter — bookmark must have both fields.",
            retryable=False,
        )
    handle = fm.author.lstrip("@")
    date_str = fm.captured.astimezone(timezone.utc).strftime("%Y-%m-%d")
    return f"{date_str}-{handle}-{fm.source_id}.md"


# ---------------------------------------------------------------------------
# Helpers — input sanitization + parsing.
# ---------------------------------------------------------------------------


def _safe_handle(raw: str) -> str:
    """Reject anything not in [A-Za-z0-9_]{1,15} — X handles already comply.

    Defense-in-depth (E-5) against an XDK response with a malformed handle
    that could later get passed to `git add` / subprocess and trigger
    injection. We sanitize at construction; the `subprocess.run([..])`
    array-form invocation in sync.git is the second layer of defense.
    """
    if _HANDLE_SAFE_RE.match(raw):
        return raw
    cleaned = _HANDLE_SANITIZE_RE.sub("_", raw)[:15]
    if not cleaned:
        return "unknown"
    log.warning("Sanitized author handle %r → %r", raw, cleaned)
    return cleaned


def _parse_tweet_datetime(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str):
        try:
            # X API uses ISO 8601 with Z suffix
            return datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None
    return None


def _build_media_from_bookmark(bookmark: Dict[str, Any]) -> Optional[CardMedia]:
    """Extract media flags + external URLs from the XDK bookmark dict."""
    media_objs = bookmark.get("_media") or []
    has_video = any(
        isinstance(m, dict) and m.get("type") in ("video", "animated_gif")
        for m in media_objs
    )
    has_images = any(
        isinstance(m, dict) and m.get("type") == "photo" for m in media_objs
    )

    external_urls = _extract_external_urls(bookmark)
    has_external = bool(external_urls)

    if not (has_video or has_images or has_external):
        return None

    return CardMedia(
        has_video=has_video,
        has_images=has_images,
        has_external_link=has_external,
        external_urls=external_urls,
        video_transcript_status=("queued" if has_video else None),
    )


def _extract_external_urls(bookmark: Dict[str, Any]) -> List[str]:
    """Pull bare URLs from entities.urls (excluding the t.co self-shortener
    where the expanded URL is also a t.co or twitter.com link)."""
    out: List[str] = []
    entities = bookmark.get("entities") or {}
    if not isinstance(entities, dict):
        return out
    for url_obj in entities.get("urls", []) or []:
        if not isinstance(url_obj, dict):
            continue
        expanded = url_obj.get("expanded_url") or url_obj.get("url")
        if not expanded:
            continue
        # Skip self-references (a quote-tweet's link to the quoted tweet)
        if "x.com/" in expanded or "twitter.com/" in expanded:
            continue
        out.append(expanded)
    return out


__all__ = [
    "write_one",
    "build_frontmatter",
    "build_body",
    "build_raw_bytes",
    "build_filename",
    "CardWriteResult",
]
