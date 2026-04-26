"""x-sensai MCP server.

CRITICAL: stdio transport uses STDOUT for JSON-RPC protocol traffic. Any
print() or library that writes to stdout corrupts the stream and Claude
Desktop silently disconnects. ALL logging goes to stderr.

Tools:
  - ping (Slice 0) — smoke test
  - search_bookmarks (Slice 1) — corpus search with [B]/[P] references
  - get_bookmark (Slice 1) — fetch full card by id
  - paste_bookmark (Slice 2) — write a paste card (requires user_confirmed)
  - recover_aborted_paste (Slice 2) — restore content from inbox by snapshot id
  - annotate_card (Slice 2) — mutate why_saved/applicability/pinned (requires user_confirmed)
  - set_pin (Slice 2) — pin/unpin a card (requires user_confirmed)
  - list_pinned (Slice 2) — list all pinned cards (read-only)
  - due_cards_for_review (Slice 2) — list cards needing /xnote review (read-only)

Mutation tools require `user_confirmed: True` per UC7 — FastMCP cannot hide
tools from tools/list, so we runtime-guard mutations against accidental
direct invocation by Claude in non-/xpaste contexts. Slash commands set
this flag explicitly.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from urllib.parse import urlparse

from xsensai.errors import XSensaiError
from xsensai.locks import filelock
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.retrieval import engine, format as fmt
from xsensai.storage import corpus, inbox, slug

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] xsensai-mcp: %(message)s",
)
log = logging.getLogger(__name__)

mcp = FastMCP("xsensai")

MAX_CONTENT_BYTES = 10 * 1024 * 1024  # 10MB cap per CEO S2 finding
MAX_CONTENT_MB = MAX_CONTENT_BYTES // (1024 * 1024)  # human-readable for messages
# DUPLICATE_WINDOW_SEC + content_fingerprint() are intentionally unwired in
# Slice 2 v0 — see /review F10 / Plan PARTIAL item (4). Wiring is ~20 min CC
# in a follow-up; leaving the slug helper in place so the wire-up is trivial.
DUPLICATE_WINDOW_SEC = 24 * 3600


@mcp.tool()
def ping(echo: str) -> str:
    """Smoke test tool. Returns 'pong: {echo}' so we can verify the
    Claude Desktop -> MCP server -> Python round-trip works end-to-end.
    """
    log.info("ping called with echo=%r", echo)
    return f"pong: {echo}"


@mcp.tool()
async def search_bookmarks(
    query: str,
    limit: int = 5,
    no_decay: bool = False,
    include_pinned: bool = True,
) -> Dict[str, Any]:
    """Search Naveed's curated bookmark corpus.

    WHEN TO CALL: the user references their saved bookmarks, their curated
    reading, "what have I bookmarked about X", "from my corpus", "what does
    my saved content say about Y", or asks for a take grounded in their taste
    rather than the general web.

    WHEN NOT TO CALL: general factual questions, web-fresh research, anything
    outside the user's curation. For those, use general knowledge or the web
    search tool.

    ARGS:
      query (str): the search text. Plain language; QMD does BM25 + ranking.
      limit (int, default 5): max hits to return.
      no_decay (bool, default False): disable recency weighting (treat older
        cards equally to newer ones).
      include_pinned (bool, default True): include pinned cards in results.
        Pinned cards bypass recency decay but still must score on relevance.

    RETURNS:
      A dict with keys:
        - hits: list of {id, kind ('B'|'P'), author_or_domain, snippet,
          permalink_or_filename, why_saved, score}. id is the card filename
          without .md; pass to get_bookmark(id) for full content.
        - meta: {fallback_fired, total_candidates, corpus_card_count}
        - rendered_markdown: the [B]/[P]-formatted reference list, ready to
          show the user verbatim. If you only need to display, use this.

    ERROR SENTINELS: if something goes wrong, the response shape is:
      {"error": {"code": "...", "message": "..."}, "rendered_markdown": "..."}
      where code is one of CORPUS_UNAVAILABLE (retryable=False; corpus is
      empty/missing), NO_RESULTS (retryable=True; broaden query), or
      INTERNAL_ERROR (check stderr).

    LATENCY: ~1s typical, 10s timeout. COST: zero — no LLM API calls in this slice.
    """
    log.info(
        "search_bookmarks: query=%r limit=%d no_decay=%s include_pinned=%s",
        query, limit, no_decay, include_pinned,
    )
    try:
        results = await engine.search(
            query, limit=limit, no_decay=no_decay, include_pinned=include_pinned
        )
    except XSensaiError as e:
        return _error_response(e)

    if not results.hits:
        if results.corpus_card_count == 0:
            err = XSensaiError(
                code="CORPUS_UNAVAILABLE",
                cause="Corpus is empty.",
                attempted=f"search_bookmarks(query={query!r})",
                next_action=(
                    "Add v2 cards to $XSENSAI_CORPUS_PATH (or run scripts/bootstrap_qmd.sh "
                    "first to set up the QMD index, then add cards)."
                ),
                retryable=False,
            )
        else:
            err = XSensaiError(
                code="NO_RESULTS",
                cause=f"No matching cards. Corpus has {results.corpus_card_count} cards.",
                attempted=f"search_bookmarks(query={query!r})",
                next_action=(
                    "Try a broader query, or remove `no decay` / `skip pins` if you used them."
                ),
                retryable=True,
            )
        return _error_response(err)

    hits_payload: List[Dict[str, Any]] = []
    rendered_lines: List[str] = []
    if results.fallback_fired:
        rendered_lines.append(
            "_Nothing recent matched well, showing older cards by relevance._\n"
        )

    for hit in results.hits:
        card = hit.card
        kind = "B" if card.fm.source_type == "bookmark" else "P"
        if kind == "B":
            author_or_domain = card.fm.author or "@unknown"
        else:
            author_or_domain = _paste_domain(card.fm.source_url)
        permalink = card.fm.source if card.fm.source_type == "bookmark" else card.md_path.name

        hits_payload.append({
            "id": card.id,
            "kind": kind,
            "author_or_domain": author_or_domain,
            "snippet": fmt.truncate_graphemes(card.content_section),
            "permalink_or_filename": permalink or card.md_path.name,
            "why_saved": card.fm.why_saved,
            "score": round(hit.combined_score, 4),
        })
        rendered_lines.append(fmt.format_reference(card))

    return {
        "hits": hits_payload,
        "meta": {
            "fallback_fired": results.fallback_fired,
            "total_candidates": results.total_candidates,
            "corpus_card_count": results.corpus_card_count,
        },
        "rendered_markdown": "\n".join(rendered_lines),
    }


@mcp.tool()
def get_bookmark(id: str) -> Dict[str, Any]:
    """Fetch a full bookmark card by id (filename without .md).

    Use after search_bookmarks returns a hit and you need the full content
    for synthesis or display. The id you pass should be the `id` field from
    a search_bookmarks hit.

    RETURNS: {id, source_type, author_or_self, source, source_url, captured,
    date, tags, pinned, why_saved, applicability, body} — full card detail.
    On not-found returns {"error": {"code": "NO_RESULTS", "message": "..."}}.
    """
    log.info("get_bookmark: id=%r", id)
    try:
        card = corpus.load_card_by_id(id)
    except XSensaiError as e:
        # Per /review F20: get_bookmark is a single-card fetch — search-shaped
        # error envelope (with hits/meta/rendered_markdown) is misleading. Use
        # the slim write-side envelope for a non-search error path.
        return _write_error_response(e)

    return {
        "id": card.id,
        "source_type": card.fm.source_type,
        "author_or_self": card.fm.author or ("self" if card.fm.source_type == "paste" else None),
        "source": card.fm.source,
        "source_url": card.fm.source_url,
        "captured": card.fm.captured.isoformat() if card.fm.captured else None,
        "date": card.fm.date.isoformat() if card.fm.date else None,
        "tags": list(card.fm.tags),
        "pinned": card.fm.pinned,
        "why_saved": card.fm.why_saved,
        "applicability": list(card.fm.applicability),
        "body": card.body,
    }


def _paste_domain(source_url: Optional[str]) -> str:
    """Mirror format.py's domain extraction so MCP `hits[].author_or_domain`
    matches `rendered_markdown`. Defends against schemes like javascript:.
    """
    if not source_url:
        return "self"
    parsed = urlparse(source_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "self"
    return parsed.hostname


def _error_response(e: XSensaiError) -> Dict[str, Any]:
    """Error envelope for read tools (search_bookmarks, get_bookmark).

    `hits` + `meta` are always present so callers that assume the success-shape
    dict don't KeyError on the error path. `details` is always present (None
    when absent) so the error shape matches `_write_error_response` and a
    generic error handler can read `error.details` from any tool.
    """
    return {
        "hits": [],
        "meta": {
            "fallback_fired": False,
            "total_candidates": 0,
            "corpus_card_count": None,
        },
        "error": {
            "code": e.code,
            "message": e.cause,
            "next_action": e.next_action,
            "retryable": e.retryable,
            "details": e.details,
        },
        "rendered_markdown": e.format(),
    }


def _write_error_response(e: XSensaiError) -> Dict[str, Any]:
    """Error envelope for write-side tools. Slimmer than _error_response
    (no hits/meta) but same error shape.
    """
    return {
        "ok": False,
        "error": {
            "code": e.code,
            "message": e.cause,
            "next_action": e.next_action,
            "retryable": e.retryable,
            "details": e.details,
        },
        "rendered_message": e.format(),
    }


def _confirmation_required(tool_name: str) -> Dict[str, Any]:
    """USER_CONFIRMATION_REQUIRED envelope per UC7. Returned by mutation
    tools when called without `user_confirmed: true`. The slash commands
    set this flag explicitly; ad-hoc Claude calls hit this guard.
    """
    err = XSensaiError(
        code="USER_CONFIRMATION_REQUIRED",
        cause=f"{tool_name} requires explicit user confirmation.",
        attempted=f"{tool_name} call without user_confirmed",
        next_action=(
            f"This tool mutates the bookmark corpus. Use the corresponding slash "
            f"command (/xpaste, /xnote, or /xpin) which prompts the user explicitly. "
            f"To bypass for scripted use, pass user_confirmed=True."
        ),
        retryable=False,
    )
    return _write_error_response(err)


def _v1_blocked_response(card_id: str, attempted_op: str) -> Dict[str, Any]:
    """V1_MUTATION_BLOCKED envelope per UC1+UC8. Slice 6 ships migration."""
    try:
        corpus_path_for_msg = corpus.resolve_corpus_path()
        log_hint = f"{corpus_path_for_msg}/_v1-upgraded.jsonl"
    except XSensaiError:
        log_hint = "{corpus}/_v1-upgraded.jsonl"
    err = XSensaiError(
        code="V1_MUTATION_BLOCKED",
        cause=f"Card {card_id!r} is v1-shape; pin/annotate ships in Slice 6 after migration.",
        attempted=f"{attempted_op}({card_id!r})",
        next_action=(
            "v1 cards have no sidecar (no raw_path/raw_checksum). Mutating them now "
            "would synthesize raw_bytes from the rendered body, losing ## Thread / "
            "## Video Transcript content. Wait for Slice 6 migration which re-fetches "
            f"from XDK. Your attempt has been logged to {log_hint} so the migration "
            "knows to prioritize this card."
        ),
        retryable=False,
    )
    return _write_error_response(err)


@mcp.tool()
async def paste_bookmark(
    content: str,
    user_confirmed: bool,  # /review F21: required (no default) — contract-correct
    why_saved: Optional[str] = None,
    source_url: Optional[str] = None,
    tags: Optional[List[str]] = None,
    clear_snapshot_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Save pasted content as a v2 paste card in Naveed's corpus.

    USAGE: This is the writer behind /xpaste. Set user_confirmed=True only
    after the user has explicitly approved the write — the /xpaste slash
    command does this after a y/n confirmation prompt.

    SECURITY NOTE (per /review F26): user_confirmed is a soft accident-guard,
    NOT a security boundary. A prompt-injection payload inside retrieved
    bookmark content that Claude later reads can instruct Claude to call
    paste_bookmark with user_confirmed=True. The guard hardens against
    accidental Claude invocations in non-/xpaste contexts; treat it as
    surface-area-narrowing, not auth.

    ARGS:
      content (str): the paste content (raw text). Required, must be non-empty.
        Capped at 10MB.
      why_saved (str, optional): user's intent note. Empty/None flips
        why_saved_pending=True so the card auto-queues for /xnote review.
      source_url (str, optional): URL the content came from.
      tags (list[str], optional): user-applied tags.
      user_confirmed (bool, default False): MUST be True. Returns
        USER_CONFIRMATION_REQUIRED otherwise.

    RETURNS: {ok: True, id: str, path: str} on success, or
      {ok: False, error: {code, message, next_action, retryable}} on failure.
      Error codes: USER_CONFIRMATION_REQUIRED, PASTE_EMPTY, LOCK_HELD,
      DISK_WRITE_FAILED.
    """
    log.info("paste_bookmark: confirmed=%s, content_bytes=%d, tags=%s",
             user_confirmed, len(content.encode("utf-8")) if content else 0, tags)

    if not user_confirmed:
        return _confirmation_required("paste_bookmark")

    if not content or not content.strip():
        return _write_error_response(XSensaiError(
            code="PASTE_EMPTY",
            cause="Paste content is empty; nothing to save.",
            attempted="paste_bookmark(content='')",
            next_action="Re-run /xpaste with non-empty content.",
            retryable=False,
        ))

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > MAX_CONTENT_BYTES:
        return _write_error_response(XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Content exceeds {MAX_CONTENT_MB}MB cap (got {len(content_bytes)} bytes).",
            attempted=f"paste_bookmark(content_bytes={len(content_bytes)})",
            next_action=f"Trim the content to under {MAX_CONTENT_MB}MB and re-run /xpaste.",
            retryable=False,
        ))

    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)

    # F10 24h dedup check (per autoplan CEO D1 + /review F10 wire-up).
    # Hash the content; if a paste card with the same fingerprint was written
    # within DUPLICATE_WINDOW_SEC, surface 'duplicate of {id}' instead of
    # writing a 2nd card. Defends against accidental double-submit.
    fingerprint = slug.content_fingerprint(content)
    dup_id = corpus.find_recent_paste_by_fingerprint(
        corpus_path, fingerprint, DUPLICATE_WINDOW_SEC,
    )
    if dup_id is not None:
        return {
            "ok": True,
            "id": dup_id,
            "duplicate_of": dup_id,
            "rendered_message": (
                f"Duplicate of recent paste {dup_id!r} (same content within "
                f"{DUPLICATE_WINDOW_SEC // 3600}h window — no new card written)."
            ),
        }

    # Build the card frontmatter (input validation only, no filesystem state).
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    body = "## Content\n\n" + content + ("\n" if not content.endswith("\n") else "")

    try:
        cf = CardFrontmatter(
            source_type="paste",
            captured=datetime.now(timezone.utc),
            author="self",
            why_saved=why_saved if (why_saved and why_saved.strip()) else None,
            why_saved_pending=not (why_saved and why_saved.strip()),
            source_url=source_url or None,
            tags=tags or [],
            content_fingerprint=fingerprint,
        )
    except Exception as e:
        return _write_error_response(XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Card frontmatter validation failed: {e}",
            attempted="paste_bookmark()",
            next_action="Check tags / source_url for invalid values.",
            retryable=False,
            details=str(e),
        ))

    # CRITICAL: slug + disambiguate INSIDE the lock context (race fix).
    # Per /review F3: wrap the entire lock + write in asyncio.to_thread so
    # the event loop isn't blocked during F_FULLFSYNC (10-100ms+ on macOS).
    # The lock + write_card sequence is sync I/O; running it in a thread
    # lets concurrent search_bookmarks queries continue serving on the
    # event loop while a paste is in flight.
    def _do_write() -> Optional[LoadedCard]:
        with filelock.with_card_write_lock(corpus_path, "xpaste") as h:
            base_slug = slug.slugify(content)
            base_filename = f"paste-{today}-{base_slug}"
            unique_filename = slug.disambiguate_slug(corpus_path, base_filename)
            md_path = corpus_path / f"{unique_filename}.md"
            new_card = LoadedCard(
                fm=cf,
                body=body,
                raw_bytes=content_bytes,
                md_path=md_path,
            )
            return corpus.write_card(new_card, h.token, corpus_path=corpus_path)

    try:
        written = await asyncio.to_thread(_do_write)
    except XSensaiError as e:
        return _write_error_response(e)

    log.info("paste_bookmark: wrote %s (why_saved_pending=%s)",
             written.id, written.fm.why_saved_pending)

    # UC9 wire-up: if the slash command pre-registered a tentative snapshot
    # for this paste flow, clear it now that the card committed successfully.
    cleared_snapshot = False
    if clear_snapshot_id is not None:
        cleared_snapshot = inbox.clear_tentative_snapshot(clear_snapshot_id, corpus_path)
        if cleared_snapshot:
            log.info("paste_bookmark: cleared tentative snapshot %s", clear_snapshot_id)

    return {
        "ok": True,
        "id": written.id,
        "path": str(written.md_path),
        "why_saved_pending": written.fm.why_saved_pending,
        "snapshot_cleared": cleared_snapshot if clear_snapshot_id else None,
        "rendered_message": (
            f"Saved card {written.id!r}. "
            + ("(why_saved_pending — auto-queued for /xnote review)"
               if written.fm.why_saved_pending else "")
        ),
    }


