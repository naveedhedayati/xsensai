"""QMD subprocess wrapper — async, UTF-8-safe, JSON-shape-contract.

QMD CLI surface (verified by Slice 1 spike — see tests/fixtures/qmd_query_output.json):

    qmd search <query> --json -c <collection> -n <limit>
    → [{"docid": str, "score": number, "file": "qmd://<coll>/<rel>", "title": str, "snippet": str}]

We use BM25 mode (`qmd search`) which doesn't require model downloads. Vector
+ rerank (`qmd query`) is available but adds first-call download latency;
defer evaluating that until we have golden-eval data.

Async via asyncio.create_subprocess_exec so concurrent search_bookmarks calls
don't serialize at the MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from xsensai.errors import XSensaiError


log = logging.getLogger(__name__)

DEFAULT_QMD_PATH = "/Users/naveedhedayati/.bun/bin/qmd"
COLLECTION_NAME = "xsensai-cards"
QMD_TIMEOUT_SEC = 10.0
QMD_UPDATE_TIMEOUT_SEC = 30.0  # reindex can be slower than query


def get_qmd_path() -> str:
    return os.environ.get("XSENSAI_QMD_PATH", DEFAULT_QMD_PATH)


@dataclass(frozen=True)
class QMDHit:
    """One hit from `qmd search --json`."""

    docid: str
    score: float
    file_uri: str
    title: str
    snippet: str

    def resolve_path(self, corpus_root: Path) -> Path:
        """Convert qmd://<collection>/<relative> → absolute path under corpus_root.

        QMD normalizes filename `_` to `-` in its URIs, so the URI path may
        not exist on disk. Fallback: if the literal URI path is missing, try
        the same name with `-` reverted to `_`. Returns the URI-derived
        path even if it doesn't exist (load_card surfaces the error).
        """
        prefix = f"qmd://{COLLECTION_NAME}/"
        if not self.file_uri.startswith(prefix):
            raise XSensaiError(
                code="INTERNAL_ERROR",
                cause=f"Unexpected QMD file URI shape: {self.file_uri!r}",
                attempted=f"resolve_path({self.file_uri})",
                next_action=(
                    f"Expected URI starting with {prefix!r}. QMD output schema may have drifted; "
                    "re-capture tests/fixtures/qmd_query_output.json and update qmd.py."
                ),
                retryable=False,
            )
        rel = self.file_uri[len(prefix):]
        primary = corpus_root / rel
        if primary.exists():
            return primary
        # QMD dash-vs-underscore normalization fallback
        underscored = rel.replace("-", "_")
        candidate = corpus_root / underscored
        if candidate.exists():
            return candidate
        # Last-resort: scan corpus for a file matching when _/- treated as same.
        # Skip _-prefixed metadata files; require a single match (multiple matches
        # mean QMD's URI is ambiguous and we shouldn't guess).
        target_norm = rel.replace("-", "_")
        matches = [
            p for p in corpus_root.glob("*.md")
            if not p.name.startswith("_") and p.name.replace("-", "_") == target_norm
        ]
        if len(matches) == 1:
            return matches[0]
        return primary  # let load_card raise a clean error


def _parse_qmd_json(stdout_bytes: bytes) -> List[QMDHit]:
    """Parse qmd --json output. Tolerates UTF-8 decode errors and asserts shape.

    QMD writes the literal string 'No results found.' to stdout (not stderr)
    when there are zero matches, even with --json. We treat that as an empty
    list. Anything else that doesn't start with '[' is schema drift.
    """
    text = stdout_bytes.decode("utf-8", errors="replace").strip()
    # Tolerate variations: "No results found." (default), "No results found"
    # (no period in some versions), wrapped with ANSI prefix/suffix. The
    # canonical JSON output starts with '['; anything else with no '[' is
    # treated as empty.
    if not text:
        return []
    if "no results found" in text.lower() and "[" not in text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause="QMD returned non-JSON output",
            attempted="qmd search --json",
            next_action=(
                "QMD output schema may have changed. Re-run the spike: "
                "qmd search 'test' --json -c xsensai-cards | head, "
                "and update tests/fixtures/qmd_query_output.json."
            ),
            retryable=False,
            details=str(e),
        ) from e

    if not isinstance(data, list):
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause=f"QMD --json output is not a JSON list (got {type(data).__name__})",
            attempted="qmd search --json",
            next_action="QMD output schema drift; re-spike and update qmd.py parser.",
            retryable=False,
        )

    hits: List[QMDHit] = []
    for i, item in enumerate(data):
        if not isinstance(item, dict):
            raise XSensaiError(
                code="INTERNAL_ERROR",
                cause=f"QMD hit #{i} is not an object",
                attempted="parse qmd output",
                next_action="QMD schema drift; check tests/fixtures/qmd_query_output.json.",
                retryable=False,
            )
        try:
            hits.append(
                QMDHit(
                    docid=str(item["docid"]),
                    score=float(item["score"]),
                    file_uri=str(item["file"]),
                    title=str(item.get("title", "")),
                    snippet=str(item.get("snippet", "")),
                )
            )
        except (KeyError, ValueError, TypeError) as e:
            raise XSensaiError(
                code="INTERNAL_ERROR",
                cause=f"QMD hit #{i} missing/malformed fields: {e}",
                attempted="parse qmd output",
                next_action="QMD schema drift; check tests/fixtures/qmd_query_output.json.",
                retryable=False,
                details=str(item),
            ) from e
    return hits


async def query(text: str, limit: int = 20, qmd_path: Optional[str] = None) -> List[QMDHit]:
    """Run `qmd search <text> --json -c xsensai-cards -n <limit>` async.

    Returns a list of QMDHit. Empty list on no matches. Raises
    XSensaiError(INTERNAL_ERROR) on subprocess failure / timeout / schema drift.
    """
    bin_path = qmd_path or get_qmd_path()
    args = [
        bin_path, "search", text,
        "--json",
        "-c", COLLECTION_NAME,
        "-n", str(limit),
    ]
    log.info("qmd query: %r limit=%d", text, limit)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause=f"QMD binary not found at {bin_path}",
            attempted=f"exec {bin_path}",
            next_action=(
                "Install QMD via 'bun install -g qmd', or set $XSENSAI_QMD_PATH "
                "to your installed binary."
            ),
            retryable=False,
            details=str(e),
        ) from e

    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=QMD_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause=f"QMD query timed out after {QMD_TIMEOUT_SEC}s",
            attempted=f"qmd search {text!r}",
            next_action="Re-run /xfind; if it persists, check `qmd status` for index health.",
            retryable=True,
        )

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if "Collection" in err_text and "not found" in err_text:
            raise XSensaiError(
                code="CORPUS_UNAVAILABLE",
                cause=f"QMD collection {COLLECTION_NAME!r} does not exist",
                attempted=f"qmd search {text!r}",
                next_action=(
                    "Run scripts/bootstrap_qmd.sh to create the collection from "
                    "your $XSENSAI_CORPUS_PATH."
                ),
                retryable=False,
            )
        raise XSensaiError(
            code="INTERNAL_ERROR",
            cause=f"QMD exited with code {proc.returncode}",
            attempted=f"qmd search {text!r}",
            next_action="Check `qmd status`; re-run after fixing.",
            retryable=True,
            details=err_text[:500] if err_text else None,
        )

    return _parse_qmd_json(stdout)


async def update(qmd_path: Optional[str] = None) -> None:
    """Run `qmd update -c xsensai-cards` to reindex the collection.

    Used by engine.search()'s read-side reindex trigger when _index-dirty is
    set. Typical runtime is a few seconds for a small corpus. Best-effort:
    on failure, logs a warning but does NOT raise — search continues with
    stale index rather than failing the user's query.
    """
    bin_path = qmd_path or get_qmd_path()
    args = [bin_path, "update", "-c", COLLECTION_NAME]
    log.info("qmd update: collection=%s", COLLECTION_NAME)

    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        log.warning("qmd update: binary not found at %s (%s); index stale", bin_path, e)
        return

    try:
        _stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=QMD_UPDATE_TIMEOUT_SEC
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        log.warning(
            "qmd update timed out after %ss; index stale until next attempt",
            QMD_UPDATE_TIMEOUT_SEC,
        )
        return

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        log.warning(
            "qmd update exited with code %d; index stale. stderr: %s",
            proc.returncode,
            err_text[:300],
        )


__all__ = [
    "QMDHit",
    "COLLECTION_NAME",
    "DEFAULT_QMD_PATH",
    "QMD_TIMEOUT_SEC",
    "QMD_UPDATE_TIMEOUT_SEC",
    "get_qmd_path",
    "query",
    "update",
]
