"""Tests for retrieval.scoring: recency, pin bypass, adaptive fallback."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from xsensai.retrieval import scoring


def _now() -> datetime:
    return datetime.now(timezone.utc)


def test_recency_today_is_one() -> None:
    assert scoring.recency_weight(_now(), no_decay=False, pinned=False) == pytest.approx(1.0, abs=0.01)


def test_recency_half_life() -> None:
    """At HALF_LIFE_DAYS old, weight is approximately 0.5."""
    old = _now() - timedelta(days=scoring.RECENCY_HALF_LIFE_DAYS)
    w = scoring.recency_weight(old, no_decay=False, pinned=False)
    assert w == pytest.approx(0.5, abs=0.01)


def test_recency_old_card_low_weight() -> None:
    very_old = _now() - timedelta(days=365)
    w = scoring.recency_weight(very_old, no_decay=False, pinned=False)
    assert w < 0.1


def test_no_decay_returns_one() -> None:
    very_old = _now() - timedelta(days=365)
    assert scoring.recency_weight(very_old, no_decay=True, pinned=False) == 1.0


def test_pinned_bypasses_recency() -> None:
    very_old = _now() - timedelta(days=365)
    assert scoring.recency_weight(very_old, no_decay=False, pinned=True) == 1.0


def test_undated_card_returns_one() -> None:
    assert scoring.recency_weight(None, no_decay=False, pinned=False) == 1.0


def test_future_dated_card_clamped_to_one() -> None:
    """Property: future-dated card cannot have weight > 1.0 (eng review M4)."""
    future = _now() + timedelta(days=30)
    w = scoring.recency_weight(future, no_decay=False, pinned=False)
    assert w == pytest.approx(1.0)
    assert w <= 1.0


def test_recency_property_newer_outranks_older_at_equal_qmd_score() -> None:
    """Two equal-QMD-score unpinned cards: newer outranks older."""
    qmd = 0.8
    new_card_date = _now() - timedelta(days=10)
    old_card_date = _now() - timedelta(days=200)
    w_new = scoring.recency_weight(new_card_date, no_decay=False, pinned=False)
    w_old = scoring.recency_weight(old_card_date, no_decay=False, pinned=False)
    assert scoring.combine_score(qmd, w_new) > scoring.combine_score(qmd, w_old)


def test_combine_score_simple() -> None:
    assert scoring.combine_score(0.8, 0.5) == pytest.approx(0.4)


def test_should_fallback_empty_scores() -> None:
    assert scoring.should_fallback([]) is True


def test_should_fallback_high_top_score_no_fallback() -> None:
    assert scoring.should_fallback([0.9, 0.7, 0.5]) is False


def test_should_fallback_low_top_with_clear_winner_no_fallback() -> None:
    """Low top but clear margin → don't fall back (we have a winner)."""
    assert scoring.should_fallback([0.20, 0.05, 0.04]) is False


def test_should_fallback_low_top_no_winner_fires() -> None:
    """Low top + tight margin + low dispersion → fallback."""
    assert scoring.should_fallback([0.20, 0.19, 0.18]) is True


def test_should_fallback_single_low_score_fires() -> None:
    assert scoring.should_fallback([0.10]) is True


def test_should_fallback_single_high_score_no_fallback() -> None:
    assert scoring.should_fallback([0.5]) is False