@mcp.tool()
def recover_aborted_paste(
    snapshot_id: Optional[str] = None,
) -> Dict[str, Any]:
    """DEPRECATED — kept for backwards-compat. Use list_recoverable_pastes()
    or get_aborted_paste(snapshot_id) directly.

    Per /review F22 split: this tool's polymorphic shape (different keys
    based on argument) was a friction point. New shape returns BOTH keys
    so generic clients don't have to branch.
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)

    entries = inbox.list_recoverable(corpus_path)
    matching = None
    if snapshot_id is not None:
        matches = [
            e for e in entries
            if e.get("snapshot_id") == snapshot_id or e.get("timestamp") == snapshot_id
        ]
        if not matches:
            return _write_error_response(XSensaiError(
                code="NO_RESULTS",
                cause=f"No recoverable inbox entry matches {snapshot_id!r}",
                attempted=f"recover_aborted_paste({snapshot_id!r})",
                next_action="Run list_recoverable_pastes() to see available entries.",
                retryable=False,
            ))
        matching = matches[0]
    return {
        "ok": True,
        "entries": entries,
        "entry": matching,
        "count": len(entries),
    }


@mcp.tool()
def list_recoverable_pastes() -> Dict[str, Any]:
    """List recoverable inbox entries (aborted /xpaste flows + tentative snapshots).

    Per /review F22 split. Read-only.
    Returns: {ok: True, entries: [...], count: N} sorted newest-first.
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)
    entries = inbox.list_recoverable(corpus_path)
    return {"ok": True, "entries": entries, "count": len(entries)}


