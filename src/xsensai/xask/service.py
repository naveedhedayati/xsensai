"""/xask orchestration entrypoint — the load-bearing Python module.

The slash command (commands/xask.md) is THIN — it calls `prepare()` and
`log_run()` here. All semantics live in this module so they're testable
and bisectable.

Architecture (per Slice 3 plan v2.2):

    /xask in Claude Code
        │
        ▼
    commands/xask.md (markdown — prompt + 2 Python invocations)
        │
        ▼
    xsensai.xask.service.prepare(question, opts)
        │           ┌── engine.search()  (existing Slice 1 retrieval)
        ▼           │
    asyncio.gather <
        │           └── web_fork.run_last30days() (Slice 3 subprocess)
        │
        ▼
    deterministic re-rank → top-3 → branch-table assembly
        │
        ▼
    {status, synthesis_prompt, rendered_message, meta}
        │
        ▼
    host Claude session synthesizes against synthesis_prompt
        │
        ▼
    template.validate() → re-prompt once if invalid → emit
        │
        ▼
    xsensai.xask.service.log_run(question, output_sha256, meta)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from xsensai.errors import XSensaiError, XSensaiInfo
from xsensai.retrieval import engine
from xsensai.retrieval.format import format_reference
from xsensai.synthesis.template import HARD_RULES, OUTPUT_TEMPLATE
from xsensai.web_fork import run_last30days
from xsensai.xask import log as xask_log
from xsensai.xask.version import PROMPT_TEMPLATE_VERSION, SERVICE_VERSION

log = logging.getLogger(__name__)

# F1 fix (review): sanitize the literal closing-tag string in any retrieved
# content before embedding inside <DATA_TO_ANALYZE> wraps. A card body or web
# payload containing the literal close-tag would otherwise escape the trust
# boundary and let injected text masquerade as system instructions to the
# host Claude. We replace with a benign marker that's clearly NOT a tag close.
_DATA_OPEN_TAG = "<DATA_TO_ANALYZE>"
_DATA_CLOSE_TAG = "</DATA_TO_ANALYZE>"
_DATA_OPEN_SAFE = "(DATA_TAG_OPEN_LITERAL)"
_DATA_CLOSE_SAFE = "(DATA_TAG_CLOSE_LITERAL)"


def _sanitize_data(text: str) -> str:
    """Neutralize literal data-wrap tags inside untrusted content."""
    if not text:
        return text
    return text.replace(_DATA_CLOSE_TAG, _DATA_CLOSE_SAFE).replace(
        _DATA_OPEN_TAG, _DATA_OPEN_SAFE
    )


# Slice 3 plan: top-3 cards into synthesis. Re-rank pool stays at 20 (engine default).
TOP_N_FOR_SYNTHESIS = 3

# Cap each card body in the synthesis prompt so a long thread doesn't crowd
# out other cards. Spec section "Truncation strategy at scale" allows full
# bodies for top-3, but we still bound it to keep the prompt predictable.
CARD_BODY_MAX_CHARS = 8000
# Cap web payload embedded in the prompt — keeps cards dominant in context.
WEB_PAYLOAD_MAX_CHARS = 4000


# Stable tie-break (Eng EC4): (combined_score DESC, captured DESC, id ASC)
def _stable_sort_key(hit) -> Tuple[float, float, str]:
    captured = hit.card.fm.captured
    captured_ts = captured.timestamp() if captured is not None else 0.0
    return (-hit.combined_score, -captured_ts, hit.card.md_path.stem)


# Override vocabulary (DX3 + DX7 fix: self-documenting + fuzzy mapping)
_CANONICAL_TOKENS = {"no decay", "skip pins", "no web", "challenge"}
_FUZZY_MAP: Dict[str, str] = {
    # → no decay
    "no recency": "no decay",
    "recency off": "no decay",
    "recency": "no decay",
    "decay": "no decay",
    "no decay weighting": "no decay",
    # → skip pins
    "no pins": "skip pins",
    "pin off": "skip pins",
    "no pinned": "skip pins",
    "skip pinned": "skip pins",
    "pin": "skip pins",
    "pinned": "skip pins",
    # → no web
    "skip web": "no web",
    "web off": "no web",
    "no web search": "no web",
    "web": "no web",
    # → challenge
    "dissent": "challenge",
    "dissenting": "challenge",
    "dissenting cards": "challenge",
    "challenge cards": "challenge",
    "pushback": "challenge",
    "challenge mode": "challenge",
}

# Precompiled override patterns (P2 perf fix): build once at module load
# rather than per parse_overrides() call.
_CANONICAL_PATTERNS: List[Tuple[re.Pattern, str]] = [
    (re.compile(r"\b" + re.escape(t) + r"\b", re.IGNORECASE), t)
    for t in sorted(_CANONICAL_TOKENS, key=len, reverse=True)
]
_FUZZY_PATTERNS: List[Tuple[re.Pattern, str, str]] = [
    (re.compile(r"\b" + re.escape(fuzzy) + r"\b", re.IGNORECASE), fuzzy, canon)
    for fuzzy, canon in sorted(
        _FUZZY_MAP.items(), key=lambda kv: len(kv[0]), reverse=True
    )
]


@dataclass
class Overrides:
    no_decay: bool = False
    skip_pins: bool = False
    no_web: bool = False
    challenge: bool = False
    fuzzy_note: Optional[str] = None  # e.g. "I read 'dissent' as `challenge`"


def parse_overrides(question: str) -> Tuple[str, Overrides]:
    """Strip canonical override tokens from the question; honor fuzzy variants.

    Returns (clean_question, Overrides). The clean question is what gets passed
    to retrieval; Overrides drives behavior.
    """
    overrides = Overrides()
    text = question
    fuzzy_matches: List[Tuple[str, str]] = []

    # Canonical: strip exact tokens (case-insensitive, word-boundaried)
    for pattern, token in _CANONICAL_PATTERNS:
        if pattern.search(text):
            text = pattern.sub("", text)
            _set_override(overrides, token)

    # Fuzzy: detect (do NOT strip — user typed "decay" and probably means it
    # literally too); just note the canonical phrasing for the user
    if text.strip() == question.strip():  # no canonical hits — try fuzzy
        for pattern, fuzzy, canon in _FUZZY_PATTERNS:
            if pattern.search(text):
                fuzzy_matches.append((fuzzy, canon))
                # only consume the FIRST fuzzy match per canonical to avoid
                # weird double-counts; do not mutate text (per /xfind pattern)
                _set_override(overrides, canon)
                break

    if fuzzy_matches:
        fuzzy_phrase, canonical = fuzzy_matches[0]
        overrides.fuzzy_note = (
            f'Note: I read "{fuzzy_phrase}" as `{canonical}`. Running with that '
            f"override. To use defaults next time, omit the keyword; to "
            f"override on purpose, append exactly `no decay`, `skip pins`, "
            f"`no web`, or `challenge`."
        )

    return text.strip(), overrides


def _set_override(o: Overrides, canonical: str) -> None:
    if canonical == "no decay":
        o.no_decay = True
    elif canonical == "skip pins":
        o.skip_pins = True
    elif canonical == "no web":
        o.no_web = True
    elif canonical == "challenge":
        o.challenge = True


@dataclass
class PrepareResult:
    """Returned to the slash command. JSON-serializable via asdict()."""

    status: str  # "ok" | "info" | "error"
    rendered_message: Optional[str]  # for info/error: the formatted envelope
    synthesis_prompt: Optional[str]  # for ok: what host Claude synthesizes against
    web_attempted: bool
    challenge_used: bool
    challenge_status: Optional[str]
    meta: Dict[str, Any] = field(default_factory=dict)


async def prepare(
    question: str,
    *,
    no_decay: bool = False,
    skip_pins: bool = False,
    no_web: bool = False,
    challenge: bool = False,
) -> PrepareResult:
    """The orchestration entrypoint.

    Steps:
        1. Parse fuzzy overrides (CLI flags take precedence over inline tokens)
        2. Concurrent: retrieval (asyncio.to_thread) + web fork (asyncio task)
        3. Branch table: handle empty corpus / no_results / web outcomes
        4. Deterministic top-3 re-rank
        5. Optional challenge pass (sync after main retrieval)
        6. Assemble synthesis_prompt with HARD_RULES + DATA_TO_ANALYZE wrap
        7. Return PrepareResult (status="ok"|"info"|"error" + meta)
    """
    if not question or not question.strip():
        info = XSensaiInfo(
            code="NO_CORPUS_MATCH",
            cause="No question was provided.",
            action_or_note="ok, nothing to ask; pass.",
            source="/xask user input",
        )
        return PrepareResult(
            status="info",
            rendered_message=info.format(),
            synthesis_prompt=None,
            web_attempted=False,
            challenge_used=False,
            challenge_status=None,
            meta={"info_code": info.code},
        )

    # Cap question length to mirror last30days runner — keeps the qmd
    # subprocess and log line bounded against runaway inputs (e.g. a card body
    # echoed back via prompt-injection).
    if len(question) > 8192:
        err = XSensaiError(
            code="INTERNAL_ERROR",
            cause=f"Question too long ({len(question)} chars > 8192).",
            attempted="/xask retrieval",
            next_action="Shorten the question. Long context belongs in cards via /xpaste, not in queries.",
            retryable=True,
        )
        return PrepareResult(
            status="error",
            rendered_message=err.format(),
            synthesis_prompt=None,
            web_attempted=False,
            challenge_used=False,
            challenge_status=None,
            meta={"error_code": err.code},
        )

    clean_q, fuzzy_overrides = parse_overrides(question)
    # CLI flags override fuzzy detection if explicitly set true
    no_decay = no_decay or fuzzy_overrides.no_decay
    skip_pins = skip_pins or fuzzy_overrides.skip_pins
    no_web = no_web or fuzzy_overrides.no_web
    challenge = challenge or fuzzy_overrides.challenge
    web_attempted = not no_web

    # Step 2: real parallelism (Eng EC2)
    web_task = None
    if web_attempted:
        web_task = asyncio.create_task(run_last30days(clean_q))
    try:
        results = await engine.search(
            clean_q,
            limit=engine.CANDIDATE_LIMIT,
            no_decay=no_decay,
            include_pinned=not skip_pins,
        )
    except XSensaiError as e:
        # F4 fix: await the cancelled web_task so the subprocess doesn't
        # leak. CancelledError is expected; suppress it.
        if web_task is not None:
            web_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await web_task
        return PrepareResult(
            status="error",
            rendered_message=e.format(),
            synthesis_prompt=None,
            web_attempted=web_attempted,
            challenge_used=challenge,
            challenge_status="skipped" if challenge else None,
            meta={"error_code": e.code},
        )

    web_result: Dict[str, Any] = (
        {"status": "skipped", "reason": "user_opted_out"}
        if web_task is None
        else await web_task
    )

    # Step 3: branch table — empty corpus
    if results.corpus_card_count == 0:
        err = XSensaiError(
            code="EMPTY_CORPUS",
            cause="Your corpus is empty.",
            attempted="/xask retrieval",
            next_action=(
                "Run /xpaste to add cards, or set XSENSAI_CORPUS_PATH to the "
                "right vault directory."
            ),
            retryable=True,
        )
        return PrepareResult(
            status="error",
            rendered_message=err.format(),
            synthesis_prompt=None,
            web_attempted=web_attempted,
            challenge_used=challenge,
            challenge_status="skipped" if challenge else None,
            meta={"error_code": err.code},
        )

    if not results.hits:
        info = XSensaiInfo(
            code="NO_CORPUS_MATCH",
            cause=f"No cards in your corpus matched: {clean_q!r}.",
            action_or_note=(
                "Try /xfind <other terms> to explore, or rephrase the question."
            ),
            source="search_bookmarks (BM25 over corpus)",
        )
        return PrepareResult(
            status="info",
            rendered_message=info.format(),
            synthesis_prompt=None,
            web_attempted=web_attempted,
            challenge_used=challenge,
            challenge_status="skipped" if challenge else None,
            meta={"info_code": info.code},
        )

    # Step 4: deterministic top-3 (Eng EC4)
    sorted_hits = sorted(results.hits, key=_stable_sort_key)
    top = sorted_hits[:TOP_N_FOR_SYNTHESIS]
    top_ids = [h.card.md_path.stem for h in top]

    # Step 5: optional challenge pass (Eng EC5 dup branch)
    challenge_status: Optional[str] = "skipped"
    dissenter = None
    if challenge:
        # Cheap dissent pass: re-search with a stance-inverting prefix
        try:
            dissent_results = await engine.search(
                f"counter-argument or dissent against: {clean_q}",
                limit=engine.CANDIDATE_LIMIT,
                no_decay=no_decay,
                include_pinned=not skip_pins,
            )
        except XSensaiError as e:
            log.warning("challenge pass failed: %s", e.code)
            challenge_status = "failed"
            dissent_results = None

        if dissent_results and dissent_results.hits:
            dissent_sorted = sorted(dissent_results.hits, key=_stable_sort_key)
            for cand in dissent_sorted:
                if cand.card.md_path.stem not in top_ids:
                    dissenter = cand
                    challenge_status = "found"
                    break
            if dissenter is None:
                # Wire CHALLENGE_NO_DISSENT info code (T9 fix) for telemetry.
                challenge_status = "no_real_dissent"
                log.info(
                    "CHALLENGE_NO_DISSENT: dissent pass surfaced only top-3 dups for %r",
                    clean_q,
                )
        elif challenge_status != "failed":
            challenge_status = "no_real_dissent"
            log.info(
                "CHALLENGE_NO_DISSENT: dissent pass returned no candidates for %r",
                clean_q,
            )

    # Step 6: assemble synthesis_prompt with HARD_RULES + DATA_TO_ANALYZE
    synthesis_prompt = _assemble_prompt(
        question=clean_q,
        top=top,
        web_result=web_result if web_attempted else None,
        dissenter=dissenter,
        challenge_used=challenge,
        challenge_status=challenge_status,
        fuzzy_note=fuzzy_overrides.fuzzy_note,
    )

    web_status_for_meta = web_result.get("status", "skipped") if web_attempted else "skipped"

    return PrepareResult(
        status="ok",
        rendered_message=None,
        synthesis_prompt=synthesis_prompt,
        web_attempted=web_attempted,
        challenge_used=challenge,
        challenge_status=challenge_status,
        meta={
            "candidates_considered": len(results.hits),
            "rerank_winners": top_ids,
            "web_fork_status": web_status_for_meta,
            "fallback_fired": results.fallback_fired,
            "corpus_card_count": results.corpus_card_count,
            "fuzzy_note_emitted": fuzzy_overrides.fuzzy_note is not None,
        },
    )


def _assemble_prompt(
    *,
    question: str,
    top: List,
    web_result: Optional[Dict[str, Any]],
    dissenter,
    challenge_used: bool,
    challenge_status: Optional[str],
    fuzzy_note: Optional[str],
) -> str:
    """Build the synthesis prompt that the host Claude session will answer."""
    # Top-3 card data (full body — top-3 only per spec section 4 truncation).
    # Cap each body at CARD_BODY_MAX_CHARS so a long thread doesn't crowd out
    # the other cards in the host model's context window. Sanitize close-tags
    # (F1 fix) before embedding so untrusted content can't escape the wrap.
    cards_block_parts: List[str] = []
    for i, hit in enumerate(top, start=1):
        card = hit.card
        ref_line = _sanitize_data(format_reference(card))
        body = card.body or ""
        if len(body) > CARD_BODY_MAX_CHARS:
            body = body[:CARD_BODY_MAX_CHARS] + "\n... [truncated]"
        body = _sanitize_data(body)
        cards_block_parts.append(
            f"[{i}] id={card.md_path.stem}\n"
            f"reference: {ref_line}\n"
            f"body:\n{body}\n"
        )
    cards_block = "\n---\n".join(cards_block_parts)

    web_block = _render_web_block(web_result)

    dissenter_block = ""
    if dissenter is not None:
        d_ref = _sanitize_data(format_reference(dissenter.card))
        d_body = _sanitize_data(dissenter.card.body or "")
        dissenter_block = (
            "\n[DISSENTER]\n"
            f"id={dissenter.card.md_path.stem}\n"
            f"reference: {d_ref}\n"
            f"body:\n{d_body}\n"
        )

    fuzzy_line = ""
    if fuzzy_note:
        fuzzy_line = f"\n(Prepend this verbatim to your output: > {fuzzy_note})\n"

    return (
        f"Question: {question}\n\n"
        f"{HARD_RULES}\n\n"
        f"<DATA_TO_ANALYZE>\n"
        f"=== Top-3 cards ===\n{cards_block}\n"
        f"{dissenter_block}"
        f"=== Web context ===\n{web_block}\n"
        f"</DATA_TO_ANALYZE>\n\n"
        f"Output template (use EXACTLY these section headers, in this order):\n"
        f"{OUTPUT_TEMPLATE}\n"
        f"{fuzzy_line}"
    )


def _render_web_block(web_result: Optional[Dict[str, Any]]) -> str:
    """Web sub-section embedded inside the DATA_TO_ANALYZE wrap.

    The slash command will translate the web status into the right user-facing
    section (## Web this week vs ## (web context unavailable...)) — this just
    feeds the host Claude the raw payload + a hint about which section to emit.
    """
    if web_result is None:
        return "(no web; user passed `no web`)\n--instruction: do NOT emit any web section--"
    status = web_result.get("status")
    if status == "ok":
        payload_str = json.dumps(web_result.get("payload", {}), ensure_ascii=False)[
            :WEB_PAYLOAD_MAX_CHARS
        ]
        # F1 fix: sanitize close-tag in web payload before embedding
        payload_str = _sanitize_data(payload_str)
        return (
            f"status=ok\npayload_compact={payload_str}\n"
            "--instruction: emit `## Web this week` summarizing the payload above (2-3 sentences OR up to 5 bullets)--"
        )
    if status == "empty":
        info = XSensaiInfo(
            code="WEB_NO_FRESH",
            cause="last30days returned no fresh items this week.",
            action_or_note="(no action)",
            source="last30days subprocess",
        )
        return (
            f"status=empty\n{info.format()}\n"
            "--instruction: emit `## (web context: nothing fresh this week)` line--"
        )
    if status == "missed":
        info = XSensaiInfo(
            code="WEB_TIMEOUT",
            cause=f"last30days exceeded the soft deadline ({web_result.get('reason')}).",
            action_or_note="Re-run with `/xask <q> no web` if you don't want web context.",
            source="last30days subprocess",
        )
        return (
            f"status=missed\n{info.format()}\n"
            "--instruction: emit `## (web context unavailable this run — timeout)` line--"
        )
    if status == "skipped":
        # Distinct info code for "not installed" vs generic "no fresh items"
        # so downstream telemetry can tell them apart (M10/A10 fix).
        info = XSensaiInfo(
            code="WEB_NOT_INSTALLED",
            cause=f"last30days skipped: {web_result.get('reason')}.",
            action_or_note=(
                "Verify last30days install: `XSENSAI_LAST30DAYS_PATH=$(which last30days)`. "
                "Re-run with `no web` to skip explicitly."
            ),
            source="last30days subprocess",
        )
        return (
            f"status=skipped\n{info.format()}\n"
            "--instruction: emit `## (web context unavailable this run — last30days not available)` line--"
        )
    if status == "failed":
        reason = web_result.get("reason", "unknown")
        # Distinguish parse error vs other failures (DX5).
        # Other failures route through WEB_FORK_FAILED (the error code, used
        # only for telemetry — slash command still emits as a status line so
        # the user gets their corpus-only answer).
        if reason.startswith("parse_error"):
            info = XSensaiInfo(
                code="WEB_PARSE",
                cause=f"last30days output didn't parse as JSON: {reason}.",
                action_or_note=(
                    "Verify last30days install matches the expected output schema. "
                    "Re-run with `no web` to skip."
                ),
                source="last30days subprocess",
            )
            return (
                f"status=failed\n{info.format()}\n"
                f"--instruction: emit `## (web context unavailable this run — {reason})` line--"
            )
        # Generic subprocess failure: log via the error code so structured
        # log scanners can pick it up, but still surface as a status line.
        log.warning("WEB_FORK_FAILED: %s", reason)
        info = XSensaiInfo(
            code="WEB_NO_FRESH",
            cause=f"last30days failed: {reason}.",
            action_or_note=(
                "Verify last30days install. Re-run with `no web` to skip."
            ),
            source="last30days subprocess",
        )
        return (
            f"status=failed [WEB_FORK_FAILED telemetry]\n{info.format()}\n"
            f"--instruction: emit `## (web context unavailable this run — {reason})` line--"
        )
    return f"status={status} (unknown — fall through to web-unavailable line)"


def log_run(
    *,
    question: str,
    output_sha256: str,
    meta: Dict[str, Any],
    duration_ms: int,
    state: str = "completed",
) -> None:
    """Append one entry to the question log per privacy mode (DX4).

    F5 fix (review): supports `state="started"` so the slash command can
    log a sentinel BEFORE host synthesis runs — a crashed synthesis then
    still leaves a bisect record. Pair entries by q_hash + close ts.

    Wrapped in try/except — observability never breaks the user's /xask
    answer. If the log write fails (disk full, permissions, flock
    contention, etc.), log a warning to stderr and return.
    """
    try:
        xask_log.append_log(
            question=question,
            top3=list(meta.get("rerank_winners", [])),
            candidates=int(meta.get("candidates_considered", 0)),
            web=str(meta.get("web_fork_status", "unknown")),
            challenge_used=bool(meta.get("challenge_used", False)),
            challenge_status=meta.get("challenge_status"),
            output_sha256=output_sha256,
            prompt_template_version=PROMPT_TEMPLATE_VERSION,
            service_version=SERVICE_VERSION,
            duration_ms=duration_ms,
            state=state,
        )
    except Exception as e:  # noqa: BLE001 — observability must not crash callers
        log.warning("xask log write failed (non-fatal): %s", e)


def _cli() -> int:
    """`python -m xsensai.xask.service [prepare|log]` entrypoint.

    stdout = JSON envelope. stderr = logs. Never reverse — the slash command
    parses stdout as JSON. We force basicConfig on stderr (A6 fix) so any
    log calls in this module or its imports cannot accidentally corrupt the
    JSON output channel.
    """
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.WARNING,
        format="%(asctime)s [%(levelname)s] xsensai.xask.service: %(message)s",
        force=True,
    )
    p = argparse.ArgumentParser(prog="xsensai.xask.service")
    sub = p.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare")
    p_prep.add_argument("--question", required=True)
    p_prep.add_argument(
        "--no-decay", action=argparse.BooleanOptionalAction, default=False
    )
    p_prep.add_argument(
        "--skip-pins", action=argparse.BooleanOptionalAction, default=False
    )
    p_prep.add_argument(
        "--no-web", action=argparse.BooleanOptionalAction, default=False
    )
    p_prep.add_argument(
        "--challenge", action=argparse.BooleanOptionalAction, default=False
    )

    p_log = sub.add_parser("log")
    p_log.add_argument("--question", required=True)
    p_log.add_argument("--output-sha256", required=True)
    p_log.add_argument("--meta-json", required=True)
    p_log.add_argument("--duration-ms", type=int, default=0)
    p_log.add_argument(
        "--state",
        choices=["started", "completed"],
        default="completed",
        help="started=log before synthesis (bisect record for crashed flows); completed=log after",
    )

    args = p.parse_args()
    if args.cmd == "prepare":
        t0 = time.time()
        result = asyncio.run(
            prepare(
                args.question,
                no_decay=args.no_decay,
                skip_pins=args.skip_pins,
                no_web=args.no_web,
                challenge=args.challenge,
            )
        )
        # add duration to meta so the slash command can pass it back to log
        result.meta["duration_ms"] = int((time.time() - t0) * 1000)
        result.meta["challenge_used"] = args.challenge
        print(json.dumps(asdict(result), ensure_ascii=False))
        return 0
    if args.cmd == "log":
        meta = json.loads(args.meta_json)
        log_run(
            question=args.question,
            output_sha256=args.output_sha256,
            meta=meta,
            duration_ms=args.duration_ms,
            state=args.state,
        )
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(_cli())


__all__ = [
    "Overrides",
    "PrepareResult",
    "parse_overrides",
    "prepare",
    "log_run",
    "TOP_N_FOR_SYNTHESIS",
]
