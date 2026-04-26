"""Dedup — set of source_ids already on disk.

Per /autoplan E-3 + S-7 fixes:
  - existing_source_ids() walks the corpus once before /xsync starts (cheap
    pre-flight check).
  - source_id_exists_under_lock() re-checks just before write (defense-in-depth
    against concurrent /xsync runs that both pass the precomputed dedup set).

Reads from frontmatter `source_id` field (v2 cards) and falls back to filename
regex extraction for v1 dialect cards whose filename embeds the tweet id but
whose frontmatter doesn't have a `source_id` key.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Set

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
    """
    out: Set[str] = set()
    # Source (a): parseable cards
    for card in iter_cards_metadata(corpus_path):
        sid = _extract_source_id(card.fm.source_id, card.md_path)
        if sid:
            out.add(sid)
    # Source (b): filename-embedded tweet ids (catches malformed v1 dialect
    # cards whose YAML doesn't parse but whose filename still embeds an id)
    corpus = corpus_mod.resolve_corpus_path(corpus_path)
    for md_path in corpus.glob("*.md"):
        if md_path.name.startswith("_") or md_path.name in {"CLAUDE.md", "README.md"}:
            continue
        m = _V1_FILENAME_TWEET_ID_RE.search(md_path.name)
        if m:
            out.add(m.group(1))
    return out


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


__all__ = ["existing_source_ids", "source_id_exists_under_lock"]
