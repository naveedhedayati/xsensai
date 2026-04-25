"""v1 read adapter — synthesize LoadedCard from existing v1-shape cards.

UC1 (autoplan Final Gate D4): pulled into Slice 1 so /xfind works against
the user's real ~25 v1 bookmarks on day one. This is read-only — no
write-back. Slice 6 ships proper migration with sidecars + LLM extraction;
this adapter is deleted then.

The real v1 schema uses x_* prefixed fields (not v2's flat names):
  type: x-bookmark         → source_type: bookmark
  x_post_id                → source_id
  x_author                 → author
  x_date                   → date
  x_source_url             → source       (X permalink)
  x_tags                   → tags
  x_extraction_status      → extraction_pending (mapped: success → False)
  x_has_video / _images    → media.has_video / has_images
  x_linked_urls            → media.external_urls
  created                  → captured

We detect v1 by: missing raw_path AND missing raw_checksum. Datetimes are
coerced to tz-aware UTC (v1 used naive YAML dates). raw_bytes is the body
bytes (best-effort verbatim until Slice 6 lands proper sidecars).
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from xsensai.errors import XSensaiError
from xsensai.model.card import CardFrontmatter, LoadedCard


V1_SENTINEL_PATH = "<v1-adapted>"


def is_v1_shape(frontmatter: Dict[str, Any]) -> bool:
    """v1-shape requires BOTH (a) no v2 sidecar fields AND (b) a positive
    bookmark/paste signal. Without (b), arbitrary `.md` files in the corpus
    directory (e.g., the vault's CLAUDE.md, README.md, design docs) would
    be silently adopted as paste cards.

    The vault has multiple v1 dialects (caught in /qa against the real
    corpus). Accept any of:
      - `type == "x-bookmark"` (real v1 schema marker)
      - `x_post_id` present (real v1 schema)
      - `source_id` + `author` present (v1.5 / hybrid)
      - `source` (X URL) + `author` present (minimal v1 dialect — most common
        in the real vault, no x_* fields)
    """
    has_v2_sidecar = "raw_path" in frontmatter or "raw_checksum" in frontmatter
    if has_v2_sidecar:
        return False
    has_v1_signal = (
        frontmatter.get("type") == "x-bookmark"
        or "x_post_id" in frontmatter
        or ("source_id" in frontmatter and "author" in frontmatter)
        or ("source" in frontmatter and "author" in frontmatter)
    )
    return has_v1_signal


def _map_v1_to_v2(v1_fm: Dict[str, Any], md_path: Path) -> Dict[str, Any]:
    """Translate the real v1 schema (x_* prefixed) to v2 field names.

    Tolerant of missing fields: bookmark cards need source_id + author;
    if those are absent, fall back to derived defaults. Pastes (no x_post_id
    AND not type=x-bookmark) get author="self".
    """
    out: Dict[str, Any] = {}

    v1_type = v1_fm.get("type")
    has_post_id = "x_post_id" in v1_fm or "source_id" in v1_fm
    has_source_pair = "source" in v1_fm and "author" in v1_fm
    is_bookmark = v1_type == "x-bookmark" or has_post_id or has_source_pair

    # Manual notes (caught in /qa): cards with x_post_id="manual_..." or
    # x_type="note" or empty x_source_url are not real X bookmarks. Route
    # them to paste so the bookmark validator doesn't reject empty source.
    if is_bookmark:
        post_id_str = str(v1_fm.get("x_post_id", ""))
        x_type = v1_fm.get("x_type")
        x_src = v1_fm.get("x_source_url")
        if (
            post_id_str.startswith("manual_")
            or x_type == "note"
            or (x_src is not None and not str(x_src).strip())
        ):
            is_bookmark = False

    out["source_type"] = "bookmark" if is_bookmark else "paste"

    # captured (v2 required) — fallback: x_date → created → updated → now
    captured = (
        v1_fm.get("captured")
        or v1_fm.get("created")
        or v1_fm.get("x_date")
        or v1_fm.get("updated")
    )
    # captured is required; if absent, use file mtime via caller. Here we
    # parse what we have — bad values raise rather than silently NOWing
    # (which would float the broken card to the top of every recency-weighted
    # query forever).
    if captured is None:
        out["captured"] = datetime.now(timezone.utc)
    else:
        out["captured"] = _strict_datetime(captured)

    if is_bookmark:
        post_id = v1_fm.get("x_post_id") or v1_fm.get("source_id")
        # YAML floats are a footgun for X tweet IDs (which can exceed 2^53).
        # Refuse them — caller raises YAML_PARSE_FAILED. Quoted strings only.
        if isinstance(post_id, float):
            raise ValueError(
                f"x_post_id parsed as float ({post_id!r}); precision lost. "
                "Quote the value as a string in the v1 card frontmatter."
            )
        out["source_id"] = str(post_id) if post_id else md_path.stem
        # author may be int/list/None depending on YAML; coerce defensively.
        author = v1_fm.get("x_author") or v1_fm.get("author") or "v1-unknown"
        if not isinstance(author, str):
            author = str(author)
        out["author"] = author if author.startswith("@") or author == "self" else f"@{author}"
        src = v1_fm.get("x_source_url") or v1_fm.get("source") or ""
        out["source"] = str(src)
        if "x_date" in v1_fm or "date" in v1_fm:
            out["date"] = _strict_datetime(v1_fm.get("x_date") or v1_fm.get("date"))
        out["source_status"] = "live"
    else:
        out["author"] = "self"
        # Source URL: paste-branch can come from v1's source_url, manual-note's
        # x_source_url, or fall through to None.
        src_url = v1_fm.get("source_url") or v1_fm.get("x_source_url")
        if src_url and str(src_url).strip():
            out["source_url"] = str(src_url)
        if "x_date" in v1_fm or "date" in v1_fm:
            out["date"] = _strict_datetime(v1_fm.get("x_date") or v1_fm.get("date"))

    # tags (v1 used x_tags or tags)
    tags = v1_fm.get("x_tags") or v1_fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    out["tags"] = list(tags)

    # extraction_pending: v1's x_extraction_status="success" means done
    ext_status = v1_fm.get("x_extraction_status")
    out["extraction_pending"] = ext_status not in ("success", "complete")

    # why_saved if present (v1 might have it raw)
    if "why_saved" in v1_fm:
        out["why_saved"] = v1_fm["why_saved"]
    if "pinned" in v1_fm:
        out["pinned"] = bool(v1_fm["pinned"])

    return out


def adapt_v1(
    md_path: Path,
    frontmatter: Dict[str, Any],
    body: str,
) -> LoadedCard:
    """Synthesize a LoadedCard from a v1-shape card on disk.

    Read-only. Does NOT write anything. Returned LoadedCard is suitable for
    retrieval (QMD already indexed the .md body); raw_bytes is best-effort
    UTF-8 of the body (true byte-exact verbatim arrives with Slice 6).
    """
    mapped = _map_v1_to_v2(frontmatter, md_path)

    try:
        cf = CardFrontmatter.model_validate(mapped)
    except Exception as e:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"v1 card adapter could not validate frontmatter: {md_path}",
            attempted=f"adapt_v1({md_path})",
            next_action=(
                "Card has v1 shape (no sidecar). Adapter is best-effort. "
                "If the card is broken, fix the frontmatter manually or wait for Slice 6 migration."
            ),
            retryable=False,
            details=str(e),
        ) from e

    raw_bytes = body.encode("utf-8")

    return LoadedCard(
        fm=cf,
        body=body,
        raw_bytes=raw_bytes,
        md_path=md_path,
    )


def _strict_datetime(value: Any) -> datetime:
    """Parse a v1 datetime to tz-aware UTC. Raises ValueError on failure
    (caller wraps as YAML_PARSE_FAILED). Refuses to silently fall back to
    now() — that would push the broken card to the top of every recency
    query forever.
    """
    from datetime import date as _date
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, _date):
        # YAML bare date (e.g., "2026-03-06") parses as datetime.date
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except ValueError as e:
            raise ValueError(f"Could not parse datetime: {value!r}") from e
    raise ValueError(f"Unsupported datetime type: {type(value).__name__} ({value!r})")


__all__ = ["adapt_v1", "is_v1_shape", "V1_SENTINEL_PATH"]
