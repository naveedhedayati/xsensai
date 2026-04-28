"""Slice 5 — lazy-extract-on-read coordination.

Problem: Spike #10 measured a 26.7pp recall gap between body+tags+summary
and body-only retrieval. With cron's DeferredExtractor leaving cards
extraction_pending=True, /xfind quality decays. Lazy-extract closes the
gap by triggering host LLM extraction the first time `/xfind` surfaces a
pending card. Subsequent `/xfind`s on the same card see the cached
result (autoplan E3 + DX D7).

Concurrency surface (autoplan E3 / Eng review / Claude voice):
  - Two /xfind sessions surfacing the same pending card both try to fire
    extraction. Naive: 2× LLM cost.
  - Solution: claim flag (`lazy_extract_in_progress=True`) under the
    `card_write` lock. Second concurrent /xfind sees the flag and
    SKIPS — returns body-only with a `(another session is extracting
    this card)` UX note (DX D7).
  - 60s timeout: if `lazy_extract_claim_at` is older than 60s and flag is
    still set, treat as crashed and reclaim. Prevents deadlock.

Run-id: `lazy-extract-{uuid4}`. Service.apply_extraction's
is_extraction_owner_path check accepts this prefix.

This module only orchestrates the CLAIM (set flag), RELEASE (clear flag).
The host LLM call lives in commands/xfind.md (analogous to Slice 3
synthesis pattern); the result is written via service.apply_extraction.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

from xsensai.errors import XSensaiError
from xsensai.locks.filelock import with_card_write_lock
from xsensai.model.card import LoadedCard
from xsensai.storage.corpus import (
    load_card_by_id,
    resolve_corpus_path,
    write_card as _write,
)


log = logging.getLogger(__name__)


# Stale-claim threshold (autoplan E3): if a flag was set more than this
# many seconds ago and no clear came, assume the prior /xfind crashed
# and reclaim. 60s is well past a reasonable host LLM call.
CLAIM_STALE_SECONDS = 60

LazyClaimOutcome = Literal[
    "claimed",      # this caller owns extraction; proceed to LLM call
    "skip_active",  # another session is mid-extraction (recent claim)
    "skip_done",    # card already extracted (extraction_pending=False)
    "reclaimed",    # prior claim is stale; this caller is now owner
    "missing",      # card not found
]


@dataclass(frozen=True)
class LazyClaimResult:
    outcome: LazyClaimOutcome
    run_id: Optional[str]
    note: str


def claim_for_lazy_extract(
    card_id: str,
    *,
    corpus_path: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> LazyClaimResult:
    """Set `lazy_extract_in_progress=True` if card is pending and unclaimed.

    Returns a LazyClaimResult with outcome:
      - claimed: caller should run the host LLM call, then call
        `service.apply_extraction(card_id, summary, tags, run_id=lazy-extract-{uuid})`
        which clears the flag implicitly via card overwrite.
      - skip_active: another session has a fresh claim; render body-only.
      - skip_done: already extracted; render normally.
      - reclaimed: prior session's claim was stale (>60s); this caller wins.
      - missing: card not found by id; should not happen in /xfind flow.
    """
    corpus = resolve_corpus_path(corpus_path)
    now = now or datetime.now(timezone.utc)
    new_run_id = f"lazy-extract-{uuid.uuid4().hex[:12]}"

    try:
        with with_card_write_lock(corpus, "xfind") as h:
            try:
                card = load_card_by_id(card_id, corpus_path=corpus)
            except XSensaiError as e:
                return LazyClaimResult(
                    outcome="missing", run_id=None, note=e.cause,
                )

            if not card.fm.extraction_pending:
                return LazyClaimResult(
                    outcome="skip_done", run_id=None,
                    note="card already extracted",
                )

            if card.fm.lazy_extract_in_progress and card.fm.lazy_extract_claim_at:
                age = now - card.fm.lazy_extract_claim_at
                if age < timedelta(seconds=CLAIM_STALE_SECONDS):
                    return LazyClaimResult(
                        outcome="skip_active",
                        run_id=None,
                        note=f"another session claimed {int(age.total_seconds())}s ago",
                    )
                # Stale: reclaim.
                outcome: LazyClaimOutcome = "reclaimed"
                note = (
                    f"prior claim {int(age.total_seconds())}s old (>{CLAIM_STALE_SECONDS}s); reclaiming"
                )
            else:
                outcome = "claimed"
                note = "claim acquired"

            new_fm = card.fm.model_copy(update={
                "lazy_extract_in_progress": True,
                "lazy_extract_claim_at": now,
            })
            new_card = LoadedCard(
                fm=new_fm, body=card.body, raw_bytes=card.raw_bytes,
                md_path=card.md_path,
            )
            _write(new_card, lock_token=h.token, corpus_path=corpus)
        return LazyClaimResult(outcome=outcome, run_id=new_run_id, note=note)
    except XSensaiError as e:
        log.warning("lazy claim failed for %s: %s", card_id, e.cause)
        return LazyClaimResult(
            outcome="missing", run_id=None, note=e.cause,
        )


def release_lazy_claim(
    card_id: str,
    *,
    corpus_path: Optional[Path] = None,
) -> bool:
    """Clear lazy_extract_in_progress + claim_at on the card.

    Called when extraction FAILED (so the next /xfind can retry).
    On success, `service.apply_extraction` writes the new card without
    the flag fields, implicitly clearing them.

    Returns True if cleared, False if card couldn't be loaded or update
    failed. Logged but does not raise.
    """
    corpus = resolve_corpus_path(corpus_path)
    try:
        with with_card_write_lock(corpus, "xfind") as h:
            card = load_card_by_id(card_id, corpus_path=corpus)
            if not card.fm.lazy_extract_in_progress:
                return True  # already cleared
            new_fm = card.fm.model_copy(update={
                "lazy_extract_in_progress": False,
                "lazy_extract_claim_at": None,
            })
            new_card = LoadedCard(
                fm=new_fm, body=card.body, raw_bytes=card.raw_bytes,
                md_path=card.md_path,
            )
            _write(new_card, lock_token=h.token, corpus_path=corpus)
        return True
    except XSensaiError as e:
        log.warning("lazy claim release failed for %s: %s", card_id, e.cause)
        return False


__all__ = [
    "LazyClaimResult",
    "LazyClaimOutcome",
    "claim_for_lazy_extract",
    "release_lazy_claim",
    "CLAIM_STALE_SECONDS",
]
