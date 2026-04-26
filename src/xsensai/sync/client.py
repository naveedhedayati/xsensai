"""XDK wrapper — XClient.

Three responsibilities (per slice-4-draft.md "XDK client wrapper" section):

1. Auth — lazy refresh on first API call; transparent re-refresh on 401;
   detect rotated refresh tokens and persist via TokenProvider.
2. Bookmarks — paginated with rate-limit (Retry-After) backoff.
3. Threads — graceful degradation per Spike #6b: search_recent for fresh
   bookmarks; search_all for >7-day-old bookmarks; classify the empty
   result so callers can set thread_fetch_status correctly.

Designed for testability: `XClient._xdk_client` is the seam. Tests mock
the underlying XDK Client + TokenProvider; XClient methods are exercised
end-to-end against the mock. No HTTP-level mocking needed for unit tests.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Literal, Optional

from xsensai.errors import XSensaiError
from xsensai.sync.auth import TokenProvider


log = logging.getLogger(__name__)


# X API: docs say search_recent is 7 days. We use a 5-day cutoff (1-day
# safety margin) before falling back to search_all.
SEARCH_RECENT_WINDOW_DAYS = 7
SEARCH_RECENT_SAFETY_DAYS = 5  # use search_all if bookmark older than this

# Rate-limit retry budget per call.
MAX_RATE_LIMIT_RETRIES = 3
MAX_NETWORK_RETRIES = 3

# Bookmark fetch fields — minimal but sufficient for v2 card construction.
DEFAULT_TWEET_FIELDS = [
    "created_at", "author_id", "conversation_id", "text", "lang",
    "entities", "attachments", "in_reply_to_user_id",
]
DEFAULT_USER_FIELDS = ["username", "name"]
DEFAULT_MEDIA_FIELDS = ["type", "url", "preview_image_url", "alt_text"]
DEFAULT_EXPANSIONS = ["author_id", "attachments.media_keys"]


ThreadStatus = Literal[
    "complete",          # fetched OP reply chain successfully
    "outside_window",    # tried both endpoints; nothing recoverable
    "unknown_empty",     # search_recent returned empty within 7 days; could be retried
    "not_applicable",    # bookmark is a single tweet (no thread)
    "failed",            # real API error; retryable
]


@dataclass(frozen=True)
class ThreadFetchResult:
    """Outcome of get_thread(). Maps directly to card.thread_fetch_status."""

    status: ThreadStatus
    replies: List[Dict[str, Any]] = field(default_factory=list)
    # Set when search_all returned 403/unauthorized — caller should emit
    # [INFO/SEARCH_ALL_UNAVAILABLE] envelope ONCE per session.
    search_all_unavailable: bool = False


@dataclass(frozen=True)
class BookmarkPage:
    """One page of get_bookmarks results.

    bookmarks: list of XDK bookmark dicts (already flattened with includes).
    next_cursor: pagination token to pass back, or None when the run is done.
    """

    bookmarks: List[Dict[str, Any]]
    next_cursor: Optional[str]


class XClient:
    """High-level X API client. Wraps XDK with auth + retries + classification.

    Slice 4 only uses 3 endpoints (bookmarks, search_recent, search_all). Any
    new endpoint should be added as a new method here, NOT by exposing the
    underlying XDK client to callers (the wrapper is the seam).
    """

    def __init__(
        self,
        token_provider: TokenProvider,
        client_id: str,
        *,
        xdk_client_factory: Any = None,  # for tests; defaults to xdk.Client
    ) -> None:
        self._token_provider = token_provider
        self._client_id = client_id
        self._xdk_client: Optional[Any] = None
        self._user_id: Optional[str] = None
        self._search_all_unavailable_reported = False
        # Defer importing xdk to avoid hard dep at module-load time (tests
        # may inject a factory before xdk is even installed).
        if xdk_client_factory is None:
            import xdk  # noqa: F401 — surfaces ImportError early in real use
            xdk_client_factory = xdk.Client
        self._xdk_client_factory = xdk_client_factory

    # --- auth ------------------------------------------------------------

    def _ensure_client(self) -> Any:
        """Lazy-init the XDK client + run the first refresh exchange."""
        if self._xdk_client is not None:
            return self._xdk_client

        refresh_tok = self._token_provider.get_refresh_token()
        self._xdk_client = self._xdk_client_factory(
            client_id=self._client_id,
            token={"refresh_token": refresh_tok, "access_token": ""},
        )

        try:
            new_token = self._xdk_client.refresh_token()
        except Exception as e:
            raise XSensaiError(
                code="AUTH_FAILED",
                cause=f"OAuth refresh exchange failed: {type(e).__name__}: {e}",
                attempted="xdk.Client.refresh_token() with stored refresh_token",
                next_action=(
                    "If this is the first run, verify your X dev app's client_id is correct. "
                    "If it's been working, your refresh token may have been rotated/revoked — "
                    "run `python -m xsensai.sync.setup_oauth` to re-authorize."
                ),
                retryable=True,
            )

        # Persist a rotated refresh token if X gave us one.
        if isinstance(new_token, dict) and "refresh_token" in new_token:
            rotated = new_token["refresh_token"]
            if rotated and rotated != refresh_tok:
                log.info("X rotated the refresh token — persisting new value via TokenProvider")
                self._token_provider.store_refresh_token(rotated)

        return self._xdk_client

    def _retry_refresh_on_401(self, fn: Any, *args: Any, **kwargs: Any) -> Any:
        """Call fn(*args, **kwargs); on 401 refresh once + retry; second 401 → AUTH_FAILED.

        Persists rotated refresh token via TokenProvider — X uses single-use
        refresh tokens per OAuth 2.0 PKCE; failing to persist the rotated
        token after a mid-call refresh would lock the user out on next run.
        """
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if not _looks_like_unauthorized(e):
                raise
            log.warning("XDK call hit 401; attempting one silent refresh + retry")
            try:
                new_token = self._xdk_client.refresh_token()  # type: ignore[union-attr]
            except Exception as refresh_err:
                raise XSensaiError(
                    code="AUTH_FAILED",
                    cause=f"Mid-call token refresh failed: {refresh_err}",
                    attempted="silent refresh after 401",
                    next_action="Re-run `python -m xsensai.sync.setup_oauth` to re-authorize.",
                    retryable=True,
                )
            # Persist a rotated refresh token mirror of _ensure_client behavior.
            # Without this, X's single-use refresh-token policy would lock the
            # user out on next /xsync (the in-memory rotation is fine for THIS
            # run, but the stored token in Keychain is stale).
            if isinstance(new_token, dict) and "refresh_token" in new_token:
                rotated = new_token["refresh_token"]
                if rotated:
                    try:
                        current = self._token_provider.get_refresh_token()
                    except Exception:
                        current = None
                    if rotated != current:
                        log.info("X rotated refresh token mid-call; persisting new value")
                        try:
                            self._token_provider.store_refresh_token(rotated)
                        except Exception as store_err:
                            log.warning("Failed to persist rotated refresh token: %s", store_err)
            try:
                return fn(*args, **kwargs)
            except Exception as second_err:
                raise XSensaiError(
                    code="AUTH_FAILED",
                    cause=f"401 persisted after refresh: {second_err}",
                    attempted="retry after silent refresh",
                    next_action="Re-run `python -m xsensai.sync.setup_oauth` to re-authorize.",
                    retryable=True,
                )

    # --- user lookup -----------------------------------------------------

    def get_authenticated_user_id(self) -> str:
        """Return the authenticated user's ID (cached after first call).

        Bookmarks endpoint requires {id} in the path. The "/2/users/me"
        endpoint returns the authenticated user's id given the bearer token.
        """
        if self._user_id is not None:
            return self._user_id
        client = self._ensure_client()

        def _fetch() -> Any:
            # XDK exposes get_me() at users/client.py:818 (verified). Tolerant
            # of dict-or-object return shape via _extract_user_id.
            return client.users.get_me()

        result = self._retry_refresh_on_401(_fetch)
        user_id = _extract_user_id(result)
        if not user_id:
            raise XSensaiError(
                code="AUTH_FAILED",
                cause="X API /users/me returned no user id.",
                attempted="client.users.get_me()",
                next_action="Verify your X dev app has 'users.read' scope.",
                retryable=False,
            )
        self._user_id = user_id
        return user_id

    # --- bookmarks -------------------------------------------------------

    def iter_bookmarks(
        self,
        *,
        max_per_page: int = 100,
        max_pages: Optional[int] = None,
    ) -> Iterator[BookmarkPage]:
        """Paginate through bookmarks (auth user). Honors Retry-After on 429.

        Caller can stop iterating early (e.g., when they've seen a known
        source_id from the dedup set) — XDK's auto-pagination is sidestepped
        here so we control budget and stop conditions.
        """
        client = self._ensure_client()
        user_id = self.get_authenticated_user_id()
        cursor: Optional[str] = None
        pages_seen = 0

        while True:
            # Wrap each page fetch in the auth-aware retry helper so a 401
            # mid-iteration triggers ONE silent refresh + retry (not recursion).
            page_data = self._retry_refresh_on_401(
                self._fetch_one_bookmark_page,
                client, user_id, cursor=cursor, max_results=max_per_page,
            )
            yield page_data
            pages_seen += 1
            if page_data.next_cursor is None:
                return
            if max_pages is not None and pages_seen >= max_pages:
                return
            cursor = page_data.next_cursor

    def _fetch_one_bookmark_page(
        self,
        client: Any,
        user_id: str,
        *,
        cursor: Optional[str],
        max_results: int,
    ) -> BookmarkPage:
        retries = 0
        while True:
            try:
                # XDK's get_bookmarks returns Iterator[GetBookmarksResponse]
                # (auto-paginates). We only consume the first page by breaking
                # out after one yield.
                iterator = client.users.get_bookmarks(
                    id=user_id,
                    max_results=max_results,
                    pagination_token=cursor,
                    tweet_fields=DEFAULT_TWEET_FIELDS,
                    user_fields=DEFAULT_USER_FIELDS,
                    media_fields=DEFAULT_MEDIA_FIELDS,
                    expansions=DEFAULT_EXPANSIONS,
                )
                result = next(iter(iterator), None)
                if result is None:
                    return BookmarkPage(bookmarks=[], next_cursor=None)
                bookmarks = _flatten_bookmark_page(result)
                next_cursor = _extract_next_cursor(result)
                return BookmarkPage(bookmarks=bookmarks, next_cursor=next_cursor)
            except Exception as e:
                if _looks_like_rate_limit(e) and retries < MAX_RATE_LIMIT_RETRIES:
                    wait = _retry_after_seconds(e, default=15.0)
                    retries += 1
                    log.warning(
                        "Rate-limited on get_bookmarks (page=%s); sleeping %.1fs (retry %d/%d)",
                        cursor or "<first>", wait, retries, MAX_RATE_LIMIT_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                if _looks_like_rate_limit(e):
                    raise XSensaiError(
                        code="X_API_RATE_LIMITED",
                        cause=f"X API rate-limited after {MAX_RATE_LIMIT_RETRIES} retries.",
                        attempted=f"client.users.get_bookmarks(id={user_id}, cursor={cursor!r})",
                        next_action="Wait ~15 minutes (rate limits reset) and re-run /xsync.",
                        retryable=True,
                    )
                if _looks_like_network_error(e) and retries < MAX_NETWORK_RETRIES:
                    wait = 2.0 ** retries
                    retries += 1
                    log.warning(
                        "Network error on get_bookmarks; sleeping %.1fs (retry %d/%d)",
                        wait, retries, MAX_NETWORK_RETRIES,
                    )
                    time.sleep(wait)
                    continue
                if _looks_like_network_error(e):
                    raise XSensaiError(
                        code="X_API_NETWORK_ERROR",
                        cause=f"X API network error after {MAX_NETWORK_RETRIES} retries: {e}",
                        attempted=f"client.users.get_bookmarks(id={user_id}, cursor={cursor!r})",
                        next_action="Check your network and re-run /xsync. The checkpoint resumes where it left off.",
                        retryable=True,
                    )
                # Unhandled — wrap and re-raise via auth-aware path. The
                # _retry_refresh_on_401 wrapper ALREADY contains the bounded
                # one-refresh-and-retry loop; previously this branch recursed
                # back into _fetch_one_bookmark_page through the wrapper,
                # which on persistent 401 would stack-overflow. Now we just
                # surface AUTH_FAILED — outer caller decides whether to retry.
                if _looks_like_unauthorized(e):
                    raise XSensaiError(
                        code="AUTH_FAILED",
                        cause=f"Persistent 401 on bookmark fetch (cursor={cursor!r}): {e}",
                        attempted=f"client.users.get_bookmarks(id={user_id})",
                        next_action="Re-run `python -m xsensai.sync.setup_oauth` to re-authorize.",
                        retryable=True,
                    )
                raise

    # --- thread fetch (graceful degradation per Spike #6b) ---------------

    def get_thread(
        self,
        *,
        conversation_id: str,
        op_handle: str,
        bookmark_age_days: Optional[float],
        max_replies: int = 20,
    ) -> ThreadFetchResult:
        """Fetch the OP reply chain for a thread bookmark.

        Branch (per Spike #6b):
          1. Try search_recent(conversation_id:X from:OP).
          2. If 200 + non-empty → complete.
          3. If 200 + empty AND age <= 7 days → unknown_empty (retryable).
          4. If 200 + empty AND age > 7 days → try search_all once.
             - non-empty → complete.
             - empty → outside_window.
             - 403/unauthorized → outside_window + flag SEARCH_ALL_UNAVAILABLE.
             - other → failed.
          5. If 5xx/network → failed.
        """
        client = self._ensure_client()
        query = f"conversation_id:{conversation_id} from:{op_handle.lstrip('@')}"
        log.debug("get_thread query=%r age_days=%s", query, bookmark_age_days)

        # Step 1: search_recent
        try:
            replies = self._search(
                client, "recent", query, max_results=max_replies,
            )
        except XSensaiError:
            return ThreadFetchResult(status="failed")
        except Exception as e:
            log.warning("search_recent unhandled error: %s", e)
            return ThreadFetchResult(status="failed")

        if replies:
            return ThreadFetchResult(status="complete", replies=replies)

        # Empty — branch on age
        if bookmark_age_days is None or bookmark_age_days <= SEARCH_RECENT_SAFETY_DAYS:
            return ThreadFetchResult(status="unknown_empty")

        # Step 2: search_all (graceful degradation)
        # F9 fix: AUTH_FAILED is propagated loudly. The original logic conflated
        # tier-gating (403 from search_all because the tier doesn't include it)
        # with auth failure (the user's token is broken). The latter would
        # silently produce incomplete cards on every bookmark — much worse
        # than a loud failure.
        try:
            replies = self._search(
                client, "all", query, max_results=max_replies,
            )
        except XSensaiError as e:
            if e.code == "AUTH_FAILED":
                # Token is broken — DO NOT silently mask. Propagate so the
                # outer run() can fail loudly with the right envelope.
                raise
            # Tier-gated (403/forbidden) vs real error: only the tier case
            # gets outside_window + search_all_unavailable flag.
            details_or_cause = (e.details or "") + " " + (e.cause or "").lower()
            if "403" in details_or_cause or "forbidden" in details_or_cause.lower():
                self._search_all_unavailable_reported = True
                return ThreadFetchResult(status="outside_window", search_all_unavailable=True)
            return ThreadFetchResult(status="failed")
        except Exception as e:
            err_str = str(e).lower()
            if "401" in err_str or "unauthorized" in err_str:
                # Same: 401 mid-search_all means the token died. Propagate.
                raise XSensaiError(
                    code="AUTH_FAILED",
                    cause=f"search_all returned 401: {e}",
                    attempted=f"posts.search_all(query=...) for thread fetch",
                    next_action="Re-run `python -m xsensai.sync.setup_oauth` to re-authorize.",
                    retryable=True,
                )
            if "403" in err_str or "forbidden" in err_str:
                self._search_all_unavailable_reported = True
                return ThreadFetchResult(status="outside_window", search_all_unavailable=True)
            log.warning("search_all unhandled error: %s", e)
            return ThreadFetchResult(status="failed")

        if replies:
            return ThreadFetchResult(status="complete", replies=replies)
        return ThreadFetchResult(status="outside_window")

    def _search(
        self,
        client: Any,
        kind: Literal["recent", "all"],
        query: str,
        *,
        max_results: int,
    ) -> List[Dict[str, Any]]:
        """Run one page of search_recent / search_all. Honors Retry-After on 429."""
        retries = 0
        method = client.posts.search_recent if kind == "recent" else client.posts.search_all
        while True:
            try:
                iterator = method(
                    query=query,
                    max_results=max_results,
                    tweet_fields=DEFAULT_TWEET_FIELDS,
                    user_fields=DEFAULT_USER_FIELDS,
                )
                page = next(iter(iterator), None)
                if page is None:
                    return []
                return _flatten_search_page(page)
            except Exception as e:
                if _looks_like_rate_limit(e) and retries < MAX_RATE_LIMIT_RETRIES:
                    wait = _retry_after_seconds(e, default=15.0)
                    retries += 1
                    time.sleep(wait)
                    continue
                if _looks_like_rate_limit(e):
                    raise XSensaiError(
                        code="X_API_RATE_LIMITED",
                        cause=f"search_{kind} rate-limited after {MAX_RATE_LIMIT_RETRIES} retries.",
                        attempted=f"posts.search_{kind}(query={query!r})",
                        next_action="Wait ~15 min and retry.",
                        retryable=True,
                    )
                raise


# ---------------------------------------------------------------------------
# Helpers — XDK response shape extraction. Tolerant to dict-or-object returns.
# ---------------------------------------------------------------------------


def _looks_like_rate_limit(e: Exception) -> bool:
    s = str(e).lower()
    return "429" in s or "rate limit" in s or "too many requests" in s


def _looks_like_network_error(e: Exception) -> bool:
    s = str(e).lower()
    return any(x in s for x in ["timeout", "connection", "504", "503", "502", "500", "network"])


def _looks_like_unauthorized(e: Exception) -> bool:
    s = str(e).lower()
    return "401" in s or "unauthorized" in s


def _retry_after_seconds(e: Exception, *, default: float) -> float:
    """Try to extract Retry-After from the exception. Fall back to `default`."""
    headers = getattr(e, "response", None)
    if headers is not None and hasattr(headers, "headers"):
        ra = headers.headers.get("Retry-After")
        if ra:
            try:
                return float(ra)
            except (TypeError, ValueError):
                pass
    return default


def _extract_user_id(result: Any) -> str:
    """Pull the user id from a /users/me response."""
    if hasattr(result, "data"):
        data = result.data
    elif isinstance(result, dict):
        data = result.get("data", {})
    else:
        data = result
    if hasattr(data, "id"):
        return str(data.id)
    if isinstance(data, dict):
        return str(data.get("id", "") or "")
    return ""


def _extract_next_cursor(page: Any) -> Optional[str]:
    """Pull next_token / pagination_token from a paginated response."""
    meta = None
    if hasattr(page, "meta"):
        meta = page.meta
    elif isinstance(page, dict):
        meta = page.get("meta")
    if meta is None:
        return None
    if hasattr(meta, "next_token"):
        return getattr(meta, "next_token", None) or None
    if isinstance(meta, dict):
        return meta.get("next_token") or meta.get("pagination_token")
    return None


def _flatten_bookmark_page(page: Any) -> List[Dict[str, Any]]:
    """Convert one XDK bookmark page into a list of self-contained dicts.

    Resolves the `includes.users` join into each tweet's `_author` field
    so callers don't have to walk the includes tree.
    """
    data = _get_attr(page, "data", default=[])
    includes = _get_attr(page, "includes", default={})
    users_idx = _build_user_index(_get_attr(includes, "users", default=[]))
    media_idx = _build_media_index(_get_attr(includes, "media", default=[]))

    out: List[Dict[str, Any]] = []
    for tweet in data or []:
        tweet_dict = _to_dict(tweet)
        author_id = tweet_dict.get("author_id")
        tweet_dict["_author"] = users_idx.get(str(author_id), {})
        # Resolve attachments.media_keys → media objects
        attachments = tweet_dict.get("attachments") or {}
        keys = attachments.get("media_keys") if isinstance(attachments, dict) else None
        if keys:
            tweet_dict["_media"] = [media_idx.get(k, {}) for k in keys]
        else:
            tweet_dict["_media"] = []
        out.append(tweet_dict)
    return out


def _flatten_search_page(page: Any) -> List[Dict[str, Any]]:
    """Like _flatten_bookmark_page but for search results (no media join needed
    in the typical thread-walk case — we only need author + text)."""
    data = _get_attr(page, "data", default=[])
    includes = _get_attr(page, "includes", default={})
    users_idx = _build_user_index(_get_attr(includes, "users", default=[]))
    out: List[Dict[str, Any]] = []
    for tweet in data or []:
        tweet_dict = _to_dict(tweet)
        author_id = tweet_dict.get("author_id")
        tweet_dict["_author"] = users_idx.get(str(author_id), {})
        out.append(tweet_dict)
    return out


def _build_user_index(users: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for u in users or []:
        u_dict = _to_dict(u)
        uid = u_dict.get("id")
        if uid:
            out[str(uid)] = u_dict
    return out


def _build_media_index(media: List[Any]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for m in media or []:
        m_dict = _to_dict(m)
        key = m_dict.get("media_key")
        if key:
            out[key] = m_dict
    return out


def _get_attr(obj: Any, name: str, *, default: Any) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_dict(obj: Any) -> Dict[str, Any]:
    """Convert an XDK model object (or already-dict) to a plain dict."""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump(exclude_none=False)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in vars(obj).items() if not k.startswith("_")}
    return dict(obj) if obj is not None else {}


__all__ = [
    "XClient",
    "BookmarkPage",
    "ThreadFetchResult",
    "ThreadStatus",
    "SEARCH_RECENT_WINDOW_DAYS",
    "SEARCH_RECENT_SAFETY_DAYS",
]
