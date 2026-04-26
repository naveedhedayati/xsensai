"""Slice 4 — XClient: auth refresh, pagination, get_thread graceful degradation.

XDK is mocked at the XClient seam (per /autoplan E-1 fix: XClient is the
testable boundary; tests don't need HTTP-level mocking).
"""

from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from xsensai.errors import XSensaiError
from xsensai.sync.auth import EnvSecretTokenProvider, ENV_VAR_NAME
from xsensai.sync.client import (
    BookmarkPage,
    ThreadFetchResult,
    XClient,
    _flatten_bookmark_page,
    _flatten_search_page,
)


def _make_client(monkeypatch, *, fake_xdk: MagicMock | None = None) -> XClient:
    monkeypatch.setenv(ENV_VAR_NAME, "test-refresh")
    provider = EnvSecretTokenProvider()
    if fake_xdk is None:
        fake_xdk = _build_fake_xdk_factory()
    return XClient(token_provider=provider, client_id="client-id-x", xdk_client_factory=fake_xdk)


def _build_fake_xdk_factory(refresh_returns: Dict[str, Any] | None = None):
    """Returns a callable that builds a MagicMock XDK Client per call."""
    refresh_returns = refresh_returns or {"access_token": "ax", "refresh_token": "test-refresh"}

    def factory(**kwargs):
        client = MagicMock()
        client.refresh_token.return_value = refresh_returns
        return client
    return factory


def test_initial_refresh_sets_up_client(monkeypatch):
    c = _make_client(monkeypatch)
    inner = c._ensure_client()
    inner.refresh_token.assert_called_once()


def test_initial_refresh_failure_raises_auth_failed(monkeypatch):
    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.side_effect = RuntimeError("refresh blew up")
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    with pytest.raises(XSensaiError) as exc:
        c._ensure_client()
    assert exc.value.code == "AUTH_FAILED"


def test_rotated_refresh_token_persists(monkeypatch):
    """If X rotates the refresh token, TokenProvider.store_refresh_token is called."""
    rotated = "ROTATED_TOKEN_NEW"

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {
            "access_token": "ax",
            "refresh_token": rotated,
        }
        return m

    monkeypatch.setenv(ENV_VAR_NAME, "OLD_TOKEN")
    provider = EnvSecretTokenProvider()
    store_calls = []
    provider.store_refresh_token = lambda t: store_calls.append(t)  # type: ignore[method-assign]

    c = XClient(token_provider=provider, client_id="x", xdk_client_factory=factory)
    c._ensure_client()
    assert store_calls == [rotated]


def test_get_thread_complete_when_search_recent_returns_replies(monkeypatch):
    fake_search_page = type("P", (), {
        "data": [{"id": "r1", "text": "reply one", "author_id": "1"}],
        "includes": {"users": [{"id": "1", "username": "alice"}]},
        "meta": None,
    })()

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {"access_token": "ax", "refresh_token": "t"}
        m.posts.search_recent.return_value = iter([fake_search_page])
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    result = c.get_thread(
        conversation_id="999",
        op_handle="alice",
        bookmark_age_days=2.0,
    )
    assert result.status == "complete"
    assert len(result.replies) == 1


def test_get_thread_unknown_empty_when_recent_empty_and_age_le_5(monkeypatch):
    """search_recent returns empty + age within safety window → unknown_empty."""
    fake_empty = type("P", (), {"data": [], "includes": {}, "meta": None})()

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {"access_token": "ax", "refresh_token": "t"}
        m.posts.search_recent.return_value = iter([fake_empty])
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    result = c.get_thread(
        conversation_id="999",
        op_handle="alice",
        bookmark_age_days=3.0,
    )
    assert result.status == "unknown_empty"


def test_get_thread_falls_back_to_search_all_when_old(monkeypatch):
    """Empty search_recent + age > 5 days → tries search_all."""
    fake_empty = type("P", (), {"data": [], "includes": {}, "meta": None})()
    fake_all = type("P", (), {
        "data": [{"id": "r1", "text": "old reply", "author_id": "1"}],
        "includes": {"users": [{"id": "1", "username": "alice"}]},
        "meta": None,
    })()

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {"access_token": "ax", "refresh_token": "t"}
        m.posts.search_recent.return_value = iter([fake_empty])
        m.posts.search_all.return_value = iter([fake_all])
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    result = c.get_thread(
        conversation_id="999",
        op_handle="alice",
        bookmark_age_days=30.0,
    )
    assert result.status == "complete"
    assert "old reply" in result.replies[0]["text"]


def test_get_thread_outside_window_when_search_all_403(monkeypatch):
    """403 on search_all → outside_window + flag SEARCH_ALL_UNAVAILABLE."""
    fake_empty = type("P", (), {"data": [], "includes": {}, "meta": None})()

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {"access_token": "ax", "refresh_token": "t"}
        m.posts.search_recent.return_value = iter([fake_empty])
        m.posts.search_all.side_effect = RuntimeError("403 Forbidden")
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    result = c.get_thread(
        conversation_id="999",
        op_handle="alice",
        bookmark_age_days=30.0,
    )
    assert result.status == "outside_window"
    assert result.search_all_unavailable is True


def test_get_thread_outside_window_when_search_all_empty(monkeypatch):
    """Both endpoints empty + age > 5 days → outside_window (truly nothing)."""
    fake_empty = type("P", (), {"data": [], "includes": {}, "meta": None})()

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {"access_token": "ax", "refresh_token": "t"}
        m.posts.search_recent.return_value = iter([fake_empty])
        m.posts.search_all.return_value = iter([fake_empty])
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    result = c.get_thread(
        conversation_id="999",
        op_handle="alice",
        bookmark_age_days=30.0,
    )
    assert result.status == "outside_window"
    assert result.search_all_unavailable is False


def test_flatten_bookmark_page_resolves_author():
    fake_page = type("P", (), {
        "data": [
            {"id": "1", "text": "hello", "author_id": "u1"},
        ],
        "includes": {
            "users": [{"id": "u1", "username": "alice", "name": "Alice"}],
            "media": [],
        },
        "meta": None,
    })()
    out = _flatten_bookmark_page(fake_page)
    assert out[0]["_author"]["username"] == "alice"


def test_flatten_search_page_resolves_author():
    fake_page = type("P", (), {
        "data": [{"id": "1", "text": "x", "author_id": "u9"}],
        "includes": {"users": [{"id": "u9", "username": "bob"}]},
        "meta": None,
    })()
    out = _flatten_search_page(fake_page)
    assert out[0]["_author"]["username"] == "bob"


def test_iter_bookmarks_pagination_stops_when_next_token_none(monkeypatch):
    """Single page run — iterator yields once and stops."""
    fake_page = type("P", (), {
        "data": [{"id": "1", "text": "x", "author_id": "u1"}],
        "includes": {"users": [{"id": "u1", "username": "alice"}], "media": []},
        "meta": {"next_token": None},
    })()

    def factory(**kwargs):
        m = MagicMock()
        m.refresh_token.return_value = {"access_token": "ax", "refresh_token": "t"}
        m.users.find_my_user.return_value = type("R", (), {"data": {"id": "USERID"}})()
        m.users.get_bookmarks.return_value = iter([fake_page])
        return m

    c = _make_client(monkeypatch, fake_xdk=factory)
    pages = list(c.iter_bookmarks(max_per_page=100))
    assert len(pages) == 1
    assert len(pages[0].bookmarks) == 1
