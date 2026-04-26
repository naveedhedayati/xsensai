"""Slice 4 sync orchestrator — `run`, `apply_extraction`, `finalize_run`,
and `extract_pending` (the /xextract entrypoint).

Per /autoplan E-1 fix: single-process design. Each public function is a
self-contained operation with its own lock cycle. We do NOT claim "locks
held across CLI invocations" — that would be unsafe with flock.

Flow for /xsync (manual mode):
  1. Slash command calls `run(mode, extractor)` ONCE.
     - `run` acquires card_write per card briefly, fetches XDK, writes cards
       with extraction_pending=True, appends checkpoint per card.
     - Returns {run_id, cards_written, extraction_prompts, extraction_strategy}.
  2. If extraction_strategy == "inline":
     - Slash command's host Claude fulfills each extraction_prompt.
     - Slash command calls `apply_extraction(card_id, summary, tags, run_id)`
       per card. Each call acquires card_write briefly, updates the
       frontmatter, releases.
  3. Slash command calls `finalize_run(run_id, success=True)` ONCE.
     - Writes _sync-status.md heartbeat. Archives checkpoint. Runs reindex
       under index_rebuild lock.

Flow for /xextract: same as steps 2-3 but kicked off by `extract_pending(mode)`.

Per E-4 invariant: card-write transaction order is always
  (a) write .md+.raw.txt with extraction_pending=True
  (b) append source_id to checkpoint
  (c) extraction (later)
"""

from __future__ import annotations

import json
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from xsensai.errors import XSensaiError, XSensaiInfo
from xsensai.locks import with_card_write_lock, with_index_rebuild_lock
from xsensai.model.card import LoadedCard
from xsensai.storage.corpus import (
    iter_cards_metadata,
    load_card_by_id,
    resolve_corpus_path,
)
from xsensai.sync.card_writer import CardWriteResult, write_one
from xsensai.sync.checkpoint import CheckpointFile, CheckpointRecord
from xsensai.sync.client import ThreadFetchResult, XClient
from xsensai.sync.dedup import (
    existing_source_ids,
    source_id_exists_under_lock,
)
from xsensai.sync.extraction import (
    Extractor,
    HostExtractor,
)
from xsensai.sync.heartbeat import update_after_run, SyncStatus
from xsensai.sync.log import SyncLogEntry, append_log
from xsensai.sync.version import SYNC_SCHEMA_VERSION


log = logging.getLogger(__name__)


SyncMode = Literal["since-last-run", "backlog", "single", "retry-failed", "preview"]
ExtractionStrategy = Literal["inline", "deferred", "none"]

# UC-2=C threshold: smart-default boundary. <=5 inline, >5 deferred.
SMART_DEFAULT_INLINE_MAX = 5


@dataclass
class RunResult:
    """Result of `run()` — what the slash command emits to the user."""

    run_id: str
    status: str  # ok | empty | partial | failed | preview
    extraction_strategy: ExtractionStrategy
    cards_written: List[Dict[str, Any]] = field(default_factory=list)
    extraction_prompts: List[Dict[str, str]] = field(default_factory=list)
    threads_unfetched_this_run: int = 0
    rendered_message: Optional[str] = None  # XSensaiError / XSensaiInfo .format()
    info_envelopes: List[str] = field(default_factory=list)
    duration_ms: int = 0
    # F2 fix: per-card write failures surface so the slash command can render
    # a partial-success summary instead of silently reporting status="ok"
    # when some cards failed to write.
    cards_failed: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class ApplyExtractionResult:
    """Result of `apply_extraction()` per card."""

    card_id: str
    ok: bool
    extraction_pending: bool  # True if validation said skip
    error: Optional[str] = None


@dataclass
class FinalizeResult:
    """Result of `finalize_run()`."""

    run_id: str
    sync_status: SyncStatus
    archived_checkpoint: Optional[Path]
    reindex_attempted: bool


# ---------------------------------------------------------------------------
# `run()` — the load-bearing entrypoint.
# ---------------------------------------------------------------------------


