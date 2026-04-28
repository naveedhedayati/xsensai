"""Slice 5 — heartbeat cron-only field tests.

Validates:
  - cron-only mirror counters (last_cron_run/success/failures/runner) are
    written when cron_runner is passed and NEVER touched by manual /xsync
  - should_show_cron_stale_banner() fires independently of
    should_show_stale_banner() (autoplan E5)
  - should_show_extraction_backlog_banner() fires past 50 count or 30 days
  - cron_never_fired() returns True for fresh status, False after one cron
  - backwards-compat: pre-Slice-5 status files (no cron fields) read with
    None / 0 defaults
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from xsensai.sync.heartbeat import (
    BANNER_CRON_FAILURE_THRESHOLD,
    BANNER_CRON_STALE_DAYS,
    EXTRACTION_BACKLOG_COUNT_THRESHOLD,
    EXTRACTION_BACKLOG_AGE_DAYS,
    STATUS_FILE_NAME,
    SyncStatus,
    read_status,
    update_after_run,
    write_status,
)


def _now() -> datetime:
    return datetime(2026, 4, 28, 12, 0, 0, tzinfo=timezone.utc)


def test_pre_slice5_status_reads_with_defaults(tmp_path: Path):
    """Pre-Slice-5 _sync-status.md (no cron fields) must read cleanly."""
    legacy_text = """---
last_run: 2026-04-25T10:00:00+00:00
last_success: 2026-04-25T10:00:00+00:00
consecutive_failures: 0
last_error: null
new_cards_this_run: 5
extraction_pending_count: 0
total_cards: 30
threads_permanently_unfetched_this_run: 0
threads_permanently_unfetched_cumulative: 0
---

