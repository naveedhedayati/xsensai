"""Retrieval engine — orchestrates QMD candidates + scoring + filtering.

async def search(...) is the public entry point used by the search_bookmarks
MCP tool. Returns SearchResults with hits + meta. Pure orchestration; no
formatting (that's format.py).
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from xsensai.errors import XSensaiError
from xsensai.model.card import LoadedCard
from xsensai.retrieval import qmd, scoring
from xsensai.storage import corpus


log = logging.getLogger(__name__)


CANDIDATE_LIMIT = 20  # how many QMD candidates to consider before scoring


@dataclass(frozen=True)
class SearchHit:
    card: LoadedCard
    qmd_score: float
    recency: float
    combined_score: float


@dataclass
class SearchResults:
    hits: List[SearchHit] = field(default_factory=list)
    fallback_fired: bool = False
    total_candidates: int = 0
    corpus_card_count: Optional[int] = None


async def search(
    query_text: str,
    limit: int = 5,
    no_decay: bool = False,
    include_pinned: bool = True,
    corpus_path: Optional[Path] = None,
) -> SearchResults:
    """Run a search and return ranked SearchHits.

    1. Resolve corpus root (raises CORPUS_UNAVAILABLE if missing).
    2. Read-side reindex trigger (Slice 2 UC2 fix): if _index-dirty marker
       exists, run qmd.update() to flush pending writes into the index, then
       unlink the marker. Cost: ~5s on first /xfind after /xpaste.
    3. Get up to CANDIDATE_LIMIT QMD candidates.
    4. Load each candidate as LoadedCard (skipping malformed with WARN).
    5. Filter pinned if requested; compute recency + combined score.
    6. Apply pin dominance bound (vs unpinned baseline + quota cap).
    7. Sort, take top `limit`, decide fallback.
    """
    corpus_root = corpus.resolve_corpus_path(corpus_path)
    await _maybe_reindex_if_dirty(corpus_root)
    qmd_hits = await qmd.query(query_text, limit=CANDIDATE_LIMIT)

    raw_hits: List[SearchHit] = []
    for qh in qmd_hits:
        path = qh.resolve_path(corpus_root)
        try:
            # Sync file I/O off the event loop so concurrent search_bookmarks
            # calls don't block each other on YAML parsing + sidecar reads.
            card = await asyncio.to_thread(corpus.load_card, path, corpus_root)
        except XSensaiError as e:
            log.warning("skipping QMD hit %s: [%s] %s", path.name, e.code, e.cause)
            continue
        if not include_pinned and card.fm.pinned:
            continue
        recency = scoring.recency_weight(
            card.fm.date or card.fm.captured,
            no_decay=no_decay,
            pinned=card.fm.pinned,
        )
        combined = scoring.combine_score(qh.score, recency)
        raw_hits.append(
            SearchHit(
                card=card,
                qmd_score=qh.score,
                recency=recency,
                combined_score=combined,
            )
        )

    pin_filtered = _apply_pin_dominance(raw_hits, limit=limit)
    pin_filtered.sort(key=lambda h: h.combined_score, reverse=True)
    top = pin_filtered[:limit]
    fallback = scoring.should_fallback([h.combined_score for h in top])

    corpus_count = _safe_corpus_count(corpus_root)

    return SearchResults(
        hits=top,
        fallback_fired=fallback,
        total_candidates=len(qmd_hits),
        corpus_card_count=corpus_count,
    )


def _apply_pin_dominance(hits: List[SearchHit], limit: int) -> List[SearchHit]:
    """Drop pinned hits that aren't competitive vs unpinned baseline.

    Rule: pinned hit kept iff combined_score >= PIN_DOMINANCE_FRACTION * max(unpinned).
    Plus quota cap: at most ceil(limit/2) pinned in the result.
    If no unpinned hits, all pinned are kept (no baseline to compare against).
    """
    pinned = [h for h in hits if h.card.fm.pinned]
    unpinned = [h for h in hits if not h.card.fm.pinned]
    if not pinned:
        return hits
    if not unpinned:
        return hits

    max_unpinned = max(h.combined_score for h in unpinned)
    threshold = scoring.PIN_DOMINANCE_FRACTION * max_unpinned
    kept_pinned = [h for h in pinned if h.combined_score >= threshold]

    pin_quota = max(1, math.ceil(limit / 2))
    kept_pinned.sort(key=lambda h: h.combined_score, reverse=True)
    kept_pinned = kept_pinned[:pin_quota]

    return unpinned + kept_pinned


# /review F7 fix: in-process coalescing lock for reindex. Two concurrent
# search() calls that both observe a fresh _index-dirty marker would each
# spawn a `qmd update` subprocess, doubling the cost on the user-facing
# /xfind path. With this asyncio.Lock, only one reindex runs at a time;
# concurrent searches await its completion and skip the duplicate work.
_REINDEX_LOCK = asyncio.Lock()


async def _maybe_reindex_if_dirty(corpus_root: Path) -> None:
    """If _index-dirty marker exists, run qmd update to flush pending writes.

    Coalescing (F7): concurrent searches share a single reindex via
    _REINDEX_LOCK. Marker is moved aside BEFORE running qmd.update so a
    re-dirty during the update isn't lost (a write that flips _index-dirty
    while qmd.update is in flight gets picked up on the NEXT search rather
    than being silently overwritten).

    Idempotent: marker check is repeated after acquiring the lock so the
    second-arriving search no-ops if the first already cleared it.
    """
    marker = corpus_root / "_index-dirty"
    if not marker.exists():
        return
    async with _REINDEX_LOCK:
        # Re-check inside the lock: the prior holder may have cleared it.
        if not marker.exists():
            return
        log.info("[REINDEX_TRIGGER] _index-dirty present; running qmd update")
        # Atomic move-aside: if a write re-dirties while update is in flight,
        # the new marker stays for the NEXT search rather than being unlinked
        # and lost.
        in_flight = marker.with_suffix(".in-flight")
        try:
            marker.rename(in_flight)
        except OSError as e:
            # If rename failed (someone else already cleared it), skip.
            log.warning("could not rename _index-dirty: %s", e)
            return
        try:
            await qmd.update()
        finally:
            try:
                in_flight.unlink()
            except OSError:
                pass


def _safe_corpus_count(corpus_root: Path) -> Optional[int]:
    """Count loadable cards (matches what iter_cards yields).

    Returns None on filesystem errors. Counts only cards that successfully
    load — this matches the user's mental model ("how many bookmarks do I
    actually have searchable") and avoids the bug where claude.md/README.md
    inflated the count past iter_cards' real yield.
    """
    try:
        count = 0
        for _ in corpus.iter_cards(corpus_path=corpus_root):
            count += 1
        return count
    except OSError:
        return None
    except XSensaiError:
        return None


__all__ = [
    "CANDIDATE_LIMIT",
    "SearchHit",
    "SearchResults",
    "search",
]