def run(
    *,
    mode: SyncMode,
    token_provider: Any,
    client_id: str,
    corpus_path: Optional[Path] = None,
    target_tweet_id: Optional[str] = None,
    extractor_override: Optional[Extractor] = None,
    inline_override: bool = False,
    defer_override: bool = False,
    max_pages: Optional[int] = None,
    now: Optional[datetime] = None,
) -> RunResult:
    """Fetch + write step. Returns extraction prompts if inline strategy chosen.

    The slash command then either runs extractions inline (calling
    apply_extraction per card) or just calls finalize_run for deferred mode.

    `inline_override` and `defer_override` are mutually exclusive — caller
    surfaces [INVALID_FLAGS] if both set.
    """
    started = time.monotonic()
    now = now or datetime.now(timezone.utc)
    run_id = str(uuid.uuid4())

    # Validate flag conflict BEFORE corpus resolution so callers without a
    # real corpus (CI test-only environments, --check probes) can still get
    # the canonical INVALID_FLAGS envelope without first hitting CORPUS_UNAVAILABLE.
    if inline_override and defer_override:
        return _failed_result(
            run_id,
            XSensaiError(
                code="INVALID_FLAGS",
                cause="Cannot pass both inline and defer overrides — they conflict.",
                attempted=f"sync.service.run(mode={mode}, inline=True, defer=True)",
                next_action="Pass at most one of `inline` or `defer`.",
                retryable=False,
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    corpus = resolve_corpus_path(corpus_path)

    if mode == "preview":
        return _run_preview(
            token_provider=token_provider,
            client_id=client_id,
            corpus=corpus,
            run_id=run_id,
            max_pages=max_pages or 1,
            started=started,
        )

    # 1. Build dedup set (precomputed) BEFORE any network call.
    on_disk = existing_source_ids(corpus_path=corpus)
    log.info("Dedup precomputed: %d existing source_ids on disk", len(on_disk))

    # 2. Fetch from XDK based on mode.
    try:
        xclient = XClient(token_provider=token_provider, client_id=client_id)
        bookmarks_to_write = _gather_bookmarks(
            xclient, mode=mode, target_tweet_id=target_tweet_id,
            on_disk=on_disk, max_pages=max_pages,
        )
    except XSensaiError as e:
        return _failed_result(run_id, e, duration_ms=int((time.monotonic() - started) * 1000))
    except Exception as e:
        return _failed_result(
            run_id,
            XSensaiError(
                code="INTERNAL_ERROR",
                cause=f"Unhandled exception in fetch: {type(e).__name__}: {e}",
                attempted=f"sync.service.run(mode={mode}) fetch step",
                next_action="Inspect logs; re-run /xsync. Checkpoint resumes if applicable.",
                retryable=True,
            ),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    n = len(bookmarks_to_write)
    if n == 0:
        info = XSensaiInfo(
            code="SYNC_DONE",
            cause="No new bookmarks since last sync.",
            action_or_note="Nothing to do. Re-run /xsync any time to pick up new bookmarks.",
            source=f"sync.service.run(mode={mode})",
        )
        return RunResult(
            run_id=run_id,
            status="empty",
            extraction_strategy="none",
            rendered_message=info.format(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # 3. Decide extraction strategy via smart default + overrides (UC-2=C).
    strategy = _decide_strategy(n=n, inline=inline_override, defer=defer_override)

    # 4. Write cards + checkpoint per card under brief card_write locks.
    checkpoint = CheckpointFile(corpus)
    cards_written: List[CardWriteResult] = []
    cards_failed: List[Dict[str, str]] = []  # F2 fix: surface per-card write failures
    threads_unfetched = 0
    info_envelopes: List[str] = []

    for bookmark in bookmarks_to_write:
        sid = str(bookmark.get("id", "")).strip()
        # Compute thread fetch in advance (no lock needed — pure fetch).
        # F3 fix: wrap in try/except so a single bookmark's thread failure
        # (rate-limit, network, etc.) doesn't tear down the whole run.
        try:
            thread = _fetch_thread_for(xclient, bookmark, now=now)
        except XSensaiError as e:
            log.warning("Thread fetch failed for source_id=%s: %s", sid, e.code)
            thread = ThreadFetchResult(status="failed")
            cards_failed.append({"source_id": sid, "error_code": e.code, "stage": "thread_fetch"})
        except Exception as e:
            log.warning("Thread fetch unhandled for source_id=%s: %s", sid, e)
            thread = ThreadFetchResult(status="failed")
            cards_failed.append({"source_id": sid, "error_code": "UNHANDLED", "stage": "thread_fetch"})

        if thread.status == "outside_window":
            threads_unfetched += 1
        if thread.search_all_unavailable and "[INFO/SEARCH_ALL_UNAVAILABLE]" not in " ".join(info_envelopes):
            info_envelopes.append(_search_all_unavailable_envelope().format())

        # Write under lock with S-7 dedup recheck
        try:
            result = _write_one_card(
                bookmark=bookmark, thread=thread, corpus=corpus,
                run_id=run_id, now=now, checkpoint=checkpoint,
            )
        except _IdempotentSkip as skip:
            log.info("Idempotent skip: source_id=%s already on disk", skip.source_id)
            continue
        except XSensaiError as e:
            log.warning("Card write failed for source_id=%s: %s", sid, e.code)
            cards_failed.append({"source_id": sid, "error_code": e.code, "stage": "write"})
            continue

        cards_written.append(result)

    # 5. If strategy is inline (and we wrote cards), produce extraction prompts.
    extraction_prompts: List[Dict[str, str]] = []
    if strategy == "inline" and cards_written:
        extractor = extractor_override or HostExtractor()
        loaded = [
            load_card_by_id(r.card_id, corpus_path=corpus)
            for r in cards_written
        ]
        # Calling extract_batch sets up the prompts on HostExtractor.
        extractor.extract_batch(loaded)
        if isinstance(extractor, HostExtractor):
            extraction_prompts = [
                {"card_id": p.card_id, "prompt_text": p.prompt_text}
                for p in extractor.pending_prompts
            ]

    duration = int((time.monotonic() - started) * 1000)
    # F2 fix: status reflects per-card outcomes. "ok" means all attempted
    # writes succeeded. "partial" means some succeeded, some failed. The
    # slash command renders SYNC_PARTIAL instead of SYNC_DONE in that case.
    if cards_failed and not cards_written:
        run_status = "failed"
    elif cards_failed:
        run_status = "partial"
    else:
        run_status = "ok"

    return RunResult(
        run_id=run_id,
        status=run_status,
        extraction_strategy=strategy if cards_written else "none",
        cards_written=[
            {
                "card_id": r.card_id,
                "md_path": str(r.md_path),
                "extraction_pending": r.extraction_pending,
                "thread_fetch_status": r.thread_fetch_status,
            }
            for r in cards_written
        ],
        cards_failed=cards_failed,
        extraction_prompts=extraction_prompts,
        threads_unfetched_this_run=threads_unfetched,
        info_envelopes=info_envelopes,
        duration_ms=duration,
    )


# ---------------------------------------------------------------------------
# `apply_extraction()` — slash command calls per card after host emits.
# ---------------------------------------------------------------------------


def apply_extraction(
    *,
    card_id: str,
    summary: str,
    tags: List[str],
    run_id: str,
    corpus_path: Optional[Path] = None,
) -> ApplyExtractionResult:
    """Update one card's frontmatter — sets summary/tags + extraction_pending=False.

    Acquires card_write briefly. If the host Claude returned empty/invalid
    output, leaves extraction_pending=True (caller can pick up via /xextract
    retry-failed later).
    """
    corpus = resolve_corpus_path(corpus_path)

    cleaned_summary = (summary or "").strip()[:400]
    cleaned_tags = [
        "".join(c for c in t.strip().lower() if c.isalnum() or c in "-_")
        for t in (tags or [])
    ]
    cleaned_tags = [t for t in cleaned_tags if t and len(t) <= 60][:5]

    if not cleaned_summary or len(cleaned_tags) < 3:
        return ApplyExtractionResult(
            card_id=card_id, ok=False, extraction_pending=True,
            error="validation failed: empty summary OR <3 tags after cleaning",
        )

    try:
        with with_card_write_lock(corpus, "xsync") as h:
            card = load_card_by_id(card_id, corpus_path=corpus)

            # F17 fix: authz check. Apply only if (a) the card was actually
            # written by /xsync (has xsync_run_id) AND it matches the caller's
            # run_id, OR (b) the card is in extraction_pending=True state and
            # the caller is /xextract retry-failed (run_id="extract-pending").
            # Without this, a malicious or confused CLI invocation could
            # overwrite ANY card's retrieval_summary + retrieval_tags by
            # forging --card-id + --run-id.
            card_run_id = card.fm.xsync_run_id
            is_retry_path = run_id.startswith("extract-pending")
            if card_run_id is not None and card_run_id != run_id and not is_retry_path:
                return ApplyExtractionResult(
                    card_id=card_id, ok=False, extraction_pending=True,
                    error=(
                        f"run_id mismatch: card was written by run {card_run_id[:8]}..., "
                        f"this call's run_id is {run_id[:8]}... — refusing to overwrite. "
                        "If you really meant to re-extract, use /xextract single <card-id> instead."
                    ),
                )
            if not card.fm.extraction_pending and not is_retry_path:
                return ApplyExtractionResult(
                    card_id=card_id, ok=False, extraction_pending=False,
                    error=(
                        f"card already has extraction_pending=False; refusing to overwrite. "
                        "If you really meant to re-extract, use /xextract single <card-id>."
                    ),
                )

            new_fm = card.fm.model_copy(update={
                "retrieval_summary": cleaned_summary,
                "retrieval_tags": cleaned_tags,
                "extraction_pending": False,
            })
            new_card = LoadedCard(
                fm=new_fm, body=card.body, raw_bytes=card.raw_bytes, md_path=card.md_path,
            )
            from xsensai.storage.corpus import write_card as _write
            _write(new_card, lock_token=h.token, corpus_path=corpus)
        return ApplyExtractionResult(card_id=card_id, ok=True, extraction_pending=False)
    except XSensaiError as e:
        return ApplyExtractionResult(
            card_id=card_id, ok=False, extraction_pending=True, error=e.format(),
        )


# ---------------------------------------------------------------------------
# `finalize_run()` — heartbeat + checkpoint archive + reindex under lock.
# ---------------------------------------------------------------------------


def finalize_run(
    *,
    run_id: str,
    success: bool,
    n_new_cards: int,
    extraction_inline: int,
    extraction_pending: int,
    threads_unfetched_this_run: int,
    last_error: Optional[str] = None,
    corpus_path: Optional[Path] = None,
    duration_ms: int = 0,
    mode: str = "since-last-run",
    skip_reindex: bool = False,
) -> FinalizeResult:
    """Heartbeat + archive checkpoint (on success) + reindex under index_rebuild."""
    corpus = resolve_corpus_path(corpus_path)
    total_cards = sum(1 for _ in iter_cards_metadata(corpus))

    status = update_after_run(
        corpus,
        success=success,
        new_cards_this_run=n_new_cards,
        extraction_pending_count=extraction_pending,
        total_cards=total_cards,
        threads_unfetched_this_run=threads_unfetched_this_run,
        last_error=last_error,
    )

    archived: Optional[Path] = None
    if success and n_new_cards > 0:
        archived = CheckpointFile(corpus).archive(run_id=run_id)

    reindex_attempted = False
    if success and n_new_cards > 0 and not skip_reindex:
        reindex_attempted = _trigger_reindex(corpus)

    # Append to xsync log
    append_log(
        SyncLogEntry(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            mode=mode,
            outcome=("success" if success else "failed"),
            n_new_cards=n_new_cards,
            extraction_inline=extraction_inline,
            extraction_pending=extraction_pending,
            threads_unfetched_this_run=threads_unfetched_this_run,
            duration_ms=duration_ms,
            sync_schema_version=SYNC_SCHEMA_VERSION,
            error_code=last_error if not success else None,
        )
    )

    return FinalizeResult(
        run_id=run_id, sync_status=status, archived_checkpoint=archived,
        reindex_attempted=reindex_attempted,
    )


# ---------------------------------------------------------------------------
# `extract_pending()` — /xextract entrypoint.
# ---------------------------------------------------------------------------


def extract_pending(
    *,
    mode: Literal["backlog", "single", "retry-failed"] = "backlog",
    target_card_id: Optional[str] = None,
    limit: Optional[int] = None,
    corpus_path: Optional[Path] = None,
) -> RunResult:
    """Drain extraction_pending=True cards. Returns extraction prompts for
    the slash command's host Claude to fulfill (same shape as run() output).
    """
    started = time.monotonic()
    corpus = resolve_corpus_path(corpus_path)
    # Prefix with "extract-pending-" so apply_extraction's F17 authz check
    # recognizes this as the legitimate retry path (and accepts the run_id
    # mismatch with the card's original xsync_run_id).
    run_id = f"extract-pending-{uuid.uuid4()}"

    pending_cards: List[LoadedCard] = []
    if mode == "single":
        if not target_card_id:
            return _failed_result(
                run_id,
                XSensaiError(
                    code="INVALID_FLAGS",
                    cause="`single` mode requires a card_id argument.",
                    attempted="/xextract single",
                    next_action="Run /xextract single <card-id>.",
                    retryable=False,
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        try:
            card = load_card_by_id(target_card_id, corpus_path=corpus)
            if card.fm.extraction_pending:
                pending_cards = [card]
        except Exception as e:
            return _failed_result(
                run_id,
                XSensaiError(
                    code="INTERNAL_ERROR",
                    cause=f"Could not load card {target_card_id!r}: {e}",
                    attempted=f"/xextract single {target_card_id}",
                    next_action="Verify the card id (filename without .md).",
                    retryable=False,
                ),
                duration_ms=int((time.monotonic() - started) * 1000),
            )
    else:
        for card in iter_cards_metadata(corpus_path=corpus):
            if card.fm.extraction_pending:
                pending_cards.append(card)
                # Use `> 0` not truthy `limit` so `--limit 0` doesn't act
                # as no-limit (was the contradiction xextract.md mentioned).
                if limit is not None and limit > 0 and len(pending_cards) >= limit:
                    break

    if not pending_cards:
        info = XSensaiInfo(
            code="NO_PENDING_EXTRACTIONS",
            cause="No cards have extraction_pending: true. Nothing to do.",
            action_or_note=(
                "Cards have summaries already, or `/xsync` deferred-mode wasn't run yet. "
                "Run /xsync to fetch new bookmarks."
            ),
            source=f"sync.service.extract_pending(mode={mode})",
        )
        return RunResult(
            run_id=run_id, status="empty", extraction_strategy="none",
            rendered_message=info.format(),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # /xextract is always inline by definition (it exists to do extraction)
    extractor = HostExtractor()
    # Need full LoadedCard for content_section
    loaded_full: List[LoadedCard] = []
    for c in pending_cards:
        try:
            loaded_full.append(load_card_by_id(c.id, corpus_path=corpus))
        except Exception as e:
            log.warning("Skipping card %s in extract_pending: %s", c.id, e)
    extractor.extract_batch(loaded_full)

    extraction_prompts = [
        {"card_id": p.card_id, "prompt_text": p.prompt_text}
        for p in extractor.pending_prompts
    ]

    duration = int((time.monotonic() - started) * 1000)
    return RunResult(
        run_id=run_id,
        status="ok",
        extraction_strategy="inline",
        cards_written=[],  # /xextract doesn't write cards; just extracts existing
        extraction_prompts=extraction_prompts,
        duration_ms=duration,
    )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


class _IdempotentSkip(Exception):
    """Raised inside _write_one_card to signal source_id already on disk."""

    def __init__(self, source_id: str) -> None:
        self.source_id = source_id


def _gather_bookmarks(
    xclient: XClient,
    *,
    mode: SyncMode,
    target_tweet_id: Optional[str],
    on_disk: set,
    max_pages: Optional[int],
) -> List[Dict[str, Any]]:
    """Pull bookmarks per mode; filter against precomputed on-disk dedup set."""
    out: List[Dict[str, Any]] = []
    if mode == "single":
        if not target_tweet_id:
            raise XSensaiError(
                code="INVALID_FLAGS",
                cause="`single` mode requires a tweet id or URL.",
                attempted=f"_gather_bookmarks(mode=single, target=None)",
                next_action="Run /xsync single <tweet-id-or-url>.",
                retryable=False,
            )
        if target_tweet_id in on_disk:
            return []  # Already have it
        # XDK doesn't expose a single-bookmark fetch; we'd need /tweets/{id}.
        # For Slice 4 minimal scope: return an empty list (caller emits
        # appropriate info). Single-tweet support lands when /tweets/{id}
        # is wired in Slice 4.5 if the user actually uses this mode.
        log.warning("single-mode fetch is stubbed in Gate B; returning empty")
        return []

    if mode == "retry-failed":
        # /xsync retry-failed isn't about RE-FETCHING; it's about re-extracting
        # cards that already exist with extraction_pending=True. Caller should
        # use /xextract retry-failed instead — return empty.
        return []

    # since-last-run / backlog: paginate bookmarks, stop early on dedup hit
    # in since-last-run mode (newest-first ordering).
    consecutive_dedup_hits = 0
    pages_seen = 0
    for page in xclient.iter_bookmarks(max_per_page=100, max_pages=max_pages):
        pages_seen += 1
        for bookmark in page.bookmarks:
            sid = str(bookmark.get("id", ""))
            if sid in on_disk:
                if mode == "since-last-run":
                    consecutive_dedup_hits += 1
                    # Heuristic: 3 consecutive duplicates = we've caught up
                    if consecutive_dedup_hits >= 3:
                        return out
                continue
            consecutive_dedup_hits = 0
            out.append(bookmark)
        if max_pages and pages_seen >= max_pages:
            break
    return out


def _decide_strategy(*, n: int, inline: bool, defer: bool) -> ExtractionStrategy:
    """Smart-default per UC-2=C. Overrides honored.

    inline+defer conflict is rejected upstream in run() with INVALID_FLAGS;
    the assert here documents the invariant for direct callers.
    """
    assert not (inline and defer), "inline and defer are mutually exclusive — caller must reject upstream"
    if inline:
        return "inline"
    if defer:
        return "deferred"
    if n <= SMART_DEFAULT_INLINE_MAX:
        return "inline"
    return "deferred"


def _fetch_thread_for(
    xclient: XClient, bookmark: Dict[str, Any], *, now: datetime,
) -> ThreadFetchResult:
    """Fetch the thread for a bookmark, classifying status per Spike #6b."""
    conv_id = str(bookmark.get("conversation_id", "")).strip()
    tweet_id = str(bookmark.get("id", "")).strip()
    if not conv_id or conv_id == tweet_id:
        # Single-tweet bookmark (no thread)
        return ThreadFetchResult(status="not_applicable")

    op_handle = (bookmark.get("_author") or {}).get("username", "")
    if not op_handle:
        return ThreadFetchResult(status="failed")

    bookmark_age_days: Optional[float] = None
    created_at_str = bookmark.get("created_at")
    if created_at_str:
        try:
            created = datetime.fromisoformat(str(created_at_str).replace("Z", "+00:00"))
            bookmark_age_days = (now - created).total_seconds() / 86400.0
        except (ValueError, TypeError):
            pass

    return xclient.get_thread(
        conversation_id=conv_id,
        op_handle=op_handle,
        bookmark_age_days=bookmark_age_days,
    )


def _write_one_card(
    *,
    bookmark: Dict[str, Any],
    thread: ThreadFetchResult,
    corpus: Path,
    run_id: str,
    now: datetime,
    checkpoint: CheckpointFile,
) -> CardWriteResult:
    """Acquire card_write briefly, S-7 dedup recheck, write card + checkpoint."""
    sid = str(bookmark.get("id", ""))
    with with_card_write_lock(corpus, "xsync") as h:
        # S-7 fix: re-check existence under lock before write
        if source_id_exists_under_lock(sid, corpus_path=corpus):
            raise _IdempotentSkip(sid)
        result = write_one(
            bookmark=bookmark, thread=thread, corpus_path=corpus,
            lock_token=h.token, run_id=run_id, captured=now,
        )
    # Append checkpoint AFTER successful write but OUTSIDE the lock
    # (checkpoint writes are atomic via O_APPEND; no lock needed).
    checkpoint.append(CheckpointRecord(
        source_id=sid,
        captured_at=now.isoformat(),
        mode="xsync",
        run_id=run_id,
    ))
    return result


def _trigger_reindex(corpus: Path) -> bool:
    """Acquire index_rebuild lock and run QMD update. Returns True on success."""
    try:
        with with_index_rebuild_lock(corpus, "xsync", heartbeat=True):
            # Lazy import — qmd module pulls in async deps
            import asyncio
            from xsensai.retrieval import qmd
            asyncio.run(qmd.update())
        return True
    except XSensaiError as e:
        log.warning("Reindex failed: %s", e.format())
        return False
    except Exception as e:
        log.warning("Reindex unhandled: %s", e)
        return False


def _search_all_unavailable_envelope() -> XSensaiInfo:
    return XSensaiInfo(
        code="SEARCH_ALL_UNAVAILABLE",
        cause="Full Archive search (search_all) returned 403 — your X API tier doesn't include it.",
        action_or_note=(
            "Threads for bookmarks >7 days old can't be back-filled with your current "
            "tier. The cards are still saved with the bookmarked tweet's text."
        ),
        source="sync.client.XClient.get_thread() fallback path",
    )


def _failed_result(run_id: str, e: XSensaiError, *, duration_ms: int) -> RunResult:
    return RunResult(
        run_id=run_id,
        status="failed",
        extraction_strategy="none",
        rendered_message=e.format(),
        duration_ms=duration_ms,
    )


def _run_preview(
    *,
    token_provider: Any,
    client_id: str,
    corpus: Path,
    run_id: str,
    max_pages: int,
    started: float,
) -> RunResult:
    """Preview mode: fetch the bookmark list (cheap owned-read) but write nothing."""
    on_disk = existing_source_ids(corpus_path=corpus)
    try:
        xclient = XClient(token_provider=token_provider, client_id=client_id)
        bookmarks = _gather_bookmarks(
            xclient, mode="backlog", target_tweet_id=None,
            on_disk=on_disk, max_pages=max_pages,
        )
    except XSensaiError as e:
        return _failed_result(run_id, e, duration_ms=int((time.monotonic() - started) * 1000))

    preview_list = [
        {
            "source_id": str(b.get("id", "")),
            "author": (b.get("_author") or {}).get("username", "?"),
            "created_at": str(b.get("created_at", "")),
            "text_preview": (b.get("text") or "")[:120],
        }
        for b in bookmarks
    ]
    info = XSensaiInfo(
        code="SYNC_DONE",
        cause=f"Preview: {len(preview_list)} bookmark(s) would be fetched. NOTHING WRITTEN.",
        action_or_note=(
            "Re-run `/xsync` (without `preview`) to actually fetch + write. "
            "If you want to skip specific bookmarks permanently, add their source_ids "
            "to `_skip-list.txt` in your corpus directory."
        ),
        source="sync.service.run(mode=preview)",
    )
    return RunResult(
        run_id=run_id,
        status="preview",
        extraction_strategy="none",
        cards_written=preview_list,  # repurposed as preview list
        rendered_message=info.format(),
        duration_ms=int((time.monotonic() - started) * 1000),
    )


__all__ = [
    "run",
    "apply_extraction",
    "finalize_run",
    "extract_pending",
    "RunResult",
    "ApplyExtractionResult",
    "FinalizeResult",
    "SyncMode",
    "ExtractionStrategy",
    "SMART_DEFAULT_INLINE_MAX",
]


# ---------------------------------------------------------------------------
# CLI — invoked from the /xsync and /xextract slash command markdown.
# Emits JSON to stdout. The slash command parses the result and either
# routes to host-Claude extraction (for inline strategy) or proceeds to
# finalize.
# ---------------------------------------------------------------------------


def _cli_make_provider() -> Any:
    """Build the default token provider — Keychain for manual /xsync."""
    from xsensai.sync.auth import KeychainTokenProvider
    return KeychainTokenProvider()


def _cli_emit_json(result: Any) -> None:
    """Serialize a dataclass result to stdout as JSON."""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(result):
        d = asdict(result)
    else:
        d = dict(result)
    # Path objects in nested dicts → str
    def _norm(v: Any) -> Any:
        if hasattr(v, "as_posix"):
            return str(v)
        if isinstance(v, dict):
            return {k: _norm(vv) for k, vv in v.items()}
        if isinstance(v, list):
            return [_norm(x) for x in v]
        return v
    print(json.dumps(_norm(d), ensure_ascii=False))


def _cli_run(args: Any) -> int:
    from xsensai.sync.auth import get_stored_client_id

    # Resolution order: --client-id flag > XSENSAI_X_CLIENT_ID env > Keychain.
    # The Keychain fallback is what makes /xsync "just work" from a fresh
    # Claude Code session — that process doesn't inherit env vars from the
    # terminal where you ran setup_oauth.
    client_id = args.client_id or get_stored_client_id()
    if not client_id:
        err = XSensaiError(
            code="OAUTH_CLIENT_ID_MISSING",
            cause="X dev app client_id is required.",
            attempted="sync.service run",
            next_action=(
                "Run `python -m xsensai.sync.setup_oauth` (it stores the "
                "client_id in macOS Keychain). Or pass --client-id explicitly. "
                "Or export XSENSAI_X_CLIENT_ID in your environment."
            ),
            retryable=True,
        )
        print(json.dumps({"status": "failed", "rendered_message": err.format()}))
        return 1

    provider = _cli_make_provider()
    try:
        result = run(
            mode=args.mode,
            token_provider=provider,
            client_id=client_id,
            target_tweet_id=args.target,
            inline_override=args.inline,
            defer_override=args.defer,
            max_pages=args.max_pages,
        )
    except XSensaiError as e:
        print(json.dumps({"status": "failed", "rendered_message": e.format()}))
        return 1
    _cli_emit_json(result)
    return 0


def _cli_apply_extraction(args: Any) -> int:
    tags = [t.strip() for t in (args.tags or "").split(",") if t.strip()]
    result = apply_extraction(
        card_id=args.card_id,
        summary=args.summary,
        tags=tags,
        run_id=args.run_id,
    )
    _cli_emit_json(result)
    return 0 if result.ok else 1


def _cli_finalize(args: Any) -> int:
    result = finalize_run(
        run_id=args.run_id,
        success=args.success,
        n_new_cards=args.new_cards,
        extraction_inline=args.inline_count,
        extraction_pending=args.pending_count,
        threads_unfetched_this_run=args.threads_unfetched,
        last_error=args.last_error,
        duration_ms=args.duration_ms,
        mode=args.mode,
        skip_reindex=args.skip_reindex,
    )
    _cli_emit_json(result)
    return 0


def _cli_extract_pending(args: Any) -> int:
    result = extract_pending(
        mode=args.mode,
        target_card_id=args.target_card_id,
        limit=args.limit,
    )
    _cli_emit_json(result)
    return 0


def _cli() -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m xsensai.sync.service")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Fetch + write step (xsync entrypoint)")
    p_run.add_argument(
        "--mode",
        choices=["since-last-run", "backlog", "single", "retry-failed", "preview"],
        default="since-last-run",
    )
    p_run.add_argument("--target", default=None,
                       help="Tweet id (single mode)")
    p_run.add_argument("--client-id", default=None)
    p_run.add_argument("--inline", action="store_true",
                       help="Force inline extraction regardless of N")
    p_run.add_argument("--defer", action="store_true",
                       help="Force deferred extraction regardless of N")
    p_run.add_argument("--max-pages", type=int, default=None)
    p_run.set_defaults(fn=_cli_run)

    p_apply = sub.add_parser("apply-extraction", help="Update one card's extraction")
    p_apply.add_argument("--card-id", required=True)
    p_apply.add_argument("--summary", required=True)
    p_apply.add_argument("--tags", default="",
                         help="Comma-separated tag list")
    p_apply.add_argument("--run-id", required=True)
    p_apply.set_defaults(fn=_cli_apply_extraction)

    p_fin = sub.add_parser("finalize", help="Heartbeat + checkpoint archive + reindex")
    p_fin.add_argument("--run-id", required=True)
    # F7 fix: use BooleanOptionalAction so the slash command can pass
    # `--no-success` on the failure/partial path. Default is True for
    # back-compat (most existing automation calls finalize after success).
    p_fin.add_argument(
        "--success",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="--success (default) marks the run successful. --no-success increments consecutive_failures.",
    )
    p_fin.add_argument("--new-cards", type=int, default=0)
    p_fin.add_argument("--inline-count", type=int, default=0)
    p_fin.add_argument("--pending-count", type=int, default=0)
    p_fin.add_argument("--threads-unfetched", type=int, default=0)
    p_fin.add_argument("--last-error", default=None)
    p_fin.add_argument("--duration-ms", type=int, default=0)
    p_fin.add_argument("--mode", default="since-last-run")
    p_fin.add_argument("--skip-reindex", action="store_true")
    p_fin.set_defaults(fn=_cli_finalize)

    p_ext = sub.add_parser("extract-pending", help="/xextract entrypoint")
    p_ext.add_argument("--mode", choices=["backlog", "single", "retry-failed"], default="backlog")
    p_ext.add_argument("--target-card-id", default=None)
    p_ext.add_argument("--limit", type=int, default=None)
    p_ext.set_defaults(fn=_cli_extract_pending)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(_cli())
