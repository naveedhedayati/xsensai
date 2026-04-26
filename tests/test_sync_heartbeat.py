"""Slice 4 — _sync-status.md heartbeat read/write + banner threshold logic."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from xsensai.sync.heartbeat import (
    BANNER_FAILURE_THRESHOLD,
    BANNER_STALE_DAYS,
    SyncStatus,
    read_status,
    update_after_run,
    write_status,
)


def test_write_then_read_round_trips(tmp_path):
    status = SyncStatus(
        last_run="2026-04-26T12:00:00+00:00",
        last_success="2026-04-26T12:00:00+00:00",
        consecutive_failures=0,
        last_error=None,
        new_cards_this_run=3,
        extraction_pending_count=0,
        total_cards=28,
    )
    write_status(tmp_path, status)
    loaded = read_status(tmp_path)
    assert loaded is not None
    assert loaded.last_run == status.last_run
    assert loaded.consecutive_failures == 0
    assert loaded.new_cards_this_run == 3
    assert loaded.total_cards == 28


def test_read_status_returns_none_when_missing(tmp_path):
    assert read_status(tmp_path) is None


def test_banner_fires_on_consecutive_failures(tmp_path):
    s = SyncStatus(
        last_run="2026-04-26T12:00:00+00:00",
        consecutive_failures=BANNER_FAILURE_THRESHOLD,
    )
    assert s.should_show_stale_banner()


def test_banner_fires_on_stale_last_success(tmp_path):
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    stale_iso = (now - timedelta(days=BANNER_STALE_DAYS + 1)).isoformat()
    s = SyncStatus(
        last_run=now.isoformat(),
        last_success=stale_iso,
        consecutive_failures=0,
    )
    assert s.should_show_stale_banner(now=now)


def test_banner_quiet_on_fresh_success(tmp_path):
    now = datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc)
    s = SyncStatus(
        last_run=now.isoformat(),
        last_success=(now - timedelta(hours=2)).isoformat(),
        consecutive_failures=0,
    )
    assert not s.should_show_stale_banner(now=now)


def test_update_after_run_resets_failures_on_success(tmp_path):
    initial = SyncStatus(
        last_run="2026-04-25T00:00:00+00:00",
        consecutive_failures=3,
        last_error="prior failure",
    )
    write_status(tmp_path, initial)
    new = update_after_run(
        tmp_path,
        success=True,
        new_cards_this_run=5,
        extraction_pending_count=2,
        total_cards=30,
    )
    assert new.consecutive_failures == 0
    assert new.last_success is not None
    assert new.last_error is None


def test_update_after_run_increments_failures_on_error(tmp_path):
    initial = SyncStatus(
        last_run="2026-04-25T00:00:00+00:00",
        last_success="2026-04-25T00:00:00+00:00",
        consecutive_failures=1,
    )
    write_status(tmp_path, initial)
    new = update_after_run(
        tmp_path,
        success=False,
        new_cards_this_run=0,
        extraction_pending_count=0,
        total_cards=30,
        last_error="rate limited",
    )
    assert new.consecutive_failures == 2
    assert new.last_error == "rate limited"
    # last_success preserved across failure
    assert new.last_success == "2026-04-25T00:00:00+00:00"


def test_threads_unfetched_cumulative_accumulates(tmp_path):
    initial = SyncStatus(
        last_run="2026-04-25T00:00:00+00:00",
        threads_permanently_unfetched_cumulative=10,
    )
    write_status(tmp_path, initial)
    new = update_after_run(
        tmp_path,
        success=True,
        new_cards_this_run=3,
        extraction_pending_count=0,
        total_cards=30,
        threads_unfetched_this_run=5,
    )
    assert new.threads_permanently_unfetched_cumulative == 15
    assert new.threads_permanently_unfetched_this_run == 5