@mcp.tool()
def get_aborted_paste(snapshot_id: str) -> Dict[str, Any]:
    """Fetch a single recoverable inbox entry by snapshot_id (or timestamp).

    Per /review F22 split. Read-only — clearing happens via paste_bookmark
    with `clear_snapshot_id` arg (UC9 wire-up).
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)
    entries = inbox.list_recoverable(corpus_path)
    matches = [
        e for e in entries
        if e.get("snapshot_id") == snapshot_id or e.get("timestamp") == snapshot_id
    ]
    if not matches:
        return _write_error_response(XSensaiError(
            code="NO_RESULTS",
            cause=f"No recoverable inbox entry matches {snapshot_id!r}",
            attempted=f"get_aborted_paste({snapshot_id!r})",
            next_action="Run list_recoverable_pastes() to see available entries.",
            retryable=False,
        ))
    return {"ok": True, "entry": matches[0]}


@mcp.tool()
def write_paste_snapshot(
    content: str,
    snapshot_id: str,
    why_saved_attempt: Optional[str] = None,
    source_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Write a tentative paste snapshot to the inbox (UC11 wire-up).

    USAGE: behind /xpaste step 1, immediately after content is received but
    before the user confirms. snapshot_id MUST be a UUID4 string the slash
    command generates. If /xpaste reaches step 7 successfully, paste_bookmark
    will clear the snapshot via its `clear_snapshot_id` arg. If /xpaste
    crashes (Ctrl-C, network drop), the snapshot survives and
    list_recoverable_pastes() surfaces it.

    Returns {ok: True, path: str} or {ok: False, error: {...}}.
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)
    try:
        path = inbox.write_tentative_snapshot(
            content, corpus_path, snapshot_id,
            why_saved_attempt=why_saved_attempt,
            source_url=source_url,
        )
    except XSensaiError as e:
        return _write_error_response(e)
    return {"ok": True, "path": str(path), "snapshot_id": snapshot_id}


@mcp.tool()
def clear_paste_snapshot(snapshot_id: str) -> Dict[str, Any]:
    """Clear a tentative paste snapshot from the inbox (UC9 wire-up).

    USAGE: called by paste_bookmark itself after a successful write when
    invoked with `clear_snapshot_id` arg (the slash command sets it). Also
    callable standalone if the user ran recover_aborted_paste manually and
    wants to remove the entry without re-pasting.

    Idempotent: returns ok=True regardless of whether the snapshot was found.
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)
    cleared = inbox.clear_tentative_snapshot(snapshot_id, corpus_path)
    return {
        "ok": True,
        "cleared": cleared,
        "snapshot_id": snapshot_id,
    }


