"""Card model — CardFrontmatter (Pydantic, validated) + LoadedCard (dataclass).

Two-type design (per Slice 1 review):
- CardFrontmatter: persisted YAML state. Strict, validated, source_type-invariant
  enforced via model_validator. Datetimes must be tz-aware UTC.
- LoadedCard: runtime card. Wraps frontmatter + body + raw_bytes + md_path.
  Frozen dataclass; constructed by load_card() in storage/.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_CHECKSUM_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class CardMedia(BaseModel):
    model_config = ConfigDict(extra="forbid")
    has_video: bool = False
    has_images: bool = False
    has_external_link: bool = False
    external_urls: List[str] = Field(default_factory=list)
    video_transcript_status: Optional[Literal["queued", "complete", "failed", "skipped"]] = None


class CardFrontmatter(BaseModel):
    """Parsed from YAML frontmatter only. Strict, validated."""

    model_config = ConfigDict(extra="forbid", strict=False)

    source_type: Literal["bookmark", "paste"]
    captured: datetime
    raw_path: Optional[str] = None
    raw_checksum: Optional[str] = None

    source: Optional[str] = None
    source_id: Optional[str] = None
    source_status: Optional[Literal["live", "deleted", "n/a"]] = None
    author: Optional[str] = None
    date: Optional[datetime] = None

    source_url: Optional[str] = None

    tags: List[str] = Field(default_factory=list)
    pinned: bool = False
    why_saved: Optional[str] = None
    why_saved_pending: bool = False
    applicability: List[str] = Field(default_factory=list)
    next_review_at: Optional[datetime] = None

    media: Optional[CardMedia] = None

    retrieval_summary: Optional[str] = None
    retrieval_tags: List[str] = Field(default_factory=list)
    extraction_pending: bool = False

    # Slice 2 /review F10: 24h idempotency fingerprint for paste_bookmark.
    # sha256 hex of the original content (NOT raw_bytes — content as the user
    # typed it). Lets paste_bookmark detect "this exact paste already happened
    # in the last 24h" without comparing every prior raw_bytes.
    content_fingerprint: Optional[str] = None

    @field_validator("captured", "date", "next_review_at")
    @classmethod
    def require_utc(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is None:
            return v
        if v.tzinfo is None:
            raise ValueError(
                "datetime must be timezone-aware (UTC). "
                "Use ISO-8601 with Z suffix in YAML."
            )
        return v.astimezone(timezone.utc)

    @field_validator("tags", "applicability", "retrieval_tags", mode="before")
    @classmethod
    def coerce_scalar_to_list(cls, v: Any) -> List[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return list(v)

    @field_validator("raw_checksum")
    @classmethod
    def validate_checksum_shape(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _CHECKSUM_RE.match(v):
            raise ValueError(
                f"raw_checksum must match {_CHECKSUM_RE.pattern!r}, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def check_source_type_invariants(self) -> "CardFrontmatter":
        if self.source_type == "bookmark":
            missing = []
            if not self.source:
                missing.append("source")
            if not self.source_id:
                missing.append("source_id")
            if not self.author:
                missing.append("author")
            if missing:
                raise ValueError(
                    f"bookmark cards require fields: {missing}"
                )
        elif self.source_type == "paste":
            if self.source_id is not None:
                raise ValueError("paste cards must not have source_id")
            if self.author and self.author != "self":
                raise ValueError(
                    f"paste cards must have author='self' or no author, got {self.author!r}"
                )
        return self


_CONTENT_HEADER_RE = re.compile(r"^##\s+Content\s*$", re.MULTILINE)
_NEXT_HEADER_RE = re.compile(r"^##\s+\S", re.MULTILINE)


@dataclass(frozen=True)
class LoadedCard:
    """Runtime card: frontmatter + rendered body + verified raw bytes + path.

    Constructed by storage.corpus.load_card(). Body is the markdown content
    after the YAML frontmatter; raw_bytes is the byte-exact source from the
    .raw.txt sidecar (or synthesized for v1 adapter cards).
    """

    fm: CardFrontmatter
    body: str
    raw_bytes: bytes
    md_path: Path

    @property
    def id(self) -> str:
        """Stable card id = filename without .md suffix."""
        return self.md_path.stem

    @property
    def content_section(self) -> str:
        """Extract just the `## Content` section body (between '## Content' and
        the next `## ` header). Falls back to full body if no header.
        """
        m = _CONTENT_HEADER_RE.search(self.body)
        if m is None:
            return self.body.strip()
        start = m.end()
        rest = self.body[start:]
        next_m = _NEXT_HEADER_RE.search(rest)
        if next_m is None:
            return rest.strip()
        return rest[: next_m.start()].strip()


__all__ = ["CardMedia", "CardFrontmatter", "LoadedCard"]
