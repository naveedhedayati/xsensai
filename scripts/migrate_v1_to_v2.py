"""v1 -> v2 corpus migration.

Slice 0: stub. Slice 6 fills this in with --dry-run, --apply, and rollback
modes per the spec's setup step 9.

Source v1 frontmatter (from /Users/naveedhedayati/Documents/Vault/04_areas/x-bookmarks/):
  x_post_id          -> source_id
  x_author           -> author
  x_source_url       -> source
  x_date             -> date
  x_extraction_status (success|failed) -> extraction_pending: bool
  x_tags             -> tags
  x_has_video        -> media.has_video
  x_has_images       -> media.has_images
  x_has_linked_content -> media.has_external_link
  x_linked_urls      -> media.external_urls
  x_engagement       -> drop (not in v2 schema)

Plus add v2-only fields:
  source_type: "bookmark"
  source_status: re-fetched ("live" or "deleted")
  retrieval_summary: LLM-generated 2 sentences
  retrieval_tags: LLM-generated 3-5 tags
  why_saved_pending: True for cards without prior annotation
  raw_path / raw_checksum: write the sidecar file

Filename convention also changes:
  v1: YYYY-MM-DD_{tweet-id}_{slug}.md
  v2: YYYY-MM-DD-{author}-{tweet-id}.md  +  YYYY-MM-DD-{author}-{tweet-id}.raw.txt

Rollback: write migrate_v1_to_v2.rollback.json with byte-exact original
filenames + bytes so --rollback restores v1 state perfectly.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "migrate_v1_to_v2.py is a stub. Full implementation lands in Slice 6.\n"
        "See script source for the migration mapping plan."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
