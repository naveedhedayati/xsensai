"""Scoring: recency curve, pin bypass, adaptive fallback.

Pure functions, no I/O. Tunable constants live at the top.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timezone
from typing import List, Optional

# Tunable: 90-day half-life (90d → weight 0.5; 180d → 0.25). Captured in
# Slice 1 risks as a guess; tune from real usage post-ship.
RECENCY_HALF_LIFE_DAYS: float = 90.0

# Adaptive fallback thresholds (per Eng review T1, supersedes constant 0.3).
# Fallback fires when ALL of these hold for the top candidates:
#   - top score is low in absolute terms
#   - margin between top-1 and top-2 is small (no clear winner)
#   - score dispersion across top-N is low (no signal)
FALLBACK_TOP_SCORE_FLOOR: float = 0.35
FALLBACK_MARGIN_THRESHOLD: float = 0.05
FALLBACK_STDEV_THRESHOLD: float = 0.05

# Pin dominance: a pinned card is kept iff its combined_score >= this fraction
# of the best unpinned combined_score. Plus a quota cap below.
PIN_DOMINANCE_FRACTION: float = 0.5


def recency_weight(card_date: Optional[datetime], no_decay: bool, pinned: bool) -> float:
    """Return a multiplicative weight in (0, 1.0]. 1.0 if no_decay or pinned.

    For undated cards, return 1.0 (no penalty).
    For future-dated cards, clamp age to 0 (avoid weight > 1.0).
    """
    if no_decay or pinned or card_date is None:
        return 1.0
    if card_date.tzinfo is None:
        card_date = card_date.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    age_days = max(0.0, (now - card_date).total_seconds() / 86400.0)
    weight = 0.5 ** (age_days / RECENCY_HALF_LIFE_DAYS)
    return min(1.0, weight)


def combine_score(qmd_score: float, recency: float) -> float:
    """Combine QMD's relevance score with recency weight."""
    return qmd_score * recency


def should_fallback(top_scores: List[float]) -> bool:
    """Adaptive fallback decision (no usage history needed).

    True when ALL of:
      - max score below absolute floor
      - margin between top-1 and top-2 is small (close call)
      - score dispersion is low (no signal)

    With <2 scores, falls back if max < floor.
    """
    if not top_scores:
        return True
    top = max(top_scores)
    if top >= FALLBACK_TOP_SCORE_FLOOR:
        return False
    if len(top_scores) < 2:
        return True
    sorted_scores = sorted(top_scores, reverse=True)
    margin = sorted_scores[0] - sorted_scores[1]
    if margin >= FALLBACK_MARGIN_THRESHOLD:
        return False
    try:
        stdev = statistics.pstdev(top_scores)
    except statistics.StatisticsError:
        stdev = 0.0
    return stdev < FALLBACK_STDEV_THRESHOLD


__all__ = [
    "RECENCY_HALF_LIFE_DAYS",
    "FALLBACK_TOP_SCORE_FLOOR",
    "FALLBACK_MARGIN_THRESHOLD",
    "FALLBACK_STDEV_THRESHOLD",
    "PIN_DOMINANCE_FRACTION",
    "recency_weight",
    "combine_score",
    "should_fallback",
]
