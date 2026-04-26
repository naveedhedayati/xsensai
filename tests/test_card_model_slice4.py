"""Slice 4 — schema forward/backward compat for new card fields.

Per /autoplan S-8 fix: model/card.py was NOT in the original Modify list and
strict validation (extra="forbid") would have rejected new fields at load.
These tests prove the schema accepts new fields AND still loads old cards.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from xsensai.model.card import CardFrontmatter


def _bookmark_base():
    return {
        "source_type": "bookmark",
        "captured": datetime(2026, 4, 26, 12, 0, tzinfo=timezone.utc),
        "source": "https://x.com/example/status/123",
        "source_id": "123",
        "author": "@example",
        "date": datetime(2026, 4, 25, 12, 0, tzinfo=timezone.utc),
    }


def test_new_thread_fetch_status_field_parses():
    fm = CardFrontmatter(**_bookmark_base(), thread_fetch_status="complete")
    assert fm.thread_fetch_status == "complete"


def test_new_thread_fetch_status_accepts_all_literals():
    for status in ["complete", "outside_window", "failed", "unknown_empty", "not_applicable"]:
        fm = CardFrontmatter(**_bookmark_base(), thread_fetch_status=status)
        assert fm.thread_fetch_status == status


def test_new_thread_fetch_status_rejects_unknown():
    with pytest.raises(Exception):  # pydantic ValidationError
        CardFrontmatter(**_bookmark_base(), thread_fetch_status="bogus")


def test_xsync_run_id_accepts_alias_and_python_name():
    """populate_by_name=True lets us set via either `_xsync_run_id` or `xsync_run_id`."""
    fm_alias = CardFrontmatter(**_bookmark_base(), **{"_xsync_run_id": "abc123"})
    assert fm_alias.xsync_run_id == "abc123"

    fm_pyname = CardFrontmatter(**_bookmark_base(), xsync_run_id="abc123")
    assert fm_pyname.xsync_run_id == "abc123"


def test_old_card_without_new_fields_still_loads():
    """Backward compat: pre-Slice-4 cards omit thread_fetch_status + xsync_run_id."""
    fm = CardFrontmatter(**_bookmark_base())
    assert fm.thread_fetch_status is None
    assert fm.xsync_run_id is None


def test_strict_extra_forbid_still_rejects_truly_unknown_keys():
    """Schema is still strict — totally novel keys must fail."""
    with pytest.raises(Exception):  # pydantic ValidationError
        CardFrontmatter(**_bookmark_base(), totally_made_up_key="x")
