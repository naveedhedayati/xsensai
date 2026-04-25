"""[B]/[P] reference block formatter.

Renders LoadedCard objects to the spec's reference format. Truncation is
grapheme-cluster-aware (regex \\X) so emoji ZWJ sequences and combining
marks aren't split mid-character.
"""

from __future__ import annotations

from urllib.parse import urlparse

import regex as _re

from xsensai.model.card import LoadedCard


SNIPPET_LEN = 80


def truncate_graphemes(text: str, limit: int = SNIPPET_LEN) -> str:
    """Truncate to at most `limit` grapheme clusters; append ... if truncated."""
    text = text.replace("\n", " ").strip()
    clusters = _re.findall(r"\X", text)
    if len(clusters) <= limit:
        return text
    return "".join(clusters[:limit]) + "..."


def format_reference(card: LoadedCard) -> str:
    """Render one card as a [B]/[P]-prefixed reference line per spec."""
    snippet = truncate_graphemes(card.content_section)
    why = card.fm.why_saved or "(no annotation yet)"

    if card.fm.source_type == "bookmark":
        author = card.fm.author or "@unknown"
        if not author.startswith("@") and author != "self":
            author = f"@{author}"
        permalink = card.fm.source or card.md_path.name
        return f"[B] {author} — {snippet} | {permalink} | why: {why}"

    if card.fm.source_url:
        host = urlparse(card.fm.source_url).hostname or card.fm.source_url
    else:
        host = "self"
    return f"[P] {host} — {snippet} | {card.md_path.name} | why: {why}"


__all__ = ["truncate_graphemes", "format_reference", "SNIPPET_LEN"]