@mcp.tool()
def get_review_cursor() -> Dict[str, Any]:
    """Read the /xnote review walk cursor (UC10 wire-up).

    Returns the last_card_id the user finished annotating in their last
    review walk. Empty cursor = no walk in progress; next walk starts from
    the oldest pending card.
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)
    last = corpus.read_review_cursor(corpus_path)
    return {"ok": True, "last_card_id": last}


@mcp.tool()
def set_review_cursor(last_card_id: Optional[str] = None) -> Dict[str, Any]:
    """Update the /xnote review walk cursor (UC10 wire-up).

    USAGE: behind /xnote review per-card actions. Set last_card_id=None
    when the walk fully completes (clears the cursor); set to the just-
    annotated card's id when the user `stop`s mid-walk.
    """
    try:
        corpus_path = corpus.resolve_corpus_path()
    except XSensaiError as e:
        return _write_error_response(e)
    corpus.write_review_cursor(corpus_path, last_card_id)
    return {"ok": True, "last_card_id": last_card_id}


@mcp.tool()
def annotate_card(
    id: str,
    user_confirmed: bool,  # /review F21: required (no default)
    why_saved: Optional[str] = None,
    applicability: Optional[List[str]] = None,
    pinned: Optional[bool] = None,
    next_review_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Mutate a card's frontmatter (why_saved / applicability / pinned / next_review_at).

    USAGE: behind /xnote single-mode + /xnote review per-card actions.
    user_confirmed must be True. None values mean leave-unchanged.

    SECURITY NOTE (per /review F26): user_confirmed is a soft accident-guard,
    NOT a security boundary. See paste_bookmark docstring.

    V1 cards (no sidecar) are REFUSED with V1_MUTATION_BLOCKED — Slice 6
    migration handles v1→v2 conversion. The attempt is logged to
    {corpus}/_v1-upgraded.jsonl so Slice 6 prioritizes those cards.
    """
    log.info("annotate_card: id=%r confirmed=%s why_saved=%r pinned=%s",
             id, user_confirmed, why_saved, pinned)

    if not user_confirmed:
        return _confirmation_required("annotate_card")

    try:
        card = corpus.load_card_by_id(id)
    except XSensaiError as e:
        return _write_error_response(e)

    # V1 refuse + log (log is best-effort and catches OSError internally;
    # no XSensaiError to handle from this call path).
    if _is_v1_card(card):
        corpus.log_v1_mutation_blocked(corpus.resolve_corpus_path(), id, "annotate")
        return _v1_blocked_response(id, "annotate_card")

    # Build mutated frontmatter
    fm_dict = card.fm.model_dump(mode="python")
    if why_saved is not None:
        # Match paste_bookmark semantic: whitespace-only counts as empty
        # (clearing why_saved by passing "   " should re-flag pending).
        meaningful = bool(why_saved and why_saved.strip())
        fm_dict["why_saved"] = why_saved if meaningful else None
        fm_dict["why_saved_pending"] = not meaningful
    if applicability is not None:
        fm_dict["applicability"] = list(applicability)
    if pinned is not None:
        fm_dict["pinned"] = bool(pinned)
    if next_review_at is not None:
        try:
            dt = datetime.fromisoformat(next_review_at.replace("Z", "+00:00"))
            fm_dict["next_review_at"] = dt
        except ValueError as e:
            return _write_error_response(XSensaiError(
                code="DISK_WRITE_FAILED",
                cause=f"Invalid next_review_at: {next_review_at!r}",
                attempted=f"annotate_card({id!r})",
                next_action="next_review_at must be ISO-8601 with timezone (e.g., 2026-05-02T00:00:00Z)",
                retryable=False,
                details=str(e),
            ))

    try:
        new_fm = CardFrontmatter.model_validate(fm_dict)
    except Exception as e:
        return _write_error_response(XSensaiError(
            code="DISK_WRITE_FAILED",
            cause="Mutated frontmatter failed validation.",
            attempted=f"annotate_card({id!r})",
            next_action="The mutation produced invalid frontmatter; check field values.",
            retryable=False,
            details=str(e),
        ))

    new_card = LoadedCard(
        fm=new_fm,
        body=card.body,
        raw_bytes=card.raw_bytes,
        md_path=card.md_path,
    )

    try:
        with filelock.with_card_write_lock(corpus.resolve_corpus_path(), "xnote") as h:
            written = corpus.write_card(new_card, h.token)
    except XSensaiError as e:
        return _write_error_response(e)

    return {
        "ok": True,
        "id": written.id,
        "why_saved": written.fm.why_saved,
        "applicability": list(written.fm.applicability),
        "pinned": written.fm.pinned,
        "rendered_message": f"Annotated {written.id!r}.",
    }


