"""x-sensai MCP server.

CRITICAL: stdio transport uses STDOUT for JSON-RPC protocol traffic. Any
print() or library that writes to stdout corrupts the stream and Claude
Desktop silently disconnects. ALL logging goes to stderr.

Tools:
  - ping (Slice 0) — smoke test
  - search_bookmarks (Slice 1) — corpus search with [B]/[P] references
  - get_bookmark (Slice 1) — fetch full card by id
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict, List, Optional  # noqa: F401

from mcp.server.fastmcp import FastMCP

from urllib.parse import urlparse

from xsensai.errors import XSensaiError
from xsensai.retrieval import engine, format as fmt
from xsensai.storage import corpus

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] xsensai-mcp: %(message)s",
)
log = logging.getLogger(__name__)

mcp = FastMCP("xsensai")


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
        return _error_response(e)

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
    """Error envelope. `hits` and `meta` keys are always present so callers
    that assume the success-shape dict don't KeyError on the error path.
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
        },
        "rendered_markdown": e.format(),
    }


def main() -> None:
    """Run the MCP server over stdio. Blocks until Claude Desktop disconnects."""
    log.info("xsensai-mcp starting (stdio transport)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
