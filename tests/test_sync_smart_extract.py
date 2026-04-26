"""Slice 4 — smart-default branch matrix (UC-2=C).

Per /autoplan E-3: tests N=4, N=5, N=6, --inline override, --defer override,
--inline + --defer conflict.
"""

from __future__ import annotations

import pytest

from xsensai.sync.service import SMART_DEFAULT_INLINE_MAX, _decide_strategy


def test_smart_default_inline_max_is_5():
    """Sanity: spec line 63 mirrors with N>5 threshold."""
    assert SMART_DEFAULT_INLINE_MAX == 5


def test_n_4_inline_default():
    assert _decide_strategy(n=4, inline=False, defer=False) == "inline"


def test_n_5_inline_default_boundary():
    """N=5 is the boundary — still inline."""
    assert _decide_strategy(n=5, inline=False, defer=False) == "inline"


def test_n_6_deferred_default_boundary():
    """N=6 crosses the threshold — deferred."""
    assert _decide_strategy(n=6, inline=False, defer=False) == "deferred"


def test_n_30_deferred_default():
    """Backfill of 30 cards → deferred."""
    assert _decide_strategy(n=30, inline=False, defer=False) == "deferred"


def test_inline_override_wins_at_n_30():
    """--inline forces inline regardless of N (user opts into the wait)."""
    assert _decide_strategy(n=30, inline=True, defer=False) == "inline"


def test_defer_override_wins_at_n_2():
    """--defer forces deferred even for tiny N."""
    assert _decide_strategy(n=2, inline=False, defer=True) == "deferred"


# Note: the inline+defer conflict case is asserted at the run() level
# (where it returns INVALID_FLAGS); see tests/test_sync_service.py.
