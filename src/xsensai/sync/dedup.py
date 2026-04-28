"""Dedup — set of source_ids already on disk.

Per /autoplan E-3 + S-7 fixes:
  - existing_source_ids() walks the corpus once before /xsync starts (cheap
    pre-flight check).
  - source_id_exists_under_lock() re-checks just before write (defense-in-depth
    against concurrent /xsync runs that both pass the precomputed dedup set).

Reads from frontmatter `source_id` field (v2 cards) and falls back to filename
regex extraction for v1 dialect cards whose filename embeds the tweet id but
whose frontmatter doesn't have a `source_id` key.

Slice 6 — tombstone-aware companion `existing_source_ids_with_tombstones()`
returns both the on-disk source_id set AND a separate `Dict[str, bool]`
mapping each source_id to whether its card is tombstoned. This lets cron
honor "skip + respect deletion" without breaking the existing `Set[str]`
contract callers (service.py:620, 643) depend on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

from xsensai.storage import corpus as corpus_mod
from xsensai.storage.corpus import iter_cards_metadata, resolve_corpus_path


# v1 dialect filenames embed the tweet id. Examples seen in the user's vault:
#   2026-03-01_2028162355511583052_17-best-practices-claude-cowork.md
#   2026-03-02_2028299099062124584_tolis-souls-cli.md
# Pattern: YYYY-MM-DD[_-]<digits>... (the id is the longest run of digits).
_V1_FILENAME_TWEET_ID_RE = re.compile(r"(\d{15,25})")


def existing_source_ids(corpus_path: Optional[Path] = None) -> Set[str]:
    """Return the set of all source_ids currently on disk in the corpus.

    Used by /xsync at startup to filter the X API result list before any
    write attempt — avoids per-card disk lookups during the hot loop.

    Defensive: unions two sources to catch malformed-card edge cases:
      (a) cards that PARSE successfully — read fm.source_id
      (b) filenames whose name embeds a tweet id, even if the card itself
          failed validation. Better to skip-fetch a malformed card's
          source_id than to write a duplicate next to a broken one.

    Slice 6: this still returns plain `Set[str]` for backward compat —
    `service.py:620, 643` callers do `if sid in on_disk` and would break
    on a tuple/dict return. Use `existing_source_ids_with_tombstones()`
    if you also need tombstone state.
    """
    sids, _tombstoned = existing_source_ids_with_tombstones(corpus_path)
    return sids


def existing_source_ids_with_tombstones(
    corpus_path: Optional[Path] = None,
) -> Tuple[Set[str], Dict[str, bool]]:
    """Same as `existing_source_ids` but also returns tombstone state.

    Returns (sids, tombstoned_by_source_id) where:
      - sids: Set[str] of every source_id present in the corpus (live OR deleted)
      - tombstoned_by_source_id: Dict[str, bool] mapping each source_id to
        whether the corresponding card has `deleted: true`. v1 dialect
        filename-only entries (no parseable card) get False (treated as live).

    Used by `card_writer.write_one()` to honor sticky deletion: if the
    cron sees a bookmark whose source_id is in `sids` AND
    `tombstoned_by_source_id[sid]` is True, skip-not-write (respect
    user's prior delete on Mac).
    """
    sids: Set[str] = set()
    tombstoned: Dict[str, bool] = {}
    # Source (a): parseable cards. Pass include_deleted=True so tombstones
    # are visible — that's the whole point of this helper.
    for card in iter_cards_metadata(corpus_path, include_deleted=True):
        sid = _extract_source_id(card.fm.source_id, card.md_path)
        if sid:
            sids.add(sid)
            # If multiple cards share a source_id (shouldn't happen post-Slice-1
            # dedup, but defense-in-depth), prefer-True so deletion is sticky
            # if any copy is tombstoned.
            tombstoned[sid] = tombstoned.get(sid, False) or card.fm.deleted
    # Source (b): filename-embedded tweet ids (catches malformed v1 dialect
    # cards whose YAML doesn't parse but whose filename still embeds an id).
    # These are necessarily NOT-tombstoned (tombstone needs a parseable card).
    corpus = corpus_mod.resolve_corpus_path(corpus_path)
    for md_path in corpus.glob("*.md"):
        if md_path.name.startswith("_") or md_path.name in {"CLAUDE.md", "README.md"}:
            continue
        m = _V1_FILENAME_TWEET_ID_RE.search(md_path.name)
        if m:
            sid = m.group(1)
            sids.add(sid)
            tombstoned.setdefault(sid, False)
    return sids, tombstoned


def source_id_exists_under_lock(
    source_id: str,
    corpus_path: Optional[Path] = None,
) -> bool:
    """Re-check existence at write time. Caller MUST hold card_write lock.

    Defense-in-depth (S-7 fix) against concurrent /xsync runs that both
    pass the precomputed dedup set.
    """
    corpus = resolve_corpus_path(corpus_path)
    # Two cheap shapes:
    #   (a) v2 filename pattern: YYYY-MM-DD-{author}-{source_id}.md
    #   (b) any .md whose frontmatter has source_id == source_id
    # We check (a) first since it's a stat() call.
    for child in corpus.glob(f"*-{source_id}.md"):
        return True
    for child in corpus.glob(f"*_{source_id}_*.md"):
        return True
    # Fallback: scan frontmatter (slower; only if filename probe missed).
    for card in iter_cards_metadata(corpus):
        sid = _extract_source_id(card.fm.source_id, card.md_path)
        if sid == source_id:
            return True
    return False


def _extract_source_id(fm_source_id: Optional[str], md_path: Path) -> Optional[str]:
    """v2 cards have fm.source_id set. v1 dialect cards don't, but their
    filename embeds the tweet id — extract via regex as fallback."""
    if fm_source_id:
        return fm_source_id
    name = md_path.name
    m = _V1_FILENAME_TWEET_ID_RE.search(name)
    if m:
        return m.group(1)
    return None


__all__ = [
    "existing_source_ids",
    "existing_source_ids_with_tombstones",
    "source_id_exists_under_lock",
]