@mcp.tool()
def set_pin(
    id: str,
    pinned: bool,
    user_confirmed: bool,  # /review F21: required (no default)
) -> Dict[str, Any]:
    """Pin or unpin a card. Idempotent.

    USAGE: behind /xpin pin/unpin modes. user_confirmed must be True.
    V1 cards refused per UC1+UC8.

    SECURITY NOTE (per /review F26): user_confirmed is a soft accident-guard,
    NOT a security boundary. See paste_bookmark docstring.
    """
    log.info("set_pin: id=%r pinned=%s confirmed=%s", id, pinned, user_confirmed)

    if not user_confirmed:
        return _confirmation_required("set_pin")

    try:
        card = corpus.load_card_by_id(id)
    except XSensaiError as e:
        return _write_error_response(e)

    if _is_v1_card(card):
        # log is best-effort, catches OSError internally; no XSensaiError to handle.
        corpus.log_v1_mutation_blocked(corpus.resolve_corpus_path(), id, "pin")
        return _v1_blocked_response(id, "set_pin")

    if card.fm.pinned == pinned:
        return {
            "ok": True,
            "id": card.id,
            "pinned": pinned,
            "rendered_message": f"Card {card.id!r} already {'pinned' if pinned else 'unpinned'} (no-op).",
        }

    fm_dict = card.fm.model_dump(mode="python")
    fm_dict["pinned"] = pinned
    new_fm = CardFrontmatter.model_validate(fm_dict)
    new_card = LoadedCard(
        fm=new_fm,
        body=card.body,
        raw_bytes=card.raw_bytes,
        md_path=card.md_path,
    )
    try:
        with filelock.with_card_write_lock(corpus.resolve_corpus_path(), "xpin") as h:
            written = corpus.write_card(new_card, h.token)
    except XSensaiError as e:
        return _write_error_response(e)

    return {
        "ok": True,
        "id": written.id,
        "pinned": written.fm.pinned,
        "rendered_message": f"{'Pinned' if pinned else 'Unpinned'} {written.id!r}.",
    }