old heartbeat
"""
    (tmp_path / STATUS_FILE_NAME).write_text(legacy_text)
    status = read_status(tmp_path)
    assert status is not None
    assert status.last_cron_run is None
    assert status.last_cron_success is None
    assert status.consecutive_cron_failures == 0
    assert status.last_cron_runner is None
    assert status.oldest_pending_age_days == 0


def test_manual_run_does_not_touch_cron_counters(tmp_path: Path):
    """Manual /xsync (cron_runner=None) must NEVER reset cron counters."""
    # Seed with a cron-failed state.
    seeded = SyncStatus(
        last_run="2026-04-25T07:00:00+00:00",
        last_success="2026-04-23T07:00:00+00:00",
        consecutive_failures=0,
        new_cards_this_run=0,
        extraction_pending_count=0,
        total_cards=30,
        last_cron_run="2026-04-25T07:00:00+00:00",
        last_cron_success="2026-04-19T07:00:00+00:00",
        consecutive_cron_failures=3,
        last_cron_runner="github-actions",
    )
    write_status(tmp_path, seeded)

    # User runs manual /xsync, succeeds. Manual fields update.
    updated = update_after_run(
        tmp_path,
        success=True,
        new_cards_this_run=2,
        extraction_pending_count=0,
        total_cards=32,
        now=_now(),
        cron_runner=None,  # MANUAL — never touches cron counters
    )
    assert updated.consecutive_failures == 0
    # Cron counters preserved verbatim.
    assert updated.last_cron_run == "2026-04-25T07:00:00+00:00"
    assert updated.last_cron_success == "2026-04-19T07:00:00+00:00"
    assert updated.consecutive_cron_failures == 3
    assert updated.last_cron_runner == "github-actions"


def test_cron_run_updates_only_cron_mirror(tmp_path: Path):
    """Cron-mode run updates BOTH the manual fields AND cron mirror."""
    updated = update_after_run(
        tmp_path,
        success=True,
        new_cards_this_run=3,
        extraction_pending_count=3,
        total_cards=33,
        now=_now(),
        cron_runner="github-actions",
        oldest_pending_age_days=2,
    )
    # Manual fields updated
    assert updated.last_run == _now().isoformat()
    assert updated.last_success == _now().isoformat()
    # Cron fields ALSO updated
    assert updated.last_cron_run == _now().isoformat()
    assert updated.last_cron_success == _now().isoformat()
    assert updated.consecutive_cron_failures == 0
    assert updated.last_cron_runner == "github-actions"
    assert updated.oldest_pending_age_days == 2


def test_cron_failure_increments_cron_counter(tmp_path: Path):
    """Cron failure increments cron-only counter; manual counter follows
    its own track."""
    update_after_run(
        tmp_path,
        success=False,
        new_cards_this_run=0,
        extraction_pending_count=0,
        total_cards=30,
        now=_now(),
        cron_runner="github-actions",
        last_error="auth failed",
    )
    update_after_run(
        tmp_path,
        success=False,
        new_cards_this_run=0,
        extraction_pending_count=0,
        total_cards=30,
        now=_now(),
        cron_runner="github-actions",
        last_error="auth failed",
    )
    final = read_status(tmp_path)
    assert final is not None
    assert final.consecutive_cron_failures == 2
    assert final.consecutive_failures == 2  # manual mirror also incremented


def test_should_show_cron_stale_banner_threshold(tmp_path: Path):
    """Cron-only banner fires at consecutive_cron_failures >= threshold."""
    status = SyncStatus(
        last_run="2026-04-28T12:00:00+00:00",
        last_success="2026-04-28T12:00:00+00:00",
        consecutive_failures=0,
        consecutive_cron_failures=BANNER_CRON_FAILURE_THRESHOLD,
        last_cron_run="2026-04-28T12:00:00+00:00",
        last_cron_success=None,
    )
    assert status.should_show_cron_stale_banner(now=_now()) is True


def test_should_show_cron_stale_banner_age(tmp_path: Path):
    """Cron-only banner fires when last_cron_run is past stale threshold."""
    long_ago = (_now() - timedelta(days=BANNER_CRON_STALE_DAYS + 1)).isoformat()
    status = SyncStatus(
        last_run=_now().isoformat(),
        last_success=_now().isoformat(),
        consecutive_failures=0,
        consecutive_cron_failures=0,
        last_cron_run=long_ago,
        last_cron_success=long_ago,
    )
    assert status.should_show_cron_stale_banner(now=_now()) is True


def test_should_show_cron_stale_banner_healthy(tmp_path: Path):
    """No banner when both manual and cron are healthy."""
    recent = (_now() - timedelta(days=1)).isoformat()
    status = SyncStatus(
        last_run=recent,
        last_success=recent,
        consecutive_failures=0,
        consecutive_cron_failures=0,
        last_cron_run=recent,
        last_cron_success=recent,
    )
    assert status.should_show_cron_stale_banner(now=_now()) is False


def test_should_show_stale_banner_includes_cron_logic(tmp_path: Path):
    """Master banner method also fires on cron-only staleness."""
    long_ago = (_now() - timedelta(days=BANNER_CRON_STALE_DAYS + 1)).isoformat()
    recent = _now().isoformat()
    status = SyncStatus(
        last_run=recent,
        last_success=recent,  # manual is fresh
        consecutive_failures=0,
        consecutive_cron_failures=0,
        last_cron_run=long_ago,  # but cron is stale
        last_cron_success=long_ago,
    )
    # Manual stale check would say False; the new cron check should fire.
    assert status.should_show_stale_banner(now=_now()) is True


def test_extraction_backlog_banner_count_threshold():
    status = SyncStatus(
        last_run=_now().isoformat(),
        extraction_pending_count=EXTRACTION_BACKLOG_COUNT_THRESHOLD,
    )
    assert status.should_show_extraction_backlog_banner() is True


def test_extraction_backlog_banner_age_threshold():
    status = SyncStatus(
        last_run=_now().isoformat(),
        extraction_pending_count=10,
        oldest_pending_age_days=EXTRACTION_BACKLOG_AGE_DAYS,
    )
    assert status.should_show_extraction_backlog_banner() is True


def test_extraction_backlog_banner_below_thresholds():
    status = SyncStatus(
        last_run=_now().isoformat(),
        extraction_pending_count=49,
        oldest_pending_age_days=29,
    )
    assert status.should_show_extraction_backlog_banner() is False


def test_cron_never_fired_returns_true_for_fresh():
    status = SyncStatus(last_run=_now().isoformat())
    assert status.cron_never_fired() is True


def test_cron_never_fired_returns_false_after_one_cron():
    status = SyncStatus(
        last_run=_now().isoformat(),
        last_cron_run=_now().isoformat(),
    )
    assert status.cron_never_fired() is False


def test_full_roundtrip_with_cron_fields(tmp_path: Path):
    """Write + read with cron fields preserves all values."""
    written = SyncStatus(
        last_run="2026-04-28T12:00:00+00:00",
        last_success="2026-04-28T12:00:00+00:00",
        consecutive_failures=0,
        new_cards_this_run=3,
        extraction_pending_count=3,
        total_cards=33,
        last_cron_run="2026-04-28T12:00:00+00:00",
        last_cron_success="2026-04-28T12:00:00+00:00",
        consecutive_cron_failures=0,
        last_cron_runner="github-actions",
        oldest_pending_age_days=2,
    )
    write_status(tmp_path, written)
    read_back = read_status(tmp_path)
    assert read_back == written
