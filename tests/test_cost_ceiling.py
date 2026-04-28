"""Slice 5 — BudgetTracker per-attempt cost ceiling tests."""

from __future__ import annotations

import os

import pytest

from xsensai.errors import XSensaiError
from xsensai.sync.cost_ceiling import (
    CAP_ENV_VAR,
    DEFAULT_CAP,
    BudgetTracker,
)


def test_from_env_no_var_uses_default():
    tracker = BudgetTracker.from_env()
    assert tracker.cap == DEFAULT_CAP


def test_from_env_uses_explicit_int(monkeypatch):
    monkeypatch.setenv(CAP_ENV_VAR, "50")
    tracker = BudgetTracker.from_env()
    assert tracker.cap == 50


def test_from_env_rejects_non_integer(monkeypatch):
    monkeypatch.setenv(CAP_ENV_VAR, "not-a-number")
    with pytest.raises(XSensaiError) as exc_info:
        BudgetTracker.from_env()
    assert exc_info.value.code == "INVALID_FLAGS"
    assert "not-a-number" in exc_info.value.cause


def test_from_env_rejects_zero(monkeypatch):
    monkeypatch.setenv(CAP_ENV_VAR, "0")
    with pytest.raises(XSensaiError) as exc_info:
        BudgetTracker.from_env()
    assert exc_info.value.code == "INVALID_FLAGS"


def test_from_env_rejects_negative(monkeypatch):
    monkeypatch.setenv(CAP_ENV_VAR, "-5")
    with pytest.raises(XSensaiError):
        BudgetTracker.from_env()


def test_record_api_call_bookmark_fetch():
    tracker = BudgetTracker(cap=10)
    tracker.record_api_call("bookmark_fetch")
    assert tracker.bookmark_fetch_count == 1
    assert tracker.thread_search_count == 0
    assert tracker.total == 1


def test_record_api_call_thread_search():
    tracker = BudgetTracker(cap=10)
    tracker.record_api_call("thread_search")
    assert tracker.thread_search_count == 1
    assert tracker.total == 1


def test_record_api_call_unknown_kind_raises():
    tracker = BudgetTracker(cap=10)
    with pytest.raises(ValueError):
        tracker.record_api_call("invalid_kind")  # type: ignore[arg-type]


def test_should_bail_below_cap():
    tracker = BudgetTracker(cap=10)
    for _ in range(9):
        tracker.record_api_call("bookmark_fetch")
    assert tracker.should_bail() is False


def test_should_bail_at_cap():
    tracker = BudgetTracker(cap=10)
    for _ in range(10):
        tracker.record_api_call("bookmark_fetch")
    assert tracker.should_bail() is True


def test_should_bail_above_cap():
    tracker = BudgetTracker(cap=10)
    for _ in range(11):
        tracker.record_api_call("thread_search")
    assert tracker.should_bail() is True


def test_should_bail_mixed_counters():
    tracker = BudgetTracker(cap=5)
    tracker.record_api_call("bookmark_fetch")
    tracker.record_api_call("bookmark_fetch")
    tracker.record_api_call("thread_search")
    tracker.record_api_call("thread_search")
    tracker.record_api_call("thread_search")
    assert tracker.total == 5
    assert tracker.should_bail() is True


def test_cost_limit_error_envelope_shape():
    tracker = BudgetTracker(cap=10)
    for _ in range(7):
        tracker.record_api_call("bookmark_fetch")
    for _ in range(3):
        tracker.record_api_call("thread_search")

    err = tracker.cost_limit_error(n_committed=4)
    assert err.code == "COST_LIMIT_REACHED"
    assert "10" in err.cause
    assert "7 bookmark fetches" in err.attempted
    assert "3 thread searches" in err.attempted
    assert err.retryable is True
    assert err.details and "4" in err.details
    assert "/xsync" in err.next_action
    assert CAP_ENV_VAR in err.next_action


def test_cost_limit_error_renders_via_format():
    tracker = BudgetTracker(cap=5)
    for _ in range(5):
        tracker.record_api_call("bookmark_fetch")
    err = tracker.cost_limit_error(n_committed=2)
    rendered = err.format()
    assert rendered.startswith("[COST_LIMIT_REACHED]")
    assert "Cards committed this run: 2" in rendered