LIST_PINNED_DEFAULT_LIMIT = 50
LIST_PINNED_MAX_LIMIT = 500


@mcp.tool()
def list_pinned(limit: int = LIST_PINNED_DEFAULT_LIMIT) -> Dict[str, Any]:
    """List pinned cards in the corpus, sorted by captured DESC.

    Read-only — no user_confirmed needed.

    Per /review F23: pagination cap added so the response shape is stable
    even as the corpus grows. limit is capped at LIST_PINNED_MAX_LIMIT.

    RETURNS: {ok: True, count: N, total: M, has_more: bool, pinned: [...]}
      where each entry has {id, source_type, author_or_domain, captured,
      why_saved, source_or_filename}.

    Per /review F4: uses iter_cards_metadata (skips sidecar sha256 verify).
    """
    capped_limit = min(max(1, limit), LIST_PINNED_MAX_LIMIT)
    try:
        cards = list(corpus.iter_cards_metadata())
    except XSensaiError as e:
        return _write_error_response(e)

    pinned = [c for c in cards if c.fm.pinned]
    pinned.sort(key=lambda c: c.fm.captured, reverse=True)
    total = len(pinned)
    pinned = pinned[:capped_limit]

    rows = []
    for c in pinned:
        if c.fm.source_type == "bookmark":
            author_or_domain = c.fm.author or "@unknown"
            ref = c.fm.source or c.md_path.name
        else:
            author_or_domain = _paste_domain(c.fm.source_url)
            ref = c.md_path.name
        rows.append({
            "id": c.id,
            "source_type": c.fm.source_type,
            "author_or_domain": author_or_domain,
            "captured": c.fm.captured.isoformat(),
            "why_saved": c.fm.why_saved,
            "source_or_filename": ref,
        })

    return {
        "ok": True,
        "count": len(rows),
        "total": total,
        "has_more": total > len(rows),
        "pinned": rows,
    }


