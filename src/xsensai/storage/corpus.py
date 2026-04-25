"""Corpus iteration — read v2 cards (with sidecar verification) + v1 cards (via adapter).

iter_cards() walks a corpus directory, yielding LoadedCard objects. Skips
malformed cards with a stderr log. Defends against duplicate source_id by
yielding only the first occurrence.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import Iterator, Optional

import frontmatter

from xsensai.errors import XSensaiError
from xsensai.model.card import CardFrontmatter, LoadedCard
from xsensai.storage import sidecar
from xsensai.storage import v1_adapter


log = logging.getLogger(__name__)


DEFAULT_CORPUS_PATH = "/Users/naveedhedayati/Documents/Vault/04_areas/x-bookmarks"


def get_corpus_path() -> Path:
    """Resolve the corpus path from $XSENSAI_CORPUS_PATH or default."""
    return Path(os.environ.get("XSENSAI_CORPUS_PATH", DEFAULT_CORPUS_PATH))


def resolve_corpus_path(corpus_path: Optional[Path] = None) -> Path:
    """Resolve and validate that the corpus path exists.

    Raises XSensaiError(CORPUS_UNAVAILABLE) if the path doesn't exist or
    isn't a directory. Distinguishes 'broken corpus' from 'empty corpus'.
    """
    p = corpus_path if corpus_path is not None else get_corpus_path()
    try:
        resolved = p.resolve(strict=True)
    except (FileNotFoundError, OSError) as e:
        raise XSensaiError(
            code="CORPUS_UNAVAILABLE",
            cause=f"Corpus path does not exist or is not accessible: {p}",
            attempted=f"resolve_corpus_path({p})",
            next_action=(
                "Set $XSENSAI_CORPUS_PATH to your bookmark vault directory, "
                "or run scripts/bootstrap_qmd.sh to set up a fresh corpus."
            ),
            retryable=False,
            details=str(e),
        ) from e
    if not resolved.is_dir():
        raise XSensaiError(
            code="CORPUS_UNAVAILABLE",
            cause=f"Corpus path exists but is not a directory: {resolved}",
            attempted=f"resolve_corpus_path({p})",
            next_action="Point $XSENSAI_CORPUS_PATH at a directory of *.md cards.",
            retryable=False,
        )
    return resolved


def load_card(md_path: Path, corpus_root: Optional[Path] = None) -> LoadedCard:
    """Load a single card. Handles v2 (with sidecar) and v1 (via adapter)."""
    try:
        post = frontmatter.load(md_path)
    except Exception as e:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"Frontmatter parse failed: {md_path}",
            attempted=f"frontmatter.load({md_path})",
            next_action="Open the card and check the YAML at the top is well-formed.",
            retryable=False,
            details=str(e),
        ) from e

    fm_dict = dict(post.metadata)
    body = post.content

    if v1_adapter.is_v1_shape(fm_dict):
        return v1_adapter.adapt_v1(md_path, fm_dict, body)

    try:
        cf = CardFrontmatter.model_validate(fm_dict)
    except Exception as e:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"Frontmatter validation failed: {md_path}",
            attempted=f"CardFrontmatter.model_validate({md_path})",
            next_action="Fix the frontmatter to match the v2 schema; see CLAUDE.md.",
            retryable=False,
            details=str(e),
        ) from e

    raw_path_str = cf.raw_path
    if raw_path_str is None:
        raise XSensaiError(
            code="YAML_PARSE_FAILED",
            cause=f"v2 card missing raw_path: {md_path}",
            attempted=f"load_card({md_path})",
            next_action="Add raw_path to the frontmatter or remove raw_checksum to fall back to v1 adapter.",
            retryable=False,
        )

    raw_path = (md_path.parent / raw_path_str).resolve()
    if corpus_root is not None:
        try:
            raw_path.relative_to(corpus_root.resolve())
        except ValueError as e:
            raise XSensaiError(
                code="DISK_WRITE_FAILED",
                cause=f"Sidecar path escapes corpus root: {raw_path}",
                attempted=f"load_card({md_path})",
                next_action=(
                    "raw_path must stay inside the corpus directory. "
                    "Restore from git or fix the frontmatter."
                ),
                retryable=False,
                details=f"corpus_root={corpus_root}, resolved={raw_path}",
            ) from e
    raw_bytes, computed_checksum = sidecar.read_sidecar(raw_path)

    if cf.raw_checksum and computed_checksum != cf.raw_checksum:
        raise XSensaiError(
            code="DISK_WRITE_FAILED",
            cause=f"Sidecar checksum mismatch: {raw_path}",
            attempted=f"load_card({md_path})",
            next_action=(
                "Card sidecar bytes do not match recorded checksum. "
                "The sidecar may have been edited by hand or corrupted; restore from git."
            ),
            retryable=False,
            details=f"expected={cf.raw_checksum}, got={computed_checksum}",
        )

    return LoadedCard(fm=cf, body=body, raw_bytes=raw_bytes, md_path=md_path)


def iter_cards(corpus_path: Optional[Path] = None) -> Iterator[LoadedCard]:
    """Iterate every card in the corpus directory (excluding _* metadata files).

    Skips malformed cards with a stderr WARNING and continues. Defends
    against duplicate source_id by logging and skipping subsequent occurrences.

    Raises XSensaiError(CORPUS_UNAVAILABLE) if the corpus path is missing or invalid.
    """
    corpus = resolve_corpus_path(corpus_path)
    seen_source_ids: set[str] = set()

    md_files = sorted(p for p in corpus.glob("*.md") if not p.name.startswith("_"))
    for md_path in md_files:
        try:
            card = load_card(md_path, corpus_root=corpus)
        except XSensaiError as e:
            log.warning("skipping card %s: [%s] %s", md_path.name, e.code, e.cause)
            continue
        sid = card.fm.source_id
        if sid:
            if sid in seen_source_ids:
                log.warning(
                    "skipping duplicate source_id %r in %s", sid, md_path.name
                )
                continue
            seen_source_ids.add(sid)
        yield card


def load_card_by_id(card_id: str, corpus_path: Optional[Path] = None) -> LoadedCard:
    """Look up a card by its id (filename without .md). Raises NO_RESULTS if missing."""
    corpus = resolve_corpus_path(corpus_path)
    md_path = corpus / f"{card_id}.md"
    if not md_path.exists():
        raise XSensaiError(
            code="NO_RESULTS",
            cause=f"No card with id {card_id!r}",
            attempted=f"load_card_by_id({card_id!r})",
            next_action="Check the id (filename without .md) returned by search_bookmarks.",
            retryable=False,
        )
    return load_card(md_path, corpus_root=corpus)


__all__ = [
    "DEFAULT_CORPUS_PATH",
    "get_corpus_path",
    "resolve_corpus_path",
    "load_card",
    "load_card_by_id",
    "iter_cards",
]