DUE_CARDS_DEFAULT_LIMIT = 10
DUE_CARDS_MAX_LIMIT = 100


@mcp.tool()
def due_cards_for_review(limit: int = DUE_CARDS_DEFAULT_LIMIT) -> Dict[str, Any]:
    """Return cards due for /xnote review, sorted by captured ASC (oldest first).

    A card is "due" if why_saved_pending=True OR next_review_at<=now.
    Read-only — no user_confirmed needed.

    RETURNS: {ok: True, count: N, total: M, has_more: bool, cursor: str?, due: [...]}
      where each entry has {id, source_type, author_or_domain, captured,
      prior_why_saved, snippet, reason ('pending' | 'review_at_due')}.

    Per /review F23: pagination cap added (limit clamped to DUE_CARDS_MAX_LIMIT).
    Per UC10 wire-up: also returns the current `_review-cursor.json` value so
    the slash command can show "resuming from card N+1 of M" UX.

    Per /review F4: uses iter_cards_metadata (skips sidecar sha256 verify).
    """
    capped_limit = min(max(1, limit), DUE_CARDS_MAX_LIMIT)
    try:
        corpus_path = corpus.resolve_corpus_path()
        cards = list(corpus.iter_cards_metadata(corpus_path))
        cursor_id = corpus.read_review_cursor(corpus_path)
    except XSensaiError as e:
        return _write_error_response(e)

    now = datetime.now(timezone.utc)
    due = []
    for c in cards:
        reason = None
        if c.fm.why_saved_pending:
            reason = "pending"
        elif c.fm.next_review_at and c.fm.next_review_at <= now:
            reason = "review_at_due"
        if reason:
            due.append((c, reason))

    due.sort(key=lambda t: t[0].fm.captured)  # oldest first
    total_due = len(due)

    # UC10 wire-up: skip past the cursor if the user has one (resume from
    # where the last walk stopped). cursor_id matches the last_card_id the
    # user finished annotating; we resume by skipping cards captured AT or
    # BEFORE that card.
    if cursor_id is not None:
        cursor_idx = next(
            (i for i, (c, _) in enumerate(due) if c.id == cursor_id),
            None,
        )
        if cursor_idx is not None:
            due = due[cursor_idx + 1:]

    due = due[:capped_limit]

    rows = []
    for c, reason in due:
        if c.fm.source_type == "bookmark":
            author_or_domain = c.fm.author or "@unknown"
        else:
            author_or_domain = _paste_domain(c.fm.source_url)
        rows.append({
            "id": c.id,
            "source_type": c.fm.source_type,
            "author_or_domain": author_or_domain,
            "captured": c.fm.captured.isoformat(),
            "prior_why_saved": c.fm.why_saved,
            "snippet": fmt.truncate_graphemes(c.content_section),
            "reason": reason,
        })

    return {
        "ok": True,
        "count": len(rows),
        "total": total_due,
        "has_more": total_due > len(rows),
        "cursor": cursor_id,
        "due": rows,
    }


def _is_v1_card(card: LoadedCard) -> bool:
    """A v1 card has neither raw_path nor raw_checksum (synthesized via the
    v1 adapter at load time). Slice 2 refuses to mutate these — UC1+UC8.
    """
    return card.fm.raw_path is None and card.fm.raw_checksum is None


def main() -> None:
    """Run the MCP server over stdio. Blocks until Claude Desktop disconnects."""
    log.info("xsensai-mcp starting (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
